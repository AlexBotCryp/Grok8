import os
import time
import json
import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# =========================
# CONFIGURACIÓN (ENV)
# =========================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

BASE_ASSET = os.getenv("BASE_ASSET", "USDC").upper()   # Moneda refugio
SMART_C2C = os.getenv("SMART_C2C", "1").lower() in ("1", "true", "yes")

# Lista de pares a vigilar (en USDC). Puedes añadir/quitar.
WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC"
).split(",") if s.strip()]

# Intervalo de velas (recomendado '1m' para micro-rotación)
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", Client.KLINE_INTERVAL_1MINUTE)

# Frecuencia del loop principal
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))

# Parámetros de señales (puedes ajustar por ENV)
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
EMA_FAST = int(os.getenv("EMA_FAST", "12"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "26"))
VOL_SHORT = int(os.getenv("VOL_SHORT", "5"))
VOL_LONG = int(os.getenv("VOL_LONG", "20"))

# Umbrales de decisión (valores pensados para mí: prudentes pero activos)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.006"))      # 0.60% neto
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT", "0.010"))       # -1.00%
MIN_ROTATE_GAIN  = float(os.getenv("MIN_ROTATE_GAIN", "0.002"))     # 0.20% neto para rotar a mejor
ENTRY_SCORE_MIN  = float(os.getenv("ENTRY_SCORE_MIN", "2.0"))       # Puntuación mínima para entrar
SCORE_ADVANTAGE  = float(os.getenv("SCORE_ADVANTAGE", "0.6"))       # Ventaja mínima sobre activo actual

# Seguridad y fees
DEFAULT_TAKER_FEE = float(os.getenv("DEFAULT_TAKER_FEE", "0.001"))  # 0.10%
SAFETY_BUFFER_PCT = float(os.getenv("SAFETY_BUFFER_PCT", "0.001"))  # 0.10% extra de colchón

# Gestión de ejecución
MIN_HOLD_SECONDS  = int(os.getenv("MIN_HOLD_SECONDS", "120"))  # mantener al menos 2 min antes de rotar
COOLDOWN_SECONDS  = int(os.getenv("COOLDOWN_SECONDS", "20"))   # pausa tras un trade para evitar flip-flop
RESERVE_PCT       = float(os.getenv("RESERVE_PCT", "0.003"))   # 0.3% para evitar fallos por redondeos

# Dust
DUST_MODE = os.getenv("DUST_MODE", "IGNORE").upper()           # IGNORE | SELL_TO_BASE
DUST_MIN_BUFFER = float(os.getenv("DUST_MIN_BUFFER", "0.05"))  # +5% sobre MIN_NOTIONAL si se vende dust

STATE_FILE = os.getenv("STATE_FILE", "state.json")


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


# =========================
# CLIENTE
# =========================
client = Client(API_KEY, API_SECRET)


