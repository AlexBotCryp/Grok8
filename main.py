# -*- coding: utf-8 -*-
import os
import time
import json
import random
import logging
import threading
import requests
import pytz
import numpy as np
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia")

# ──────────────────────────────────────────────────────────────────────────────
# Config (ENV)
# ──────────────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

# xAI / Grok (OpenAI-compatible)
XAI_API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or ""
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-0709").strip()
GROK_MINUTES = int(os.getenv("GROK_MINUTES", "15"))  # aumentado para menos uso y cautela

# Mercado / símbolos
MONEDA_BASE = os.getenv("MONEDA_BASE", "USDC").upper()
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "1000000"))  # aumentado para activos más líquidos
MAX_POSICIONES = int(os.getenv("MAX_POSICIONES", "3"))  # más posiciones para diversificar riesgo
MIN_SALDO_COMPRA = float(os.getenv("MIN_SALDO_COMPRA", "50"))  # aumentado para trades significativos
PORCENTAJE_USDC = float(os.getenv("PORCENTAJE_USDC", "0.3"))  # solo 30% por posición para reducir exposición
ALLOWED_SYMBOLS = [
    s.strip().upper() for s in os.getenv(
        "ALLOWED_SYMBOLS",
        "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,XRPUSDC,ADAUSDC,TONUSDC,LINKUSDC"
    ).split(",") if s.strip()
]  # menos símbolos, enfocados en los más estables y líquidos

# Estrategia (más conservadora: TP alto, SL estricto, trailing protector)
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "2.0")) / 100.0      # +2.0% para ganancias sustanciales
STOP_LOSS = float(os.getenv("STOP_LOSS", "-0.5")) / 100.0         # -0.5% corte rápido de pérdidas
TRAILING_STOP = float(os.getenv("TRAILING_STOP", "0.5")) / 100.0  # -0.5% protege ganancias temprano
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.001"))    # 0.1%
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "40"))               # <40, comprar en sobreventa fuerte
RSI_SELL_MIN = float(os.getenv("RSI_SELL_MIN", "70"))             # >70, vender en sobrecompra
MIN_NET_GAIN_ABS = float(os.getenv("MIN_NET_GAIN_ABS", "1.0"))    # umbral neto alto para cubrir fees + margen

# Ritmo / límites (menos agresivo)
TRADE_COOLDOWN_SEC = int(os.getenv("TRADE_COOLDOWN_SEC", "900"))  # 15 min cooldown
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "4"))  # bajo para menos ops y fees

# Riesgo diario (menos riesgo)
PERDIDA_MAXIMA_DIARIA = float(os.getenv("PERDIDA_MAXIMA_DIARIA", "50"))

# Horarios
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = int(os.getenv("RESUMEN_HORA", "23"))

# Archivos
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# ──────────────────────────────────────────────────────────────────────────────
# Clientes
# ──────────────────────────────────────────────────────────────────────────────
for var, name in [(API_KEY, "BINANCE_API_KEY"), (API_SECRET, "BINANCE_API_SECRET")]:
    if not var:
        raise ValueError(f"Falta variable de entorno: {name}")

client = Client(API_KEY, API_SECRET)

openai_client = None
if XAI_API_KEY and GROK_MODEL:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
        logger.info(f"Grok activado con modelo {GROK_MODEL}")
    except Exception as e:
        logger.warning(f"No se pudo inicializar Grok: {e}; se continúa sin IA.")
        openai_client = None
else:
    logger.info("Grok desactivado (falta XAI_API_KEY/GROK_API_KEY o GROK_MODEL).")

# ──────────────────────────────────────────────────────────────────────────────
# Locks / caches / estados
# ──────────────────────────────────────────────────────────────────────────────
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()
ULTIMA_COMPRA = {}
ULTIMAS_OPERACIONES = []
DUST_THRESHOLD = 0.5

ALL_TICKERS = {}
ALL_TICKERS_TS = 0.0
ALL_TICKERS_TTL = 60  # 1 min para menos llamadas API

KLINES_CACHE = {}
KLINES_TTL = 900  # 15 min para menos refrescos

_LAST_GROK_TS = 0

