# main.py
import os, json, time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

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
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
TP_PCT             = Decimal(os.getenv("TP_PCT", "0.006"))  # 0.6%
SL_PCT             = Decimal(os.getenv("SL_PCT", "0.007"))  # 0.7%
ALLOC_PCT          = Decimal(os.getenv("ALLOC_PCT", "1.0")) # 100% del USDC libre
STATE_PATH         = os.getenv("STATE_PATH", "state.json")
WATCHLIST          = os.getenv("WATCHLIST", "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,LTCUSDC")

LOG_PREFIX = lambda lvl: f"{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {lvl} -"

def info(msg): print(f"{LOG_PREFIX('INFO')} {msg}")
def warn(msg): print(f"{LOG_PREFIX('WARNING')} {msg}")
def err (msg): print(f"{LOG_PREFIX('ERROR')} {msg}")

# ========= Cliente =========
if not API_KEY or not API_SECRET:
    warn("Claves Binance no configuradas. Estás en modo lectura hasta que las añadas.")
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

# ========= Exchange info / filtros =========
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

def price_tick(symbol: str) -> Decimal:
    f = _get_filter(symbol, "PRICE_FILTER")
    return Decimal(str(f["tickSize"])) if f else Decimal("0")

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

def round_by_tick(px: Decimal, tick: Decimal) -> Decimal:
    if tick == 0: return px
    n_ticks = (px / tick).to_integral_value(rounding=ROUND_DOWN)
    return n_ticks * tick

# ========= Datos / Precios =========
def last_price(symbol: str) -> Decimal:
    r = client.get_symbol_ticker(symbol=symbol)
    return Decimal(str(r["price"]))

def get_klines(symbol: str, interval="1m", limit=60) -> List[dict]:
    # Devuelve lista con dicts simples: time, open, high, low, close, volume
    raws = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    kls = []
    for k in raws:
        kls.append({
            "t": int(k[0]),
            "o": Decimal(str(k[1])),
            "h": Decimal(str(k[2])),
            "l": Decimal(str(k[3])),
            "c": Decimal(str(k[4])),
            "v": Decimal(str(k[5])),
        })
    return kls

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

# ========= Consolidación a USDC =========
def sell_all_to_usdc(asset: str, qty: Decimal) -> bool:
    if asset == BASE_ASSET: return False
    symbol = f"{asset}{BASE_ASSET}"
    if not symbol_exists(symbol):
        info(f"🚫 No existe {symbol}, no consolido {asset}.")
        return False

    px = last_price(symbol)
    if px <= 0:
        info(f"🚫 Sin precio para {symbol}.")
        return False

    notional = qty * px
    ex_min = min_notional(symbol)
    min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC

    if notional < min_needed:
        info(f"🟡 {asset}: notional {notional:.4f} < min {min_needed:.4f}. "
             f"{'Ignoro (dust)' if DUST_MODE=='IGNORE' else 'Intento vender igualmente'}")
        if DUST_MODE == "IGNORE":
            return False

    step = lot_step(symbol)
    q_round = round_by_step(qty, step) if step > 0 else qty
    if q_round <= 0:
        info(f"🟡 {asset}: qty tras redondeo es 0. No vendo.")
        return False

    if LIVE_MODE:
        try:
            info(f"🔁 Consolida {asset} -> {BASE_ASSET}: {symbol} qty={q_round}")
            client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=float(q_round))
            time.sleep(0.7)
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
    non_base = {a: q for a, q in bals.items() if a != BASE_ASSET and q > 0}
    if non_base:
        info(f"🧹 Consolidación: {len(non_base)} activos ≠ {BASE_ASSET} detectados.")
        for a, q in non_base.items():
            sell_all_to_usdc(a, q)
    # Re-log saldo base
    usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {usdc:.2f}")
    # Fija estado a sin posición
    state["base_asset"] = BASE_ASSET
    state["open_symbol"] = None
    state["open_qty"] = 0.0
    state["avg_price"] = 0.0
    state["last_trade_ts"] = int(time.time())
    save_state(state)
    info("✅ Posición base forzada: USDC. Sin posición abierta.")

