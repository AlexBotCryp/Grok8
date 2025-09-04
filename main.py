import os, json, time, threading, sys
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_DOWN
import ssl
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# logs sin buffer incluso si olvidas -u
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

print(">> Boot sequence starting...", flush=True)

import numpy as np
from dateutil import tz

# Binance
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    from binance.streams import ThreadedWebsocketManager
    print(">> Binance libs OK", flush=True)
except Exception as e:
    print(f">> ERROR importando binance libs: {repr(e)}", flush=True)
    raise

# OpenAI (Grok) opcional
try:
    from openai import OpenAI
    print(">> OpenAI client OK", flush=True)
except Exception:
    OpenAI = None
    print(">> OpenAI client no disponible (seguimos sin Grok)", flush=True)

STATE_PATH = "state.json"

# ===================== Utilidades =====================
def env(key, default=None):
    return os.getenv(key, default)

def parse_float(s, default=0.0):
    if s is None: return float(default)
    try: return float(str(s).replace(",", "."))
    except Exception: return float(default)

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

# ===================== Mini HTTP server (Web Service) =====================
def start_http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                body = f"OK {now_ts()}".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, fmt, *args): return
    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[HTTP] Listening on 0.0.0.0:{port}", flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ===================== Telegram (stdlib) =====================
def tg_http(method, params: dict):
    token = env("TG_BOT_TOKEN")
    if not token: return False, {"error": "Falta TG_BOT_TOKEN"}, None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try: js = json.loads(body)
            except Exception: js = {"ok": False, "raw": body}
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
    if not chat: return False, "CHAT_ID vacío"
    ok, err, _ = tg_http("sendMessage", {"chat_id": chat, "text": msg[:4000]})
    if not ok: print(f"[TG] sendMessage ERROR -> {err}", flush=True)
    return ok, (None if ok else err)

def tg_autotest():
    token = env("TG_BOT_TOKEN"); chat = env("TG_CHAT_ID")
    if not token:
        print("[TG] Desactivado: falta TG_BOT_TOKEN", flush=True); return
    ok, err, data = tg_http("getMe", {})
    if not ok:
        print(f"[TG] getMe ERROR -> {err}", flush=True); return
    me = data.get("result", {})
    print(f"[TG] getMe OK. Bot: @{me.get('username')} (id {me.get('id')})", flush=True)
    if chat:
        okc, errc, datac = tg_http("getChat", {"chat_id": chat})
        if okc:
            title = datac.get("result", {}).get("title") or datac.get("result", {}).get("username") or datac.get("result", {}).get("first_name")
            print(f"[TG] getChat OK. Chat: {title} (id {chat})", flush=True)
        else:
            print(f"[TG] getChat ERROR -> {errc}", flush=True)
    okm, errm = tg_send(f"🤖 Autotest {now_ts()} — hola, Alex.")
    if okm: print("[TG] sendMessage OK (prueba)", flush=True)

# ===================== Configuración =====================
BINANCE_API_KEY = env("BINANCE_API_KEY")
BINANCE_API_SECRET = env("BINANCE_API_SECRET")

SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "BTCUSDC,DOGEUSDC,TRXUSDC").split(",") if s.strip()]
INTERVAL = env("INTERVAL", "3m")
CANDLES  = int(env("CANDLES", "200"))

RSI_LEN  = int(env("RSI_LEN", "14"))
EMA_FAST = int(env("EMA_FAST", "9"))
EMA_SLOW = int(env("EMA_SLOW", "21"))
VOL_SPIKE = parse_float(env("VOL_SPIKE", "1.20"), 1.20)