# ──────────────────────────────────────────────────────────────────────────────
# Backoff para Binance (ban-aware, anti -1003/429/418)
# ──────────────────────────────────────────────────────────────────────────────
def binance_call(fn, *args, tries=6, base_delay=0.8, max_delay=300, **kwargs):
    import re
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            msg = str(e)
            code = getattr(e, "code", None)
            status = getattr(e, "status_code", None)

            # Ban con timestamp exacto
            m = re.search(r"IP banned until\s+(\d{13})", msg)
            if m:
                ban_until_ms = int(m.group(1))
                now_ms = int(time.time() * 1000)
                wait_s = max(0, (ban_until_ms - now_ms) / 1000.0) + 1.0
                logger.warning(f"[BAN] IP baneada hasta {ban_until_ms} (epoch ms). Durmiendo {wait_s:.1f}s.")
                time.sleep(min(wait_s, 900))  # dormir como máx 15 min por ciclo
                attempt = 0
                continue

            # Rate-limit
            if code in (-1003, -1003.0) or status in (418, 429) or "Too many requests" in msg:
                attempt += 1
                if attempt > tries:
                    logger.error(f"Rate limit persistente tras {tries} intentos: {msg}")
                    raise
                # Retry-After si está disponible
                ra = None
                try:
                    h = getattr(e, "response", None)
                    if h is not None:
                        ra = h.headers.get("Retry-After")
                except Exception:
                    pass
                delay = min(max_delay, (float(ra) if ra else (2 ** attempt) * base_delay) + random.random())
                logger.warning(f"[RATE] {msg} -> backoff {delay:.1f}s (intento {attempt}/{tries})")
                time.sleep(delay)
                continue

            # Otros errores transitorios
            attempt += 1
            if attempt <= tries:
                delay = min(max_delay, (1.7 ** attempt) * base_delay + random.random())
                logger.warning(f"[API] {msg} -> reintento en {delay:.1f}s (intento {attempt}/{tries})")
                time.sleep(delay)
                continue
            raise
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt <= tries:
                delay = min(max_delay, (1.7 ** attempt) * base_delay + random.random())
                logger.warning(f"[NET] {e} -> reintento en {delay:.1f}s (intento {attempt}/{tries})")
                time.sleep(delay)
                continue
            raise

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades generales
# ──────────────────────────────────────────────────────────────────────────────
def now_tz(): return datetime.now(TZ_MADRID)
def get_current_date(): return now_tz().date().isoformat()

def cargar_json(file):
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error leyendo {file}: {e}")
    return {}

def atomic_write_json(data, file):
    tmp = file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, file)

def guardar_json(data, file): atomic_write_json(data, file)

