# main.py
# Bot “cesta 1-2” con uso ~100% del saldo, filtros de entrada (RSI/EMA/ATR),
# trailing con beneficio real, control de comisiones, cooldown por símbolo
# y manejo de stepSize/minNotional para evitar "dust".
#
# Requisitos:
#   python-binance==1.0.19
#   pandas, numpy, requests
#
# ENV requeridas:
#   BINANCE_API_KEY, BINANCE_API_SECRET
#   TELEGRAM_TOKEN (opcional), TELEGRAM_CHAT_ID (opcional)
# Opcionales:
#   WATCHLIST = "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC"
#   SCAN_INTERVAL_SEC = "30"
#   MAX_OPEN_POSITIONS = "2"
#   MIN_USD_PER_ORDER = "20"
#   FEE_RATE_SIDE = "0.001"  # 0.1% por lado si no conocemos tu tarifa exacta
#
# Nota: Este script está pensado para mercados ***USDC*** (par XXXUSDC).

import os
import time
import json
import math
import signal
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import pandas as pd
import requests
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

# --------------------------- Config & Logging ---------------------------

def _parse_float_env(name: str, default: float) -> float:
    val = os.getenv(name, str(default))
    # A prueba de "0,005" con coma
    val = val.replace(",", ".")
    try:
        return float(val)
    except Exception:
        return default

WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC"
).replace(" ", "").split(",")

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "30"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MIN_USD_PER_ORDER = _parse_float_env("MIN_USD_PER_ORDER", 20.0)

# Riesgo / asignación
ALLOC_SINGLE = 0.995   # si sólo 1 posición, usa ~99.5% (buffer fees/redondeos)
ALLOC_DUAL   = 0.497   # si 2 posiciones, ~49.7% cada una

# Entradas (indicadores)
RSI_PERIOD = 14
EMA_FAST   = 9
EMA_SLOW   = 21
ATR_PERIOD = 14
ATR_PCT_MIN = 0.0015   # 0.15% mínimo de volatilidad

# Salidas (beneficio y protección)
TP_TRIGGER_PCT = 0.004   # activar trailing a partir de +0.40%
TRAIL_PCT      = 0.003   # trailing 0.30% desde el pico
HARD_SL_PCT    = 0.006   # stop-loss duro de -0.60%
MIN_HOLD_SEC   = 240     # mínimo 4 min en posición antes de pensar en cerrar

# Comisiones y umbrales de beneficio real
FEE_RATE_SIDE  = _parse_float_env("FEE_RATE_SIDE", 0.001)  # 0.1% por lado (ajusta si tienes descuento)
RT_FEE_RATE    = FEE_RATE_SIDE * 2
MIN_ABS_PROFIT = 0.5      # no cerrar si ganancia < 0.5 USDC (salvo SL)
MIN_PROFIT_XFEE = 1.5     # beneficio bruto >= 1.5x comisiones ida+vuelta

# Enfriamiento por símbolo para no recomprar enseguida
SYMBOL_COOLDOWN_SEC = 900  # 15 min

# Timeframe para indicadores (más estable que 1m)
KLINE_INTERVAL = Client.KLINE_INTERVAL_3MINUTE
KLINE_LIMIT = 200  # suficiente para RSI/EMAs/ATR

# Telegram (opcional)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("bot")

# --------------------------- Binance Client ---------------------------

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
if not API_KEY or not API_SECRET:
    log.error("❌ Falta BINANCE_API_KEY o BINANCE_API_SECRET en variables de entorno.")
    raise SystemExit(1)

client = Client(API_KEY, API_SECRET)

# --------------------------- Utils ---------------------------

def now_ts() -> float:
    return time.time()

