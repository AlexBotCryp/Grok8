# -*- coding: utf-8 -*-
"""
Bot spot con WebSockets (miniticker, bookTicker, kline 5m),
gestión por ATR (SL/TP/Trailing), control de pérdidas diario por %,
1 posición a la vez con 100% del USDC disponible.

Requiere:
  python-binance >= 1.0.17  (funciona con varias variantes de import)
  numpy, pytz, apscheduler, requests
"""
import os, time, json, random, logging, threading, requests
import pytz, numpy as np
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta

from binance.client import Client
from binance.exceptions import BinanceAPIException

# Soporte de distintas firmas del TWM según versión
TWM = None
try:
    from binance.streams import ThreadedWebsocketManager as _TWM
    TWM = _TWM
except Exception:
    try:
        from binance import ThreadedWebsocketManager as _TWM
        TWM = _TWM
    except Exception:
        TWM = None

from apscheduler.schedulers.background import BackgroundScheduler

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia-ws")

# ──────────────────────────────────────────────────────────────────────────────
# Config (ENV)
# ──────────────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

# IA consultiva (opcional)
XAI_API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or ""
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-0709").strip()
GROK_MINUTES = int(os.getenv("GROK_MINUTES", "6"))

# Mercado / símbolos
MONEDA_BASE = os.getenv("MONEDA_BASE", "USDC").upper()
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "100000"))
MAX_POSICIONES = int(os.getenv("MAX_POSICIONES", "1"))
PORCENTAJE_USDC = float(os.getenv("PORCENTAJE_USDC", "1.0"))
ALLOWED_SYMBOLS = [
    s.strip().upper() for s in os.getenv(
        "ALLOWED_SYMBOLS",
        "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,TONUSDC,AVAXUSDC,LINKUSDC,NEARUSDC,ADAUSDC,DOGEUSDC,TRXUSDC,MATICUSDC,OPUSDC,ARBUSDC,ATOMUSDC"
    ).split(",") if s.strip()
]

# Estrategia basada en ATR
ATR_MULT_SL = float(os.getenv("ATR_MULT_SL", "1.5"))
ATR_MULT_TP = float(os.getenv("ATR_MULT_TP", "1.0"))
ATR_TRAIL   = float(os.getenv("ATR_TRAIL",   "1.2"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "40"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "62"))
RSI_SELL_MIN = float(os.getenv("RSI_SELL_MIN","58"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.15"))  # ATR/price*100 mínimo

# Spread máximo (por bookTicker) para entrar/salir
SPREAD_MAX_PCT = float(os.getenv("SPREAD_MAX_PCT", "0.10"))

# Ritmo / límites
TRADE_COOLDOWN_SEC = int(os.getenv("TRADE_COOLDOWN_SEC", "90"))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "18"))
COOLDOWN_POST_STOP_MIN = int(os.getenv("COOLDOWN_POST_STOP_MIN", "30"))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "6"))

# Riesgo diario por equity (%)
PERDIDA_MAXIMA_DIARIA_PCT = float(os.getenv("PERDIDA_MAXIMA_DIARIA_PCT", "3.0"))

# WebSockets
WS_ENABLE = os.getenv("WS_ENABLE", "true").lower() in ("1","true","yes","on")
WS_SYMBOLS_MAX = int(os.getenv("WS_SYMBOLS_MAX", "20"))  # tope de símbolos con kline 5m
WS_ONLY_POSITIONS = os.getenv("WS_ONLY_POSITIONS", "false").lower() in ("1","true","yes","on")

# Horarios
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = int(os.getenv("RESUMEN_HORA", "23"))

# Archivos
REGISTRO_FILE = "registro.json"
STATE_FILE    = "state.json"

# ──────────────────────────────────────────────────────────────────────────────
# Clientes
# ──────────────────────────────────────────────────────────────────────────────
for var, name in [(API_KEY, "BINANCE_API_KEY"), (API_SECRET, "BINANCE_API_SECRET")]:
    if not var:
        raise ValueError(f"Falta variable de entorno: {name}")