def enviar_telegram(mensaje: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM DESACTIVADO] {mensaje}")
        return
    try:
        def _send():
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje[:4000]}
            )
            resp.raise_for_status()
        tries = 0
        while True:
            try:
                _send(); break
            except Exception:
                tries += 1
                if tries >= 3: raise
                time.sleep(min(10, 1.5 ** tries + random.random()))
    except Exception as e:
        logger.error(f"Telegram falló: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# PnL diario / Riesgo
# ──────────────────────────────────────────────────────────────────────────────
def actualizar_pnl_diario(realized_pnl, fees=0.1):
    with LOCK:
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_data[today] = pnl_data.get(today, 0) + float(realized_pnl) - fees
        guardar_json(pnl_data, PNL_DIARIO_FILE)
        return pnl_data[today]

def pnl_hoy():
    pnl = cargar_json(PNL_DIARIO_FILE)
    return pnl.get(get_current_date(), 0)

def puede_comprar():
    hoy = pnl_hoy()
    return hoy > -PERDIDA_MAXIMA_DIARIA if hoy <= 0 else hoy > -(PERDIDA_MAXIMA_DIARIA * 1.5)

def reset_diario():
    with LOCK:
        pnl = cargar_json(PNL_DIARIO_FILE)
        hoy = get_current_date()
        if hoy not in pnl:
            pnl[hoy] = 0
            guardar_json(pnl, PNL_DIARIO_FILE)

# ──────────────────────────────────────────────────────────────────────────────
# Mercado: info símbolos y precisión
# ──────────────────────────────────────────────────────────────────────────────
def dec(x: str) -> Decimal:
    try:
        return Decimal(x)
    except (InvalidOperation, TypeError):
        return Decimal('0')

def load_symbol_info(symbol):
    if symbol in INVALID_SYMBOL_CACHE: return None
    if symbol in SYMBOL_CACHE: return SYMBOL_CACHE[symbol]
    try:
        info = binance_call(client.get_symbol_info, symbol=symbol)
        if info is None:
            INVALID_SYMBOL_CACHE.add(symbol)
            return None
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        market_lot = next((f for f in info['filters'] if f['filterType'] == 'MARKET_LOT_SIZE'), None)
        pricef = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        notional_f = next((f for f in info['filters'] if f['filterType'] in ('NOTIONAL','MIN_NOTIONAL')), None)
        meta = {
            "stepSize": dec(lot.get('stepSize', '0')),
            "minQty": dec(lot.get('minQty', '0')),
            "marketStepSize": dec(market_lot.get('stepSize', '0')) if market_lot else dec(lot.get('stepSize','0')),
            "marketMinQty": dec(market_lot.get('minQty', '0')) if market_lot else dec(lot.get('minQty','0')),
            "tickSize": dec(pricef.get('tickSize', '0')),
            "minNotional": dec(notional_f.get('minNotional', '0')) if notional_f else dec('0'),
            "applyToMarket": bool(notional_f.get('applyToMarket', True)) if notional_f else True,
            "baseAsset": info['baseAsset'],
            "quoteAsset": info['quoteAsset'],
        }
        if meta["marketStepSize"] <= 0 or meta["marketMinQty"] <= 0:
            meta["marketStepSize"] = meta["stepSize"]
            meta["marketMinQty"] = meta["minQty"]
        SYMBOL_CACHE[symbol] = meta
        return meta
    except Exception as e:
        logger.info(f"Error cargando info de {symbol}: {e}")
        INVALID_SYMBOL_CACHE.add(symbol)
        return None

def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0: return qty
    steps = (qty / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * step).normalize()

def quantize_quote(quote: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0: return quote
    steps = (quote / tick).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * tick).normalize()

def min_quote_for_market(symbol) -> Decimal:
    meta = load_symbol_info(symbol)
    if not meta: return Decimal('0')
    return (meta["minNotional"] * Decimal('1.01')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

# ──────────────────────────────────────────────────────────────────────────────
# Cache masiva de tickers
# ──────────────────────────────────────────────────────────────────────────────
def refresh_all_tickers(force=False):
    global ALL_TICKERS, ALL_TICKERS_TS
    now = time.time()
    if not force and (now - ALL_TICKERS_TS) < ALL_TICKERS_TTL:
        return
    data = binance_call(client.get_ticker)  # sin symbol => 24h tickers de todos
    tmp = {}
    for t in data:
        sym = t.get("symbol")
        if sym:
            tmp[sym] = t
    ALL_TICKERS = tmp
    ALL_TICKERS_TS = now

def get_cached_ticker(symbol):
    refresh_all_tickers()
    return ALL_TICKERS.get(symbol)

def safe_get_ticker(symbol): return get_cached_ticker(symbol)

# ──────────────────────────────────────────────────────────────────────────────
# Cache de klines
# ──────────────────────────────────────────────────────────────────────────────
def get_klines_cached(symbol, interval, limit=40, ttl=KLINES_TTL):
    key = (symbol, interval, limit)
    now = time.time()
    data, ts = KLINES_CACHE.get(key, (None, 0))
    if data is not None and (now - ts) < ttl:
        return data
    ks = binance_call(client.get_klines, symbol=symbol, interval=interval, limit=limit)
    KLINES_CACHE[key] = (ks, now)
    return ks

# ──────────────────────────────────────────────────────────────────────────────
# Indicadores
# ──────────────────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed > 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else np.inf
    rsi = 100 - (100 / (1 + rs))
    upvals, downvals = up, down
    for d in deltas[period:]:
        upval = max(d, 0); downval = -min(d, 0)
        upvals = (upvals * (period - 1) + upval) / period
        downvals = (downvals * (period - 1) + downval) / period
        rs = upvals / downvals if downvals != 0 else np.inf
        rsi = 100 - (100 / (1 + rs))
    return float(rsi)

def calculate_ema(closes, period=5):
    if len(closes) < period: return closes[-1]
    ema = float(np.mean(closes[:period]))
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

# ──────────────────────────────────────────────────────────────────────────────
# Grok decisionador (más conservador)
# ──────────────────────────────────────────────────────────────────────────────
def _grok_can_call():
    return openai_client is not None and (time.time() - _LAST_GROK_TS) >= (GROK_MINUTES * 60)

def _grok_decide(kind: str, symbol: str, payload: dict):
    global _LAST_GROK_TS
    try:
        system = "Responde 'si 0.xx [razón breve]' o 'no 0.xx [razón breve]'. Sé prudente y solo permite operaciones si hay alto potencial de ganancia con bajo riesgo y después de considerar comisiones."
        user = f"{kind.upper()} {symbol} datos:{json.dumps(payload, separators=(',',':'))}"
        resp = openai_client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            max_tokens=25,
            temperature=0.2  # baja para decisiones conservadoras
        )
        _LAST_GROK_TS = time.time()
        txt = (resp.choices[0].message.content or "").strip().lower()
        ok = txt.startswith("si") or txt.startswith("sí")
        conf = 0.0
        reason = ""
        parts = txt.split(" ", 2)
        if len(parts) > 1:
            try:
                conf = float(parts[1])
            except:
                pass
        if len(parts) > 2:
            reason = " ".join(parts[2:])
        return (ok, conf, reason)
    except Exception as e:
        logger.info(f"IA no disponible; fallback conservador: {e}")
        return (False, 0.0, "error - assuming no")

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades varias
# ──────────────────────────────────────────────────────────────────────────────
def base_from_symbol(symbol: str) -> str:
    if symbol.endswith(MONEDA_BASE):
        return symbol[:-len(MONEDA_BASE)]
    meta = load_symbol_info(symbol)
    return meta["baseAsset"] if meta else symbol.replace(MONEDA_BASE, "")

def precio_medio_si_hay(symbol, lookback_days=30):
    try:
        since = int((now_tz() - timedelta(days=lookback_days)).timestamp() * 1000)
        trades = binance_call(client.get_my_trades, symbol=symbol, startTime=since)
        buys = [t for t in trades if t.get('isBuyer')]
        if not buys: return None
        qty_sum = Decimal('0'); cost_sum = Decimal('0')
        for t in buys:
            qty = dec(t['qty']); price = dec(t['price']); comm = dec(t.get('commission','0'))
            comm_asset = t.get('commissionAsset','')
            cost_sum += qty * price + (comm if comm_asset == MONEDA_BASE else Decimal('0'))
            qty_sum += qty
        if qty_sum > 0: return float(cost_sum / qty_sum)
    except Exception as e:
        logger.warning(f"No se pudo calcular precio medio {symbol}: {e}")
    return None

def inicializar_registro():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        try:
            cuenta = binance_call(client.get_account)
            for b in cuenta['balances']:
                asset = b['asset']; free = float(b['free'])
                if asset != MONEDA_BASE and free > 0.0000001:
                    symbol = asset + MONEDA_BASE
                    if symbol not in ALLOWED_SYMBOLS: continue
                    if symbol in INVALID_SYMBOL_CACHE: continue
                    if not load_symbol_info(symbol): continue
                    try:
                        t = safe_get_ticker(symbol)
                        if not t: continue
                        precio_actual = float(t['lastPrice'])
                        pm = precio_medio_si_hay(symbol) or precio_actual
                        registro[symbol] = {
                            "cantidad": float(free),
                            "precio_compra": float(pm),
                            "timestamp": now_tz().isoformat(),
                            "high_since_buy": float(precio_actual),
                            "from_cartera": True
                        }
                        logger.info(f"Posición inicial: {symbol} {free} a {pm} (last {precio_actual})")
                    except Exception:
                        continue
            guardar_json(registro, REGISTRO_FILE)
        except BinanceAPIException as e:
            logger.error(f"Error inicializando registro: {e}")

def quantize_amounts_for_market(symbol, quote_to_spend: Decimal):
    meta = load_symbol_info(symbol)
    if not meta: return None, None, None
    min_quote = min_quote_for_market(symbol)
    if quote_to_spend < min_quote:
        return meta, None, min_quote
    q_spend = quantize_quote(quote_to_spend, meta["tickSize"])
    return meta, q_spend, min_quote

def market_sell_with_fallback(symbol: str, qty: Decimal, meta: dict):
    attempts = 0; last_err = None
    q = quantize_qty(qty, meta["marketStepSize"])
    while attempts < 3 and q > Decimal('0'):
        try:
            return binance_call(client.order_market_sell, symbol=symbol, quantity=format(q, 'f'))
        except BinanceAPIException as e:
            last_err = e
            if e.code == -1013:
                q = quantize_qty(q - meta["marketStepSize"], meta["marketStepSize"])
                attempts += 1
                logger.warning(f"{symbol}: ajustando qty por LOT_SIZE, intento {attempts}, qty={q}")
                continue
            raise
    if last_err: raise last_err

def executed_qty_from_order(order_resp) -> float:
    try:
        fills = order_resp.get('fills') or []
        if fills: return sum(float(f.get('qty', 0)) for f in fills if f)
    except Exception: pass
    try:
        executed_quote = float(order_resp.get('cummulativeQuoteQty', 0))
        price = float(order_resp.get('price') or 0) or float(order_resp.get('fills', [{}])[0].get('price', 0) or 0)
        if executed_quote and price: return executed_quote / price
    except Exception: pass
    return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Selección de criptos
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos(max_candidates=25):
    try:
        candidates = []
        refresh_all_tickers()
        for sym in ALLOWED_SYMBOLS:
            if sym in INVALID_SYMBOL_CACHE: continue
            t = ALL_TICKERS.get(sym)
            if not t: continue
            vol = float(t.get("quoteVolume", 0) or 0)
            if vol > MIN_VOLUME: candidates.append(t)

        filtered = []
        top = sorted(candidates, key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)[:max_candidates]
        for t in top:
            symbol = t["symbol"]
            klines = get_klines_cached(symbol, Client.KLINE_INTERVAL_15MINUTE, limit=40)  # intervalo más largo para tendencias estables
            closes = [float(k[4]) for k in klines]
            if len(closes) < 20: continue
            rsi = calculate_rsi(closes)
            ema5 = calculate_ema(closes, 5)
            precio = float(t.get("lastPrice", 0) or 0)
            if precio <= 0 or precio <= ema5: continue  # solo buy si momentum positivo
            ganancia_bruta = precio * TAKE_PROFIT
            com_compra = precio * COMMISSION_RATE
            com_venta = (precio * (1 + TAKE_PROFIT)) * COMMISSION_RATE
            if (ganancia_bruta - (com_compra + com_venta)) < MIN_NET_GAIN_ABS * 2:  # margen extra para fees
                continue
            t['rsi'] = rsi; t['ema5'] = ema5
            filtered.append(t)
        return sorted(filtered, key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
    except BinanceAPIException as e:
        logger.error(f"Error en mejores_criptos: {e}")
        return []

# ──────────────────────────────────────────────────────────────────────────────
# Trading
# ──────────────────────────────────────────────────────────────────────────────
def comprar():
    if not puede_comprar():
        logger.info("Límite de pérdida diaria alcanzado. No se comprará más hoy.")
        return
    try:
        saldo_spot = safe_get_balance(MONEDA_BASE)
        if saldo_spot < MIN_SALDO_COMPRA:
            logger.info("Saldo USDC insuficiente para comprar.")
            return

        cantidad_usdc = saldo_spot * PORCENTAJE_USDC  # solo parte del saldo
        criptos = mejores_criptos()
        registro = cargar_json(REGISTRO_FILE)

        if len(registro) >= MAX_POSICIONES:
            logger.info("Máximo de posiciones abiertas alcanzado. No se comprará más.")
            return

        now_ts = time.time()
        global ULTIMAS_OPERACIONES
        ULTIMAS_OPERACIONES = [t for t in ULTIMAS_OPERACIONES if now_ts - t < 3600]
        if len(ULTIMAS_OPERACIONES) >= MAX_TRADES_PER_HOUR:
            logger.info("Tope de operaciones por hora alcanzado. No se compra en este ciclo.")
            return

        compradas = 0
        for cripto in criptos:
            if compradas >= 1: break
            symbol = cripto["symbol"]
            if symbol in registro: continue
            last = ULTIMA_COMPRA.get(symbol, 0)
            if now_ts - last < TRADE_COOLDOWN_SEC: continue

            t = safe_get_ticker(symbol)
            if not t: continue
            precio = dec(str(t.get("lastPrice", "0")))
            if precio <= 0: continue
            rsi = cripto.get("rsi", 50.0)

            meta, quote_to_spend, min_quote = quantize_amounts_for_market(symbol, dec(str(cantidad_usdc)))
            if quote_to_spend is None:
                logger.info(f"{symbol}: no alcanza minNotional ({float(min_quote):.2f} {MONEDA_BASE}).")
                continue

            base_signal = rsi < RSI_BUY_MAX

            ok_grok, conf, reason = (False, 0.0, "")
            if base_signal:
                payload = {
                    "rsi": round(float(rsi),2),
                    "price": round(float(precio),8),
                    "tp": TAKE_PROFIT,
                    "sl": STOP_LOSS,
                    "vol": float(t.get("quoteVolume",0) or 0),
                    "chg24h": float(t.get("priceChangePercent",0) or 0),
                    "comm_rate": COMMISSION_RATE
                }
                ok_grok, conf, reason = _grok_decide("buy", symbol, payload)

            if base_signal and ok_grok and conf >= 0.7:  # require high confidence
                try:
                    orden = binance_call(
                        client.create_order,
                        symbol=symbol, side="BUY", type="MARKET",
                        quoteOrderQty=format(quote_to_spend, 'f')
                    )
                    executed_qty = executed_qty_from_order(orden)
                    if executed_qty <= 0:
                        executed_qty = float(quote_to_spend) / float(precio)

                    with LOCK:
                        registro = cargar_json(REGISTRO_FILE)
                        registro[symbol] = {
                            "cantidad": executed_qty,
                            "precio_compra": float(precio),
                            "timestamp": now_tz().isoformat(),
                            "high_since_buy": float(precio)
                        }
                        guardar_json(registro, REGISTRO_FILE)

                    enviar_telegram(f"🟢 BUY {symbol} ~{float(quote_to_spend):.2f} {MONEDA_BASE} @ ~{float(precio):.6f} (RSI {rsi:.1f}, IA {conf:.2f} {reason})")
                    compradas += 1
                    ULTIMA_COMPRA[symbol] = now_ts
                    ULTIMAS_OPERACIONES.append(now_ts)
                except BinanceAPIException as e:
                    logger.error(f"Error comprando {symbol}: {e}")
                except Exception as e:
                    logger.error(f"Error inesperado comprando {symbol}: {e}")
            else:
                logger.info(f"No se compra {symbol}: base={base_signal}, IA={conf:.2f} {reason}")
    except Exception as e:
        logger.error(f"Error general en compra: {e}")

def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        nuevos_registro = {}
        dust_positions = []

        for symbol, data in list(registro.items()):
            try:
                precio_compra = dec(str(data["precio_compra"]))
                high_since_buy = dec(str(data.get("high_since_buy", data["precio_compra"])))

                t = safe_get_ticker(symbol)
                if not t:
                    nuevos_registro[symbol] = data; continue
                precio_actual = dec(str(t.get("lastPrice", "0")))
                if precio_actual <= 0:
                    nuevos_registro[symbol] = data; continue

                cambio = (precio_actual - precio_compra) / (precio_compra if precio_compra != 0 else Decimal('1'))

                klines = get_klines_cached(symbol, Client.KLINE_INTERVAL_15MINUTE, limit=40)  # intervalo más largo
                closes = [float(k[4]) for k in klines]
                rsi = calculate_rsi(closes)
                meta = load_symbol_info(symbol)
                if not meta:
                    nuevos_registro[symbol] = data; continue

                if precio_actual > high_since_buy:
                    data["high_since_buy"] = float(precio_actual)
                    guardar_json(registro, REGISTRO_FILE)
                    high_since_buy = precio_actual

                asset = base_from_symbol(symbol)
                cantidad_wallet = dec(str(safe_get_balance(asset)))
                if cantidad_wallet <= 0:
                    dust_positions.append(symbol); continue

                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                    dust_positions.append(symbol); continue

                if meta["applyToMarket"] and meta["minNotional"] > 0 and precio_actual > 0:
                    notional_est = qty * precio_actual
                    if notional_est < meta["minNotional"] or float(notional_est) < DUST_THRESHOLD:
                        dust_positions.append(symbol); continue

                ganancia_bruta = float(qty) * (float(precio_actual) - float(precio_compra))
                com_compra = float(precio_compra) * float(qty) * COMMISSION_RATE
                com_venta = float(precio_actual) * float(qty) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - com_compra - com_venta

                trailing_trigger = (float(precio_actual) - float(high_since_buy)) / float(high_since_buy) <= -TRAILING_STOP
                vender_por_stop = float(cambio) <= STOP_LOSS or trailing_trigger
                vender_por_profit = (float(cambio) >= TAKE_PROFIT or rsi > RSI_SELL_MIN) and ganancia_neta > MIN_NET_GAIN_ABS

                ok_grok, conf, reason = (True, 1.0, "")
                if openai_client is not None and (vender_por_stop or vender_por_profit):
                    payload = {
                        "chg": round(float(cambio),5),
                        "rsi": round(float(rsi),2),
                        "tp": TAKE_PROFIT, "sl": STOP_LOSS,
                        "trail": TRAILING_STOP,
                        "net": round(ganancia_neta,4),
                        "comm_rate": COMMISSION_RATE
                    }
                    ok_grok, conf, reason = _grok_decide("sell", symbol, payload)
                    if vender_por_profit and ganancia_neta < MIN_NET_GAIN_ABS * 1.5 and conf < 0.7:
                        ok_grok = False

                if vender_por_stop or (vender_por_profit and ok_grok):
                    try:
                        orden = market_sell_with_fallback(symbol, qty, meta)
                        total_hoy = actualizar_pnl_diario(ganancia_neta)
                        motivo = "SL/Trailing" if vender_por_stop else "TP/RSI"
                        enviar_telegram(
                            f"🔴 SELL {symbol} {float(qty):.8f} @ ~{float(precio_actual):.6f} "
                            f"(Δ {float(cambio)*100:.2f}%) PnL≈{ganancia_neta:.2f} {MONEDA_BASE}. "
                            f"{motivo}. IA {conf:.2f} {reason}. PnL hoy {total_hoy:.2f}"
                        )
                    except BinanceAPIException as e:
                        logger.error(f"Error vendiendo {symbol}: {e}")
                        dust_positions.append(symbol); continue
                else:
                    nuevos_registro[symbol] = data
                    logger.info(f"No se vende {symbol}: Δ{float(cambio)*100:.2f}%, RSI {rsi:.1f}, net {ganancia_neta:.4f}")
            except Exception as e:
                logger.error(f"Error vendiendo {symbol}: {e}")
                nuevos_registro[symbol] = data

        limpio = {sym: d for sym, d in nuevos_registro.items() if sym not in dust_positions}
        guardar_json(limpio, REGISTRO_FILE)
        if dust_positions:
            enviar_telegram(f"🧹 Limpiado dust: {', '.join(dust_positions)}")

# ──────────────────────────────────────────────────────────────────────────────
# Resumen diario
# ──────────────────────────────────────────────────────────────────────────────
def resumen_diario():
    try:
        cuenta = binance_call(client.get_account)
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy_v = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy_v:.2f} {MONEDA_BASE}\nBalances:\n"
        total_value = 0
        refresh_all_tickers()
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0.001:
                mensaje += f"{b['asset']}: {total:.6f}\n"
                if b['asset'] != MONEDA_BASE:
                    sym = b['asset'] + MONEDA_BASE
                    t = ALL_TICKERS.get(sym)
                    if t:
                        total_value += total * float(t.get('lastPrice', 0) or 0)
                else:
                    total_value += total
        mensaje += f"Valor total estimado: {total_value:.2f} {MONEDA_BASE}"
        enviar_telegram(mensaje)
        seven_days_ago = (now_tz() - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error en resumen diario: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Inicio
# ──────────────────────────────────────────────────────────────────────────────
def safe_get_balance(asset):
    try:
        b = binance_call(client.get_asset_balance, asset=asset)
        if not b: return 0.0
        return float(b.get('free', 0))
    except Exception as e:
        logger.error(f"Error obteniendo balance {asset}: {e}")
        return 0.0

if __name__ == "__main__":
    inicializar_registro()
    enviar_telegram("🤖 Bot IA activo: Modo conservador activado, bajo riesgo, diversificación, focus en net profit post-fees.")

    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', minutes=15, id="comprar")  # menos frecuente
    scheduler.add_job(vender_y_convertir, 'interval', minutes=5, id="vender")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl")
    scheduler.start()
    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