def notify(text: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            log.warning(f"[TG] Notificación fallida: {e}")

def fmt_usd(x: float) -> str:
    return f"${x:.2f}"

def safe_round(x: float, prec: int = 8) -> float:
    return float(f"{x:.{prec}f}")

# --------------------------- Exchange Info / Filters ---------------------------

_symbol_filters_cache: Dict[str, Dict[str, Any]] = {}

def refresh_exchange_info():
    global _symbol_filters_cache
    info = client.get_exchange_info()
    cache = {}
    for sym in info["symbols"]:
        s = sym["symbol"]
        fil = {f["filterType"]: f for f in sym["filters"]}
        step = float(fil["LOT_SIZE"]["stepSize"])
        min_qty = float(fil["LOT_SIZE"]["minQty"])
        tick = float(fil["PRICE_FILTER"]["tickSize"])
        # MIN_NOTIONAL puede venir como 'MIN_NOTIONAL' (minNotional) o 'NOTIONAL' (minNotional/notional)
        min_notional = None
        if "MIN_NOTIONAL" in fil:
            min_notional = float(fil["MIN_NOTIONAL"].get("minNotional", fil["MIN_NOTIONAL"].get("notional", 0)))
        elif "NOTIONAL" in fil:
            min_notional = float(fil["NOTIONAL"].get("minNotional", fil["NOTIONAL"].get("notional", 0)))
        cache[s] = {
            "stepSize": step,
            "minQty": min_qty,
            "tickSize": tick,
            "minNotional": min_notional or 10.0  # fallback sensato
        }
    _symbol_filters_cache = cache
    log.info("✅ Exchange info cargada.")

def get_filters(symbol: str) -> Dict[str, Any]:
    if not _symbol_filters_cache:
        refresh_exchange_info()
    return _symbol_filters_cache[symbol]

def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step

# --------------------------- Indicadores ---------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(window=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-12)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def fetch_klines_df(symbol: str) -> pd.DataFrame:
    raw = client.get_klines(symbol=symbol, interval=KLINE_INTERVAL, limit=KLINE_LIMIT)
    cols = ["open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open","high","low","close","volume","qav","taker_base","taker_quote"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df

def build_indicators(symbol: str) -> Dict[str, Any]:
    df = fetch_klines_df(symbol)
    df["ema_f"] = ema(df["close"], EMA_FAST)
    df["ema_s"] = ema(df["close"], EMA_SLOW)
    df["rsi"]   = rsi(df["close"], RSI_PERIOD)
    df["atr"]   = atr(df, ATR_PERIOD)
    df["atr_pct"] = df["atr"] / df["close"]
    last = df.iloc[-1]
    prev = df.iloc[-2]
    return {
        "price": float(last["close"]),
        "ema_f": float(last["ema_f"]),
        "ema_s": float(last["ema_s"]),
        "rsi": float(last["rsi"]),
        "rsi_prev": float(prev["rsi"]),
        "atr_pct": float(last["atr_pct"])
    }

# --------------------------- Balances / Órdenes ---------------------------

def get_free(asset: str) -> float:
    b = client.get_asset_balance(asset=asset)
    if not b:
        return 0.0
    return float(b.get("free", 0.0))

def get_price(symbol: str) -> float:
    return float(client.get_symbol_ticker(symbol=symbol)["price"])

def compute_buy_qty(symbol: str, allocation_usdc: float, price: float) -> float:
    f = get_filters(symbol)
    step = f["stepSize"]
    min_notional = f["minNotional"]
    qty_raw = (allocation_usdc / price) * (1 - FEE_RATE_SIDE)
    qty = round_step(qty_raw, step)
    # asegúrate de cumplir minNotional
    if qty * price < min_notional:
        return 0.0
    return qty

def compute_sell_qty(symbol: str, free_qty: float) -> float:
    f = get_filters(symbol)
    step = f["stepSize"]
    sell_qty = max(free_qty - step, 0.0)  # deja 1 step de colchón
    return round_step(sell_qty, step)

def market_buy(symbol: str, qty: float) -> Dict[str, Any]:
    return client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=str(qty))

def market_sell(symbol: str, qty: float) -> Dict[str, Any]:
    return client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=str(qty))

def quote_from_symbol(symbol: str) -> str:
    # asume XXXUSDC
    return "USDC"

def base_from_symbol(symbol: str) -> str:
    return symbol.replace("USDC", "")

# --------------------------- Estrategia / Estado ---------------------------

class Position:
    def __init__(self, symbol: str, qty: float, entry_price: float):
        self.symbol = symbol
        self.qty = qty
        self.entry = entry_price
        self.open_ts = now_ts()
        self.peak = entry_price  # precio máximo desde entrada (para trailing)
        self.trailing_active = False

    def age_sec(self) -> float:
        return now_ts() - self.open_ts

    def update_peak(self, px: float):
        if px > self.peak:
            self.peak = px

    def activate_trailing(self):
        self.trailing_active = True