# ========= Señales simples (EMA + momento + volumen básico) =========
def score_symbol(symbol: str) -> Tuple[Decimal, dict]:
    """
    Calcula una puntuación simple basada en:
      - Precio actual por encima de EMA(20)
      - Pendiente EMA(20) positiva
      - Cambio % últimas 5 velas
      - Volumen por encima de mediana simple
    """
    try:
        kl = get_klines(symbol, interval="1m", limit=40)
        closes = [k["c"] for k in kl]
        vols   = [k["v"] for k in kl]
        if len(closes) < 25:
            return Decimal("0"), {"reason": "pocas velas"}

        ema20_hist = []
        acc = []
        for c in closes:
            acc.append(c)
            if len(acc) >= 20:
                ema20_hist.append(ema(acc[-20:], 20))
            else:
                ema20_hist.append(Decimal("0"))
        px = closes[-1]
        e20 = ema20_hist[-1]
        e20_prev = ema20_hist[-2] if len(ema20_hist) >= 2 else e20

        above = Decimal("1") if px > e20 else Decimal("0")
        slope = (e20 - e20_prev) / (e20_prev if e20_prev != 0 else e20 + Decimal("1"))
        slope_ok = Decimal("1") if slope > 0 else Decimal("0")

        # Momento últimas 5 velas
        px5 = closes[-6]
        mom = (px - px5) / px5 if px5 > 0 else Decimal("0")

        # Volumen: compara última vela vs mediana simple (aprox)
        vols_sorted = sorted(vols[-20:])
        v_median = vols_sorted[len(vols_sorted)//2]
        v_ok = Decimal("1") if vols[-1] >= v_median else Decimal("0")

        score = above * Decimal("1.2") + slope_ok * Decimal("0.8") + (mom * Decimal("2.0")) + (v_ok * Decimal("0.4"))
        meta = {
            "px": str(px), "ema20": str(e20),
            "above": bool(above), "slope_pos": bool(slope_ok),
            "mom_pct": float(mom), "v_ok": bool(v_ok)
        }
        return score, meta
    except Exception as e:
        warn(f"{symbol} fallo señal: {e}")
        return Decimal("0"), {"reason":"error señal"}

# ========= Órdenes =========
def place_market_buy(symbol: str, usdc_to_spend: Decimal) -> Optional[dict]:
    px = last_price(symbol)
    if px <= 0:
        info(f"⛔ {symbol} sin precio válido.")
        return None

    ex_min = min_notional(symbol)
    min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if usdc_to_spend < min_needed:
        info(f"⛔ Notional insuficiente ({usdc_to_spend:.2f} < {min_needed:.2f}) para {symbol}.")
        return None

    step = lot_step(symbol)
    qty = (usdc_to_spend / px)
    qty = round_by_step(qty, step) if step > 0 else qty
    if qty <= 0:
        info(f"⛔ Qty redondeada es 0 para {symbol}.")
        return None

    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=float(qty))
            info(f"✅ COMPRA {symbol} qty={qty} notional≈{(qty*px):.2f}")
            return o
        except BinanceAPIException as be:
            err(f"Orden BUY falló {symbol}: {be.message}")
            return None
        except Exception as e:
            err(f"Orden BUY falló {symbol}: {e}")
            return None
    else:
        info(f"[DRY] BUY {symbol} qty={qty} notional≈{(qty*px):.2f}")
        return {"dry": True, "symbol": symbol, "qty": float(qty), "price": float(px)}

def place_market_sell(symbol: str, qty: Decimal) -> Optional[dict]:
    step = lot_step(symbol)
    q = round_by_step(qty, step) if step > 0 else qty
    if q <= 0:
        info(f"⛔ Qty de venta redondeada es 0 para {symbol}.")
        return None
    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=float(q))
            px = last_price(symbol)
            info(f"✅ VENTA {symbol} qty={q} notional≈{(q*px):.2f}")
            return o
        except BinanceAPIException as be:
            err(f"Orden SELL falló {symbol}: {be.message}")
            return None
        except Exception as e:
            err(f"Orden SELL falló {symbol}: {e}")
            return None
    else:
        px = last_price(symbol)
        info(f"[DRY] SELL {symbol} qty={q} notional≈{(q*px):.2f}")
        return {"dry": True, "symbol": symbol, "qty": float(q), "price": float(px)}

# ========= Gestión de posición =========
def position_open() -> bool:
    return bool(state.get("open_symbol"))

def open_symbol() -> Optional[str]:
    return state.get("open_symbol")

def manage_position():
    sym = open_symbol()
    if not sym:
        return
    try:
        px = last_price(sym)
        avg = Decimal(str(state.get("avg_price", 0)))
        qty = Decimal(str(state.get("open_qty", 0)))
        if avg <= 0 or qty <= 0:
            info("⚠️ Estado inconsistente, cierro posición en memoria.")
            state["open_symbol"] = None
            state["open_qty"] = 0.0
            state["avg_price"] = 0.0
            save_state(state)
            return

        up = (px - avg) / avg
        down = (avg - px) / avg

        if up >= TP_PCT:
            info(f"🎯 TP alcanzado {sym}: +{float(up)*100:.2f}% (avg={avg}, px={px})")
            if place_market_sell(sym, qty):
                # Volver a USDC
                asset = sym.replace(BASE_ASSET, "")
                sell_all_to_usdc(asset, qty)  # por si vendimos a otro par (seguridad)
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
            return

        if down >= SL_PCT:
            info(f"🛑 SL activado {sym}: -{float(down)*100:.2f}% (avg={avg}, px={px})")
            if place_market_sell(sym, qty):
                asset = sym.replace(BASE_ASSET, "")
                sell_all_to_usdc(asset, qty)
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
            return

        info(f"📈 Mantengo {sym}: px={px} avg={avg} PnL={(px-avg)/avg*100:.2f}%")

    except Exception as e:
        warn(f"manage_position error: {e}")

