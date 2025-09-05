# main.py
import os
import time
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from collections import defaultdict, deque

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

# Cotizamos contra USDC (puedes usar USDT si quieres)
QUOTE = os.getenv("QUOTE_ASSET", "USDC")

# Máximo 2 posiciones simultáneas, usa el 100% de la cartera
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS", "2"))
MAX_PORTFOLIO_USE  = Decimal(os.getenv("MAX_PORTFOLIO_USE", "1.00").replace(",", "."))  # 100%
USER_MIN_NOTIONAL  = Decimal(os.getenv("USER_MIN_NOTIONAL", "20").replace(",", "."))     # compra/venta mínima deseada

# Lista de símbolos a escanear
WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,AVAXUSDC"
).replace(" ", "").split(",")

# Señales/gestión
TRAIL_PCT       = Decimal(os.getenv("TRAIL_PCT",       "0.006").replace(",", "."))  # trailing 0.6%
TAKE_PROFIT_PCT = Decimal(os.getenv("TAKE_PROFIT_PCT", "0.012").replace(",", "."))  # TP 1.2%
STOP_LOSS_PCT   = Decimal(os.getenv("STOP_LOSS_PCT",   "0.008").replace(",", "."))  # SL 0.8%

# Frecuencia y seguridad
SCAN_SEC        = int(os.getenv("SCAN_SEC", "25"))
SAFETY_QTY_PCT  = Decimal(os.getenv("SAFETY_QTY_PCT", "0.001").replace(",", "."))   # 0.1% para absorber fees/redondeo
COOLDOWN_SEC    = int(os.getenv("COOLDOWN_SEC", "180"))                              # si queda en dust
REBUY_COOLDOWN_SEC = int(os.getenv("REBUY_COOLDOWN_SEC", "180"))                    # no recomprar enseguida mismo símbolo
GLOBAL_BUY_GAP_SEC = int(os.getenv("GLOBAL_BUY_GAP_SEC", "30"))                     # separa compras entre sí
MIN_HOLD_SEC       = int(os.getenv("MIN_HOLD_SEC", "600"))                           # mantener posición mínimo (evita churn)

# Indicadores
EMA_FAST_N = int(os.getenv("EMA_FAST_N", "12"))
EMA_SLOW_N = int(os.getenv("EMA_SLOW_N", "26"))
RSI_N      = int(os.getenv("RSI_N", "14"))

# Zona horaria
BOT_TZ_STR = os.getenv("BOT_TZ", "Europe/Madrid")

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bot")
getcontext().prec = 28

# =========================
# Cliente Binance
# =========================
if not API_KEY or not API_SECRET:
    log.warning("⚠️ Falta BINANCE_API_KEY o BINANCE_API_SECRET.")
client = Client(API_KEY, API_SECRET)

# =========================
# Telegram
# =========================
def tg_enabled() -> bool:
    return bool(TG_TOKEN and TG_CHAT)