positions: Dict[str, Position] = {}  # symbol -> Position
last_exit_time: Dict[str, float] = {} # cooldown por símbolo

def allowed_to_reenter(symbol: str) -> bool:
    last = last_exit_time.get(symbol, 0.0)
    return now_ts() - last >= SYMBOL_COOLDOWN_SEC

def open_positions_count() -> int:
    return len(positions)

def free_usdc() -> float:
    return get_free("USDC")

def hedge_profit_requirements(entry_px: float, current_px: float, notional: float) -> bool:
    gross = (current_px - entry_px) / entry_px
    abs_profit = (current_px - entry_px) * (notional / current_px)
    return (gross >= RT_FEE_RATE * MIN_PROFIT_XFEE) and (abs_profit >= MIN_ABS_PROFIT)

# --------------------------- Señales ---------------------------

def entry_signal(ind: Dict[str, Any]) -> bool:
    # Reglas:
    # 1) Cruce RSI de <50 a >50
    # 2) EMA_f > EMA_s
    # 3) ATR% suficiente
    rsi_ok = ind["rsi_prev"] <= 50 and ind["rsi"] > 50
    ema_ok = ind["ema_f"] > ind["ema_s"]
    vol_ok = ind["atr_pct"] >= ATR_PCT_MIN
    return rsi_ok and ema_ok and vol_ok

# --------------------------- Loop principal ---------------------------

_running = True
def handle_sigterm(signum, frame):
    global _running
    _running = False
    log.info("🛑 Señal recibida, apagando con gracia…")

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

def try_cleanup_dust():
    # Intenta vender restos si cumplen minNotional; si no, los deja en paz.
    for symbol, pos in list(positions.items()):
        # si tenemos posición, no es "dust"
        pass
    # para assets sin posición:
    for sym in WATCHLIST:
        base = base_from_symbol(sym)
        free_base = get_free(base)
        if free_base <= 0:
            continue
        qty_sell = compute_sell_qty(sym, free_base)
        if qty_sell <= 0:
            continue
        px = get_price(sym)
        min_notional = get_filters(sym)["minNotional"]
        if qty_sell * px >= min_notional:
            try:
                res = market_sell(sym, qty_sell)
                log.info(f"🧹 Limpieza DUST {sym}: vendidas {qty_sell}")
            except Exception as e:
                log.warning(f"🧹 Limpieza {sym} fallida: {e}")