client = Client(API_KEY, API_SECRET)

openai_client = None
_LAST_GROK_TS = 0
if XAI_API_KEY and GROK_MODEL:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
        logger.info(f"Grok consultivo activo: {GROK_MODEL}")
    except Exception as e:
        logger.warning(f"No se pudo iniciar Grok: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Locks / caches / estados
# ──────────────────────────────────────────────────────────────────────────────
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()

DUST_THRESHOLD = 0.5
ULTIMAS_OPERACIONES = []
ULTIMA_COMPRA = {}
POST_STOP_BAN = {}

# Caches por WS
PRICE_CACHE = {}         # symbol -> {"last":float,"bid":float,"ask":float,"t":int}
CANDLES_5M = {}          # symbol -> list of kline dicts (close time asc)
K_MAX = 200              # máximo de velas guardadas por símbolo

# REST fallbacks
ALL_TICKERS = {}
ALL_TICKERS_TS = 0.0
ALL_TICKERS_TTL = 45     # más alto: WS es la fuente principal
KLINES_CACHE = {}
KLINES_TTL = 300

# WS manager
twm = None
ws_started = False

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────────────────────────────────────
def now_tz(): return datetime.now(TZ_MADRID)
def today_key(): return now_tz().date().isoformat()

def load_json(file, default=None):
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error leyendo {file}: {e}")
    return {} if default is None else default

def save_json(data, file):
    tmp = file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, file)

def enviar_telegram(mensaje: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TG OFF] {mensaje}")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje[:4000]}
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Telegram falló: {e}")

def dec(x: str) -> Decimal:
    try:
        return Decimal(x)
    except (InvalidOperation, TypeError):
        return Decimal('0')

# ──────────────────────────────────────────────────────────────────────────────
# Binance helpers (backoff)
# ──────────────────────────────────────────────────────────────────────────────
def binance_call(fn, *args, tries=5, base_delay=0.9, max_delay=240, **kwargs):
    import re
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            msg = str(e); code = getattr(e,"code",None); status = getattr(e,"status_code",None)
            m = re.search(r"IP banned until\s+(\d{13})", msg)
            if m:
                ban_until_ms = int(m.group(1))
                now_ms = int(time.time()*1000)
                wait_s = max(0, (ban_until_ms - now_ms)/1000.0) + 2.0
                logger.warning(f"[BAN] durmiendo {wait_s:.1f}s")
                time.sleep(min(wait_s, 900)); attempt = 0; continue
            if code in (-1003,) or status in (418,429) or "Too many requests" in msg:
                attempt += 1
                if attempt > tries: raise
                delay = min(max_delay, (2.0**attempt)*base_delay + random.random())
                logger.warning(f"[RATE] backoff {delay:.1f}s (try {attempt}/{tries})")
                time.sleep(delay); continue
            attempt += 1
            if attempt <= tries:
                delay = min(max_delay, (1.7**attempt)*base_delay + random.random())
                logger.warning(f"[API] reintento {delay:.1f}s (try {attempt}/{tries})")
                time.sleep(delay); continue
            raise
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt <= tries:
                delay = min(max_delay, (1.7**attempt)*base_delay + random.random())
                logger.warning(f"[NET] reintento {delay:.1f}s")
                time.sleep(delay); continue
            raise

