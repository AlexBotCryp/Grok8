import os, json, time, threading
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_DOWN
import ssl
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np
from dateutil import tz
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.streams import ThreadedWebsocketManager

# OpenAI (Grok compatible). Si falta el paquete, seguimos sin Grok.
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

STATE_PATH = "state.json"

# ===================== Utilidades =====================
def env(key, default=None):
    return os.getenv(key, default)

def parse_float(s, default=0.0):
    if s is None:
        return float(default)
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return float(default)

def now_ts():
    dt = datetime.now(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"positions": {}, "pnl_history": {}, "tokens_used": 0}
    with open(STATE_PATH, "r") as f:
        return json.load(f)

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)

# ===================== Mini HTTP server (para Web Service) =====================
def start_http_server():
    """Pequeño servidor para mantener vivo un Web Service en Render."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                body = f"OK {now_ts()}".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            # Silenciar logs por request
            return

    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[HTTP] Listening on 0.0.0.0:{port}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

# ===================== Telegram (stdlib) =====================
def tg_http(method, params: dict):
    token = env("TG_BOT_TOKEN")
    if not token:
        return False, {"error": "Falta TG_BOT_TOKEN"}, None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                js = json.loads(body)
            except Exception:
                js = {"ok": False, "raw": body}
            ok = (resp.status == 200) and bool(js.get("ok"))
            return ok, (None if ok else js), (js if ok else None)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            js = json.loads(body)
        except Exception:
            js = {"ok": False, "status": e.code, "raw": str(e)}
        return False, js, None
    except Exception as e:
        return False, {"ok": False, "error": repr(e)}, None

def tg_send(msg: str):
    chat = env("TG_CHAT_ID")
    if not chat:
        return False, "CHAT_ID vacío"
    ok, err, _ = tg_http("sendMessage", {"chat_id": chat, "text": msg[:4000]})
    if not ok:
        print(f"[TG] sendMessage ERROR -> {err}")
    return ok, (None if ok else err)

def tg_autotest():
    token = env("TG_BOT_TOKEN")
    chat = env("TG_CHAT_ID")
    if not token:
        print("[TG] Desactivado: falta TG_BOT_TOKEN")
        return
    ok, err, data = tg_http("getMe", {})
    if not ok:
        print(f"[TG] getMe ERROR -> {err}")
        return
    me = data.get("result", {})
    print(f"[TG] getMe OK. Bot: @{me.get('username')} (id {me.get('id')})")
    if chat:
        okc, errc, datac = tg_http("getChat", {"chat_id": chat})
        if okc:
            title = datac.get("result", {}).get("title") or datac.get("result", {}).get("username") or datac.get("result", {}).get("first_name")
            print(f"[TG] getChat OK. Chat detectado: {title} (id {chat})")
        else:
            print(f"[TG] getChat ERROR -> {errc}  (¿El bot está dentro del chat? ¿ID correcto?)")
    if chat:
        okm, errm = tg_send(f"🤖 Autotest {now_ts()} — hola, Alex.")
        if okm:
            print("[TG] sendMessage OK (mensaje de prueba enviado)")
        else:
            print(f"[TG] sendMessage ERROR -> {errm}")

# ===================== Configuración =====================
BINANCE_API_KEY = env("BINANCE_API_KEY")
BINANCE_API_SECRET = env("BINANCE_API_SECRET")

SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "BTCUSDC,DOGEUSDC,TRXUSDC").split(",") if s.strip()]
INTERVAL = env("INTERVAL", "3m")     # 1m/3m/5m/15m...
CANDLES  = int(env("CANDLES", "200"))

RSI_LEN  = int(env("RSI_LEN", "14"))
EMA_FAST = int(env("EMA_FAST", "9"))
EMA_SLOW = int(env("EMA_SLOW", "21"))
VOL_SPIKE = parse_float(env("VOL_SPIKE", "1.20"), 1.20)  # vol actual > 1.2 * media

MIN_EXPECTED_GAIN_PCT = parse_float(env("MIN_EXPECTED_GAIN_PCT", "0.05"), 0.05)  # por ATR%
TAKE_PROFIT_PCT = parse_float(env("TAKE_PROFIT_PCT", "0.006"), 0.006)
STOP_LOSS_PCT   = parse_float(env("STOP_LOSS_PCT", "0.008"), 0.008)
TRAIL_PCT       = parse_float(env("TRAIL_PCT", "0.004"), 0.004)
MIN_ORDER_USD   = parse_float(env("MIN_ORDER_USD", "20"), 20)
ALLOCATION_PCT  = parse_float(env("ALLOCATION_PCT", "1.0"), 1.0)  # usar 100% del USDC
DAILY_MAX_LOSS_USD = parse_float(env("DAILY_MAX_LOSS_USD", "25"), 25)
FEE_PCT = parse_float(env("FEE_PCT", "0.001"), 0.001)  # aprox 0.1% por lado

# Recomendado para Web Service: 60–90s
LOOP_SECONDS = int(env("LOOP_SECONDS", "60"))

# Grok (opcional)
GROK_ENABLE  = env("GROK_ENABLE", "false").lower() == "true"
GROK_BASE_URL = env("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_API_KEY  = env("GROK_API_KEY")
GROK_MODEL    = env("GROK_MODEL", "grok")
GROK_MODELS_FALLBACK = [GROK_MODEL, "grok", "grok-2-latest", "grok-2-mini"]
_current_grok_model_idx = 0
MAX_TOKENS_DAILY = int(env("MAX_TOKENS_DAILY", "2000"))

# ===================== Clientes Binance/Grok =====================
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

llm = None
if GROK_ENABLE and GROK_API_KEY and OpenAI is not None:
    try:
        llm = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)
        print(f"[GROK] Cliente inicializado (modelo preferido: {GROK_MODEL})")
    except Exception as e:
        print(f"[GROK] Deshabilitado por init error: {repr(e)}")
        GROK_ENABLE = False
else:
    if GROK_ENABLE and not GROK_API_KEY:
        print("[GROK] Deshabilitado: falta GROK_API_KEY")

# ===================== Indicadores =====================
def ema(arr, period):
    arr = np.asarray(arr, dtype=float)
    k = 2 / (period + 1)
    out = np.zeros_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out

def rsi(arr, period=14):
    arr = np.asarray(arr, dtype=float)
    delta = np.diff(arr)
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = ema(up, period)
    roll_down = ema(down, period)
    rs = np.divide(roll_up, roll_down, out=np.zeros_like(roll_up), where=roll_down != 0)
    rsi_vals = 100 - (100 / (1 + rs))
    rsi_vals = np.insert(rsi_vals, 0, 50.0)
    return rsi_vals

def atr_pct(highs, lows, closes, period=14):
    highs, lows, closes = map(lambda x: np.asarray(x, dtype=float), (highs, lows, closes))
    trs = []
    for i in range(1, len(closes)):
        h, l, c1 = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - c1), abs(l - c1))
        trs.append(tr)
    if not trs:
        return 0.0
    atr = ema(np.array(trs), period)[-1]
    return atr / float(closes[-1])

# ===================== Mercado: WS + cachés =====================
_symbol_info_cache = {}

def get_symbol_info(sym):
    if sym in _symbol_info_cache:
        return _symbol_info_cache[sym]
    info = client.get_symbol_info(sym)  # una sola vez por símbolo
    if not info:
        raise RuntimeError(f"Symbol info not found for {sym}")
    f = {flt["filterType"]: flt for flt in info["filters"]}
    step = Decimal(f["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(f["LOT_SIZE"]["minQty"])
    min_notional = Decimal(f.get("MIN_NOTIONAL", {}).get("minNotional", "5"))
    _symbol_info_cache[sym] = {"step": step, "min_qty": min_qty, "min_notional": min_notional}
    return _symbol_info_cache[sym]

def round_step(qty, step):
    q = (Decimal(qty) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return float(q)

# ---- WebSocket buffers ----
MARKET = {}  # sym -> {"o":[], "h":[], "l":[], "c":[], "v":[], "ready": False}
MARKET_LOCK = threading.Lock()

def _ensure_symbol(sym):
    with MARKET_LOCK:
        if sym not in MARKET:
            MARKET[sym] = {"o": [], "h": [], "l": [], "c": [], "v": [], "ready": False}

def kline_handler(msg):
    try:
        k = msg.get("data", {}).get("k", {})
        sym = k.get("s")
        if not sym:
            return
        _ensure_symbol(sym)
        o = float(k["o"]); h = float(k["h"]); l = float(k["l"]); c = float(k["c"]); v = float(k["v"])
        closed = bool(k.get("x", False))
        with MARKET_LOCK:
            buf = MARKET[sym]
            if closed or not buf["c"]:
                buf["o"].append(o); buf["h"].append(h); buf["l"].append(l); buf["c"].append(c); buf["v"].append(v)
            else:
                buf["o"][-1] = o
                buf["h"][-1] = max(buf["h"][-1], h)
                buf["l"][-1] = min(buf["l"][-1], l)
                buf["c"][-1] = c
                buf["v"][-1] = v
            for key in ("o","h","l","c","v"):
                if len(buf[key]) > CANDLES:
                    buf[key] = buf[key][-CANDLES:]
            if len(buf["c"]) >= max(60, int(CANDLES*0.6)):
                buf["ready"] = True
    except Exception as e:
        print(f"[WS] handler error: {repr(e)}")

def fetch_klines(sym, interval, limit):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if not buf or not buf["ready"]:
            raise RuntimeError(f"WS no listo para {sym}")
        o = buf["o"][-limit:]
        h = buf["h"][-limit:]
        l = buf["l"][-limit:]
        c = buf["c"][-limit:]
        v = buf["v"][-limit:]
        return o, h, l, c, v

def get_price(sym):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if buf and buf["c"]:
            return float(buf["c"][-1])
    # Fallback (evitar): REST solo si no hay WS
    return float(client.get_symbol_ticker(symbol=sym)["price"])

# ---- Caché de balance ----
BALANCE_CACHE = {"free": 0.0, "ts": 0.0}
BALANCE_TTL = 60  # segundos

def get_free_usdc():
    now = time.time()
    if now - BALANCE_CACHE["ts"] < BALANCE_TTL:
        return BALANCE_CACHE["free"]
    bal = client.get_asset_balance(asset="USDC")
    free = float(bal["free"]) if bal else 0.0
    BALANCE_CACHE["free"] = free
    BALANCE_CACHE["ts"] = now
    return free

def _invalidate_balance_cache():
    BALANCE_CACHE["ts"] = 0.0

# ===================== Grok =====================
def grok_decide(payload_dict):
    global _current_grok_model_idx
    if not (GROK_ENABLE and llm):
        return "HOLD"
    st = load_state()
    if st.get("tokens_used", 0) > MAX_TOKENS_DAILY:
        return "HOLD"

    prompt = (
        "Eres un asistente de trading spot cripto. Analiza el JSON y responde SOLO una palabra: BUY, SELL o HOLD.\n"
        "Compra solo si hay confluencia fuerte y potencial razonable. Evita sobreoperar.\n"
        "JSON:\n" + json.dumps(payload_dict, ensure_ascii=False)
    )

    for attempt in range(_current_grok_model_idx, len(GROK_MODELS_FALLBACK)):
        model_name = GROK_MODELS_FALLBACK[attempt]
        try:
            resp = llm.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=4
            )
            text = (resp.choices[0].message.content or "").strip().upper()
            used = len(prompt) // 3 + 4
            st["tokens_used"] = st.get("tokens_used", 0) + used
            save_state(st)
            _current_grok_model_idx = attempt
            if "BUY" in text: return "BUY"
            if "SELL" in text: return "SELL"
            return "HOLD"
        except Exception as e:
            err = repr(e)
            if ("404" in err or "Not found" in err or "model" in err.lower() or "401" in err):
                continue
            break

    print("[GROK] deshabilitado (no se pudo usar ningún modelo de fallback)")
    return "HOLD"

# ===================== PnL / control diario =====================
def today_key():
    return date.today().isoformat()

def add_realized_pnl(amount_usd):
    st = load_state()
    day = today_key()
    d = st.get("pnl_history", {})
    d[day] = round(d.get(day, 0.0) + float(amount_usd), 6)
    st["pnl_history"] = d
    save_state(st)

def reached_daily_loss():
    st = load_state()
    pnl = st.get("pnl_history", {}).get(today_key(), 0.0)
    return pnl <= -abs(DAILY_MAX_LOSS_USD)

# ===================== Órdenes =====================
def place_buy(sym, quote_qty):
    info = get_symbol_info(sym)
    price = get_price(sym)
    qty = quote_qty / price
    qty = round_step(qty, info["step"])
    if qty < float(info["min_qty"]):
        raise RuntimeError(f"Qty {qty} < min_qty for {sym}")
    order = client.order_market_buy(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order, qty, price

def place_sell(sym, qty):
    info = get_symbol_info(sym)
    qty = round_step(qty, info["step"])
    order = client.order_market_sell(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order

# ===================== Estrategia principal =====================
def evaluate_and_trade():
    if reached_daily_loss():
        return

    st = load_state()
    for sym in SYMBOLS:
        # Espera pasiva a que el WS tenga datos suficientes
        with MARKET_LOCK:
            if not MARKET.get(sym, {}).get("ready", False):
                continue
        try:
            o, h, l, c, v = fetch_klines(sym, INTERVAL, CANDLES)
            closes = np.array(c, dtype=float)
            vols   = np.array(v, dtype=float)
            price  = float(closes[-1])

            r        = rsi(closes, RSI_LEN)
            ema_f    = ema(closes, EMA_FAST)
            ema_s    = ema(closes, EMA_SLOW)
            vol_base = vols[-50:-1].mean() if len(vols) > 50 else (vols.mean() if len(vols) else 0.0)
            vol_ok   = vols[-1] > VOL_SPIKE * max(vol_base, 1e-9)
            trend_up = ema_f[-1] > ema_s[-1]
            rsi_val  = r[-1]
            atrp     = atr_pct(h, l, c, period=14)

            expected_gain_ok = atrp >= MIN_EXPECTED_GAIN_PCT

            pos = st["positions"].get(sym)
            in_pos = pos is not None

            # ---- Gestión en posición ----
            if in_pos:
                entry = pos["entry"]
                qty   = pos["qty"]
                best  = pos.get("best", entry)
                tp_price = entry * (1 + TAKE_PROFIT_PCT)
                sl_price = entry * (1 - STOP_LOSS_PCT)

                if price > best:
                    best = price
                    pos["best"] = best

                if price >= tp_price:
                    place_sell(sym, qty)
                    pnl = (price * (1 - FEE_PCT) - entry * (1 + FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    tg_send(f"✅ SELL TP {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if best > entry and price <= best * (1 - TRAIL_PCT):
                    place_sell(sym, qty)
                    pnl = (price * (1 - FEE_PCT) - entry * (1 + FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    tg_send(f"⚠️ SELL TRAIL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if price <= sl_price:
                    place_sell(sym, qty)
                    pnl = (price * (1 - FEE_PCT) - entry * (1 + FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    tg_send(f"❌ SELL SL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if GROK_ENABLE:
                    features = {
                        "symbol": sym, "price": price, "entry": entry,
                        "rsi": rsi_val, "ema_fast": float(ema_f[-1]), "ema_slow": float(ema_s[-1]),
                        "atr_pct": atrp, "vol_ok": bool(vol_ok), "unrealized_pct": (price / entry - 1)
                    }
                    decision = grok_decide(features)
                    if decision == "SELL" and (price / entry - 1) > 0:
                        place_sell(sym, qty)
                        pnl = (price * (1 - FEE_PCT) - entry * (1 + FEE_PCT)) * qty
                        add_realized_pnl(pnl)
                        st["positions"].pop(sym, None)
                        save_state(st)
                        tg_send(f"🤖 SELL by Grok {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                continue

            # ---- Señal de compra ----
            base_signal = (trend_up and rsi_val > 45 and vol_ok) or (rsi_val < 30 and trend_up and vol_ok)
            if not (base_signal and expected_gain_ok):
                continue

            decision = "BUY"
            if GROK_ENABLE:
                features = {
                    "symbol": sym, "price": price, "rsi": rsi_val,
                    "ema_fast": float(ema_f[-1]), "ema_slow": float(ema_s[-1]),
                    "atr_pct": atrp, "vol_ok": bool(vol_ok),
                    "min_expected_gain_pct": MIN_EXPECTED_GAIN_PCT
                }
                decision = grok_decide(features)
            if decision != "BUY":
                continue

            usdc = get_free_usdc()
            if usdc < MIN_ORDER_USD:
                continue

            quote_qty = max(MIN_ORDER_USD, usdc * ALLOCATION_PCT * 0.995)

            info = get_symbol_info(sym)
            if Decimal(str(quote_qty)) < info["min_notional"]:
                quote_qty = float(info["min_notional"]) + 1.0

            order, qty, raw_entry = place_buy(sym, quote_qty)
            st["positions"][sym] = {
                "entry": raw_entry * (1 + FEE_PCT),  # entrada con comisión
                "qty": qty,
                "best": raw_entry
            }
            save_state(st)
            tg_send(f"🟢 BUY {sym} {qty} @ {raw_entry:.8f} | Notional ≈ {qty*raw_entry:.2f} USDC")

        except BinanceAPIException as be:
            if be.code == -1003:
                tg_send("⛔️ IP baneada por peso REST. Ya usamos WebSocket; espera a que se levante el ban.")
            else:
                tg_send(f"⚠️ BinanceAPIException {sym}: {be.status_code} {be.message}")
            time.sleep(5)
        except Exception as e:
            tg_send(f"⚠️ Error {sym}: {repr(e)}")
            time.sleep(2)

# ===================== Reseteo tokens / WS / Heartbeat =====================
def midnight_reset_tokens():
    while True:
        now = datetime.now(tz=tz.tzlocal())
        if now.hour == 0 and now.minute < 1:
            st = load_state()
            st["tokens_used"] = 0
            save_state(st)
            time.sleep(60)
        time.sleep(20)

def start_streams(twm: ThreadedWebsocketManager):
    for sym in SYMBOLS:
        _ensure_symbol(sym)
        twm.start_kline_socket(callback=kline_handler, symbol=sym.lower(), interval=INTERVAL)
    print("[WS] Streams iniciados")

def heartbeat():
    while True:
        print(f"[HB] alive {now_ts()} — symbols: {','.join(SYMBOLS)}")
        time.sleep(60)

# ===================== Main =====================
def main():
    start_http_server()  # <- necesario si usas Web Service
    tg_autotest()
    tg_send("🤖 Bot iniciado.")

    # WebSockets
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    start_streams(twm)

    # Threads auxiliares
    threading.Thread(target=midnight_reset_tokens, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

    # Loop principal
    while True:
        try:
            evaluate_and_trade()
        except Exception as e:
            tg_send(f"🔥 Loop error: {repr(e)}")
        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    print(f"[{now_ts()}] Starting...")
    main()
