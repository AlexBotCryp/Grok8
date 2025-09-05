# main.py
import os
import time
import math
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from collections import defaultdict
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance.enums import *

# =========================
# CONFIGURACIÓN POR ENV
# =========================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Cotizamos TODO contra USDC (puedes cambiarlo p.ej. a USDT)
QUOTE = os.getenv("QUOTE_ASSET", "USDC")

# Lista de símbolos a escanear (separados por comas)
WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,AVAXUSDC"
).replace(" ", "").split(",")

# Porcentaje trailing para venta (0.004 = 0.4%)
TRAIL_PCT = Decimal(os.getenv("TRAIL_PCT", "0.004").replace(",", "."))

# Beneficio/Stop opcional (si <=0, desactivado)
TAKE_PROFIT_PCT = Decimal(os.getenv("TAKE_PROFIT_PCT", "0.006").replace(",", "."))  # 0.6%
STOP_LOSS_PCT   = Decimal(os.getenv("STOP_LOSS_PCT",   "0.010").replace(",", "."))  # 1.0%

# Mínimo notional deseado para operar (además del minNotional del exchange)
# Útil para evitar compras minúsculas que pierden en comisiones:
USER_MIN_NOTIONAL = Decimal(os.getenv("USER_MIN_NOTIONAL", "20"))  # 20 USDC

# Intervalo de escaneo en segundos
SCAN_SEC = int(os.getenv("SCAN_SEC", "15"))

# Seguridad para qty (absorber fee/redondeos)
SAFETY_QTY_PCT = Decimal(os.getenv("SAFETY_QTY_PCT", "0.001").replace(",", "."))  # 0.1%

# Cooldown cuando el símbolo queda en "dust" o falla por minQty/minNotional
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "180"))

# Evitar recomprar el mismo símbolo inmediatamente después de venderlo (en segundos)
REBUY_COOLDOWN_SEC = int(os.getenv("REBUY_COOLDOWN_SEC", "120"))

# Porcentaje máximo de la cartera USDC a usar en una compra (0-1)
MAX_PORTFOLIO_USE = Decimal(os.getenv("MAX_PORTFOLIO_USE", "0.80").replace(",", "."))  # 80%

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# Decimal precisión alta para evitar errores de redondeo
getcontext().prec = 28

# =========================
# Cliente Binance
# =========================
if not API_KEY or not API_SECRET:
    log.warning("⚠️ Falta BINANCE_API_KEY o BINANCE_API_SECRET en variables de entorno.")

client = Client(API_KEY, API_SECRET)

# =========================
# Cache de reglas
# =========================
_RULES_CACHE = {}
_LAST_REJECT = {}         # symbol -> timestamp de último rechazo por dust/minQty/minNotional
_LAST_SELL_TIME = {}      # symbol -> timestamp última venta (para evitar recomprar de inmediato)
_POSITIONS = {}           # symbol -> dict con estado (entrada, peak, qty, etc.)

def now_ts() -> float:
    return time.time()

def should_skip_for_a_while(symbol: str, wait_sec: int = COOLDOWN_SEC) -> bool:
    t = _LAST_REJECT.get(symbol)
    return bool(t and (now_ts() - t) < wait_sec)

def mark_reject(symbol: str):
    _LAST_REJECT[symbol] = now_ts()

def mark_recent_sell(symbol: str):
    _LAST_SELL_TIME[symbol] = now_ts()

def sold_recently(symbol: str, window: int = REBUY_COOLDOWN_SEC) -> bool:
    t = _LAST_SELL_TIME.get(symbol)
    return bool(t and (now_ts() - t) < window)

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

    base_precision = max(0, -step.as_tuple().exponent)
    price_precision = max(0, -tick.as_tuple().exponent)

    rules = {
        "step": step,
        "min_qty": min_qty,
        "price_tick": tick,
        "min_notional": min_notional,
        "base_precision": base_precision,
        "price_precision": price_precision,
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
    if not bal:
        return Decimal("0")
    return Decimal(bal["free"])

def get_locked(asset: str) -> Decimal:
    bal = client.get_asset_balance(asset=asset)
    if not bal:
        return Decimal("0")
    return Decimal(bal.get("locked", "0"))

def last_price(symbol: str) -> Decimal:
    t = client.get_symbol_ticker(symbol=symbol)
    return Decimal(t["price"])

def qty_sellable(symbol: str, price: Decimal, qty_free: Decimal) -> Decimal:
    rules = get_symbol_rules(symbol)
    qty = round_down_qty(qty_free, rules["step"])
    if qty <= 0:
        return Decimal("0")

    # Notional check
    notional = qty * price
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)
    if notional < min_notional_req:
        # intentar subir… pero solo vender lo que tenemos, así que si no llega => dust
        if qty < rules["min_qty"]:
            return Decimal("0")
        return Decimal("0")

    # Respeta minQty
    if qty < rules["min_qty"]:
        return Decimal("0")

    return qty