MIN_EXPECTED_GAIN_PCT = parse_float(env("MIN_EXPECTED_GAIN_PCT", "0.05"), 0.05)
TAKE_PROFIT_PCT = parse_float(env("TAKE_PROFIT_PCT", "0.006"), 0.006)
STOP_LOSS_PCT   = parse_float(env("STOP_LOSS_PCT", "0.008"), 0.008)
TRAIL_PCT       = parse_float(env("TRAIL_PCT", "0.004"), 0.004)
MIN_ORDER_USD   = parse_float(env("MIN_ORDER_USD", "20"), 20)
ALLOCATION_PCT  = parse_float(env("ALLOCATION_PCT", "1.0"), 1.0)
DAILY_MAX_LOSS_USD = parse_float(env("DAILY_MAX_LOSS_USD", "25"), 25)
FEE_PCT = parse_float(env("FEE_PCT", "0.001"), 0.001)

LOOP_SECONDS = int(env("LOOP_SECONDS", "60"))

# Adopción de cartera y dust
ADOPT_ON_START = env("ADOPT_ON_START", "true").lower() == "true"
ADOPT_MIN_USD  = parse_float(env("ADOPT_MIN_USD", "10"), 10)
DUST_SELL_USD  = parse_float(env("DUST_SELL_USD", "5"), 5)

# Rutas cripto→cripto
BRIDGES = [s.strip().upper() for s in env("BRIDGES", "USDC,USDT,BTC,BNB,ETH").split(",") if s.strip()]

# Grok (opcional)
GROK_ENABLE  = env("GROK_ENABLE", "false").lower() == "true"
GROK_BASE_URL = env("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_API_KEY  = env("GROK_API_KEY")
GROK_MODEL    = env("GROK_MODEL", "grok")
GROK_MODELS_FALLBACK = [GROK_MODEL, "grok", "grok-2-latest", "grok-2-mini"]
_current_grok_model_idx = 0
MAX_TOKENS_DAILY = int(env("MAX_TOKENS_DAILY", "2000"))

# ===================== Clientes =====================
print(">> Creando cliente de Binance…", flush=True)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
print(">> Cliente Binance OK", flush=True)

llm = None
if GROK_ENABLE and GROK_API_KEY and OpenAI is not None:
    try:
        llm = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)
        print(f"[GROK] Cliente inicializado (modelo {GROK_MODEL})", flush=True)
    except Exception as e:
        print(f"[GROK] Deshabilitado por init error: {repr(e)}", flush=True)
        GROK_ENABLE = False

# ===================== Indicadores =====================
def ema(arr, period):
    arr = np.asarray(arr, dtype=float)
    k = 2 / (period + 1)
    out = np.zeros_like(arr); out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out

def rsi(arr, period=14):
    arr = np.asarray(arr, dtype=float)
    delta = np.diff(arr)
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = ema(up, period); roll_down = ema(down, period)
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
    if not trs: return 0.0
    atr = ema(np.array(trs), period)[-1]
    return atr / float(closes[-1])

# ===================== Mercado y WS =====================
_symbol_info_cache = {}
_symbol_status = set()  # símbolos "TRADING"
_base_to_quotes = {}    # base -> set(quotes)
_quotes_to_base = {}    # quote -> set(bases)

