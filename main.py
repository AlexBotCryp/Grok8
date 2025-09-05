import os
import time
import json
import math
import logging
import traceback
from datetime import datetime, timedelta, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

# ---------------------- Config & Logs ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

INTERVAL = os.getenv("INTERVAL", "1m")
LOOKBACK = int(os.getenv("LOOKBACK", "120"))
LOOP_SEC = int(os.getenv("LOOP_SEC", "20"))

BASE = "USDC"
USE_CAPITAL_PCT = float(os.getenv("USE_CAPITAL_PCT", "1.0"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "20"))

WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC"
).split(",") if s.strip()]

FEE_PCT = float(os.getenv("FEE_PCT", "0.001"))                 # 0.1% taker
EXTRA_PROFIT_PCT = float(os.getenv("EXTRA_PROFIT_PCT", "0.001"))  # 0.1% extra
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.01"))      # 1% SL (0=off)
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "0.0"))               # 0.5% trailing

REBUY_COOLDOWN_MIN = int(os.getenv("REBUY_COOLDOWN_MIN", "5"))
REENTRY_MIN_PCT = float(os.getenv("REENTRY_MIN_PCT", "0.005")) # 0.5%

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"

client = Client(API_KEY, API_SECRET)

# ---------------------- Utils ----------------------
def now_utc():
    return datetime.now(timezone.utc)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_exit": {}, "trailing": {}, "cooldown": {}}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_exit": {}, "trailing": {}, "cooldown": {}}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)

def notify(msg: str):
    logging.info(msg)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
            )
        except Exception as e:
            logging.warning(f"[TG] {e}")

def round_step(value, step):
    if step is None:
        return value
    precision = int(round(-math.log10(float(step))))
    return float(f"{value:.{precision}f}")

def get_symbol_filters(symbol_info):
    lot_step = None
    price_tick = None
    min_notional = None
    min_qty = None
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            lot_step = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "PRICE_FILTER":
            price_tick = float(f["tickSize"])
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            # Some markets use NOTIONAL, older MIN_NOTIONAL exists too
            min_notional = float(f.get("minNotional") or f.get("notional"))
    return lot_step, price_tick, min_notional, min_qty

def fetch_klines(symbol, interval, limit):
    # Small sleep to avoid weight spikes when scanning many symbols
    time.sleep(0.05)
    return client.get_klines(symbol=symbol, interval=interval, limit=limit)

def closes_from_klines(kl):
    return [float(x[4]) for x in kl]