# ========= Bucle de escaneo =========
_last_cycle_start = 0

def scan_loop():
    global _last_cycle_start
    start_t = time.time()
    _last_cycle_start = start_t
    info("🤖 Escaneando…")

    # 1) Forzar base USDC si así se configura (solo una vez al arranque, pero puedes dejarlo aquí seguro)
    # (comentado para no hacerlo en cada ciclo)
    # if AUTO_CONSOLIDATE: force_base_position_usdc()

    # 2) Si hay posición abierta -> gestionar
    if position_open():
        manage_position()
        spent = time.time() - start_t
        if spent > SCAN_INTERVAL_SEC - 1:
            warn("⏱️ Ciclo se alarga, corto para evitar 'skipped'.")
        return

    # 3) Sin posición abierta: verificar USDC libre
    free_usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {free_usdc:.2f}")

    if free_usdc < MIN_NOTIONAL_USDC:
        info(f"⛔ USDC insuficiente para operar (min={MIN_NOTIONAL_USDC}).")
        return

    # Cooldown
    if COOLDOWN_SEC > 0 and (time.time() - state.get("last_trade_ts", 0)) < COOLDOWN_SEC:
        wait = COOLDOWN_SEC - int(time.time() - state.get("last_trade_ts", 0))
        info(f"⏳ Cooldown activo {wait}s.")
        return

    # 4) Watchlist (filtrar solo símbolos existentes y con sufijo USDC)
    symbols = [s.strip().upper() for s in WATCHLIST.split(",") if s.strip()]
    symbols = [s for s in symbols if s.endswith(BASE_ASSET) and symbol_exists(s)]
    if not symbols:
        info("⛔ WATCHLIST vacía o inválida.")
        return

    # 5) Puntuar y elegir mejor candidato
    best_sym = None
    best_score = Decimal("-999")
    best_meta = {}
    for s in symbols:
        sc, meta = score_symbol(s)
        info(f"[SIG] {s} score={float(sc):.4f} meta={meta}")
        if sc > best_score:
            best_score, best_sym, best_meta = sc, s, meta

    if best_sym is None or best_score <= 0:
        info("🟡 Sin señal clara (score<=0). No compro.")
        return

    # 6) Comprar
    to_spend = free_usdc * ALLOC_PCT
    order = place_market_buy(best_sym, to_spend)
    if order:
        # Leer qty real (si LIVE) o estimada (dry)
        px_now = last_price(best_sym)
        if "fills" in (order or {}):
            # sumar qty llenada
            qty = sum(Decimal(str(f["qty"])) for f in order["fills"])
            avg = (sum(Decimal(str(f["price"])) * Decimal(str(f["qty"])) for f in order["fills"]) / qty) if qty > 0 else px_now
        else:
            # dry-run u orden sin fills (depende de la api)
            qty = to_spend / px_now
            qty = round_by_step(qty, lot_step(best_sym))
            avg = px_now

        state["open_symbol"] = best_sym
        state["open_qty"] = float(qty)
        state["avg_price"] = float(avg)
        state["last_trade_ts"] = int(time.time())
        save_state(state)
    else:
        info("🟠 No se pudo ejecutar compra: revisa logs anteriores.")

    spent = time.time() - start_t
    if spent > SCAN_INTERVAL_SEC - 1:
        warn("⏱️ Ciclo se alarga, corto para evitar 'skipped'.")

# ========= Arranque =========
def main():
    info("🤖 Bot iniciado. Escaneando…")
    refresh_exchange_info()
    info("✅ Exchange info cargada.")
    info(f"🧹 Dust: {'ignorado' if DUST_MODE=='IGNORE' else 'intento vender (SELL)'} (DUST_MODE={DUST_MODE}).")

    # Fuerza base USDC y limpia estado de posición
    if AUTO_CONSOLIDATE:
        force_base_position_usdc()

    # Log de sincronización
    if position_open():
        sym = open_symbol()
        info(f"ℹ️ Sincronizado estado con posición actual: {sym}")
    else:
        info(f"ℹ️ Sin posición abierta. Base: {BASE_ASSET}")

    # Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_loop, "interval",
                      seconds=SCAN_INTERVAL_SEC,
                      max_instances=1, coalesce=True, misfire_grace_time=10)
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        info("🛑 Detenido por usuario.")
    finally:
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    main()