def main():
    log.info("🤖 Bot iniciado. Escaneando…")
    refresh_exchange_info()
    cycle = 0

    while _running:
        t0 = time.time()
        try:
            # --- Venta / gestión de posiciones ---
            for sym, pos in list(positions.items()):
                ind = build_indicators(sym)
                px = ind["price"]
                notional = pos.qty * px
                pos.update_peak(px)

                # Activar trailing sólo si el pico supera el umbral y además cubre comisiones+min abs
                profit_needed = max(
                    TP_TRIGGER_PCT,
                    RT_FEE_RATE * MIN_PROFIT_XFEE
                )

                if ((pos.peak - pos.entry) / pos.entry) >= profit_needed and \
                   hedge_profit_requirements(pos.entry, pos.peak, pos.qty * pos.peak) and \
                   pos.age_sec() >= MIN_HOLD_SEC:
                    pos.activate_trailing()

                # Condición de trailing: si activo y cae TRAIL_PCT desde el pico
                if pos.trailing_active:
                    drawdown = (pos.peak - px) / pos.peak
                    # vender sólo si al precio actual aún se cumplen beneficios netos
                    if drawdown >= TRAIL_PCT and hedge_profit_requirements(pos.entry, px, notional):
                        qty_sell = compute_sell_qty(sym, pos.qty)
                        if qty_sell > 0 and notional >= MIN_USD_PER_ORDER:
                            try:
                                market_sell(sym, qty_sell)
                                log.info(f"✅ VENTA {sym} (TRAIL) qty={qty_sell:.8f} notional≈{fmt_usd(qty_sell*px)}")
                                notify(f"✅ VENTA {sym} (trailing) {qty_sell:.8f} a ~{fmt_usd(px)}")
                            except Exception as e:
                                log.error(f"❌ Venta {sym} fallo: {e}")
                                continue
                            # actualizar cooldown y cerrar posición
                            last_exit_time[sym] = now_ts()
                            del positions[sym]
                            continue

                # Stop-loss duro (protección última)
                if ((px - pos.entry) / pos.entry) <= -HARD_SL_PCT and pos.age_sec() >= MIN_HOLD_SEC:
                    qty_sell = compute_sell_qty(sym, pos.qty)
                    if qty_sell > 0 and notional >= MIN_USD_PER_ORDER:
                        try:
                            market_sell(sym, qty_sell)
                            log.info(f"🛑 SL {sym} qty={qty_sell:.8f} notional≈{fmt_usd(qty_sell*px)}")
                            notify(f"🛑 STOP {sym} {qty_sell:.8f} a ~{fmt_usd(px)}")
                        except Exception as e:
                            log.error(f"❌ SL {sym} fallo: {e}")
                            continue
                        last_exit_time[sym] = now_ts()
                        del positions[sym]
                        continue

            # --- Entradas (sólo si hay hueco) ---
            if open_positions_count() < MAX_OPEN_POSITIONS:
                free = free_usdc()
                if open_positions_count() == 0:
                    alloc = free * ALLOC_SINGLE
                else:
                    alloc = free * (ALLOC_DUAL / (1.0 if open_positions_count()==1 else 1.0))

                # Evita órdenes demasiado pequeñas
                if alloc >= max(MIN_USD_PER_ORDER, 25.0):
                    # Evalúa watchlist y elige el mejor candidato con señal válida
                    candidates: List[Tuple[str, Dict[str, Any]]] = []
                    for sym in WATCHLIST:
                        if sym in positions:
                            continue
                        if not allowed_to_reenter(sym):
                            continue
                        try:
                            ind = build_indicators(sym)
                        except Exception as e:
                            log.warning(f"[IND] {sym} error: {e}")
                            continue
                        if entry_signal(ind):
                            candidates.append((sym, ind))

                    # Ordena candidatos por “fuerza”: RSI más alto y distancia EMA_f-EMA_s
                    if candidates:
                        candidates.sort(
                            key=lambda t: (t[1]["rsi"], t[1]["ema_f"] - t[1]["ema_s"], t[1]["atr_pct"]),
                            reverse=True
                        )
                        # Abrir 1 símbolo por ciclo como máximo
                        sym, ind = candidates[0]
                        px = ind["price"]
                        qty = compute_buy_qty(sym, alloc, px)

                        if qty * px >= max(MIN_USD_PER_ORDER, get_filters(sym)["minNotional"]) and qty > 0:
                            try:
                                market_buy(sym, qty)
                                positions[sym] = Position(sym, qty, px)
                                log.info(f"✅ COMPRA {sym} qty={qty:.8f} notional≈{fmt_usd(qty*px)}")
                                notify(f"✅ COMPRA {sym} {qty:.8f} a ~{fmt_usd(px)}")
                            except Exception as e:
                                log.error(f"❌ Compra {sym} fallo: {e}")
                        else:
                            log.info(f"[SKIP] {sym} qty insuficiente ({qty:.8f}) o minNotional no cumple.")
                else:
                    log.debug(f"[SKIP] USDC insuficiente para nueva entrada. Free={fmt_usd(free)}")

            # Limpieza de restos cada N ciclos
            cycle += 1
            if cycle % 30 == 0:
                try_cleanup_dust()

        except Exception as e:
            log.error(f"⚠️ Error en ciclo: {e}")

        # Control de ritmo y solapamientos
        elapsed = time.time() - t0
        if elapsed > SCAN_INTERVAL_SEC * 0.8:
            log.warning("⏱️ Ciclo se alarga, ajusta SCAN_INTERVAL_SEC si ves 'skips'.")
        sleep_s = max(0.0, SCAN_INTERVAL_SEC - elapsed)
        time.sleep(sleep_s)

if __name__ == "__main__":
    main()