def tg_send(text: str):
    if not tg_enabled():
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": text}, timeout=4)
        if r.status_code != 200:
            log.error(f"❌ Telegram HTTP {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"❌ Telegram excepción: {e}")

# =========================
# Estado y caches
# =========================
_RULES_CACHE = {}
_LAST_REJECT = {}        # symbol -> ts último rechazo (dust/minQty/minNotional)
_LAST_SELL   = {}        # symbol -> ts última venta (para evitar recomprar)
_LAST_GLOBAL_BUY = 0.0   # separa compras entre sí

# Posiciones en cartera (solo contaremos base free >0 como posición)
_POSITIONS = {}          # symbol -> {entry, peak, qty, ts_entry}

# Indicadores por símbolo
_IND = {
    # symbol: {
    #   "ema_f": Decimal,
    #   "ema_s": Decimal,
    #   "rsi": Decimal or None,
    #   "avg_gain": Decimal,
    #   "avg_loss": Decimal,
    #   "last_px": Decimal,
    #   "warmup": int (ticks acumulados)
    # }
}
_TICK_MEM = defaultdict(lambda: deque(maxlen=3))  # memoria ligera de últimos precios

# caches por ciclo (para reducir llamadas API)
_price_cache = {}        # symbol -> Decimal(price)
_bal_free = {}           # asset -> Decimal(free)
_bal_locked = {}         # asset -> Decimal(locked)

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
# Reglas símbolo y utilidades
# =========================
def get_symbol_rules(symbol: str):
    if symbol in _RULES_CACHE:
        return _RULES_CACHE[symbol]
    info = client.get_symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Symbol info no disponible para {symbol}")
    filters = {f["filterType"]: f for f in info["filters"]}
    lot = filters["LOT_SIZE"]; price_filter = filters["PRICE_FILTER"]
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
    step = Decimal(lot["stepSize"]); min_qty = Decimal(lot["minQty"])
    tick = Decimal(price_filter["tickSize"])
    min_notional = Decimal(notional_filter.get("minNotional", "0")) if notional_filter else Decimal("0")
    rules = {"step": step, "min_qty": min_qty, "price_tick": tick, "min_notional": min_notional}
    _RULES_CACHE[symbol] = rules
    return rules

def round_down_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0: return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

def last_price(symbol: str) -> Decimal:
    p = _price_cache.get(symbol)
    if p is None:
        t = client.get_symbol_ticker(symbol=symbol)
        p = Decimal(t["price"])
        _price_cache[symbol] = p
    return p

def get_free(asset: str) -> Decimal:
    return _bal_free.get(asset, Decimal("0"))

def get_locked(asset: str) -> Decimal:
    return _bal_locked.get(asset, Decimal("0"))

# =========================
# Indicadores (EMA + RSI incremental)
# =========================
def update_indicators(symbol: str, px: Decimal):
    d = _IND.get(symbol)
    if d is None:
        _IND[symbol] = {
            "ema_f": px,
            "ema_s": px,
            "rsi": None,
            "avg_gain": Decimal("0"),
            "avg_loss": Decimal("0"),
            "last_px": px,
            "warmup": 1
        }
        return

    # EMA
    alpha_f = Decimal(2) / Decimal(EMA_FAST_N + 1)
    alpha_s = Decimal(2) / Decimal(EMA_SLOW_N + 1)
    d["ema_f"] = (px - d["ema_f"]) * alpha_f + d["ema_f"]
    d["ema_s"] = (px - d["ema_s"]) * alpha_s + d["ema_s"]

    # RSI Wilder incremental
    change = px - d["last_px"]
    gain = change if change > 0 else Decimal("0")
    loss = -change if change < 0 else Decimal("0")
    if d["warmup"] < RSI_N:
        # Acumula medias iniciales
        d["avg_gain"] = (d["avg_gain"] * (d["warmup"] - 1) + gain) / max(1, d["warmup"])
        d["avg_loss"] = (d["avg_loss"] * (d["warmup"] - 1) + loss) / max(1, d["warmup"])
        d["rsi"] = None
        d["warmup"] += 1
    else:
        d["avg_gain"] = (d["avg_gain"] * (RSI_N - 1) + gain) / RSI_N
        d["avg_loss"] = (d["avg_loss"] * (RSI_N - 1) + loss) / RSI_N
        if d["avg_loss"] == 0:
            d["rsi"] = Decimal("100")
        else:
            rs = d["avg_gain"] / d["avg_loss"]
            d["rsi"] = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

    d["last_px"] = px

def indicator_score(symbol: str):
    d = _IND.get(symbol)
    if not d or d["rsi"] is None:
        return None
    ema_ok = d["ema_f"] > d["ema_s"]
    rsi = d["rsi"]
    # Evita sobrecompra fuerte
    if rsi >= 70:
        return None
    # Score mezcla momentum + tendencia
    ema_ratio = (d["ema_f"] / d["ema_s"]) if d["ema_s"] > 0 else Decimal("1")
    score = (rsi - Decimal("50")) + (ema_ratio - Decimal("1")) * Decimal("150")
    # Penaliza si EMA no cruza aún
    if not ema_ok:
        score -= Decimal("10")
    return float(score)

# =========================
# Cantidades comprables/vendibles
# =========================
def qty_sellable(symbol: str, price: Decimal, qty_free: Decimal) -> Decimal:
    rules = get_symbol_rules(symbol)
    qty = round_down_qty(qty_free, rules["step"])
    if qty <= 0:
        return Decimal("0")
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)
    if (qty * price) < min_notional_req:
        return Decimal("0")
    if qty < rules["min_qty"]:
        return Decimal("0")
    return qty