def ema(series, period):
    if len(series) < period:
        return []
    k = 2 / (period + 1)
    ema_vals = [sum(series[:period]) / period]
    for price in series[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    # pad to same length
    pad = [None] * (len(series) - len(ema_vals))
    return pad + ema_vals

def rsi(series, period=14):
    if len(series) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = series[i] - series[i-1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis = []
    for i in range(period + 1, len(series)):
        ch = series[i] - series[i-1]
        gain = max(ch, 0)
        loss = abs(min(ch, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else 999999
        rsis.append(100 - (100 / (1 + rs)))
    pad = [None] * (len(series) - len(rsis))
    return pad + rsis

def get_price(symbol):
    return float(client.get_symbol_ticker(symbol=symbol)["price"])

def account_free(asset):
    balances = client.get_asset_balance(asset=asset)
    if not balances:
        return 0.0
    return float(balances["free"]) + float(balances["locked"]) * 0.0

def get_symbol_info_cached(cache, symbol):
    if symbol not in cache:
        cache[symbol] = client.get_symbol_info(symbol)
    return cache[symbol]

def can_reenter(state, symbol, current_price):
    # Cooldown check
    cd = state.get("cooldown", {}).get(symbol)
    if cd:
        if now_utc().timestamp() < cd:
            return False, f"en cooldown hasta {datetime.fromtimestamp(cd, tz=timezone.utc)}"
    # Variación mínima desde la última salida
    le = state.get("last_exit", {}).get(symbol)
    if le:
        last_px = le.get("price")
        if last_px:
            diff = abs(current_price - last_px) / last_px
            if diff < REENTRY_MIN_PCT:
                return False, f"variación {diff:.4f} < REENTRY_MIN_PCT {REENTRY_MIN_PCT:.4f}"
    return True, ""

def set_cooldown(state, symbol):
    expire = now_utc() + timedelta(minutes=REBUY_COOLDOWN_MIN)
    state.setdefault("cooldown", {})[symbol] = expire.timestamp()

def record_exit(state, symbol, price):
    state.setdefault("last_exit", {})[symbol] = {
        "price": price,
        "ts": now_utc().timestamp()
    }
    set_cooldown(state, symbol)

def required_profit_pct():
    # Beneficio mínimo que cubra comisiones ida+vuelta + margen extra
    return 2 * FEE_PCT + EXTRA_PROFIT_PCT

# ---------------------- Signals ----------------------
def compute_signals(symbol):
    k = fetch_klines(symbol, INTERVAL, LOOKBACK)
    c = closes_from_klines(k)
    if len(c) < 50:
        return None
    ema12 = ema(c, 12)
    ema26 = ema(c, 26)
    r14 = rsi(c, 14)
    px = c[-1]
    e12, e26, r = ema12[-1], ema26[-1], r14[-1]
    if None in (e12, e26, r):
        return None

    # Señal de compra conservadora contra latigazos:
    # - EMA12 > EMA26 (tendencia corta al alza)
    # - RSI cruza al alza zona 35-55 (temprano pero con fuerza)
    # - Confirmación: cierre actual > EMA12
    buy = (e12 > e26) and (35 < r < 60) and (px > e12)

    # Señal de venta:
    # - Si ya tenemos posición, se usa gestión de beneficio/SL/trailling más abajo.
    # Aquí solo devolvemos indicadores.
    return {
        "price": px,
        "ema12": e12,
        "ema26": e26,
        "rsi": r,
        "buy": buy
    }

# ---------------------- Orders ----------------------
def place_market_buy(symbol, usdc_amount, sym_info_cache):
    si = get_symbol_info_cached(sym_info_cache, symbol)
    lot_step, _, min_notional, min_qty = get_symbol_filters(si)
    price = get_price(symbol)
    qty = usdc_amount / price

    if min_notional and usdc_amount < min_notional:
        raise ValueError(f"Notional {usdc_amount:.2f} < minNotional {min_notional}")

    if min_qty and qty < min_qty:
        qty = min_qty

    if lot_step:
        qty = max(qty, min_qty or 0)
        qty = round_step(qty, lot_step)

    order = client.order_market_buy(symbol=symbol, quantity=qty)
    return order, price, qty

def place_market_sell(symbol, qty, sym_info_cache):
    si = get_symbol_info_cached(sym_info_cache, symbol)
    lot_step, _, _, min_qty = get_symbol_filters(si)
    if lot_step:
        qty = round_step(qty, lot_step)
    if min_qty and qty < min_qty:
        raise ValueError(f"Qty {qty} < minQty {min_qty}")
    order = client.order_market_sell(symbol=symbol, quantity=qty)
    return order

# ---------------------- Portfolio Helpers ----------------------
def consolidate_dust_to_usdc(symbol_info_cache):
    """Vende cualquier coin (del watchlist) que tenga saldo residual > minNotional a USDC."""
    try:
        acct = client.get_account()
        balances = {b["asset"]: float(b["free"]) for b in acct["balances"]}
        for sym in WATCHLIST:
            asset = sym.replace(BASE, "")
            if asset == BASE:
                continue
            free = balances.get(asset, 0.0)
            if free <= 0:
                continue
            try:
                si = get_symbol_info_cached(symbol_info_cache, sym)
                _, _, min_notional, _ = get_symbol_filters(si)
                px = get_price(sym)
                notional = free * px
                if min_notional and notional < min_notional:
                    continue
                notify(f"Consolidando residuo: vendiendo {free} {asset} -> {BASE}")
                place_market_sell(sym, free, symbol_info_cache)
                time.sleep(0.3)
            except Exception as e:
                logging.warning(f"[Consolidación] {sym}: {e}")
                continue
    except Exception as e:
        logging.warning(f"[Consolidación general] {e}")

def current_position(symbol_info_cache):
    """Devuelve (symbol, asset_free_qty, price_now) si hay una única posición de watchlist (no USDC)."""
    acct = client.get_account()
    balances = {b["asset"]: float(b["free"]) for b in acct["balances"]}
    for sym in WATCHLIST:
        asset = sym.replace(BASE, "")
        free = balances.get(asset, 0.0)
        if free and free > 0:
            px = get_price(sym)
            return sym, asset, free, px
    return None, None, 0.0, 0.0

def usdc_balance():
    return account_free(BASE)

# ---------------------- Strategy Core ----------------------
def select_candidate(state, sym_info_cache):
    """Elige el mejor símbolo para comprar EXCLUYENDO el último vendido si incumple filtros."""
    scored = []
    for sym in WATCHLIST:
        try:
            sig = compute_signals(sym)
            if not sig:
                continue

            px = sig["price"]
            ok, why = can_reenter(state, sym, px)
            # Permitimos comprar símbolos que NO estén en cooldown/variación bloqueada
            if not ok:
                # Lo excluimos del ranking si no está permitido reentrar
                logging.debug(f"[{sym}] Excluido por filtro reentrada: {why}")
                continue

            # Ranking sencillo: RSI medio + distancia EMA12-EMA26
            rank = 0.0
            if sig["buy"]:
                rank += 1.0
            rank += max(0.0, sig["ema12"] - sig["ema26"]) / max(1e-9, sig["price"]) * 100
            rank += max(0.0, 55 - abs(50 - sig["rsi"]))  # favorece RSI ~50 (inicio impulso)
            scored.append((rank, sym, sig))
        except BinanceAPIException as e:
            logging.warning(f"[{sym}] API: {e}")
        except Exception as e:
            logging.warning(f"[{sym}] {e}")

    if not scored:
        return None, None

    scored.sort(reverse=True, key=lambda x: x[0])
    best = scored[0]
    return best[1], best[2]

def manage_trailing(state, symbol, entry_px, current_px):
    if TRAIL_PCT <= 0:
        return False  # no trailing decision
    t = state.setdefault("trailing", {}).get(symbol)
    if not t:
        # inicializa pico
        state["trailing"][symbol] = {"peak": current_px, "armed": False}
        return False
    peak = t.get("peak", current_px)
    armed = t.get("armed", False)
    if current_px > peak:
        peak = current_px
        state["trailing"][symbol] = {"peak": peak, "armed": True}
        return False
    # Si baja TRAIL_PCT desde el máximo, dispara venta
    if armed and (peak - current_px) / peak >= TRAIL_PCT:
        return True
    return False

def strategy_loop():
    state = load_state()
    sym_info_cache = {}

    # Consolidar residuos al arrancar (no bloqueante)
    consolidate_dust_to_usdc(sym_info_cache)

    notify("🤖 Bot iniciado. Escaneando…")

    while True:
        try:
            # ¿Tenemos posición abierta en alguna del watchlist?
            sym_pos, asset, qty, px_now = current_position(sym_info_cache)
            usdc = usdc_balance()

            if sym_pos:
                # Gestionar salida: target de beneficio neto, stop loss, trailing
                # Guardar/leer precio de entrada aproximado vía última orden no es trivial con market,
                # así que estimamos por balance inicial vs actual. Simplificamos: recalculamos usando
                # el último 'last_exit' como referencia inversa si existiera. Para mayor precisión podrías
                # persistir 'last_entry' en el estado con el precio de ejecución.
                # Aquí optamos por consultar la media de las últimas velas como fallback del entry.
                sig = compute_signals(sym_pos)
                if not sig:
                    time.sleep(LOOP_SEC)
                    continue

                current_px = sig["price"]

                # Estimar entry: si tenemos trailing state con 'peak', usamos primera asignación como pista.
                # Para un entry robusto, añadimos un campo 'last_entry' al estado cuando compremos.
                entry_info = load_state()  # recarga ligera por si se editó en otra ruta
                last_entry = entry_info.get("last_entry", {}).get(sym_pos, {}).get("price")
                if not last_entry:
                    # fallback simple: suponer que compramos cerca del EMA12 al cruzar
                    last_entry = sig["ema12"]

                pnl = (current_px - last_entry) / last_entry if last_entry else 0.0
                need = required_profit_pct()

                do_trail = manage_trailing(state, sym_pos, last_entry, current_px)
                hit_tp = pnl >= need
                hit_sl = (STOP_LOSS_PCT > 0) and (pnl <= -STOP_LOSS_PCT)

                if hit_tp or hit_sl or do_trail:
                    reason = "TP" if hit_tp else ("SL" if hit_sl else "TRAIL")
                    notify(f"💰 Vendiendo {asset} ({sym_pos}) por {reason}. PnL={pnl*100:.2f}%")
                    try:
                        place_market_sell(sym_pos, qty, sym_info_cache)
                        save_state(state)  # guardar trailing/estado por si acaso
                        # Registrar salida y activar cooldown
                        record_exit(state, sym_pos, current_px)
                        save_state(state)
                    except Exception as e:
                        notify(f"❌ Error al vender {sym_pos}: {e}")
                    time.sleep(LOOP_SEC)
                    continue

                # Si no toca vender, no hacer nada
                time.sleep(LOOP_SEC)
                continue

            # Si NO hay posición abierta, buscamos compra (rotación inteligente)
            candidate, sig = select_candidate(state, sym_info_cache)
            usdc = usdc_balance()
            if candidate and sig and usdc * USE_CAPITAL_PCT >= MIN_ORDER_USD:
                # Otra capa de seguridad: vuelve a chequear filtros anti-recompra
                ok, why = can_reenter(state, candidate, sig["price"])
                if not ok:
                    logging.info(f"Rechazado {candidate} por reentry: {why}")
                    time.sleep(LOOP_SEC)
                    continue

                if not sig["buy"]:
                    # No forzamos compra si la señal no es clara
                    time.sleep(LOOP_SEC)
                    continue

                capital = usdc * USE_CAPITAL_PCT
                capital = max(capital, 0.0)
                if capital < MIN_ORDER_USD:
                    time.sleep(LOOP_SEC)
                    continue

                try:
                    order, exec_px, qty = place_market_buy(candidate, capital, sym_info_cache)
                    notify(f"🛒 Comprado {candidate} qty={qty} ~{exec_px:.6f} con {capital:.2f} {BASE}")
                    # Persistir precio de entrada para TP/SL
                    st = load_state()
                    st.setdefault("last_entry", {})[candidate] = {
                        "price": exec_px,
                        "ts": now_utc().timestamp()
                    }
                    # Resetear trailing para el símbolo
                    st.setdefault("trailing", {})[candidate] = {"peak": exec_px, "armed": False}
                    save_state(st)
                except Exception as e:
                    notify(f"❌ Error al comprar {candidate}: {e}")

            time.sleep(LOOP_SEC)

        except BinanceAPIException as e:
            logging.warning(f"[BinanceAPI] {e}")
            time.sleep(2)
        except BinanceRequestException as e:
            logging.warning(f"[BinanceRequest] {e}")
            time.sleep(2)
        except Exception as e:
            logging.error(f"Loop error: {e}\n{traceback.format_exc()}")
            time.sleep(3)

# ---------------------- Entry ----------------------
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        raise SystemExit("Faltan BINANCE_API_KEY / BINANCE_API_SECRET")
    strategy_loop()
