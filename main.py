import os, json, time, math, threading
from datetime import datetime, date
from decimal import Decimal, ROUND_DOWN
import numpy as np
import requests
from dateutil import tz
from binance.client import Client
from binance.exceptions import BinanceAPIException

# NEW: import guard para OpenAI y fallback limpio
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

STATE_PATH = "state.json"

# ---------- Utilidades ----------
def env(key, default=None):
    v = os.getenv(key, default)
    return v

def parse_float(s, default=0.0):
    if s is None: return float(default)
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return float(default)

def now_ts():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"positions":{}, "pnl_history":{}, "tokens_used":0}
    with open(STATE_PATH,"r") as f:
        return json.load(f)

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp,"w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)

def send_telegram(msg):
    token = env("TG_BOT_TOKEN")
    chat = env("TG_CHAT_ID")
    if not token or not chat: 
        return False, "TOKEN o CHAT_ID vacío"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id":chat,"text":msg[:4000]}
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        return ok, (None if ok else r.text)
    except Exception as e:
        return False, repr(e)

def tg_autotest():
    """Prueba Telegram al arrancar y muestra errores útiles."""
    enabled = bool(env("TG_BOT_TOKEN")) and bool(env("TG_CHAT_ID"))
    if not enabled:
        print("[TG] Desactivado: falta TG_BOT_TOKEN o TG_CHAT_ID")
        return
    ok, err = send_telegram(f"🤖 Bot iniciando {now_ts()} (autotest)")
    if ok:
        print("[TG] OK: mensaje de prueba enviado")
    else:
        print(f"[TG] ERROR: {err}")

# ---------- Config ----------
BINANCE_API_KEY = env("BINANCE_API_KEY")
BINANCE_API_SECRET = env("BINANCE_API_SECRET")

SYMBOLS = [s.strip().upper() for s in env("SYMBOLS","BTCUSDC,DOGEUSDC,TRXUSDC").split(",") if s.strip()]
INTERVAL = env("INTERVAL","3m")
CANDLES = int(env("CANDLES","200"))

RSI_LEN = int(env("RSI_LEN","14"))
EMA_FAST = int(env("EMA_FAST","9"))
EMA_SLOW = int(env("EMA_SLOW","21"))
VOL_SPIKE = parse_float(env("VOL_SPIKE","1.20"),1.20)

MIN_EXPECTED_GAIN_PCT = parse_float(env("MIN_EXPECTED_GAIN_PCT","0.05"),0.05)
TAKE_PROFIT_PCT = parse_float(env("TAKE_PROFIT_PCT","0.006"),0.006)
STOP_LOSS_PCT = parse_float(env("STOP_LOSS_PCT","0.008"),0.008)
TRAIL_PCT = parse_float(env("TRAIL_PCT","0.004"),0.004)
MIN_ORDER_USD = parse_float(env("MIN_ORDER_USD","20"),20)
ALLOCATION_PCT = parse_float(env("ALLOCATION_PCT","1.0"),1.0)
DAILY_MAX_LOSS_USD = parse_float(env("DAILY_MAX_LOSS_USD","25"),25)
FEE_PCT = parse_float(env("FEE_PCT","0.001"),0.001)

LOOP_SECONDS = int(env("LOOP_SECONDS","45"))

GROK_ENABLE = env("GROK_ENABLE","true").lower() == "true"
GROK_BASE_URL = env("GROK_BASE_URL","https://api.x.ai/v1")
GROK_API_KEY = env("GROK_API_KEY")
GROK_MODEL = env("GROK_MODEL","grok-beta")
MAX_TOKENS_DAILY = int(env("MAX_TOKENS_DAILY","2000"))

# ---------- Clientes ----------
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# NEW: creación robusta del cliente Grok con fallback
llm = None
if GROK_ENABLE and GROK_API_KEY and OpenAI is not None:
    try:
        # Nota: fijamos compat con httpx 0.27.2 en requirements.
        llm = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)
        print("[GROK] Cliente inicializado")
    except Exception as e:
        print(f"[GROK] Deshabilitado por error de init: {repr(e)}")
        GROK_ENABLE = False
else:
    if not GROK_API_KEY:
        print("[GROK] Deshabilitado: falta GROK_API_KEY")
    elif OpenAI is None:
        print("[GROK] Deshabilitado: paquete openai no disponible")

# ---------- Indicadores ----------
def ema(arr, period):
    arr = np.asarray(arr, dtype=float)
    k = 2/(period+1)
    ema_vals = np.zeros_like(arr)
    ema_vals[0] = arr[0]
    for i in range(1,len(arr)):
        ema_vals[i] = arr[i]*k + ema_vals[i-1]*(1-k)
    return ema_vals

