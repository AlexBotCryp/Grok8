# main.py
import os
import time
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from binance.client import Client
import requests

# =========================
# CONFIG (ENV)
# =========================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# Cotizamos contra USDC (cámbialo a USDT si quieres)
QUOTE = os.getenv("QUOTE_ASSET", "USDC")

# Lista de símbolos a escanear (coma-separado)
WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,AVAXUSDC"
).replace(" ", "").split(",")

# Señales/gestión
TRAIL_PCT         = Decimal(os.getenv("TRAIL_PCT",         "0.004").replace(",", "."))  # 0.4%
TAKE_PROFIT_PCT   = Decimal(os.getenv("TAKE_PROFIT_PCT",   "0.006").replace(",", "."))  # 0.6%  (<=0 desactiva)
STOP_LOSS_PCT     = Decimal(os.getenv("STOP_LOSS_PCT",     "0.010").replace(",", "."))  # 1.0%  (<=0 desactiva)
USER_MIN_NOTIONAL = Decimal(os.getenv("USER_MIN_NOTIONAL", "20").replace(",", "."))     # mínimo deseado (además del exchange)
SCAN_SEC          = int(os.getenv("SCAN_SEC", "15"))
SAFETY_QTY_PCT    = Decimal(os.getenv("SAFETY_QTY_PCT", "0.001").replace(",", "."))     # 0.1% buffer qty
COOLDOWN_SEC      = int(os.getenv("COOLDOWN_SEC", "180"))                                # enfriar si queda en dust
REBUY_COOLDOWN_SEC= int(os.getenv("REBUY_COOLDOWN_SEC", "120"))
MAX_PORTFOLIO_USE = Decimal(os.getenv("MAX_PORTFOLIO_USE", "0.80").replace(",", "."))   # usa hasta 80% del quote
BOT_TZ_STR        = os.getenv("BOT_TZ", "UTC")                                           # "Europe/Madrid" recomendado

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bot")

# Precisión decimal alta
getcontext().prec = 28

# =========================
# Cliente Binance
# =========================
if not API_KEY or not API_SECRET:
    log.warning("⚠️ Falta BINANCE_API_KEY o BINANCE_API_SECRET.")

client = Client(API_KEY, API_SECRET)

# =========================
# Telegram helpers
# =========================
def tg_enabled() -> bool:
    return bool(TG_TOKEN and TG_CHAT)

def tg_send(text: str):
    if not tg_enabled():
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT, "text": text}
        # timeout corto para no bloquear el ciclo
        requests.post(url, json=payload, timeout=4)
    except Exception as e:
        log.warning(f"⚠️ Telegram no envió: {e}")

# =========================
# Estado y caches
# =========================
_RULES_CACHE = {}
_LAST_REJECT = {}      # symbol -> ts último rechazo (dust/minQty/minNotional)
_LAST_SELL   = {}      # symbol -> ts última venta
_POSITIONS   = {}      # symbol -> {entry, peak, qty}
_TICK_MEM    = defaultdict(list)  # memoria de precios recientes

def now_ts() -> float:
    return time.time()

def mark_reject(symbol: str):
    _LAST_REJECT[symbol] = now_ts()

def should_skip(symbol: str, wait: int = COOLDOWN_SEC) -> bool:
    t = _LAST_REJECT.get(symbol)
    return bool(t and (now_ts() - t) < wait)

def mark_recent_sell(symbol: str):
    _LAST_SELL[symbol] = now_ts()

def sold_recently(symbol: str, window: int = REBUY_COOLDOWN_SEC) -> bool:
    t = _LAST_SELL.get(symbol)
    return bool(t and (now_ts() - t) < window)

# =========================
# Reglas de símbolo y utilidades
# =========================
def get_symbol_rules(symbol: str):
    if symbol in _RULES_CACHE:
        return _RULES_CACHE[symbol]

    info = client.get_symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Symbol info no disponible para {symbol}")

    filters = {f["filterType"]: f for f in info["filters"]}
    lot = filters["LOT_SIZE"]
    price_filter = filters["PRICE_FILTER"]
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")

    step = Decimal(lot["stepSize"])
    min_qty = Decimal(lot["minQty"])
    tick = Decimal(price_filter["tickSize"])
    min_notional = Decimal(notional_filter.get("minNotional", "0")) if notional_filter else Decimal("0")

    rules = {
        "step": step,
        "min_qty": min_qty,
        "price_tick": tick,
        "min_notional": min_notional,
    }
    _RULES_CACHE[symbol] = rules
    return rules

def round_down_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

def round_down_price(px: Decimal, tick: Decimal) -> Decimal:
    if tick == 0:
        return px
    return (px / tick).to_integral_value(rounding=ROUND_DOWN) * tick