def qty_buyable(symbol: str, price: Decimal, quote_free: Decimal) -> Decimal:
    """Calcular qty de compra cumpliendo notional mínimo y stepSize."""
    rules = get_symbol_rules(symbol)

    # No usar más del % configurado del quote disponible
    max_quote = quote_free * MAX_PORTFOLIO_USE
    # Asegurar mínimo notional (regla exchange y regla usuario)
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)

    # Si no llegamos al mínimo notional, no compramos
    if max_quote < min_notional_req:
        return Decimal("0")

    # qty bruta por el max_quote
    qty = max_quote / price
    # margen por seguridad
    qty = qty * (Decimal(1) - SAFETY_QTY_PCT)
    # redondeo a step
    qty = round_down_qty(qty, rules["step"])

    # Validar minQty y notional tras redondeo
    if qty < rules["min_qty"]:
        return Decimal("0")
    if qty * price < min_notional_req:
        return Decimal("0")
    return qty

def cancel_all(symbol: str):
    try:
        client.cancel_open_orders(symbol=symbol)
    except Exception as e:
        # algunas cuentas no tienen permisos o no hay órdenes
        pass

def safe_market_sell(symbol: str):
    base_asset = symbol.replace(QUOTE, "")
    price = last_price(symbol)

    acc_free = get_free(base_asset)
    acc_locked = get_locked(base_asset)

    # Si hay locked, cancelar órdenes para liberar
    if acc_locked > 0:
        cancel_all(symbol)
        time.sleep(0.2)
        acc_free = get_free(base_asset)

    qty = qty_sellable(symbol, price, acc_free)
    if qty <= 0:
        mark_reject(symbol)
        log.info(f"⚠️ {symbol}: saldo {acc_free} no vendible (minQty/minNotional/step). Marcado DUST. No reintento por {COOLDOWN_SEC}s.")
        return None

    # Buffer para absorber fee y evitar que Binance lo reduzca por debajo del step
    qty = qty * (Decimal(1) - SAFETY_QTY_PCT)
    qty = round_down_qty(qty, get_symbol_rules(symbol)["step"])
    if qty <= 0:
        mark_reject(symbol)
        log.info(f"⚠️ {symbol}: qty se volvió 0 tras safety/rounding. No reintento por {COOLDOWN_SEC}s.")
        return None

    try:
        order = client.order_market_sell(symbol=symbol, quantity=float(qty))
        mark_recent_sell(symbol)
        return order
    except Exception as e:
        mark_reject(symbol)
        log.error(f"❌ Error al vender {symbol}: {e}")
        return None

def safe_market_buy(symbol: str):
    price = last_price(symbol)
    quote_free = get_free(QUOTE)

    qty = qty_buyable(symbol, price, quote_free)
    if qty <= 0:
        log.info(f"⚠️ {symbol}: no compramos (quote={quote_free} insuficiente para minNotional/usuario).")
        return None

    # precio a tick
    tick = get_symbol_rules(symbol)["price_tick"]
    # aunque sea market, redondear puede ayudar para otras rutas si cambias a LIMIT
    price_rd = round_down_price(price, tick)

    try:
        order = client.order_market_buy(symbol=symbol, quantity=float(qty))
        return order
    except Exception as e:
        log.error(f"❌ Error al comprar {symbol}: {e}")
        return None

# =========================
# Señales muy simples de ejemplo
# =========================
def compute_signal(symbol: str):
    """
    Ejemplo mínimo:
    - Si no tenemos posición: compra cuando el precio sube 0.2% en los últimos ticks.
    - Si tenemos posición: aplica trailing, TP y SL.
    Esto es sólo un placeholder sencillo; integra aquí tu lógica real (RSI/EMA/volumen/Grok, etc.)
    """
    px_now = last_price(symbol)

    pos = _POSITIONS.get(symbol)
    if not pos:
        # Sin posición => detectar micro momentum (placeholder)
        # Truco mínimo: comparar precio actual con el de hace X segundos guardado en una memoria simple
        hist = _TICK_MEM[symbol]
        if len(hist) >= 2:
            px_prev = hist[-2]
            change = (px_now - px_prev) / px_prev if px_prev > 0 else Decimal(0)
            if change >= Decimal("0.002"):  # +0.2%
                return {"action": "BUY", "price": px_now}
        return {"action": "HOLD", "price": px_now}

    # Con posición => trailing / TP / SL
    entry = pos["entry"]
    peak = pos.get("peak", entry)
    qty  = pos["qty"]

    # Actualizar pico si sube
    if px_now > peak:
        peak = px_now
        pos["peak"] = peak

    pnl_pct = (px_now - entry) / entry

    # Take Profit
    if TAKE_PROFIT_PCT > 0 and pnl_pct >= TAKE_PROFIT_PCT:
        return {"action": "SELL_TP", "price": px_now, "pnl": pnl_pct}

    # Stop Loss
    if STOP_LOSS_PCT > 0 and pnl_pct <= (STOP_LOSS_PCT * Decimal(-1)):
        return {"action": "SELL_SL", "price": px_now, "pnl": pnl_pct}

    # Trailing: si cae más de TRAIL_PCT desde el peak, vende
    drawdown_from_peak = (peak - px_now) / peak if peak > 0 else Decimal(0)
    if drawdown_from_peak >= TRAIL_PCT:
        return {"action": "SELL_TRAIL", "price": px_now, "pnl": pnl_pct}

    return {"action": "HOLD", "price": px_now}