def round_step(qty, step):
    q = (Decimal(qty) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return float(q)

def get_symbol_info(sym):
    if sym in _symbol_info_cache: return _symbol_info_cache[sym]
    info = client.get_symbol_info(sym)
    if not info: raise RuntimeError(f"Symbol info not found for {sym}")
    f = {flt["filterType"]: flt for flt in info["filters"]}
    step = Decimal(f["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(f["LOT_SIZE"]["minQty"])
    min_notional = Decimal(f.get("MIN_NOTIONAL", {}).get("minNotional", "5"))
    _symbol_info_cache[sym] = {"step": step, "min_qty": min_qty, "min_notional": min_notional}
    return _symbol_info_cache[sym]

def load_exchange_map():
    global _symbol_status, _base_to_quotes, _quotes_to_base
    info = client.get_exchange_info()
    active = [s for s in info["symbols"] if s["status"] == "TRADING"]
    _symbol_status = set([s["symbol"] for s in active])
    _base_to_quotes = {}
    _quotes_to_base = {}
    for s in active:
        base, quote, sym = s["baseAsset"], s["quoteAsset"], s["symbol"]
        _base_to_quotes.setdefault(base, set()).add(quote)
        _quotes_to_base.setdefault(quote, set()).add(base)
    print(f"[MAP] símbolos activos: {len(_symbol_status)}", flush=True)

def symbol_of(base, quote):
    sym = f"{base}{quote}"
    return sym if sym in _symbol_status else None

# ---- WebSocket buffers ----
MARKET = {}  # sym -> {"o":[], "h":[], "l":[], "c":[], "v":[], "ready": False}
MARKET_LOCK = threading.Lock()

def _ensure_symbol_ws(sym):
    with MARKET_LOCK:
        if sym not in MARKET:
            MARKET[sym] = {"o": [], "h": [], "l": [], "c": [], "v": [], "ready": False}

def kline_handler(msg):
    try:
        k = msg.get("data", {}).get("k", {})
        sym = k.get("s")
        if not sym: return
        _ensure_symbol_ws(sym)
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
                if len(buf[key]) > CANDLES: buf[key] = buf[key][-CANDLES:]
            if len(buf["c"]) >= max(60, int(CANDLES*0.6)): buf["ready"] = True
    except Exception as e:
        print(f"[WS] handler error: {repr(e)}", flush=True)

def fetch_klines(sym, interval, limit):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if not buf or not buf["ready"]: raise RuntimeError(f"WS no listo para {sym}")
        o = buf["o"][-limit:]; h = buf["h"][-limit:]; l = buf["l"][-limit:]; c = buf["c"][-limit:]; v = buf["v"][-limit:]
        return o, h, l, c, v

def get_last_price(sym, fallback_rest=True):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if buf and buf["c"]: return float(buf["c"][-1])
    if fallback_rest:
        # REST (puntual) si aún no hay WS
        px = client.get_symbol_ticker(symbol=sym)["price"]
        return float(px)
    raise RuntimeError(f"no price for {sym}")

# ---- Balance cache ----
BALANCE_CACHE = {"free": {}, "ts": 0.0}
BALANCE_TTL = 30

def get_all_balances():
    now = time.time()
    if now - BALANCE_CACHE["ts"] < BALANCE_TTL:
        return BALANCE_CACHE["free"]
    acc = client.get_account()
    free = {}
    for b in acc["balances"]:
        amt = float(b["free"])
        if amt > 0: free[b["asset"]] = amt
    BALANCE_CACHE["free"] = free; BALANCE_CACHE["ts"] = now
    return free

def _invalidate_balance_cache():
    BALANCE_CACHE["ts"] = 0.0

# ===================== Grok =====================
def grok_decide(payload_dict):
    global _current_grok_model_idx
    if not (GROK_ENABLE and llm): return "HOLD"
    st = load_state()
    if st.get("tokens_used", 0) > MAX_TOKENS_DAILY: return "HOLD"
    prompt = "Eres un asistente de trading spot cripto. Analiza el JSON y responde SOLO una palabra: BUY, SELL o HOLD.\nJSON:\n" + json.dumps(payload_dict, ensure_ascii=False)
    for attempt in range(_current_grok_model_idx, len(GROK_MODELS_FALLBACK)):
        model_name = GROK_MODELS_FALLBACK[attempt]
        try:
            resp = llm.chat.completions.create(model=model_name, messages=[{"role":"user","content":prompt}], temperature=0.1, max_tokens=4)
            text = (resp.choices[0].message.content or "").strip().upper()
            used = len(prompt)//3 + 4
            st["tokens_used"] = st.get("tokens_used",0) + used; save_state(st)
            _current_grok_model_idx = attempt
            if "BUY" in text: return "BUY"
            if "SELL" in text: return "SELL"
            return "HOLD"
        except Exception as e:
            if any(x in repr(e) for x in ["404","Not found","model","401"]): continue
            break
    print("[GROK] deshabilitado (fallbacks fallidos)", flush=True)
    return "HOLD"

# ===================== Rutas y conversiones A→B =====================
def find_route(base_from, asset_to):
    """Devuelve ruta como lista de (symbol, side, kind) hops.
       side: 'SELL' o 'BUY' (del punto de vista del primer activo).
       kind: 'DIRECT' o 'BRIDGE'."""
    base_from = base_from.upper()
    asset_to  = asset_to.upper()
    if base_from == asset_to: return []

    # 1-hop directo
    sym = symbol_of(base_from, asset_to)
    if sym: return [(sym, "SELL", "DIRECT")]  # vender base_from para recibir asset_to
    sym = symbol_of(asset_to, base_from)
    if sym: return [(sym, "BUY", "DIRECT")]   # comprar asset_to usando base_from como quote

    # 2-hop vía bridges
    for bridge in BRIDGES:
        if bridge in (base_from, asset_to): continue
        s1 = symbol_of(base_from, bridge)
        s1_rev = symbol_of(bridge, base_from)
        s2 = symbol_of(asset_to, bridge)
        s2_rev = symbol_of(bridge, asset_to)
        # Opciones de 2 saltos:
        # from -> bridge
        if s1: hop1 = (s1, "SELL", "BRIDGE")            # BASE=from, QUOTE=bridge
        elif s1_rev: hop1 = (s1_rev, "BUY", "BRIDGE")   # BASE=bridge, QUOTE=from
        else: hop1 = None

        # bridge -> to
        if s2: hop2 = (s2, "BUY", "BRIDGE")             # BASE=asset_to, QUOTE=bridge  (comprar asset_to usando bridge)
        elif s2_rev: hop2 = (s2_rev, "SELL", "BRIDGE")  # BASE=bridge, QUOTE=asset_to (vender bridge para obtener asset_to)
        else: hop2 = None

        if hop1 and hop2: return [hop1, hop2]

    return None  # no hay ruta corta

def market_buy(symbol, base_qty=None, quote_qty=None):
    # intenta usar quoteOrderQty si se pasa quote_qty
    if quote_qty and quote_qty > 0:
        order = client.create_order(symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=round(quote_qty, 8))
        return order
    if base_qty and base_qty > 0:
        order = client.order_market_buy(symbol=symbol, quantity=base_qty)
        return order
    raise RuntimeError("market_buy: faltan cantidades")

def convert_asset(asset_from, asset_to, amount_from):
    """Convierte amount_from (en asset_from) hacia asset_to usando 1-2 saltos."""
    route = find_route(asset_from, asset_to)
    if not route: raise RuntimeError(f"No hay ruta {asset_from}->{asset_to}")

    cur_asset = asset_from
    cur_amount = amount_from
    for (sym, side, _kind) in route:
        info = get_symbol_info(sym)
        step = info["step"]

        # Determinar orientación del par
        # sym == BASEQUOTE
        base = sym[:-len(asset_to)] if sym.endswith(asset_to) else sym[:-len(cur_asset)]  # cálculo rápido aproximado
        # Mejor forma: obtener desde exchange_info - pero simplificamos con heurística:
        # vamos a pedir el detalle del símbolo real para base/quote (cacheado)
        # Como no lo tenemos directo, estimamos con *_symbol_info_cache? -> no trae base/quote.
        # Alternativa: reconstituir con _base_to_quotes mapping:
        # buscamos split: si asset_from en base_to_quotes y asset_to en set -> BASE=asset_from
        base_guess = None
        if asset_from in _base_to_quotes and any(sym == f"{asset_from}{q}" for q in _base_to_quotes[asset_from]):
            # podemos deducir para este sym:
            pass  # lo resolveremos al calcular con precios

        # Precio actual para dimensionar cantidades
        px = get_last_price(sym, fallback_rest=True)

        if side == "SELL":
            # vendemos 'cur_asset' (que debe ser BASE del símbolo) para recibir QUOTE
            qty_base = round_step(cur_amount, step)
            if qty_base <= 0:
                raise RuntimeError(f"Cantidad base insuficiente en {sym}")
            order = client.order_market_sell(symbol=sym, quantity=qty_base)
            # tras vender, recibimos 'quote' (aprox qty_base * px)
            cur_amount = qty_base * px * (1 - FEE_PCT)
            cur_asset = sym.replace(asset_from, "")  # quote tentativa
        else:
            # BUY: compramos el BASE usando como quote 'cur_asset'
            # aquí usamos quoteOrderQty en la cantidad 'cur_amount'
            quote_qty = cur_amount * (1 - FEE_PCT)
            order = market_buy(sym, quote_qty=quote_qty)
            # tras comprar, la cantidad base recibida ≈ quote/px
            cur_amount = (quote_qty / px) * (1 - FEE_PCT)
            cur_asset = sym.replace(asset_to, "") if sym.endswith(asset_to) else asset_to  # base tentativa

        _invalidate_balance_cache()
        time.sleep(0.2)  # breathing room entre hops

    return True

# ===================== PnL / control diario =====================
def today_key():
    return date.today().isoformat()

def add_realized_pnl(amount_usd):
    st = load_state(); day = today_key()
    d = st.get("pnl_history", {}); d[day] = round(d.get(day, 0.0) + float(amount_usd), 6)
    st["pnl_history"] = d; save_state(st)

def reached_daily_loss():
    st = load_state(); pnl = st.get("pnl_history", {}).get(today_key(), 0.0)
    return pnl <= -abs(DAILY_MAX_LOSS_USD)

# ===================== WS Manager & precios =====================
MARKET = {}
MARKET_LOCK = threading.Lock()

def start_streams(twm: 'ThreadedWebsocketManager', symbols):
    for sym in symbols:
        _ensure_symbol_ws(sym)
        twm.start_kline_socket(callback=kline_handler, symbol=sym.lower(), interval=INTERVAL)
    print(f"[WS] Streams iniciados: {len(symbols)} símbolos", flush=True)

def ensure_ws_for(sym, twm):
    if sym not in MARKET:
        _ensure_symbol_ws(sym)
        twm.start_kline_socket(callback=kline_handler, symbol=sym.lower(), interval=INTERVAL)
        print(f"[WS] Añadido stream para {sym}", flush=True)

# ===================== Órdenes spot USDC (estrategia principal) =====================
def place_buy(sym, quote_qty):
    info = get_symbol_info(sym)
    price = get_last_price(sym)
    qty = quote_qty / price
    qty = round_step(qty, info["step"])
    if qty < float(info["min_qty"]): raise RuntimeError(f"Qty {qty} < min_qty {sym}")
    order = client.order_market_buy(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order, qty, price

def place_sell(sym, qty):
    info = get_symbol_info(sym)
    qty = round_step(qty, info["step"])
    order = client.order_market_sell(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order

# ===================== Estrategia (RSI/EMA/Vol/ATR) =====================
def evaluate_and_trade():
    if reached_daily_loss(): return
    st = load_state()
    for sym in SYMBOLS:
        with MARKET_LOCK:
            if not MARKET.get(sym, {}).get("ready", False): continue
        try:
            o,h,l,c,v = fetch_klines(sym, INTERVAL, CANDLES)
            closes = np.array(c, dtype=float); vols = np.array(v, dtype=float); price = float(closes[-1])
            r = rsi(closes, RSI_LEN); ema_f = ema(closes, EMA_FAST); ema_s = ema(closes, EMA_SLOW)
            vol_base = vols[-50:-1].mean() if len(vols)>50 else (vols.mean() if len(vols) else 0.0)
            vol_ok  = vols[-1] > VOL_SPIKE * max(vol_base, 1e-9)
            trend_up = ema_f[-1] > ema_s[-1]
            rsi_val = r[-1]; atrp = atr_pct(h, l, c, period=14)
            expected_gain_ok = atrp >= MIN_EXPECTED_GAIN_PCT

            pos = st["positions"].get(sym); in_pos = pos is not None

            # --- Gestión en posición ---
            if in_pos:
                entry = pos["entry"]; qty = pos["qty"]; best = pos.get("best", entry)
                tp_price = entry * (1 + TAKE_PROFIT_PCT); sl_price = entry * (1 - STOP_LOSS_PCT)
                if price > best: pos["best"] = price
                if price >= tp_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl); st["positions"].pop(sym, None); save_state(st)
                    tg_send(f"✅ SELL TP {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC"); continue
                if pos.get("best", entry) > entry and price <= pos["best"] * (1 - TRAIL_PCT):
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl); st["positions"].pop(sym, None); save_state(st)
                    tg_send(f"⚠️ SELL TRAIL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC"); continue
                if price <= sl_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl); st["positions"].pop(sym, None); save_state(st)
                    tg_send(f"❌ SELL SL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC"); continue

                # Grok decide SELL oportunista en verde
                if GROK_ENABLE:
                    features = {"symbol":sym,"price":price,"entry":entry,"rsi":rsi_val,"ema_fast":float(ema_f[-1]),"ema_slow":float(ema_s[-1]),"atr_pct":atrp,"vol_ok":bool(vol_ok),"unrealized_pct":(price/entry - 1)}
                    decision = grok_decide(features)
                    if decision == "SELL" and (price/entry - 1) > 0:
                        place_sell(sym, qty)
                        pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                        add_realized_pnl(pnl); st["positions"].pop(sym, None); save_state(st)
                        tg_send(f"🤖 SELL by Grok {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                continue

            # --- Señal de compra ---
            base_signal = (trend_up and rsi_val > 45 and vol_ok) or (rsi_val < 30 and trend_up and vol_ok)
            if not (base_signal and expected_gain_ok): continue

            usdc_bal = get_all_balances().get("USDC", 0.0)
            if usdc_bal < MIN_ORDER_USD: continue

            quote_qty = max(MIN_ORDER_USD, usdc_bal * ALLOCATION_PCT * 0.995)
            info = get_symbol_info(sym)
            if Decimal(str(quote_qty)) < info["min_notional"]:
                quote_qty = float(info["min_notional"]) + 1.0

            if GROK_ENABLE:
                features = {"symbol":sym,"price":price,"rsi":rsi_val,"ema_fast":float(ema_f[-1]),"ema_slow":float(ema_s[-1]),"atr_pct":atrp,"vol_ok":bool(vol_ok),"min_expected_gain_pct":MIN_EXPECTED_GAIN_PCT}
                decision = grok_decide(features)
                if decision != "BUY": continue

            order, qty, raw_entry = place_buy(sym, quote_qty)
            st["positions"][sym] = {"entry": raw_entry*(1+FEE_PCT), "qty": qty, "best": raw_entry}
            save_state(st)
            tg_send(f"🟢 BUY {sym} {qty} @ {raw_entry:.8f} | Notional ≈ {qty*raw_entry:.2f} USDC")

        except BinanceAPIException as be:
            if be.code == -1003:
                tg_send("⛔️ IP baneada por peso REST. Ya usamos WS; espera a que se levante.")
            else:
                tg_send(f"⚠️ BinanceAPIException {sym}: {be.status_code} {be.message}")
            time.sleep(5)
        except Exception as e:
            tg_send(f"⚠️ Error {sym}: {repr(e)}"); time.sleep(2)

# ===================== Adopción de cartera y limpieza de dust =====================
def adopt_portfolio(twm):
    if not ADOPT_ON_START: return
    load_exchange_map()

    # Asegura streams de watchlist + puente USDC para señales
    ws_set = set(SYMBOLS)
    for base in {s[:-4] for s in SYMBOLS if s.endswith("USDC")}:
        ws_set.add(f"{base}USDC")
    # Abrimos websockets
    twm.start()
    start_streams(twm, sorted(ws_set))

    balances = get_all_balances()
    st = load_state()
    adopted = []
    dust_sold = []
    later = []

    for asset, amt in balances.items():
        if asset in ("USDC","BUSD"): continue
        # Intentar valorar en USDC
        sym_direct = symbol_of(asset, "USDC")
        sym_rev    = symbol_of("USDC", asset)
        value_usdc = 0.0
        try:
            if sym_direct:
                ensure_ws_for(sym_direct, twm)
                px = get_last_price(sym_direct, fallback_rest=True)
                value_usdc = amt * px
                target_sym = sym_direct
            elif sym_rev:
                ensure_ws_for(sym_rev, twm)
                px = get_last_price(sym_rev, fallback_rest=True)
                value_usdc = amt / max(px, 1e-12)
                target_sym = f"{asset}USDC"  # lo tratamos como si existiera para el estado
            else:
                # sin par USDC; marcamos para convertir más tarde a USDC si es dust
                later.append((asset, amt))
                continue
        except Exception:
            later.append((asset, amt)); continue

        if value_usdc < DUST_SELL_USD:
            try:
                convert_asset(asset, "USDC", amt)
                dust_sold.append(f"{asset} ({amt})")
            except Exception as e:
                tg_send(f"⚠️ No pude vender dust {asset}: {repr(e)}")
            continue

        if value_usdc >= ADOPT_MIN_USD:
            # crear posición con entrada = precio actual (con comisión) y qty = amt redondeado a lot
            try:
                info = get_symbol_info(target_sym)
                qty = round_step(amt, info["step"])
                if qty <= 0: continue
                price = get_last_price(sym_direct or sym_rev, fallback_rest=True)
                st["positions"][target_sym] = {"entry": price*(1+FEE_PCT), "qty": qty, "best": price}
                adopted.append(f"{target_sym} qty≈{qty}")
                ensure_ws_for(target_sym, twm)
            except Exception as e:
                tg_send(f"⚠️ No pude adoptar {asset}: {repr(e)}")

    save_state(st)

    # Limpieza de dust para “later” (sin par USDC), pero sólo si su valor es pequeño
    for asset, amt in later:
        try:
            # calcula valor aproximado vía USDT si existe
            v_usd = 0.0
            sym_u = symbol_of(asset, "USDT")
            if sym_u:
                px = get_last_price(sym_u, fallback_rest=True); v_usd = amt * px
            if v_usd < DUST_SELL_USD and v_usd > 0:
                convert_asset(asset, "USDC", amt)
                dust_sold.append(f"{asset} ({amt})")
        except Exception as e:
            tg_send(f"⚠️ Dust pendiente {asset}: {repr(e)}")

    if adopted:
        tg_send("📥 Adopción de cartera: " + ", ".join(adopted))
    if dust_sold:
        tg_send("🧹 Dust vendido: " + ", ".join(dust_sold))

# ===================== Heartbeat =====================
def heartbeat():
    while True:
        print(f"[HB] alive {now_ts()} — symbols: {','.join(SYMBOLS)}", flush=True)
        time.sleep(60)

# ===================== Main =====================
def main():
    print(f"[BOOT] {now_ts()} — starting...", flush=True)
    start_http_server()
    tg_autotest(); tg_send("🤖 Bot iniciado.")

    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    # Adopta cartera y abre streams
    adopt_portfolio(twm)

    # Threads auxiliares
    threading.Thread(target=heartbeat, daemon=True).start()

    # Loop principal
    while True:
        try:
            evaluate_and_trade()
        except Exception as e:
            print(f"[MAIN LOOP] error: {repr(e)}", flush=True)
            tg_send(f"🔥 Loop error: {repr(e)}")
        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    print(f"[{now_ts()}] Entrypoint", flush=True)
    main()