def get_free(asset: str) -> Decimal:
    bal = client.get_asset_balance(asset=asset)
    if not bal: return Decimal("0")
    return Decimal(bal["free"])

def get_locked(asset: str) -> Decimal:
    bal = client.get_asset_balance(asset=asset)
    if not bal: return Decimal("0")
    return Decimal(bal.get("locked", "0"))

def last_price(symbol: str) -> Decimal:
    t = client.get_symbol_ticker(symbol=symbol)
    return Decimal(t["price"])

# =========================
# Validaciones de cantidad
# =========================
def qty_sellable(symbol: str, price: Decimal, qty_free: Decimal) -> Decimal:
    rules = get_symbol_rules(symbol)
    qty = round_down_qty(qty_free, rules["step"])
    if qty <= 0:
        return Decimal("0")

    # notional requerido (exchange vs usuario)
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)
    if (qty * price) < min_notional_req:
        return Decimal("0")
    if qty < rules["min_qty"]:
        return Decimal("0")
    return qty

def qty_buyable(symbol: str, price: Decimal, quote_free: Decimal) -> Decimal:
    rules = get_symbol_rules(symbol)
    max_quote = quote_free * MAX_PORTFOLIO_USE
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)

    if max_quote < min_notional_req:
        return Decimal("0")

    qty = max_quote / price
    qty = qty * (Decimal(1) - SAFETY_QTY_PCT)
    qty = round_down_qty(qty, rules["step"])

    if qty < rules["min_qty"]:
        return Decimal("0")
    if (qty * price) < min_notional_req:
        return Decimal("0")
    return qty

def cancel_all(symbol: str):
    try:
        client.cancel_open_orders(symbol=symbol)
    except Exception:
        pass

# =========================
# Órdenes seguras
# =========================
def safe_market_sell(symbol: str):
    base = symbol.replace(QUOTE, "")
    price = last_price(symbol)

    free = get_free(base)
    locked = get_locked(base)
    if locked > 0:
        cancel_all(symbol)
        time.sleep(0.2)
        free = get_free(base)

    qty = qty_sellable(symbol, price, free)
    if qty <= 0:
        mark_reject(symbol)
        msg = f"⚠️ {symbol}: saldo {free} no vendible (minQty/minNotional/step). DUST. Cooldown {COOLDOWN_SEC}s."
        log.info(msg); tg_send(msg)
        return None

    qty = qty * (Decimal(1) - SAFETY_QTY_PCT)
    qty = round_down_qty(qty, get_symbol_rules(symbol)["step"])
    if qty <= 0:
        mark_reject(symbol)
        msg = f"⚠️ {symbol}: qty 0 tras safety/rounding. Cooldown {COOLDOWN_SEC}s."
        log.info(msg); tg_send(msg)
        return None

    try:
        order = client.order_market_sell(symbol=symbol, quantity=float(qty))
        mark_recent_sell(symbol)
        return order
    except Exception as e:
        mark_reject(symbol)
        msg = f"❌ Error al vender {symbol}: {e}"
        log.error(msg); tg_send(msg)
        return None

def safe_market_buy(symbol: str):
    price = last_price(symbol)
    quote_free = get_free(QUOTE)

    qty = qty_buyable(symbol, price, quote_free)
    if qty <= 0:
        log.info(f"⚠️ {symbol}: no se compra (quote={quote_free} insuficiente para minNotional).")
        return None

    try:
        order = client.order_market_buy(symbol=symbol, quantity=float(qty))
        return order
    except Exception as e:
        msg = f"❌ Error al comprar {symbol}: {e}"
        log.error(msg); tg_send(msg)
        return None

# =========================
# Señal placeholder (sustituye por tu lógica)
# =========================
def compute_signal(symbol: str):
    """
    Placeholder:
    - Sin posición: compra si sube ~0.2% vs tick anterior.
    - Con posición: TP, SL y trailing.
    """
    px_now = last_price(symbol)
    hist = _TICK_MEM[symbol]
    if len(hist) >= 1:
        px_prev = hist[-1]
        if symbol not in _POSITIONS:
            change = (px_now - px_prev) / px_prev if px_prev > 0 else Decimal(0)
            if change >= Decimal("0.002"):
                return {"action": "BUY", "price": px_now}

    pos = _POSITIONS.get(symbol)
    if not pos:
        return {"action": "HOLD", "price": px_now}

    entry = pos["entry"]
    peak  = pos.get("peak", entry)
    if px_now > peak:
        peak = px_now
        pos["peak"] = peak

    pnl_pct = (px_now - entry) / entry

    if TAKE_PROFIT_PCT > 0 and pnl_pct >= TAKE_PROFIT_PCT:
        return {"action": "SELL_TP", "price": px_now, "pnl": pnl_pct}

    if STOP_LOSS_PCT > 0 and pnl_pct <= (STOP_LOSS_PCT * Decimal(-1)):
        return {"action": "SELL_SL", "price": px_now, "pnl": pnl_pct}

    dd = (peak - px_now) / peak if peak > 0 else Decimal(0)
    if dd >= TRAIL_PCT:
        return {"action": "SELL_TRAIL", "price": px_now, "pnl": pnl_pct}

    return {"action": "HOLD", "price": px_now}