def rsi(arr, period=14):
    arr = np.asarray(arr, dtype=float)
    delta = np.diff(arr)
    up = np.where(delta>0, delta, 0.0)
    down = np.where(delta<0, -delta, 0.0)
    roll_up = ema(up, period)
    roll_down = ema(down, period)
    rs = np.divide(roll_up, roll_down, out=np.zeros_like(roll_up), where=roll_down!=0)
    rsi = 100 - (100/(1+rs))
    rsi = np.insert(rsi,0,50.0)
    return rsi

def atr_pct(highs, lows, closes, period=14):
    highs, lows, closes = map(lambda x: np.asarray(x, dtype=float), (highs,lows,closes))
    trs = []
    for i in range(1,len(closes)):
        h, l, c1 = highs[i], lows[i], closes[i-1]
        tr = max(h-l, abs(h-c1), abs(l-c1))
        trs.append(tr)
    if not trs:
        return 0.0
    atr = ema(np.array(trs), period)[-1]
    pct = atr / float(closes[-1])
    return pct

# ---------- Mercado ----------
_symbol_info_cache = {}
def get_symbol_info(sym):
    if sym in _symbol_info_cache:
        return _symbol_info_cache[sym]
    info = client.get_symbol_info(sym)
    if not info:
        raise RuntimeError(f"Symbol info not found for {sym}")
    f = {flt["filterType"]:flt for flt in info["filters"]}
    step = Decimal(f["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(f["LOT_SIZE"]["minQty"])
    min_notional = Decimal(f.get("MIN_NOTIONAL", {}).get("minNotional","5"))
    _symbol_info_cache[sym] = {"step":step,"min_qty":min_qty,"min_notional":min_notional}
    return _symbol_info_cache[sym]

def round_step(qty, step):
    q = (Decimal(qty) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return float(q)

def fetch_klines(sym, interval, limit):
    ks = client.get_klines(symbol=sym, interval=interval, limit=limit)
    o,h,l,c,v = [],[],[],[],[]
    for k in ks:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

def get_price(sym):
    return float(client.get_symbol_ticker(symbol=sym)["price"])

def get_free_usdc():
    bal = client.get_asset_balance(asset="USDC")
    return float(bal["free"]) if bal else 0.0

# ---------- Grok ----------
def grok_decide(payload_dict):
    if not (GROK_ENABLE and llm):
        return "HOLD"
    st = load_state()
    if st.get("tokens_used",0) > MAX_TOKENS_DAILY:
        return "HOLD"
    prompt = (
        "Eres un asistente de trading spot cripto. Analiza el JSON y responde SOLO una palabra: BUY, SELL o HOLD.\n"
        "Compra solo si hay confluencia fuerte y potencial razonable. Evita sobreoperar.\n"
        "JSON:\n" + json.dumps(payload_dict, ensure_ascii=False)
    )
    try:
        resp = llm.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.1,
            max_tokens=4
        )
        text = resp.choices[0].message.content.strip().upper()
        used = len(prompt)//3 + 4
        st["tokens_used"] = st.get("tokens_used",0) + used
        save_state(st)
        if "BUY" in text: return "BUY"
        if "SELL" in text: return "SELL"
        return "HOLD"
    except Exception as e:
        print(f"[GROK] fallo decide: {repr(e)}")
        return "HOLD"

# ---------- PnL / control diario ----------
def today_key():
    return date.today().isoformat()

def add_realized_pnl(amount_usd):
    st = load_state()
    day = today_key()
    d = st.get("pnl_history",{})
    d[day] = round(d.get(day,0.0) + float(amount_usd), 6)
    st["pnl_history"] = d
    save_state(st)

def reached_daily_loss():
    st = load_state()
    pnl = st.get("pnl_history",{}).get(today_key(), 0.0)
    return pnl <= -abs(DAILY_MAX_LOSS_USD)

# ---------- Órdenes ----------
def place_buy(sym, quote_qty):
    info = get_symbol_info(sym)
    price = get_price(sym)
    qty = quote_qty / price
    qty = round_step(qty, info["step"])
    if qty < float(info["min_qty"]):
        raise RuntimeError(f"Qty {qty} < min_qty for {sym}")
    order = client.order_market_buy(symbol=sym, quantity=qty)
    return order, qty, price

def place_sell(sym, qty):
    info = get_symbol_info(sym)
    qty = round_step(qty, info["step"])
    order = client.order_market_sell(symbol=sym, quantity=qty)
    return order

# ---------- Estrategia ----------
def evaluate_and_trade():
    if reached_daily_loss():
        return

    st = load_state()
    usdc = get_free_usdc()

    for sym in SYMBOLS:
        try:
            o,h,l,c,v = fetch_klines(sym, INTERVAL, CANDLES)
            closes = np.array(c, dtype=float)
            vols = np.array(v, dtype=float)
            price = float(closes[-1])

            r = rsi(closes, RSI_LEN)
            ema_f = ema(closes, EMA_FAST)
            ema_s = ema(closes, EMA_SLOW)
            vol_ok = vols[-1] > VOL_SPIKE * (vols[-50:-1].mean() if len(vols) > 50 else vols.mean())
            trend_up = ema_f[-1] > ema_s[-1]
            rsi_val = r[-1]
            atrp = atr_pct(h,l,c, period=14)

            expected_gain_ok = atrp >= MIN_EXPECTED_GAIN_PCT

            pos = st["positions"].get(sym)
            in_pos = pos is not None

            # --- Gestión en posición ---
            if in_pos:
                entry = pos["entry"]
                qty = pos["qty"]
                best = pos.get("best", entry)
                tp_price = entry * (1 + TAKE_PROFIT_PCT)
                sl_price = entry * (1 - STOP_LOSS_PCT)

                if price > best:
                    best = price
                    pos["best"] = best

                if price >= tp_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    send_telegram(f"✅ SELL TP {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if best > entry and price <= best * (1 - TRAIL_PCT):
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    send_telegram(f"⚠️ SELL TRAIL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if price <= sl_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    save_state(st)
                    send_telegram(f"❌ SELL SL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                if GROK_ENABLE and not reached_daily_loss():
                    features = {
                        "symbol":sym, "price":price, "entry":entry,
                        "rsi":rsi_val, "ema_fast":float(ema_f[-1]), "ema_slow":float(ema_s[-1]),
                        "atr_pct":atrp, "vol_ok":bool(vol_ok), "unrealized_pct": (price/entry - 1)
                    }
                    decision = grok_decide(features)
                    if decision == "SELL" and (price/entry - 1) > 0:
                        place_sell(sym, qty)
                        pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                        add_realized_pnl(pnl)
                        st["positions"].pop(sym, None)
                        save_state(st)
                        send_telegram(f"🤖 SELL by Grok {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                continue

            # --- Señal de compra ---
            base_signal = (trend_up and rsi_val > 45 and vol_ok) or (rsi_val < 30 and trend_up and vol_ok)
            if not (base_signal and expected_gain_ok):
                continue

            decision = "BUY"
            if GROK_ENABLE:
                features = {
                    "symbol":sym, "price":price, "rsi":rsi_val,
                    "ema_fast":float(ema_f[-1]), "ema_slow":float(ema_s[-1]),
                    "atr_pct":atrp, "vol_ok":bool(vol_ok),
                    "min_expected_gain_pct":MIN_EXPECTED_GAIN_PCT
                }
                decision = grok_decide(features)
            if decision != "BUY":
                continue

            usdc = get_free_usdc()
            if usdc < MIN_ORDER_USD:
                continue

            quote_qty = usdc * ALLOCATION_PCT
            quote_qty = max(MIN_ORDER_USD, quote_qty * 0.995)

            info = get_symbol_info(sym)
            if Decimal(str(quote_qty)) < info["min_notional"]:
                quote_qty = float(info["min_notional"]) + 1.0

            order, qty, entry = place_buy(sym, quote_qty)
            st["positions"][sym] = {
                "entry": entry*(1+FEE_PCT),
                "qty": qty,
                "best": entry
            }
            save_state(st)
            send_telegram(f"🟢 BUY {sym} {qty} @ {entry:.8f} | Notional ≈ {qty*entry:.2f} USDC")

        except BinanceAPIException as be:
            if be.code == -1003:
                send_telegram("⛔️ IP baneada por peso de peticiones. Sube LOOP_SECONDS o usa websockets.")
            else:
                send_telegram(f"⚠️ BinanceAPIException {sym}: {be.status_code} {be.message}")
            time.sleep(5)
        except Exception as e:
            send_telegram(f"⚠️ Error {sym}: {repr(e)}")
            time.sleep(2)

def midnight_reset_tokens():
    while True:
        now = datetime.now(tz=tz.tzlocal())
        if now.hour == 0 and now.minute < 1:
            st = load_state()
            st["tokens_used"] = 0
            save_state(st)
            time.sleep(60)
        time.sleep(20)

def main():
    tg_autotest()  # NEW: prueba TG al inicio
    ok, _ = send_telegram("🤖 Bot iniciado.")
    if not ok:
        print("[TG] No se pudo enviar mensaje de inicio (ver logs arriba).")
    threading.Thread(target=midnight_reset_tokens, daemon=True).start()
    while True:
        try:
            evaluate_and_trade()
        except Exception as e:
            send_telegram(f"🔥 Loop error: {repr(e)}")
        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    print(f"[{now_ts()}] Starting...")
    main()