def qty_buyable(symbol: str, price: Decimal, quote_free: Decimal) -> Decimal:
    rules = get_symbol_rules(symbol)
    min_notional_req = max(rules["min_notional"], USER_MIN_NOTIONAL)
    if quote_free < min_notional_req:
        return Decimal("0")
    qty = (quote_free / price) * (Decimal(1) - SAFETY_QTY_PCT)
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
def safe_market_sell(symbol: str, reason: str):
    base = symbol.replace(QUOTE, "")
    price = last_price(symbol)
    free = get_free(base); locked = get_locked(base)
    if locked > 0:
        cancel_all(symbol); time.sleep(0.2)

    qty = qty_sellable(symbol, price, free)
    if qty <= 0:
        mark_reject(symbol)
        msg = f"⚠️ {symbol}: saldo {free} no vendible (dust/minQty/minNotional). Cooldown {COOLDOWN_SEC}s."
        log.info(msg); tg_send(msg)
        return None

    qty = round_down_qty(qty * (Decimal(1) - SAFETY_QTY_PCT), get_symbol_rules(symbol)["step"])
    if qty <= 0:
        mark_reject(symbol)
        msg = f"⚠️ {symbol}: qty 0 tras safety/rounding. Cooldown {COOLDOWN_SEC}s."
        log.info(msg); tg_send(msg)
        return None

    try:
        order = client.order_market_sell(symbol=symbol, quantity=float(qty))
        mark_recent_sell(symbol)
        msg = f"✅ VENTA {symbol} ({reason}) qty={qty} notional≈{(qty*price):.2f}"
        log.info(msg); tg_send(msg)
        return order
    except Exception as e:
        mark_reject(symbol)
        msg = f"❌ Error al vender {symbol}: {e}"
        log.error(msg); tg_send(msg)
        return None

def safe_market_buy(symbol: str, allocation_quote: Decimal):
    price = last_price(symbol)
    qty = qty_buyable(symbol, price, allocation_quote)
    if qty <= 0:
        log.info(f"⚠️ {symbol}: no compra (quote={allocation_quote} insuficiente).")
        return None
    try:
        order = client.order_market_buy(symbol=symbol, quantity=float(qty))
        msg = f"✅ COMPRA {symbol} qty={qty} notional≈{(qty*price):.2f}"
        log.info(msg); tg_send(msg)
        return order
    except Exception as e:
        msg = f"❌ Error al comprar {symbol}: {e}"
        log.error(msg); tg_send(msg)
        return None

# =========================
# Posiciones
# =========================
def have_position(symbol: str) -> bool:
    base = symbol.replace(QUOTE, "")
    return get_free(base) > Decimal("0")

def refresh_position_from_account(symbol: str):
    base = symbol.replace(QUOTE, "")
    q = get_free(base)
    px = last_price(symbol)
    if q > 0:
        if symbol not in _POSITIONS:
            _POSITIONS[symbol] = {"entry": px, "peak": px, "qty": q, "ts_entry": now_ts()}
        else:
            _POSITIONS[symbol]["qty"] = q
            # mantener entry original; actualizar peak si corresponde
            if px > _POSITIONS[symbol]["peak"]:
                _POSITIONS[symbol]["peak"] = px
    else:
        _POSITIONS.pop(symbol, None)

def position_pnl(symbol: str):
    pos = _POSITIONS.get(symbol)
    if not pos:
        return None
    entry = pos["entry"]
    px = last_price(symbol)
    if entry <= 0:
        return None
    return (px - entry) / entry

