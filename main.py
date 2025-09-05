# main.py
import os, json, time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException

# ========= Config por entorno =========
API_KEY        = os.getenv("BINANCE_API_KEY", "")
API_SECRET     = os.getenv("BINANCE_API_SECRET", "")
LIVE_MODE      = os.getenv("LIVE_MODE", "1") == "1"  # si 0 => dry-run
BASE_ASSET     = os.getenv("FORCE_BASE", "USDC").upper()

AUTO_CONSOLIDATE   = os.getenv("AUTO_CONSOLIDATE", "1") == "1"
DUST_MODE          = os.getenv("DUST_MODE", "IGNORE").upper()   # IGNORE o SELL
MIN_NOTIONAL_USDC  = Decimal(os.getenv("MIN_NOTIONAL_USDC", "20"))
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", "15"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", "0"))
TP_PCT             = Decimal(os.getenv("TP_PCT", "0.006"))  # 0.6%
SL_PCT             = Decimal(os.getenv("SL_PCT", "0.007"))  # 0.7%
ALLOC_PCT          = Decimal(os.getenv("ALLOC_PCT", "1.0")) # 100% del USDC libre
STATE_PATH         = os.getenv("STATE_PATH", "state.json")
WATCHLIST          = os.getenv("WATCHLIST", "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,LTCUSDC")

# Telegram config
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TG_OFF     = os.getenv("TELEGRAM_DISABLE", "0") == "1"

# ========= Logger =========
LOG_PREFIX = lambda lvl: f"{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {lvl} -"
def info(msg): print(f"{LOG_PREFIX('INFO')} {msg}")
def warn(msg): print(f"{LOG_PREFIX('WARNING')} {msg}")
def err (msg): print(f"{LOG_PREFIX('ERROR')} {msg}")

# ========= Telegram =========
def tg_notify(text: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True):
    """Envía un mensaje a Telegram con reintentos básicos."""
    if TG_OFF or not TG_TOKEN or not TG_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text[:3900],
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    for i in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                time.sleep(min(max(wait, 2), 10))
                continue
            print(f"[TG] HTTP {r.status_code}: {r.text}")
            return False
        except Exception as e:
            print(f"[TG] Error: {e}")
            time.sleep(1.5)
    return False

# ========= Cliente Binance =========
client = Client(API_KEY, API_SECRET)

# ========= Estado persistente =========
def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        warn(f"No se pudo guardar estado: {e}")

state = load_state()
state.setdefault("base_asset", BASE_ASSET)
state.setdefault("open_symbol", None)
state.setdefault("open_qty", 0.0)
state.setdefault("avg_price", 0.0)
state.setdefault("last_trade_ts", 0)

# ========= Exchange info =========
SYMBOL_INFO: Dict[str, dict] = {}

def refresh_exchange_info():
    global SYMBOL_INFO
    ex = client.get_exchange_info()
    SYMBOL_INFO = {s["symbol"]: s for s in ex["symbols"]}

def symbol_exists(symbol: str) -> bool:
    return symbol in SYMBOL_INFO

def _get_filter(symbol: str, ftype: str) -> Optional[dict]:
    info = SYMBOL_INFO.get(symbol)
    if not info: return None
    for f in info["filters"]:
        if f["filterType"] == ftype:
            return f
    return None

def lot_step(symbol: str) -> Decimal:
    f = _get_filter(symbol, "LOT_SIZE")
    return Decimal(str(f["stepSize"])) if f else Decimal("0")

def min_notional(symbol: str) -> Decimal:
    f = _get_filter(symbol, "NOTIONAL") or _get_filter(symbol, "MIN_NOTIONAL")
    if f:
        v = f.get("minNotional") or f.get("notional") or "0"
        return Decimal(str(v))
    return Decimal("0")