# Memoria muy simple de ticks recientes por símbolo
_TICK_MEM = defaultdict(list)

def update_tick_mem(symbol: str, price: Decimal, maxlen: int = 5):
    arr = _TICK_MEM[symbol]
    arr.append(price)
    if len(arr) > maxlen:
        arr.pop(0)

# =========================
# Gestión de posiciones
# =========================
def have_position(symbol: str) -> bool:
    base = symbol.replace(QUOTE, "")
    free = get_free(base)
    return free > Decimal("0")

def refresh_position_from_account(symbol: str):
    """Sincroniza posición a partir del saldo real por si hubo operaciones externas."""
    base = symbol.replace(QUOTE, "")
    q = get_free(base)
    if q > 0:
        px = last_price(symbol)
        _POSITIONS[symbol] = {
            "entry": px,  # si no la sabemos, tomamos precio actual de referencia
            "peak": px,
            "qty": q
        }
    else:
        _POSITIONS.pop(symbol, None)

def on_buy_filled(symbol: str, order):
    # Releer saldo para conocer qty exacta
    base = symbol.replace(QUOTE, "")
    q = get_free(base)
    px = last_price(symbol)
    _POSITIONS[symbol] = {
        "entry": px,
        "peak": px,
        "qty": q
    }
    log.info(f"✅ COMPRA ejecutada {symbol}: qty={q} @~{px}")

def on_sell_filled(symbol: str, order, reason: str):
    # Tras vender, sincroniza y marca cooldown de recompra
    refresh_position_from_account(symbol)
    _POSITIONS.pop(symbol, None)
    mark_recent_sell(symbol)
    log.info(f"✅ VENTA ejecutada {symbol} ({reason}). Evitamos recomprar durante {REBUY_COOLDOWN_SEC}s.")

# =========================
# Loop principal
# =========================
def scan_symbol(symbol: str):
    try:
        px = last_price(symbol)
    except Exception as e:
        log.error(f"❌ No se pudo obtener precio {symbol}: {e}")
        return

    update_tick_mem(symbol, px)

    # Sincroniza estado básico si no cuadra con el saldo real
    if have_position(symbol) and symbol not in _POSITIONS:
        refresh_position_from_account(symbol)
    if (not have_position(symbol)) and (symbol in _POSITIONS):
        _POSITIONS.pop(symbol, None)

    sig = compute_signal(symbol)
    action = sig["action"]
    pnl = sig.get("pnl")

    if action.startswith("SELL"):
        # Evitar reintento si estamos en cooldown por rechazos previos
        if should_skip_for_a_while(symbol):
            return
        if not have_position(symbol):
            return

        reasons = {
            "SELL_TP": "TP",
            "SELL_SL": "SL",
            "SELL_TRAIL": "TRAIL"
        }
        reason = reasons.get(action, "SIG")

        log.info(f"💰 Vendiendo {symbol} por {reason}. PnL={pnl and round(float(pnl)*100, 2)}%")
        order = safe_market_sell(symbol)
        if order:
            on_sell_filled(symbol, order, reason)

    elif action == "BUY":
        # Evita recomprar el mismo símbolo inmediatamente
        if sold_recently(symbol):
            return
        if have_position(symbol):
            return

        order = safe_market_buy(symbol)
        if order:
            on_buy_filled(symbol, order)

    # HOLD => no hacer nada

def scan_loop():
    for sym in WATCHLIST:
        # Filtra símbolos no existentes en cuenta/futuros
        try:
            get_symbol_rules(sym)  # fuerza cache/valida símbolo
        except Exception as e:
            log.warning(f"⚠️ {sym}: no válido o sin info de exchange: {e}")
            continue

        scan_symbol(sym)

def main():
    log.info("🤖 Bot iniciado. Escaneando…")
    sched = BackgroundScheduler(timezone=str(time.tzname))
    sched.add_job(scan_loop, 'interval', seconds=SCAN_SEC, max_instances=1)
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