# =========================
# Lógica de señales y gestión de cartera (máx. 2 posiciones, 100% uso)
# =========================
def evaluate_and_trade():
    global _LAST_GLOBAL_BUY

    # 1) Actualiza posiciones desde saldos reales
    for sym in WATCHLIST:
        refresh_position_from_account(sym)

    open_symbols = list(_POSITIONS.keys())
    num_open = len(open_symbols)

    # 2) Reglas de salida para posiciones abiertas
    for sym in open_symbols:
        if should_skip(sym):
            continue
        pos = _POSITIONS[sym]
        px = last_price(sym)
        pos["peak"] = max(pos["peak"], px)
        pnl = position_pnl(sym)
        hold_time = now_ts() - pos["ts_entry"]

        # Ventas por SL/TP siempre activas; trailing solo si ya hemos tenido subida
        if pnl is not None:
            if TAKE_PROFIT_PCT > 0 and pnl >= TAKE_PROFIT_PCT:
                safe_market_sell(sym, "TP")
                continue
            if STOP_LOSS_PCT > 0 and pnl <= (STOP_LOSS_PCT * Decimal(-1)):
                safe_market_sell(sym, "SL")
                continue

            # Trailing: si cae desde el pico más que TRAIL_PCT
            if pos["peak"] > 0:
                dd = (pos["peak"] - px) / pos["peak"]
                if dd >= TRAIL_PCT and hold_time >= MIN_HOLD_SEC/2:
                    safe_market_sell(sym, "TRAIL")
                    continue

    # Recalcula tras ventas
    for sym in WATCHLIST:
        refresh_position_from_account(sym)
    open_symbols = list(_POSITIONS.keys())
    num_open = len(open_symbols)

    # 3) Selección de compras (máx. 2 posiciones en total)
    quote_free = get_free(QUOTE)
    if num_open >= MAX_POSITIONS or quote_free <= Decimal("0"):
        return

    # Enfriamiento global entre compras para no spamear
    if now_ts() - _LAST_GLOBAL_BUY < GLOBAL_BUY_GAP_SEC:
        return

    # Construye ranking de candidatos por score
    scored = []
    for sym in WATCHLIST:
        if have_position(sym) or sold_recently(sym):
            continue
        sc = indicator_score(sym)
        if sc is None:
            continue
        # filtro mínimo para considerar compra
        if sc < 2.5:   # umbral prudente
            continue
        scored.append((sc, sym))

    if not scored:
        return

    scored.sort(reverse=True)  # mayor score primero
    # Decide cuántos comprar para llegar a MAX_POSITIONS
    slots = MAX_POSITIONS - num_open
    to_buy_syms = [s for _, s in scored[:slots]]

    # Asignación: usa el 100% de la cartera disponible repartido entre los seleccionados
    # (si 1 símbolo -> 100%; si 2 símbolos -> 50% y 50%)
    n = len(to_buy_syms)
    if n == 0:
        return

    allocation_each = (quote_free * MAX_PORTFOLIO_USE) / Decimal(n)

    bought_any = False
    for sym in to_buy_syms:
        order = safe_market_buy(sym, allocation_each)
        if order:
            # inicializa/actualiza posición
            refresh_position_from_account(sym)
            # si position nueva, fija entry como precio actual y peak=entry
            if sym in _POSITIONS:
                _POSITIONS[sym]["entry"] = last_price(sym)
                _POSITIONS[sym]["peak"]  = _POSITIONS[sym]["entry"]
                _POSITIONS[sym]["ts_entry"] = now_ts()
            bought_any = True

    if bought_any:
        _LAST_GLOBAL_BUY = now_ts()

# =========================
# Ciclo: precache de precios, balances, actualizar indicadores
# =========================
def precache_prices_and_balances():
    global _price_cache, _bal_free, _bal_locked
    # Precios
    try:
        tickers = client.get_all_tickers()
        _price_cache = {t['symbol']: Decimal(t['price']) for t in tickers}
    except Exception as e:
        log.error(f"❌ Error al cargar precios: {e}")
        _price_cache = {}
    # Balances
    try:
        acc = client.get_account()
        free = {}; locked = {}
        for b in acc.get("balances", []):
            asset = b["asset"]
            free[asset] = Decimal(b["free"])
            locked[asset] = Decimal(b["locked"])
        _bal_free, _bal_locked = free, locked
    except Exception as e:
        log.error(f"❌ Error al cargar balances: {e}")
        _bal_free, _bal_locked = {}, {}

def scan_loop():
    start = time.time()

    # 1) cache precios + balances en 2 llamadas
    precache_prices_and_balances()

    # 2) actualizar indicadores con precio actual
    for sym in WATCHLIST:
        # corta si se alarga demasiado el ciclo
        if time.time() - start > SCAN_SEC - 1:
            log.warning("⏱️ Ciclo se alarga, corto para evitar 'skipped'.")
            break
        try:
            # valida reglas solo 1a vez (cache)
            get_symbol_rules(sym)
        except Exception as e:
            log.warning(f"⚠️ {sym}: sin info de exchange: {e}")
            continue

        px = _price_cache.get(sym)
        if px is None:
            continue

        _TICK_MEM[sym].append(px)
        update_indicators(sym, px)
        time.sleep(0.01)

    # 3) aplicar lógica de trading (salidas + entradas)
    evaluate_and_trade()

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
    tg_send("🚀 Bot cripto iniciado y listo. Modo: máx 2 posiciones, 100% cartera.")

    # Scheduler compacto y sin solapamiento
    sched = BackgroundScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "misfire_grace_time": SCAN_SEC}
    )
    sched.add_job(scan_loop, 'interval', seconds=SCAN_SEC, max_instances=1, id="scan", replace_existing=True)
    # Heartbeat opcional
    sched.add_job(lambda: tg_send("💓 Heartbeat: bot en marcha."), 'interval', minutes=15,
                  id="heartbeat", coalesce=True, max_instances=1, replace_existing=True)
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