def round_by_step(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0: return qty
    n_steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return n_steps * step

# ========= Precios / velas =========
def last_price(symbol: str) -> Decimal:
    r = client.get_symbol_ticker(symbol=symbol)
    return Decimal(str(r["price"]))

def get_klines(symbol: str, interval="1m", limit=60) -> List[dict]:
    raws = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return [{"c": Decimal(str(k[4]))} for k in raws]

def ema(values: List[Decimal], period: int) -> Decimal:
    if not values: return Decimal("0")
    k = Decimal("2") / Decimal(period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (Decimal("1") - k)
    return e

# ========= Balances =========
def free_balance(asset: str) -> Decimal:
    try:
        bal = client.get_asset_balance(asset=asset)
        return Decimal(bal["free"]) if bal else Decimal("0")
    except Exception as e:
        warn(f"No se pudo leer balance de {asset}: {e}")
        return Decimal("0")

def all_free_balances() -> Dict[str, Decimal]:
    bals = {}
    for b in client.get_account()["balances"]:
        q = Decimal(b["free"])
        if q > 0:
            bals[b["asset"]] = q
    return bals

# ========= Consolidación =========
def sell_all_to_usdc(asset: str, qty: Decimal) -> bool:
    if asset == BASE_ASSET: return False
    symbol = f"{asset}{BASE_ASSET}"
    if not symbol_exists(symbol):
        info(f"🚫 No existe {symbol}, no consolido {asset}.")
        return False
    px = last_price(symbol)
    notional = qty * px
    min_needed = min_notional(symbol) or MIN_NOTIONAL_USDC
    if notional < min_needed:
        info(f"🟡 {asset} notional bajo {notional}")
        return False
    q_round = round_by_step(qty, lot_step(symbol))
    if LIVE_MODE:
        try:
            info(f"🔁 Consolida {asset}->{BASE_ASSET} qty={q_round}")
            client.create_order(symbol=symbol, side=SIDE_SELL,
                                type=ORDER_TYPE_MARKET, quantity=float(q_round))
            return True
        except Exception as e:
            err(f"Error vendiendo {asset}->{BASE_ASSET}: {e}")
            return False
    else:
        info(f"[DRY] Vendería {asset}->{BASE_ASSET} qty={q_round}")
        return True

def force_base_position_usdc():
    refresh_exchange_info()
    bals = all_free_balances()
    for a, q in bals.items():
        if a != BASE_ASSET:
            sell_all_to_usdc(a, q)
    usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {usdc:.2f}")
    state["open_symbol"] = None
    state["open_qty"] = 0.0
    state["avg_price"] = 0.0
    save_state(state)
    info("✅ Posición base forzada: USDC. Sin posición abierta.")
    tg_notify("✅ Bot iniciado y consolidado a <b>USDC</b>. Sin posición abierta.")

# ========= Señal simple =========
def score_symbol(symbol: str) -> Decimal:
    kl = get_klines(symbol, limit=40)
    closes = [k["c"] for k in kl]
    if len(closes) < 20: return Decimal("0")
    px = closes[-1]
    ema20 = ema(closes[-20:], 20)
    return (px - ema20) / ema20

# ========= Órdenes =========
def place_market_buy(symbol: str, usdc_to_spend: Decimal):
    px = last_price(symbol)
    qty = round_by_step(usdc_to_spend / px, lot_step(symbol))
    if LIVE_MODE:
        o = client.create_order(symbol=symbol, side=SIDE_BUY,
                                type=ORDER_TYPE_MARKET, quantity=float(qty))
        info(f"✅ COMPRA {symbol} qty={qty}")
        tg_notify(f"🟢 <b>COMPRA</b> {symbol}\nQty: <code>{qty}</code>\nPrecio≈<code>{px:.6f}</code>")
        return o, qty, px
    else:
        info(f"[DRY] BUY {symbol} qty={qty}")
        return {"dry": True}, qty, px

def place_market_sell(symbol: str, qty: Decimal):
    q = round_by_step(qty, lot_step(symbol))
    if LIVE_MODE:
        o = client.create_order(symbol=symbol, side=SIDE_SELL,
                                type=ORDER_TYPE_MARKET, quantity=float(q))
        info(f"✅ VENTA {symbol} qty={q}")
        tg_notify(f"🔴 <b>VENTA</b> {symbol}\nQty: <code>{q}</code>")
        return o
    else:
        info(f"[DRY] SELL {symbol} qty={q}")
        return {"dry": True}

# ========= Gestión =========
def position_open(): return bool(state.get("open_symbol"))
def open_symbol(): return state.get("open_symbol")

def manage_position():
    sym = open_symbol()
    if not sym: return
    px = last_price(sym)
    avg = Decimal(str(state.get("avg_price", 0)))
    qty = Decimal(str(state.get("open_qty", 0)))
    up = (px - avg) / avg
    down = (avg - px) / avg
    if up >= TP_PCT:
        info(f"🎯 TP {sym} +{float(up)*100:.2f}%")
        place_market_sell(sym, qty)
        state["open_symbol"] = None
        save_state(state)
    elif down >= SL_PCT:
        info(f"🛑 SL {sym} -{float(down)*100:.2f}%")
        place_market_sell(sym, qty)
        state["open_symbol"] = None
        save_state(state)
    else:
        info(f"📊 {sym} PnL={(px-avg)/avg*100:.2f}%")

# ========= Loop =========
def scan_loop():
    info("🤖 Escaneando…")
    if position_open():
        manage_position()
        return
    free_usdc = free_balance(BASE_ASSET)
    if free_usdc < MIN_NOTIONAL_USDC:
        info("⛔ USDC insuficiente.")
        return
    best_sym, best_score = None, Decimal("-999")
    for s in [x.strip().upper() for x in WATCHLIST.split(",")]:
        if not symbol_exists(s): continue
        sc = score_symbol(s)
        if sc > best_score:
            best_sym, best_score = s, sc
    if not best_sym or best_score <= 0:
        info("🟡 Sin señal clara.")
        return
    to_spend = free_usdc * ALLOC_PCT
    order, qty, px = place_market_buy(best_sym, to_spend)
    state["open_symbol"] = best_sym
    state["open_qty"] = float(qty)
    state["avg_price"] = float(px)
    save_state(state)

# ========= Arranque =========
def main():
    info("🤖 Bot iniciado.")
    refresh_exchange_info()
    info("✅ Exchange info cargada.")
    if AUTO_CONSOLIDATE:
        force_base_position_usdc()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_loop, "interval",
                      seconds=SCAN_INTERVAL_SEC,
                      max_instances=1, coalesce=True, misfire_grace_time=10)
    scheduler.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        info("🛑 Detenido por usuario.")
    finally:
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    main()