def update_tick_mem(symbol: str, price: Decimal, maxlen: int = 5):
    arr = _TICK_MEM[symbol]
    arr.append(price)
    if len(arr) > maxlen:
        arr.pop(0)

# =========================
# Posiciones
# =========================
def have_position(symbol: str) -> bool:
    base = symbol.replace(QUOTE, "")
    return get_free(base) > Decimal("0")

def refresh_position_from_account(symbol: str):
    base = symbol.replace(QUOTE, "")
    q = get_free(base)
    if q > 0:
        px = last_price(symbol)
        _POSITIONS[symbol] = {"entry": px, "peak": px, "qty": q}
    else:
        _POSITIONS.pop(symbol, None)

def on_buy_filled(symbol: str, order):
    base = symbol.replace(QUOTE, "")
    q = get_free(base)
    px = last_price(symbol)
    _POSITIONS[symbol] = {"entry": px, "peak": px, "qty": q}
    msg = f"✅ COMPRA {symbol}: qty={q} @~{px}"
    log.info(msg); tg_send(msg)

def on_sell_filled(symbol: str, order, reason: str):
    refresh_position_from_account(symbol)
    _POSITIONS.pop(symbol, None)
    mark_recent_sell(symbol)
    msg = f"✅ VENTA {symbol} ({reason}). Rebuy cooldown {REBUY_COOLDOWN_SEC}s."
    log.info(msg); tg_send(msg)

# =========================
# Escaneo por símbolo
# =========================
def scan_symbol(symbol: str):
    try:
        px = last_price(symbol)
    except Exception as e:
        msg = f"❌ Precio {symbol}: {e}"
        log.error(msg); tg_send(msg)
        return

    update_tick_mem(symbol, px)

    # Sincroniza estado con saldo real
    if have_position(symbol) and symbol not in _POSITIONS:
        refresh_position_from_account(symbol)
    if (not have_position(symbol)) and (symbol in _POSITIONS):
        _POSITIONS.pop(symbol, None)

    sig = compute_signal(symbol)
    action = sig["action"]
    pnl = sig.get("pnl")

    if action.startswith("SELL"):
        if should_skip(symbol):
            return
        if not have_position(symbol):
            return

        reasons = {"SELL_TP": "TP", "SELL_SL": "SL", "SELL_TRAIL": "TRAIL"}
        reason = reasons.get(action, "SIG")
        log.info(f"💰 Vendiendo {symbol} por {reason}. PnL={pnl and round(float(pnl)*100, 2)}%")
        order = safe_market_sell(symbol)
        if order:
            on_sell_filled(symbol, order, reason)

    elif action == "BUY":
        if sold_recently(symbol):
            return
        if have_position(symbol):
            return
        order = safe_market_buy(symbol)
        if order:
            on_buy_filled(symbol, order)

    # HOLD => nada

def scan_loop():
    start = time.time()
    for i, sym in enumerate(WATCHLIST):
        # Si el ciclo se está alargando, corta para no pisar el siguiente
        if time.time() - start > SCAN_SEC - 1:
            log.warning("⏱️ Ciclo se alarga, corto para evitar 'skipped'.")
            break

        try:
            get_symbol_rules(sym)  # valida símbolo y cachea
        except Exception as e:
            log.warning(f"⚠️ {sym}: símbolo no válido o sin info: {e}")
            continue

        scan_symbol(sym)
        # micro-sleep para repartir carga y no bloquear si hay latencia
        time.sleep(0.05)

# =========================
# Main
# =========================
def main():
    # Zona horaria robusta
    try:
        tz = pytz.timezone(BOT_TZ_STR)
    except Exception:
        tz = pytz.UTC
        log.warning(f"⚠️ BOT_TZ='{BOT_TZ_STR}' no válida. Uso UTC.")

    log.info("🤖 Bot iniciado. Escaneando…")

    # Scheduler con coalesce para no encolar ciclos atrasados
    sched = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "misfire_grace_time": SCAN_SEC})
    # max_instances=1 para evitar solapar ciclos; coalesce=True evitará colas.
    sched.add_job(scan_loop, 'interval', seconds=SCAN_SEC, max_instances=1, id="scan")
    sched.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Saliendo…")
    finally:
        sched.shutdown(wait=False)

if __name__ == "__main__":
    main()