# ──────────────────────────────────────────────────────────────────────────────
# Mercado: info símbolos y precisión
# ──────────────────────────────────────────────────────────────────────────────
def load_symbol_info(symbol):
    if symbol in INVALID_SYMBOL_CACHE: return None
    if symbol in SYMBOL_CACHE: return SYMBOL_CACHE[symbol]
    try:
        info = binance_call(client.get_symbol_info, symbol=symbol)
        if info is None:
            INVALID_SYMBOL_CACHE.add(symbol); return None
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        market_lot = next((f for f in info['filters'] if f['filterType'] == 'MARKET_LOT_SIZE'), None)
        pricef = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        notional_f = next((f for f in info['filters'] if f['filterType'] in ('NOTIONAL','MIN_NOTIONAL')), None)
        meta = {
            "stepSize": dec(lot.get('stepSize','0')),
            "minQty": dec(lot.get('minQty','0')),
            "marketStepSize": dec(market_lot.get('stepSize','0')) if market_lot else dec(lot.get('stepSize','0')),
            "marketMinQty": dec(market_lot.get('minQty','0')) if market_lot else dec(lot.get('minQty','0')),
            "tickSize": dec(pricef.get('tickSize','0')),
            "minNotional": dec(notional_f.get('minNotional','0')) if notional_f else dec('0'),
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
        logger.info(f"Error info {symbol}: {e}")
        INVALID_SYMBOL_CACHE.add(symbol); return None

def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0: return qty
    steps = (qty / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * step).normalize()

def min_quote_for_market(symbol) -> Decimal:
    meta = load_symbol_info(symbol)
    if not meta: return Decimal('0')
    return (meta["minNotional"] * Decimal('1.02')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

# ──────────────────────────────────────────────────────────────────────────────
# REST fallbacks para tickers/klines (arranque)
# ──────────────────────────────────────────────────────────────────────────────
def refresh_all_tickers(force=False):
    global ALL_TICKERS, ALL_TICKERS_TS
    now = time.time()
    if not force and (now - ALL_TICKERS_TS) < ALL_TICKERS_TTL: return
    data = binance_call(client.get_ticker)
    ALL_TICKERS = {t.get("symbol"): t for t in data if t.get("symbol")}
    ALL_TICKERS_TS = now

def get_klines_cached(symbol, interval, limit=200, ttl=KLINES_TTL):
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
def ema(arr, period):
    if len(arr) < period: return float(arr[-1])
    m = 2/(period+1.0)
    e = float(np.mean(arr[:period]))
    for x in arr[period:]:
        e = (x - e)*m + e
    return e

def rsi(arr, period=14):
    if len(arr) < period + 1: return 50.0
    deltas = np.diff(arr)
    up = deltas.clip(min=0).sum()/period
    down = -deltas.clip(max=0).sum()/period
    rs = up/(down if down != 0 else 1e-9)
    r = 100 - (100/(1+rs))
    au, ad = up, down
    for d in deltas[period:]:
        upv = max(d,0); dnv = -min(d,0)
        au = (au*(period-1)+upv)/period
        ad = (ad*(period-1)+dnv)/period
        rs = au/(ad if ad!=0 else 1e-9)
        r = 100 - (100/(1+rs))
    return float(r)

def atr_from_ohlc(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        hl = highs[i]-lows[i]
        hc = abs(highs[i]-closes[i-1])
        lc = abs(lows[i]-closes[i-1])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return np.mean(trs) if trs else 0.0
    atr = np.mean(trs[:period])
    for tr in trs[period:]:
        atr = (atr*(period-1)+tr)/period
    return float(atr)

# ──────────────────────────────────────────────────────────────────────────────
# IA consultiva
# ──────────────────────────────────────────────────────────────────────────────
def _grok_can_call():
    return openai_client is not None and (time.time() - _LAST_GROK_TS) >= (GROK_MINUTES*60)

def grok_opinion(kind: str, symbol: str, payload: dict):
    global _LAST_GROK_TS
    if not _grok_can_call(): return ("skip", 0.0, "cooldown")
    try:
        system = "Da consejo corto: 'buy/sell/hold 0.xx [razón breve]'. Máx 15 palabras."
        user = f"{kind.upper()} {symbol} datos:{json.dumps(payload, separators=(',',':'))}"
        resp = openai_client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            max_tokens=24, temperature=0.4
        )
        _LAST_GROK_TS = time.time()
        txt = (resp.choices[0].message.content or "").strip().lower()
        parts = txt.split(" ", 2)
        act, conf, reason = "hold", 0.0, ""
        if parts: act = parts[0]
        if len(parts)>1:
            try: conf = float(parts[1])
            except: pass
        if len(parts)>2: reason = parts[2]
        return (act, conf, reason)
    except Exception as e:
        return ("hold", 0.0, f"ia_err:{e}")

# ──────────────────────────────────────────────────────────────────────────────
# Estado diario / equity
# ──────────────────────────────────────────────────────────────────────────────
def init_day_state():
    st = load_json(STATE_FILE, {})
    tk = today_key()
    if st.get("day") != tk:
        st = {"day": tk, "start_equity": cartera_value_usdc(), "pnl_today": 0.0}
        save_json(st, STATE_FILE)
    return st

def update_pnl_today(delta):
    st = load_json(STATE_FILE, {})
    st.setdefault("day", today_key())
    st["pnl_today"] = round(st.get("pnl_today", 0.0) + float(delta), 8)
    save_json(st, STATE_FILE)
    return st["pnl_today"]

def daily_risk_ok():
    st = init_day_state()
    start_eq = st.get("start_equity", 0.0) or cartera_value_usdc()
    max_loss = start_eq * (PERDIDA_MAXIMA_DIARIA_PCT/100.0)
    return st.get("pnl_today", 0.0) > -max_loss + 1e-6

# ──────────────────────────────────────────────────────────────────────────────
# WS callbacks
# ──────────────────────────────────────────────────────────────────────────────
def _update_price(symbol, last=None, bid=None, ask=None, ts=None):
    d = PRICE_CACHE.get(symbol, {})
    if last is not None: d["last"] = float(last)
    if bid is not None:  d["bid"]  = float(bid)
    if ask is not None:  d["ask"]  = float(ask)
    if ts is not None:   d["t"]    = int(ts)
    PRICE_CACHE[symbol] = d

def on_miniticker(msg):
    # msg: {"e":"24hrMiniTicker","E":..,"s":"BTCUSDT","c":"last","o":"open",...}
    try:
        symbol = msg.get("s")
        if not symbol: return
        _update_price(symbol, last=float(msg.get("c",0)), ts=msg.get("E"))
    except Exception as e:
        logger.debug(f"miniTicker err: {e}")

def on_book_ticker(msg):
    # msg: {"u":..., "s":"BTCUSDC", "b":"bestBid","B":"bidQty","a":"bestAsk","A":"askQty"}
    try:
        symbol = msg.get("s")
        if not symbol: return
        _update_price(symbol, bid=float(msg.get("b",0)), ask=float(msg.get("a",0)))
    except Exception as e:
        logger.debug(f"bookTicker err: {e}")

def on_kline(msg):
    # msg: {"e":"kline","s":"BTCUSDC","k":{"t":..., "T":..., "i":"5m", "o":"..","h":"..","l":"..","c":"..","x":true}}
    try:
        symbol = msg.get("s")
        k = msg.get("k", {})
        if not symbol or not k: return
        if k.get("i") not in ("5m","5min","5m "): return
        close_time = int(k.get("T"))
        o = float(k.get("o")); h = float(k.get("h")); l = float(k.get("l")); c = float(k.get("c"))
        final = bool(k.get("x", False))  # si cerró vela
        arr = CANDLES_5M.get(symbol, [])
        if arr and arr[-1]["T"] == close_time:
            arr[-1] = {"T":close_time,"o":o,"h":h,"l":l,"c":c,"x":final}
        else:
            arr.append({"T":close_time,"o":o,"h":h,"l":l,"c":c,"x":final})
            if len(arr) > K_MAX: arr = arr[-K_MAX:]
        CANDLES_5M[symbol] = arr
        # actualiza last
        _update_price(symbol, last=c, ts=close_time)
    except Exception as e:
        logger.debug(f"kline err: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# WS start/stop
# ──────────────────────────────────────────────────────────────────────────────
def start_streams(symbols):
    global twm, ws_started
    if not WS_ENABLE or TWM is None:
        logger.info("WebSockets desactivado o no disponible; usando REST.")
        return
    if ws_started:
        logger.info("WS ya iniciado.")
        return
    try:
        twm = TWM(api_key=API_KEY, api_secret=API_SECRET)
        twm.start()
        # miniTicker para TODOS (siempre uno solo)
        try:
            # all-market mini ticker (si está en tu versión)
            twm.start_miniticker_socket(callback=on_miniticker)
            logger.info("miniTicker global iniciado.")
        except Exception:
            # si no existe, por símbolo
            for s in symbols:
                twm.start_symbol_miniticker_socket(callback=on_miniticker, symbol=s)
            logger.info("miniTicker por símbolo iniciado.")

        # bookTicker para mejores spreads (global si posible)
        try:
            twm.start_book_ticker_socket(callback=on_book_ticker)
            logger.info("bookTicker global iniciado.")
        except Exception:
            for s in symbols:
                twm.start_book_ticker_socket(callback=on_book_ticker, symbol=s)
            logger.info("bookTicker por símbolo iniciado.")

        # kline 5m: según preferencia (solo cartera o lista limitada)
        k_symbols = set(symbols)
        if WS_ONLY_POSITIONS:
            reg = registro_load()
            k_symbols = set(reg.keys()) if reg else set()
            if not k_symbols:
                # arrancar al menos BTC/ETH para “calentar” buffers
                k_symbols = set([x for x in symbols if x in ("BTCUSDC","ETHUSDC")])
        else:
            if len(k_symbols) > WS_SYMBOLS_MAX:
                k_symbols = set(list(symbols)[:WS_SYMBOLS_MAX])

        for s in k_symbols:
            twm.start_kline_socket(callback=on_kline, symbol=s, interval=Client.KLINE_INTERVAL_5MINUTE)
        logger.info(f"kline 5m iniciado para: {', '.join(sorted(k_symbols))}")
        ws_started = True
    except Exception as e:
        logger.warning(f"No se pudieron iniciar WS: {e}")

def stop_streams():
    global twm, ws_started
    try:
        if twm:
            twm.stop()
        ws_started = False
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# Valoración de cartera con cache
# ──────────────────────────────────────────────────────────────────────────────
def cartera_value_usdc():
    try:
        acct = binance_call(client.get_account)
        total = 0.0
        for b in acct['balances']:
            asset = b['asset']
            qty = float(b['free']) + float(b['locked'])
            if qty <= 0: continue
            if asset == MONEDA_BASE:
                total += qty
            else:
                sym = asset + MONEDA_BASE
                px = PRICE_CACHE.get(sym, {}).get("last")
                if px is None:
                    refresh_all_tickers()
                    t = ALL_TICKERS.get(sym)
                    px = float(t.get('lastPrice',0) or 0) if t else 0
                total += qty*float(px or 0)
        return total
    except Exception:
        return 0.0

def safe_get_balance(asset):
    try:
        b = binance_call(client.get_asset_balance, asset=asset)
        if not b: return 0.0
        return float(b.get('free', 0))
    except Exception as e:
        logger.error(f"Error balance {asset}: {e}")
        return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Registro posiciones
# ──────────────────────────────────────────────────────────────────────────────
def registro_load(): return load_json(REGISTRO_FILE, {})
def registro_save(r):  save_json(r, REGISTRO_FILE)

def inicializar_registro():
    with LOCK:
        reg = registro_load()
        acct = binance_call(client.get_account)
        # precalienta precios vía REST la primera vez
        refresh_all_tickers(force=True)
        for b in acct['balances']:
            asset = b['asset']; free = float(b['free'])
            if asset != MONEDA_BASE and free > 1e-8:
                sym = asset + MONEDA_BASE
                if sym in INVALID_SYMBOL_CACHE: continue
                if not load_symbol_info(sym): continue
                last = PRICE_CACHE.get(sym, {}).get("last")
                if last is None:
                    t = ALL_TICKERS.get(sym)
                    last = float(t.get("lastPrice",0) or 0) if t else 0
                if last <= 0: continue
                if sym not in reg:
                    reg[sym] = {
                        "cantidad": free,
                        "precio_compra": last,
                        "timestamp": now_tz().isoformat(),
                        "high_since_buy": last,
                        "sl": None, "tp": None, "be": None
                    }
                    logger.info(f"Detectada posición inicial: {sym} {free} @ ~{last}")
        registro_save(reg)

# ──────────────────────────────────────────────────────────────────────────────
# Señales con datos WS (o REST si no hay cache)
# ──────────────────────────────────────────────────────────────────────────────
def get_indicators(symbol):
    """Devuelve dict con {price, ema50, ema200, rsi14, atr, atr_pct, spread_pct} usando cache WS si es posible."""
    # precio y spread
    pr = PRICE_CACHE.get(symbol, {})
    last = pr.get("last")
    bid, ask = pr.get("bid"), pr.get("ask")
    spread_pct = None
    if bid and ask and ask > 0:
        spread_pct = (ask - bid) / ask * 100.0

    # velas 5m
    k = CANDLES_5M.get(symbol)
    if not k or len(k) < 60 or not k[-1].get("x", True):
        # fallback REST si no hay suficientes velas finalizadas
        ks = get_klines_cached(symbol, Client.KLINE_INTERVAL_5MINUTE, limit=200)
        closes = np.array([float(x[4]) for x in ks], dtype=float)
        highs  = np.array([float(x[2]) for x in ks], dtype=float)
        lows   = np.array([float(x[3]) for x in ks], dtype=float)
        price = float(closes[-1])
    else:
        closes = np.array([x["c"] for x in k if x.get("x", True)], dtype=float)
        highs  = np.array([x["h"] for x in k if x.get("x", True)], dtype=float)
        lows   = np.array([x["l"] for x in k if x.get("x", True)], dtype=float)
        price = float(closes[-1])

    ema50 = ema(closes, 50)
    ema200= ema(closes, 200)
    r = rsi(closes, 14)
    atr = atr_from_ohlc(highs, lows, closes, 14)
    atr_pct = (atr/price)*100.0 if price>0 else 0.0
    if last is None: last = price
    return {
        "price": float(last),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "rsi": float(r),
        "atr": float(atr),
        "atr_pct": float(atr_pct),
        "spread_pct": float(spread_pct) if spread_pct is not None else None
    }

# ──────────────────────────────────────────────────────────────────────────────
# Selección y entradas
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos(max_candidates=20):
    # usa precios del WS si existen; si no, REST
    vols = []
    try:
        # en ausencia de WS de volumen en vivo, usamos 24h (REST)
        refresh_all_tickers()
        for sym in ALLOWED_SYMBOLS:
            t = ALL_TICKERS.get(sym)
            if not t: continue
            vol = float(t.get("quoteVolume",0) or 0)
            last = PRICE_CACHE.get(sym, {}).get("last")
            if last is None:
                last = float(t.get("lastPrice",0) or 0)
            if vol >= MIN_VOLUME and last > 0 and last >= 0.00002:
                vols.append((sym, vol))
    except Exception:
        pass
    vols.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in vols[:max_candidates]]

def cooling_off(symbol):
    until = POST_STOP_BAN.get(symbol, 0)
    return time.time() < until

def comprar():
    if not daily_risk_ok():
        logger.info("Límite de pérdida diaria alcanzado.")
        return
    reg = registro_load()
    if len(reg) >= MAX_POSICIONES:
        logger.info("Máximo de posiciones abiertas.")
        return

    usdc = safe_get_balance(MONEDA_BASE)
    if usdc <= 5:
        logger.info("USDC insuficiente.")
        return

    now_ts = time.time()
    global ULTIMAS_OPERACIONES
    ULTIMAS_OPERACIONES = [t for t in ULTIMAS_OPERACIONES if now_ts - t < 3600]
    if len(ULTIMAS_OPERACIONES) >= MAX_TRADES_PER_HOUR:
        logger.info("Límite de trades/hora.")
        return

    for symbol in mejores_criptos():
        if symbol in reg or cooling_off(symbol): continue
        last_buy = ULTIMA_COMPRA.get(symbol, 0)
        if now_ts - last_buy < TRADE_COOLDOWN_SEC: continue

        try:
            ind = get_indicators(symbol)
            price, ema50, ema200 = ind["price"], ind["ema50"], ind["ema200"]
            r, atr, atr_pct, spread = ind["rsi"], ind["atr"], ind["atr_pct"], ind["spread_pct"]

            if not (ema50 > ema200 and price > ema50):  # tendencia
                continue
            if not (RSI_BUY_MIN <= r <= RSI_BUY_MAX):
                continue
            if atr_pct < MIN_ATR_PCT:
                continue
            if spread is not None and spread > SPREAD_MAX_PCT:
                continue

            meta = load_symbol_info(symbol)
            if not meta: continue
            min_notional = float(min_quote_for_market(symbol))
            quote_to_spend = max(min_notional, usdc*PORCENTAJE_USDC)

            order = binance_call(
                client.create_order,
                symbol=symbol, side="BUY", type="MARKET",
                quoteOrderQty=f"{quote_to_spend:.8f}"
            )
            # precio de ejecución
            fills = order.get('fills') or []
            exec_price = price
            if fills:
                q = sum(float(f['qty']) for f in fills)
                c = sum(float(f['price'])*float(f['qty']) for f in fills)
                exec_price = c/q if q>0 else price
            qty = sum(float(f['qty']) for f in fills) if fills else float(quote_to_spend/price)

            sl = exec_price - ATR_MULT_SL*atr
            tp = exec_price + ATR_MULT_TP*atr

            with LOCK:
                reg = registro_load()
                reg[symbol] = {
                    "cantidad": qty,
                    "precio_compra": float(exec_price),
                    "timestamp": now_tz().isoformat(),
                    "high_since_buy": float(exec_price),
                    "sl": float(sl), "tp": float(tp), "be": None
                }
                registro_save(reg)

            act, conf, reason = ("skip",0,"")
            if openai_client:
                act, conf, reason = grok_opinion("buy", symbol, {"rsi":round(r,1),"atr_pct":round(atr_pct,3),"spread":spread})

            enviar_telegram(f"🟢 BUY {symbol} ~{quote_to_spend:.2f} {MONEDA_BASE} @ {exec_price:.6f} | RSI {r:.1f} ATR% {atr_pct:.2f} Spread {spread if spread is not None else '-'}% | IA {act} {conf:.2f} {reason}")
            ULTIMA_COMPRA[symbol] = now_ts
            ULTIMAS_OPERACIONES.append(now_ts)
            break  # solo 1 compra por ciclo
        except BinanceAPIException as e:
            logger.error(f"Error comprando {symbol}: {e}")
        except Exception as e:
            logger.error(f"Error compra {symbol}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Gestión de salidas
# ──────────────────────────────────────────────────────────────────────────────
def market_sell(symbol, qty, meta):
    q = quantize_qty(Decimal(str(qty)), meta["marketStepSize"])
    if q <= 0: return None
    return binance_call(client.order_market_sell, symbol=symbol, quantity=f"{q:.12f}")

def gestionar_posiciones():
    with LOCK:
        reg = registro_load()
    if not reg: return

    nuevos = {}
    to_clean = []

    for symbol, data in reg.items():
        try:
            ind = get_indicators(symbol)
            price, r, atr = ind["price"], ind["rsi"], ind["atr"]
            spread = ind["spread_pct"]

            if price <= 0:
                nuevos[symbol] = data; continue
            if spread is not None and spread > SPREAD_MAX_PCT:
                # si spread alto, evitamos salir con mala ejecución salvo SL
                pass

            qty = float(data.get("cantidad", 0))
            if qty <= 0:
                to_clean.append(symbol); continue

            entry = float(data["precio_compra"])
            high  = float(data.get("high_since_buy", entry))
            sl = data.get("sl"); tp = data.get("tp"); be = data.get("be")

            # actualiza high
            if price > high:
                high = price

            # activa BE al alcanzar +0.75R
            if be is None and price >= (entry + 0.75*ATR_MULT_TP*atr):
                be = max(entry, entry + 0.05*atr)

            # trailing
            trail = max((high - ATR_TRAIL*atr), (be if be else -1e9))

            # condiciones
            hit_sl = price <= (sl if sl is not None else entry - ATR_MULT_SL*atr)
            hit_tp = price >= (tp if tp is not None else entry + ATR_MULT_TP*atr)
            hit_trail = price <= trail

            # tiempo máximo
            ts_open = datetime.fromisoformat(data["timestamp"])
            open_secs = (now_tz() - ts_open.replace(tzinfo=TZ_MADRID)).total_seconds()
            too_long = open_secs >= MAX_HOLD_HOURS*3600

            # RSI over para tomar beneficios
            take_rsi = (r >= RSI_SELL_MIN) and price > entry

            reason = None
            if hit_sl or hit_trail or too_long:
                reason = "SL" if hit_sl else ("TRAIL" if hit_trail else "TIME")
            elif hit_tp or take_rsi:
                reason = "TP/RSI"

            if reason:
                meta = load_symbol_info(symbol)
                if not meta:
                    nuevos[symbol] = data; continue
                before_val = qty*price
                order = market_sell(symbol, qty, meta)
                if order is None:
                    nuevos[symbol] = data; continue

                fills = order.get('fills') or []
                exec_p = price
                if fills:
                    qf = sum(float(f['qty']) for f in fills)
                    cf = sum(float(f['price'])*float(f['qty']) for f in fills)
                    exec_p = cf/qf if qf>0 else price

                pnl = (exec_p - entry)*qty
                total = update_pnl_today(pnl)
                enviar_telegram(f"🔴 SELL {symbol} {qty:.8f} @ {exec_p:.6f} | motivo {reason} | PnL {pnl:.2f} {MONEDA_BASE} | PnL hoy {total:.2f}")

                if reason in ("SL","TRAIL"):
                    POST_STOP_BAN[symbol] = time.time() + COOLDOWN_POST_STOP_MIN*60
                to_clean.append(symbol)
            else:
                data["high_since_buy"] = high
                data["sl"] = float(entry - ATR_MULT_SL*atr) if sl is None else float(sl)
                data["tp"] = float(entry + ATR_MULT_TP*atr) if tp is None else float(tp)
                data["be"] = be
                nuevos[symbol] = data
        except Exception as e:
            logger.error(f"Gestión {symbol} error: {e}")
            nuevos[symbol] = data

    for s in to_clean:
        if s in nuevos: del nuevos[s]
    with LOCK:
        registro_save(nuevos)

# ──────────────────────────────────────────────────────────────────────────────
# Resumen diario
# ──────────────────────────────────────────────────────────────────────────────
def resumen_diario():
    try:
        st = init_day_state()
        total_value = cartera_value_usdc()
        pnl_hoy = st.get("pnl_today", 0.0)
        enviar_telegram(f"📊 Resumen ({today_key()}): PNL hoy {pnl_hoy:.2f} {MONEDA_BASE} | Equity {total_value:.2f} | Inicio {st.get('start_equity',0):.2f}")
    except Exception as e:
        logger.error(f"Resumen diario error: {e}")

def reset_diario():
    init_day_state()

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_day_state()
    inicializar_registro()
    # Inicia WS (miniTicker/bookTicker siempre; kline 5m según flags)
    start_streams(ALLOWED_SYMBOLS)

    enviar_telegram("🤖 Bot IA WS activo: 5m tendencia + ATR, trailing dinámico, 100% USDC, control diario %, spread-check, WS en vivo.")

    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', minutes=4, id="comprar")
    scheduler.add_job(gestionar_posiciones, 'interval', minutes=1, id="gestionar")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=3, id="reset")
    scheduler.start()

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        try:
            scheduler.shutdown()
        except Exception:
            pass
        stop_streams()
        logger.info("Bot detenido.")