# =========================
# UTILIDADES DE MERCADO
# =========================
class MarketMeta:
    def __init__(self):
        self.symbols_info: Dict[str, dict] = {}
        self.asset_pairs: Dict[Tuple[str, str], str] = {}  # (base, quote) -> SYMBOL
        self.fee_cache: Dict[str, float] = {}
        self.klines_cache: Dict[str, dict] = {}            # symbol -> {ts_minute, data}

    def load(self):
        ex = client.get_exchange_info()
        for s in ex["symbols"]:
            if s.get("status") != "TRADING":
                continue
            symbol = s["symbol"]
            base = s["baseAsset"]
            quote = s["quoteAsset"]
            self.symbols_info[symbol] = s
            self.asset_pairs[(base, quote)] = symbol

    def symbol_exists(self, base: str, quote: str) -> Optional[str]:
        return self.asset_pairs.get((base.upper(), quote.upper()))

    def get_filters(self, symbol: str) -> dict:
        info = self.symbols_info[symbol]
        return {flt["filterType"]: flt for flt in info["filters"]}

    def step_round(self, qty: float, step: float) -> float:
        return math.floor(qty / step) * step

    def price_round(self, price: float, tick: float) -> float:
        return math.floor(price / tick) * tick

    def get_taker_fee(self, symbol: str) -> float:
        if symbol in self.fee_cache:
            return self.fee_cache[symbol]
        try:
            fees = client.get_trade_fee(symbol=symbol)
            if fees and isinstance(fees, list):
                taker = float(fees[0].get("taker", DEFAULT_TAKER_FEE))
            else:
                taker = DEFAULT_TAKER_FEE
        except Exception:
            taker = DEFAULT_TAKER_FEE
        self.fee_cache[symbol] = taker
        return taker

    def get_price(self, symbol: str) -> float:
        return float(client.get_symbol_ticker(symbol=symbol)["price"])

    def get_klines(self, symbol: str, limit: int = 120) -> List[List]:
        """Cachea por minuto para reducir peso de API."""
        now_minute = int(time.time() // 60)
        cached = self.klines_cache.get(symbol)
        if cached and cached.get("ts_minute") == now_minute:
            return cached["data"]
        data = client.get_klines(symbol=symbol, interval=KLINE_INTERVAL, limit=limit)
        self.klines_cache[symbol] = {"ts_minute": now_minute, "data": data}
        return data

market = MarketMeta()


# =========================
# ESTADO
# =========================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        logging.warning(f"[STATE] No se pudo guardar estado: {e}")


# =========================
# INDICADORES (sin numpy)
# =========================
def ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []
    k = 2 / (period + 1)
    out = []
    ema_prev = sum(values[:period]) / period
    out = [None] * (period - 1) + [ema_prev]
    for price in values[period:]:
        ema_prev = price * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out

def rsi(values: List[float], period: int) -> List[float]:
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out = []
    rs = (avg_gain / avg_loss) if avg_loss != 0 else float('inf')
    out.append(100 - (100 / (1 + rs)))
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else float('inf')
        out.append(100 - (100 / (1 + rs)))
    # Prepend None to align length with prices
    return [None] * (len(values) - len(out)) + out

def sma(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    out = []
    window_sum = sum(values[:period])
    out.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out.append(window_sum / period)
    return [None] * (len(values) - len(out)) + out


# =========================
# BALANCES
# =========================
def get_balances_free() -> Dict[str, float]:
    acc = client.get_account()
    out = {}
    for b in acc["balances"]:
        free = float(b["free"])
        if free > 0:
            out[b["asset"]] = free
    return out

def quote_free(quote: str) -> float:
    return get_balances_free().get(quote.upper(), 0.0)


# =========================
# DUST
# =========================
def min_trade_ok(symbol: str, qty: float, side: str, price: Optional[float] = None) -> bool:
    try:
        f = market.get_filters(symbol)
        if price is None:
            price = market.get_price(symbol)
        notional = qty * price
        min_notional = float(f.get("MIN_NOTIONAL", {}).get("minNotional", "0"))
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        qty_rounded = market.step_round(qty, step)
        return qty_rounded > 0 and notional >= min_notional * (1.0 + DUST_MIN_BUFFER)
    except Exception:
        return False

def clean_dust_to_base():
    if DUST_MODE == "IGNORE":
        logging.info("🧹 Dust: ignorado (DUST_MODE=IGNORE).")
        return
    balances = get_balances_free()
    for asset, qty in balances.items():
        if asset in ("BNB", BASE_ASSET) or qty <= 0:
            continue
        direct = market.symbol_exists(asset, BASE_ASSET)
        if not direct:
            continue
        f = market.get_filters(direct)
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        qty_ok = market.step_round(qty, step)
        if qty_ok <= 0:
            continue
        try:
            price = market.get_price(direct)
            if not min_trade_ok(direct, qty_ok, side="SELL", price=price):
                continue
            client.order_market_sell(symbol=direct, quantity=qty_ok)
            logging.info(f"🧹 Limpieza DUST {direct}: vendidas {qty_ok}")
        except BinanceAPIException as e:
            logging.warning(f"[DUST] No se pudo vender {asset}->{BASE_ASSET}: {e.message}")


# =========================
# RUTEO CRIPTO→CRIPTO
# =========================
def estimate_route_fees(src_asset: str, dst_asset: str) -> Tuple[List[Tuple[str, str]], float]:
    """
    Devuelve (ruta, fee_total_aprox).
    Ruta: lista de (symbol, side) en orden de ejecución.
    Fee total: suma de taker fees estimadas de cada leg.
    """
    src = src_asset.upper()
    dst = dst_asset.upper()
    if src == dst:
        return ([], 0.0)

    # Directo: (dst/src) BUY
    direct_buy = market.symbol_exists(dst, src)
    if direct_buy:
        fee = market.get_taker_fee(direct_buy)
        return ([(direct_buy, "BUY")], fee)

    # Directo: (src/dst) SELL
    direct_sell = market.symbol_exists(src, dst)
    if direct_sell:
        fee = market.get_taker_fee(direct_sell)
        return ([(direct_sell, "SELL")], fee)

    # Puentes
    bridges = ["USDC", "USDT"]
    best = (None, float("inf"))
    for br in bridges:
        leg1 = market.symbol_exists(src, br)  # SELL src/br
        leg2 = market.symbol_exists(dst, br)  # BUY dst/br
        if leg1 and leg2:
            fee = market.get_taker_fee(leg1) + market.get_taker_fee(leg2)
            route = [(leg1, "SELL"), (leg2, "BUY")]
            if fee < best[1]:
                best = (route, fee)
    if best[0] is None:
        return (None, float("inf"))
    return best  # (route, fee_total)


def execute_route(src_asset: str, dst_asset: str) -> bool:
    route, _fee = estimate_route_fees(src_asset, dst_asset)
    if not route:
        if src_asset == dst_asset:
            logging.info("ℹ️ Ruta vacía (activos iguales).")
            return True
        logging.warning(f"⚠️ No hay ruta disponible {src_asset} → {dst_asset}")
        return False

    for symbol, side in route:
        f = market.get_filters(symbol)
        base = market.symbols_info[symbol]["baseAsset"]
        quote = market.symbols_info[symbol]["quoteAsset"]
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        tick = float(f.get("PRICE_FILTER", {}).get("tickSize", "0.00000001"))
        price = market.get_price(symbol)

        try:
            if side == "SELL":
                qty_avail = get_balances_free().get(base, 0.0)
                qty = market.step_round(qty_avail * (1 - RESERVE_PCT), step)
                if qty <= 0 or not min_trade_ok(symbol, qty, "SELL", price=price):
                    logging.warning(f"⚠️ SELL no cumple mínimos {symbol} qty={qty}")
                    return False
                client.order_market_sell(symbol=symbol, quantity=qty)
                logging.info(f"🔁 SELL {symbol} qty={qty}")

            elif side == "BUY":
                quote_avail = get_balances_free().get(quote, 0.0)
                est_qty = (quote_avail * (1 - RESERVE_PCT)) / price
                qty = market.step_round(est_qty, step)
                if qty <= 0 or not min_trade_ok(symbol, qty, "BUY", price=price):
                    logging.warning(f"⚠️ BUY no cumple mínimos {symbol} qty={qty}")
                    return False
                client.order_market_buy(symbol=symbol, quantity=qty)
                logging.info(f"🔁 BUY  {symbol} qty={qty}")
            else:
                logging.error(f"Side desconocido: {side}")
                return False

            time.sleep(0.4)  # respirar para no pasar peso

        except BinanceAPIException as e:
            logging.error(f"[ROUTE] Falló orden {symbol} {side}: {e.message}")
            return False

    logging.info(f"✅ Swap completado: {src_asset} → {dst_asset} (ruta {route})")
    return True


# =========================
# PUNTUACIÓN DE OPORTUNIDADES
# =========================
def analyze_symbol(symbol: str) -> Optional[dict]:
    """
    Devuelve dict con métricas y score. symbol es ej. 'BTCUSDC'.
    """
    try:
        kl = market.get_klines(symbol, limit=120)
        closes = [float(x[4]) for x in kl]
        vols   = [float(x[5]) for x in kl]
        if len(closes) < max(EMA_SLOW + 1, RSI_PERIOD + 2, VOL_LONG + 1):
            return None

        ema_f = ema(closes, EMA_FAST)
        ema_s = ema(closes, EMA_SLOW)
        rsi_v = rsi(closes, RSI_PERIOD)
        sma_vol_short = sma(vols, VOL_SHORT)
        sma_vol_long  = sma(vols, VOL_LONG)

        px = closes[-1]
        ef = ema_f[-1]
        es = ema_s[-1]
        rv = rsi_v[-1]
        vshort = sma_vol_short[-1]
        vlong  = sma_vol_long[-1]

        # Señales básicas
        cross_up = ef is not None and es is not None and ef > es
        cross_down = ef is not None and es is not None and ef < es
        vol_boost = (vshort is not None and vlong is not None and vshort > vlong * 1.05)

        # Score (hecho para mí): combina sobreventa + cruce + volumen
        score = 0.0
        if rv is not None:
            if rv < 30: score += 1.4
            elif rv < 38: score += 1.0
            elif rv < 45: score += 0.6
            elif rv < 55: score += 0.2  # neutral-positivo
            else: score += 0.0

        if cross_up: score += 0.8
        if vol_boost: score += 0.6

        return {
            "symbol": symbol,
            "price": px,
            "ema_fast": ef,
            "ema_slow": es,
            "rsi": rv,
            "vol_boost": vol_boost,
            "cross_up": cross_up,
            "cross_down": cross_down,
            "score": round(score, 3),
        }
    except Exception as e:
        logging.warning(f"[ANALYZE] {symbol} error: {e}")
        return None


def best_candidate() -> Optional[dict]:
    best = None
    for sym in WATCHLIST:
        info = analyze_symbol(sym)
        if not info:
            continue
        if info["score"] < ENTRY_SCORE_MIN:
            continue
        if best is None or info["score"] > best["score"]:
            best = info
    return best


# =========================
# LÓGICA DE ROTACIÓN
# =========================
def current_non_base_assets() -> List[str]:
    bals = get_balances_free()
    return [a for a, q in bals.items() if q > 0 and a not in (BASE_ASSET, "BNB")]

def symbol_to_asset(symbol: str) -> str:
    # 'BTCUSDC' -> 'BTC'
    return market.symbols_info[symbol]["baseAsset"]

def asset_to_symbol_base(asset: str, quote: str = BASE_ASSET) -> Optional[str]:
    return market.symbol_exists(asset, quote)

def approx_position_entry(state: dict, asset: str) -> Optional[float]:
    pos = state.get("position", {})
    if pos.get("asset") == asset:
        return pos.get("entry_price")
    return None

def update_state_after_buy(state: dict, asset: str, entry_price: float):
    state["position"] = {
        "asset": asset,
        "entry_price": entry_price,
        "ts": int(time.time()),
        "last_trade_ts": int(time.time())
    }
    save_state(state)

def clear_position_state(state: dict):
    state["position"] = {}
    save_state(state)

def can_trade_again(state: dict) -> bool:
    pos = state.get("position", {})
    last_trade_ts = pos.get("last_trade_ts") or state.get("last_trade_ts")
    if last_trade_ts is None:
        return True
    return (time.time() - last_trade_ts) >= COOLDOWN_SECONDS

def mark_trade(state: dict):
    now = int(time.time())
    if "position" in state:
        state["position"]["last_trade_ts"] = now
    state["last_trade_ts"] = now
    save_state(state)


def try_enter_from_base(state: dict):
    # Si estamos en BASE, buscar mejor candidato y comprar si supera score
    cand = best_candidate()
    if not cand:
        return
    dst_asset = symbol_to_asset(cand["symbol"])

    # Ejecutar ruta BASE -> dst_asset (normalmente BUY dst/BASE)
    route, fee_est = estimate_route_fees(BASE_ASSET, dst_asset)
    if route is None:
        logging.info(f"❌ No hay ruta BASE->{dst_asset}")
        return

    # Compramos todo el BASE disponible
    # En la mayoría de WATCHLIST hay par directo dst/BASE -> BUY
    ok = execute_route(BASE_ASSET, dst_asset)
    if ok:
        # Precio de entrada aproximado
        entry_price = cand["price"]
        update_state_after_buy(state, dst_asset, entry_price)
        logging.info(f"✅ ENTRADA en {dst_asset} ~{entry_price} (score={cand['score']})")
        mark_trade(state)


def try_rotate_between_assets(state: dict):
    # Estamos en posición: decidir si rotar a otra con mejor score
    pos = state.get("position", {})
    cur_asset = pos.get("asset")
    entry = pos.get("entry_price")
    ts_entry = pos.get("ts", 0)

    if not cur_asset or entry is None:
        return

    if (time.time() - ts_entry) < MIN_HOLD_SECONDS:
        return

    # Señal mejor candidata
    cand = best_candidate()
    if not cand:
        return
    dst_asset = symbol_to_asset(cand["symbol"])
    if dst_asset == cur_asset:
        return

    # Score actual del activo en mano (si está en WATCHLIST)
    cur_symbol = asset_to_symbol_base(cur_asset, BASE_ASSET)
    cur_score = 0.0
    if cur_symbol:
        info = analyze_symbol(cur_symbol)
        if info:
            cur_score = info["score"]

    # Ventaja de score suficiente
    if cand["score"] < cur_score + SCORE_ADVANTAGE:
        return

    # Ganancia/pérdida actual (aprox) del activo en mano
    try:
        cur_px = market.get_price(cur_symbol) if cur_symbol else None
    except Exception:
        cur_px = None

    if cur_px is None:
        return

    gross_gain = (cur_px / entry) - 1.0  # >0 si vamos ganando

    # Estimar fees de ruta cur_asset -> dst_asset
    route, fee_est = estimate_route_fees(cur_asset, dst_asset)
    if route is None:
        return

    # Condiciones de rotación:
    # A) Take profit neto (ganancia >= fees + buffer + TAKE_PROFIT_PCT)
    need_gain_tp = fee_est + SAFETY_BUFFER_PCT + TAKE_PROFIT_PCT
    # B) Rotación por mejor oportunidad (ganancia >= fees + buffer + MIN_ROTATE_GAIN)
    need_gain_rot = fee_est + SAFETY_BUFFER_PCT + MIN_ROTATE_GAIN
    # C) Stop loss (si vamos mal, salir hacia mejor candidato aunque no haya ganancia)
    stop_loss = (-gross_gain) >= STOP_LOSS_PCT

    if gross_gain >= need_gain_tp or gross_gain >= need_gain_rot or stop_loss:
        logging.info(
            f"♻️ ROTACIÓN {cur_asset}→{dst_asset}: "
            f"gain={gross_gain:.4%} need(tp)={need_gain_tp:.4%} need(rot)={need_gain_rot:.4%} "
            f"score_now={cur_score:.2f} -> {cand['score']:.2f}"
        )
        if not can_trade_again(state):
            return
        ok = execute_route(cur_asset, dst_asset)
        if ok:
            update_state_after_buy(state, dst_asset, cand["price"])
            mark_trade(state)


def refuge_if_needed(state: dict):
    """
    Si estamos en posición y:
      - No hay candidatos con score >= ENTRY_SCORE_MIN, y
      - Tenemos pérdida mayor a STOP_LOSS_PCT
    entonces rotar a BASE_ASSET.
    """
    pos = state.get("position", {})
    cur_asset = pos.get("asset")
    entry = pos.get("entry_price")
    if not cur_asset or entry is None:
        return

    # ¿Hay algún candidato decente? (si lo hay, mejor rotar a él que a BASE)
    cand = best_candidate()
    if cand:
        return

    # Si no hay candidatos y vamos mal, refugiarnos
    cur_symbol = asset_to_symbol_base(cur_asset, BASE_ASSET)
    if not cur_symbol:
        return

    cur_px = market.get_price(cur_symbol)
    gross_gain = (cur_px / entry) - 1.0
    if (-gross_gain) >= STOP_LOSS_PCT:
        route, _ = estimate_route_fees(cur_asset, BASE_ASSET)
        if route is None:
            return
        if not can_trade_again(state):
            return
        logging.info(f"🛟 REFUGIO {cur_asset} → {BASE_ASSET} (loss={gross_gain:.2%})")
        ok = execute_route(cur_asset, BASE_ASSET)
        if ok:
            clear_position_state(state)
            mark_trade(state)


# =========================
# LOOP PRINCIPAL
# =========================
def loop():
    logging.info("🤖 Bot iniciado. Escaneando…")
    market.load()
    logging.info("✅ Exchange info cargada.")

    # Limpieza de dust (por defecto IGNORE: no hace nada)
    clean_dust_to_base()

    state = load_state()

    while True:
        try:
            holds = current_non_base_assets()

            # Caso 1: estamos en BASE (sin posiciones)
            if not holds:
                try_enter_from_base(state)

            # Caso 2: tenemos una posición (tomas el primer activo distinto de BASE/BNB)
            else:
                cur_asset = holds[0]
                # Sanear estado si no coincide
                if state.get("position", {}).get("asset") != cur_asset:
                    # Si el bot no registró esta compra (manual/externa), setea precio entrada aprox
                    sym = asset_to_symbol_base(cur_asset, BASE_ASSET)
                    px = market.get_price(sym) if sym else None
                    if px:
                        update_state_after_buy(state, cur_asset, px)
                        logging.info(f"ℹ️ Sincronizado estado con posición actual: {cur_asset} ~{px}")

                # Intentar rotación por oportunidad
                try_rotate_between_assets(state)

                # Si todo está feo, refugio
                refuge_if_needed(state)

            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            logging.info("⏹️ Bot detenido por usuario.")
            break
        except BinanceAPIException as e:
            logging.warning(f"[API] {e.message}. Pausa breve…")
            time.sleep(2)
        except Exception as e:
            logging.exception(f"Error en loop: {e}")
            time.sleep(3)


if __name__ == "__main__":
    loop()
