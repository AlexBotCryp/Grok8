import os, time, json, logging, requests, pytz, numpy as np, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from time import monotonic
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ───────────── CONFIG BÁSICA ─────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-spot")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", "10000"))  # Render Web Service

# ───────────── ESTRATEGIA (agresiva, rotación continua) ─────────────
USD_MIN = float(os.getenv("USD_MIN", "5"))     # órdenes pequeñas para que siempre se mueva
USD_MAX = float(os.getenv("USD_MAX", "8"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.015"))      # 1.5% TP
STOP_LOSS = float(os.getenv("STOP_LOSS", "-0.02"))          # -2% SL
ROTATE_PROFIT = float(os.getenv("ROTATE_PROFIT", "0.006"))  # 0.6%: rotación rápida
TRAIL_ACTIVATE = float(os.getenv("TRAIL_ACTIVATE", "0.007"))# trailing al +0.7%
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "0.005"))          # trailing 0.5%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "30000"))
RESUMEN_HORA_LOCAL = int(os.getenv("RESUMEN_HORA", "23"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "30"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "70"))
RSI_SELL_OVERBOUGHT = float(os.getenv("RSI_SELL_OVERBOUGHT", "68"))

# Nunca pararse: force buy rápido
AGGRESSIVE_MODE = os.getenv("AGGRESSIVE_MODE", "true").lower() == "true"
FORCE_BUY_AFTER_SEC = int(os.getenv("FORCE_BUY_AFTER_SEC", "60"))  # 60s sin compras => fuerza
MAX_BUY_ATTEMPTS_PER_QUOTE = int(os.getenv("MAX_BUY_ATTEMPTS_PER_QUOTE", "12"))

# Quotes preferidas (empezamos con stables para moverse)
PREFERRED_QUOTES = [q.strip().upper() for q in os.getenv(
    "PREFERRED_QUOTES", "USDC,USDT"
).split(",") if q.strip()]

# Comisión de Binance (taker). Intentaremos leer la real por símbolo; fallback a esta:
COMMISSION_DEFAULT = float(os.getenv("COMMISSION_DEFAULT", "0.001"))  # 0.1%
SLIPPAGE_BUFFER_PCT = float(os.getenv("SLIPPAGE_BUFFER_PCT", "0.0005"))  # 0.05% colchón

# LLM (ChatGPT - OpenAI) como filtro permisivo
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_BLOCK_THRESHOLD = float(os.getenv("LLM_BLOCK_THRESHOLD", "10"))  # solo bloquea si <= 10 (muy feo)
LLM_MAX_CALLS_PER_MIN = int(os.getenv("LLM_MAX_CALLS_PER_MIN", "10"))

# Zona horaria
TZ_NAME = os.getenv("TZ", "Europe/Madrid")
TIMEZONE = pytz.timezone(TZ_NAME)

# Stables y blacklist
STABLES = [s.strip().upper() for s in os.getenv(
    "STABLES",
    "USDT,USDC,FDUSD,TUSD,BUSD,DAI,USDP,USTC,EUR,TRY,GBP,BRL,ARS"
).split(",") if s.strip()]
NOT_PERMITTED = set()  # símbolos baneados por -2010

# Archivos
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# ───────────── ESTADO GLOBAL ─────────────
client = None
EX_INFO_READY = False
SYMBOL_MAP = {}  # symbol -> {base, quote, status, filters}

# Caché candidatos
CAND_CACHE_TS = 0.0
CAND_CACHE = []

# Última compra (para force-buy)
LAST_BUY_TS = 0.0
BUY_LOCK = threading.Lock()   # lock anti-solapes

# LLM rate-limit
_llm_window = {"start": 0.0, "count": 0}
_llm_lock = threading.Lock()

# Fee cache
FEE_CACHE = {}  # symbol -> taker fee float

# ───────────── TELEGRAM ─────────────
def telegram_enabled():
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

def enviar_telegram(msg: str):
    if not telegram_enabled():
        logger.debug(f"(Telegram OFF) {msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

# ───────────── HTTP (health) para Render ─────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/health", "/"):
                self.send_response(200); self.end_headers()
                self.wfile.write(b"ok")
            elif self.path == "/selftest":
                status = {
                    "telegram": "ON" if telegram_enabled() else "OFF",
                    "binance_client": "READY" if client else "NOT_INIT",
                    "exchange_info": "READY" if EX_INFO_READY else "LOADING",
                    "positions_file_exists": os.path.exists(REGISTRO_FILE),
                    "pnl_file_exists": os.path.exists(PNL_DIARIO_FILE),
                    "blacklist_size": len(NOT_PERMITTED),
                    "llm": "ON" if (LLM_ENABLED and bool(OPENAI_API_KEY)) else "OFF"
                }
                body = json.dumps(status).encode()
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        except Exception as e:
            logger.warning(f"HealthHandler error: {e}")

def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"HTTP server escuchando en 0.0.0.0:{PORT}")
    server.serve_forever()

# ───────────── UTILIDADES ─────────────
def cargar_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"No se pudo leer {path}: {e}")
    return default

def guardar_json(obj, path):
    try:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except Exception as e:
        logger.warning(f"No se pudo guardar {path}: {e}")

def hoy_str():
    return datetime.now(TIMEZONE).date().isoformat()

def actualizar_pnl_diario(delta):
    pnl = cargar_json(PNL_DIARIO_FILE, {})
    d = hoy_str()
    pnl[d] = pnl.get(d, 0) + float(delta)
    guardar_json(pnl, PNL_DIARIO_FILE)
    return pnl[d]

def backoff_sleep(e: Exception):
    if isinstance(e, BinanceAPIException) and getattr(e, "code", None) == -1003:
        logger.warning("Rate limit alto (-1003). Pausa 120s.")
        time.sleep(120)
    else:
        time.sleep(2)

# ───────────── BINANCE INIT ─────────────
def init_binance_client():
    global client
    if client is None:
        if not (API_KEY and API_SECRET):
            logger.warning("Faltan claves de Binance; el bot no operará hasta que las pongas.")
            return
        try:
            client = Client(API_KEY, API_SECRET)
            client.ping()
            logger.info("Binance client OK.")
        except Exception as e:
            client = None
            logger.warning(f"No se pudo inicializar Binance client: {e}")

def load_exchange_info():
    global EX_INFO_READY, SYMBOL_MAP
    if not client:
        return
    try:
        info = client.get_exchange_info()
        SYMBOL_MAP.clear()
        for s in info["symbols"]:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            SYMBOL_MAP[s["symbol"]] = {
                "base": s["baseAsset"],
                "quote": s["quoteAsset"],
                "status": s["status"],
                "filters": filters,
            }
        EX_INFO_READY = True
        logger.info(f"exchangeInfo cargada: {len(SYMBOL_MAP)} símbolos.")
    except Exception as e:
        EX_INFO_READY = False
        logger.warning(f"Fallo cargando exchangeInfo (reintentará): {e}")
        backoff_sleep(e)

# ───────────── HELPERS MERCADO ─────────────
def symbol_exists(base, quote):
    sym = base + quote
    return sym if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"] == "TRADING" else None

def find_best_route(base_asset, target_quote_list):
    for q in target_quote_list:
        sym = symbol_exists(base_asset, q)
        if sym:
            return sym, q
    return None, None

def get_filter_values(symbol):
    f = SYMBOL_MAP[symbol]["filters"]
    lot = f.get("LOT_SIZE", {})
    price = f.get("PRICE_FILTER", {})
    notional = f.get("MIN_NOTIONAL", {})
    step = Decimal(lot.get("stepSize", "1"))
    tick = Decimal(price.get("tickSize", "1"))
    min_notional = float(notional.get("minNotional", "0"))
    return step, tick, min_notional

def quantize_qty(qty, step: Decimal):
    if step == 0:
        return qty
    d = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(d)

def safe_get_klines(symbol, interval, limit):
    while True:
        try:
            return client.get_klines(symbol=symbol, interval=interval, limit=limit)
        except Exception as e:
            logger.warning(f"Klines error {symbol}: {e}")
            backoff_sleep(e)
            if client is None:
                break

def safe_get_ticker_24h():
    while True:
        try:
            return client.get_ticker()
        except Exception as e:
            logger.warning(f"get_ticker error: {e}")
            backoff_sleep(e)
            if client is None:
                break

def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    arr = np.array(closes, dtype=float)
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period]); avg_loss = np.mean(losses[:period])
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0: return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    return float(rsi)

def obtener_precio(symbol):
    while True:
        try:
            t = client.get_ticker(symbol=symbol)
            return float(t["lastPrice"])
        except Exception as e:
            logger.warning(f"precio error {symbol}: {e}")
            backoff_sleep(e)
            if client is None: break

def min_usd_to_quote_amount(quote: str, usd_amount: float) -> float:
    """Convierte monto en USD a unidades de 'quote' usando precio QUOTEUSDT si existe."""
    if quote in ("USDT","USDC"): 
        return usd_amount
    sym = quote + "USDT"
    if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"] == "TRADING":
        p = obtener_precio(sym)
        if p and p > 0:
            return usd_amount / p
    return usd_amount  # fallback

# ───────────── FEE / COMISIÓN ─────────────
def get_commission_rate(symbol: str) -> float:
    """Intenta leer taker fee real; fallback COMMISSION_DEFAULT."""
    try:
        if symbol in FEE_CACHE:
            return FEE_CACHE[symbol]
        fees = client.get_trade_fee(symbol=symbol)
        # API puede devolver lista; campos suelen venir como str o float
        if isinstance(fees, list) and fees:
            f = fees[0]
            taker = float(f.get("takerCommission", COMMISSION_DEFAULT))
        elif isinstance(fees, dict):
            taker = float(fees.get("takerCommission", COMMISSION_DEFAULT))
        else:
            taker = COMMISSION_DEFAULT
        # Si parece muy pequeña/0, usa default
        if taker <= 0 or taker > 0.01:
            taker = COMMISSION_DEFAULT
        FEE_CACHE[symbol] = taker
        return taker
    except Exception:
        return COMMISSION_DEFAULT

def expected_net_after_fee(buy_price: float, cur_price: float, qty: float, fee_rate: float) -> float:
    """
    Devuelve PnL neto (en moneda quote del símbolo) considerando comisiones de compra y venta.
    net = qty*(cur_price - buy_price) - fee_rate*(buy_price*qty) - fee_rate*(cur_price*qty)
    Además, resta pequeño buffer de slippage.
    """
    if qty <= 0: return 0.0
    gross = qty * (cur_price - buy_price)
    fees = fee_rate * (buy_price * qty) + fee_rate * (cur_price * qty)
    slip = SLIPPAGE_BUFFER_PCT * (cur_price * qty)
    return gross - fees - slip

# ───────────── LLM (ChatGPT) ─────────────
def llm_rate_ok() -> bool:
    if not (LLM_ENABLED and OPENAI_API_KEY and OPENAI_MODEL):
        return False
    now = monotonic()
    with _llm_lock:
        if _llm_window["start"] == 0.0:
            _llm_window["start"] = now
        if now - _llm_window["start"] > 60.0:
            _llm_window["start"] = now
            _llm_window["count"] = 0
        if _llm_window["count"] < LLM_MAX_CALLS_PER_MIN:
            _llm_window["count"] += 1
            return True
        return False

def llm_score_entry(symbol: str, quote: str, price: float, rsi: float, vol_quote: float, trend_hint: str) -> tuple:
    """
    Devuelve (score 0..100, reason).
    Si LLM no disponible o rate-limit, devuelve (50, 'skip').
    """
    if not llm_rate_ok():
        return 50.0, "skip"
    try:
        prompt = (
            "Eres un asistente de trading spot para cripto con horizonte muy corto (scalping/rotación).\n"
            "Evalúa si vale la pena COMPRAR ahora (entrada rápida), buscando salidas con TP bajo o trailing.\n"
            "Evita velas exhaustas y rupturas falsas. Considera RSI y fuerza/volumen.\n"
            f"Par: {symbol} (quote {quote})\n"
            f"Precio: {price:.8f} | RSI(14): {rsi:.1f} | Volumen quote 24h: {vol_quote:.0f}\n"
            f"Tendencia 5m: {trend_hint}\n"
            "Devuelve SOLO JSON: {\"score\": 0-100, \"reason\": \"muy breve\"}"
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 80
        }
        resp = requests.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        try:
            j = json.loads(text)
            score = float(j.get("score", 50))
            reason = str(j.get("reason", "")).strip()[:140]
            return max(0.0, min(100.0, score)), reason
        except Exception:
            import re
            m = re.search(r"(\d{1,3})", text)
            score = float(m.group(1)) if m else 50.0
            return max(0.0, min(100.0, score)), text[:140]
    except Exception as e:
        logger.debug(f"LLM fallo: {e}")
        return 50.0, "error"

# ───────────── SCAN + CACHÉ ─────────────
def scan_candidatos():
    if not (client and EX_INFO_READY):
        return []
    candidatos = []
    tickers = safe_get_ticker_24h()
    if not tickers:
        return []

    # Filtro RELAJADO
    symbols_ok = set()
    for sym, meta in SYMBOL_MAP.items():
        if (meta["status"] == "TRADING"
            and meta["quote"] in PREFERRED_QUOTES
            and meta["base"] not in STABLES
            and sym not in NOT_PERMITTED):
            symbols_ok.add(sym)

    by_quote = {q: [] for q in PREFERRED_QUOTES}
    for t in tickers:
        sym = t["symbol"]
        if sym not in symbols_ok:
            continue
        q = SYMBOL_MAP[sym]["quote"]
        vol = float(t.get("quoteVolume", 0.0))
        if vol >= MIN_QUOTE_VOLUME:
            by_quote[q].append((vol, t))

    reduced = []
    for q, arr in by_quote.items():
        arr.sort(key=lambda x: x[0], reverse=True)
        reduced.extend([t for _, t in arr[:200]])  # top 200 por quote

    for t in reduced:
        sym = t["symbol"]
        try:
            kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 60)
            if not kl: continue
            closes = [float(k[4]) for k in kl]
            if len(closes) < RSI_PERIOD + 2: continue
            rsi = calculate_rsi(closes, RSI_PERIOD)
            if closes[-1] > closes[-2] and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX:
                candidatos.append({
                    "symbol": sym,
                    "quote": SYMBOL_MAP[sym]["quote"],
                    "rsi": rsi,
                    "lastPrice": float(t["lastPrice"]),
                    "quoteVolume": float(t["quoteVolume"])
                })
        except Exception as e:
            logger.debug(f"scan cand error {sym}: {e}")

    candidatos.sort(key=lambda x: (x["quoteVolume"], -abs(x["rsi"] - (RSI_BUY_MIN + RSI_BUY_MAX)/2)), reverse=True)
    return candidatos

def get_candidates_cached():
    global CAND_CACHE_TS, CAND_CACHE
    now = time.time()
    if (now - CAND_CACHE_TS) < 30 and CAND_CACHE:
        return CAND_CACHE
    items = scan_candidatos()
    CAND_CACHE = items
    CAND_CACHE_TS = now
    return items

# ───────────── POSICIONES / BALANCES ─────────────
def leer_posiciones():  return cargar_json(REGISTRO_FILE, {})
def escribir_posiciones(reg): guardar_json(reg, REGISTRO_FILE)

def holdings_por_asset():
    if not client:
        return {}
    try:
        acc = client.get_account()
        res = {}
        for bal in acc.get("balances", []):
            asset = bal.get("asset")
            total = float(bal.get("free", 0)) + float(bal.get("locked", 0))
            if asset and total > 0:
                res[asset] = total
        return res
    except Exception as e:
        logger.warning(f"holdings error: {e}")
        backoff_sleep(e)
        return {}

def free_base_qty(symbol: str) -> float:
    try:
        base = SYMBOL_MAP[symbol]["base"]
        bal = client.get_asset_balance(asset=base) or {}
        return float(bal.get("free", 0.0))
    except Exception:
        return 0.0

# ───────────── COMPRAS ─────────────
def comprar_oportunidad_for_quote(quote, reg, balances):
    """Intenta gastar una orden (5–8 USD por defecto) en la quote indicada para rotación inmediata."""
    if len(reg) >= MAX_OPEN_POSITIONS:
        return False
    disponible = float(balances.get(quote, 0.0))
    if disponible < min(USD_MIN, USD_MAX):
        return False

    cand_all = get_candidates_cached()
    candidatos = [c for c in cand_all if c["quote"] == quote and c["symbol"] not in reg]
    elegido = candidatos[0] if candidatos else None

    if elegido:
        base_asset = SYMBOL_MAP[elegido["symbol"]]["base"]
        if base_asset in STABLES or elegido["symbol"] in NOT_PERMITTED:
            elegido = None

    if not elegido:
        # Fallback liquidez alta con RSI suave
        for base in ["BTC","ETH","SOL","BNB","MATIC","XRP","ADA","TRX","DOGE","LINK","TON","OP","ARB","SUI","APT"]:
            if base in STABLES: continue
            sym = symbol_exists(base, quote)
            if (sym and sym not in reg and sym not in NOT_PERMITTED and SYMBOL_MAP[sym]["status"] == "TRADING"):
                try:
                    kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 30)
                    if not kl: continue
                    closes = [float(k[4]) for k in kl]
                    rsi = calculate_rsi(closes, RSI_PERIOD)
                    if 35 <= rsi <= 70:
                        elegido = {"symbol": sym, "quote": quote, "rsi": rsi, "lastPrice": closes[-1], "quoteVolume": 0}
                        logger.info(f"[ROTATE] Elegido fallback: {sym}")
                        break
                except Exception:
                    continue

    # FORCE BUY: top volumen en quote si aún no hay
    if not elegido and AGGRESSIVE_MODE:
        logger.info(f"[FORCE] Rotación: forzando entrada por top volumen en {quote}")
        tickers = safe_get_ticker_24h() or []
        candidatos_force = []
        for t in tickers:
            sym = t["symbol"]
            if sym in NOT_PERMITTED or sym not in SYMBOL_MAP:
                continue
            meta = SYMBOL_MAP[sym]
            if (meta["status"] == "TRADING"
                and meta["quote"] == quote
                and meta["base"] not in STABLES):
                try:
                    vol = float(t.get("quoteVolume", 0.0))
                    candidatos_force.append((vol, sym))
                except Exception:
                    continue
        candidatos_force.sort(reverse=True)
        if candidatos_force:
            elegido_sym = candidatos_force[0][1]
            elegido = {"symbol": elegido_sym, "quote": quote, "rsi": 50.0, "lastPrice": 0, "quoteVolume": candidatos_force[0][0]}
            logger.info(f"[FORCE] Elegido top volumen (rotación) {quote}: {elegido_sym}")

    if not elegido:
        return False

    symbol = elegido["symbol"]
    price = obtener_precio(symbol)
    if not price:
        return False
    step, _, _ = get_filter_values(symbol)

    # Scoring LLM permisivo (solo bloquea si muy negativo)
    proceed_llm = True
    if LLM_ENABLED and OPENAI_API_KEY:
        trend_hint = "alcista"
        score, reason = llm_score_entry(symbol, quote, price, float(elegido.get("rsi", 50.0)), float(elegido.get("quoteVolume", 0.0)), trend_hint)
        enviar_telegram(f"🧠 LLM {symbol} score={score:.1f} • {reason}")
        logger.info(f"[LLM] {symbol} score={score:.1f} motivo={reason}")
        if score <= LLM_BLOCK_THRESHOLD:
            proceed_llm = False
            logger.info(f"[LLM] BLOQUEA {symbol} por score ≤ {LLM_BLOCK_THRESHOLD}")
    if not proceed_llm:
        return False

    # Dimensionar orden en términos de quote (si quote no es USD)
    orden_en_quote = min_usd_to_quote_amount(quote, min(next_order_size(), disponible))
    qty = quantize_qty(orden_en_quote / price, step)
    if qty <= 0:
        return False

    # Rechazar compra si el TP % no cubre comisiones esperadas (neto > 0)
    fee_rate = get_commission_rate(symbol)
    future_price = price * (1 + TAKE_PROFIT)
    net_tp = expected_net_after_fee(price, future_price, qty, fee_rate)
    if net_tp <= 0:
        # prueba incremental si hay margen
        orden_en_quote = min(max(orden_en_quote * 1.3, min(USD_MIN, USD_MAX)), disponible)
        qty = quantize_qty(orden_en_quote / price, step)
        net_tp = expected_net_after_fee(price, future_price, qty, fee_rate)
        if qty <= 0 or net_tp <= 0:
            logger.info(f"[NET] {symbol}: TP no cubre comisiones (net={net_tp:.6f} {quote}).")
            return False

    # Verificar minNotional
    def min_notional_ok(symbol, price, qty):
        _, _, min_notional = get_filter_values(symbol)
        return (price * qty) >= min_notional * 1.02

    if not min_notional_ok(symbol, price, qty):
        orden_en_quote = min(max(orden_en_quote * 1.3, min(USD_MIN, USD_MAX)), disponible)
        qty = quantize_qty(orden_en_quote / price, step)
        if qty <= 0 or not min_notional_ok(symbol, price, qty):
            logger.info(f"[ROTATE] {symbol}: no alcanza minNotional con saldo disponible.")
            return False

    try:
        orden = client.order_market_buy(symbol=symbol, quantity=qty)
        filled_qty = float(orden.get("executedQty", qty))
        last_price = obtener_precio(symbol) or price

        reg[symbol] = {
            "qty": float(filled_qty),
            "buy_price": float(last_price),
            "peak": float(last_price),
            "quote": quote,
            "ts": datetime.now(TIMEZONE).isoformat()
        }
        escribir_posiciones(reg)
        global LAST_BUY_TS
        LAST_BUY_TS = time.time()
        enviar_telegram(f"🟢 Compra (rotación) {symbol} qty={filled_qty} @ {last_price:.8f} {quote} | fee={fee_rate*100:.2f}%")
        logger.info(f"[ROTATE] Comprado {symbol} (executedQty={filled_qty}).")
        return True
    except BinanceAPIException as e:
        logger.error(f"Compra (rotación) error {symbol}: {e}")
        if getattr(e, "code", None) == -2010:
            NOT_PERMITTED.add(symbol)
            logger.warning(f"Añadido a blacklist: {symbol}")
        backoff_sleep(e)
    except Exception as e:
        logger.error(f"Compra (rotación) error {symbol}: {e}")
        backoff_sleep(e)
    return False

def comprar_oportunidad():
    global LAST_BUY_TS
    if not (client and EX_INFO_READY): return

    if not BUY_LOCK.acquire(blocking=False):
        logger.info("[SKIP] comprar_oportunidad ya está en curso (lock).")
        return
    try:
        reg = leer_posiciones()
        if len(reg) >= MAX_OPEN_POSITIONS: return
        balances = holdings_por_asset()

        try:
            dbg = {q: float(balances.get(q, 0.0)) for q in PREFERRED_QUOTES}
            logger.info(f"[DEBUG] Saldos por quote: {dbg}")
        except Exception:
            pass

        now = time.time()
        tiempo_sin_comprar = now - LAST_BUY_TS if LAST_BUY_TS > 0 else 1e9

        for quote in PREFERRED_QUOTES:
            if len(reg) >= MAX_OPEN_POSITIONS: break
            disponible = float(balances.get(quote, 0.0))
            intentos = 0
            while disponible >= min(USD_MIN, USD_MAX) and len(reg) < MAX_OPEN_POSITIONS:
                intentos += 1
                if intentos > MAX_BUY_ATTEMPTS_PER_QUOTE: break

                cand_all = get_candidates_cached()
                candidatos = [c for c in cand_all if c["quote"] == quote and c["symbol"] not in reg]
                logger.info(f"[DEBUG] {quote}: candidatos RSI={len(candidatos)} (total cache={len(cand_all)})")
                elegido = candidatos[0] if candidatos else None

                if elegido:
                    base_asset = SYMBOL_MAP[elegido["symbol"]]["base"]
                    if base_asset in STABLES or elegido["symbol"] in NOT_PERMITTED:
                        elegido = None

                if not elegido:
                    for base in ["BTC","ETH","SOL","BNB","MATIC","XRP","ADA","TRX","DOGE","LINK","TON","OP","ARB","SUI","APT"]:
                        if base in STABLES: continue
                        sym = symbol_exists(base, quote)
                        if (sym and sym not in reg and sym not in NOT_PERMITTED and SYMBOL_MAP[sym]["status"] == "TRADING"):
                            try:
                                kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 30)
                                if not kl: continue
                                closes = [float(k[4]) for k in kl]
                                rsi = calculate_rsi(closes, RSI_PERIOD)
                                if 35 <= rsi <= 70:
                                    elegido = {"symbol": sym, "quote": quote, "rsi": rsi,
                                               "lastPrice": closes[-1], "quoteVolume": 0}
                                    break
                            except Exception:
                                continue

                if not elegido and AGGRESSIVE_MODE and tiempo_sin_comprar >= FORCE_BUY_AFTER_SEC:
                    logger.info(f"[FORCE] {quote}: {tiempo_sin_comprar:.0f}s sin compras -> Forzando entrada")
                    tickers = safe_get_ticker_24h() or []
                    candidatos_force = []
                    for t in tickers:
                        sym = t["symbol"]
                        if sym in NOT_PERMITTED or sym not in SYMBOL_MAP:
                            continue
                        meta = SYMBOL_MAP[sym]
                        if (meta["status"] == "TRADING"
                            and meta["quote"] == quote
                            and meta["base"] not in STABLES):
                            try:
                                vol = float(t.get("quoteVolume", 0.0))
                                candidatos_force.append((vol, sym))
                            except Exception:
                                continue
                    candidatos_force.sort(reverse=True)
                    if candidatos_force:
                        elegido_sym = candidatos_force[0][1]
                        elegido = {"symbol": elegido_sym, "quote": quote, "rsi": 50.0, "lastPrice": 0, "quoteVolume": candidatos_force[0][0]}
                        logger.info(f"[FORCE] Elegido top volumen {quote}: {elegido_sym}")

                if not elegido:
                    logger.info(f"Sin candidato para {quote}; saldo se mantiene hasta próximo ciclo.")
                    break

                symbol = elegido["symbol"]
                price = obtener_precio(symbol)
                if not price: break
                step, _, _ = get_filter_values(symbol)

                # LLM permisivo
                proceed_llm = True
                if LLM_ENABLED and OPENAI_API_KEY:
                    trend_hint = "alcista" if elegido.get("lastPrice", price) >= price else "mixta"
                    score, reason = llm_score_entry(symbol, quote, price, float(elegido.get("rsi", 50.0)), float(elegido.get("quoteVolume", 0.0)), trend_hint)
                    enviar_telegram(f"🧠 LLM {symbol} score={score:.1f} • {reason}")
                    logger.info(f"[LLM] {symbol} score={score:.1f} motivo={reason}")
                    if score <= LLM_BLOCK_THRESHOLD:
                        proceed_llm = False
                        logger.info(f"[LLM] BLOQUEA {symbol} por score ≤ {LLM_BLOCK_THRESHOLD}")

                if not proceed_llm:
                    if len(candidatos) > 1:
                        candidatos = candidatos[1:]
                        continue
                    else:
                        break

                # Dimensionado por quote
                orden_en_quote = min_usd_to_quote_amount(quote, min(next_order_size(), disponible))
                qty = quantize_qty(orden_en_quote / price, step)
                if qty <= 0:
                    break

                # Compra solo si TP cubre fees (net > 0)
                fee_rate = get_commission_rate(symbol)
                net_tp = expected_net_after_fee(price, price*(1+TAKE_PROFIT), qty, fee_rate)
                if net_tp <= 0:
                    orden_en_quote = min(max(orden_en_quote * 1.3, min(USD_MIN, USD_MAX)), disponible)
                    qty = quantize_qty(orden_en_quote / price, step)
                    net_tp = expected_net_after_fee(price, price*(1+TAKE_PROFIT), qty, fee_rate)
                    if qty <= 0 or net_tp <= 0:
                        logger.info(f"[NET] {symbol}: TP no cubre comisiones (net={net_tp:.6f} {quote}).")
                        break

                # minNotional
                _, _, min_notional = get_filter_values(symbol)
                if (price * qty) < min_notional * 1.02:
                    orden_en_quote = min(max(orden_en_quote * 1.3, min(USD_MIN, USD_MAX)), disponible)
                    qty = quantize_qty(orden_en_quote / price, step)
                    if qty <= 0 or (price*qty) < min_notional * 1.02:
                        logger.info(f"{symbol}: no alcanza minNotional.")
                        break

                try:
                    orden = client.order_market_buy(symbol=symbol, quantity=qty)
                    filled_qty = float(orden.get("executedQty", qty))
                    last_price = obtener_precio(symbol) or price

                    reg[symbol] = {
                        "qty": float(filled_qty),
                        "buy_price": float(last_price),
                        "peak": float(last_price),
                        "quote": quote,
                        "ts": datetime.now(TIMEZONE).isoformat()
                    }
                    escribir_posiciones(reg)
                    LAST_BUY_TS = time.time()
                    enviar_telegram(f"🟢 Compra {symbol} qty={filled_qty} @ {last_price:.8f} {quote} | fee={fee_rate*100:.2f}%")
                    balances = holdings_por_asset()
                    disponible = float(balances.get(quote, 0.0))
                except BinanceAPIException as e:
                    logger.error(f"Compra error {symbol}: {e}")
                    if getattr(e, "code", None) == -2010:
                        NOT_PERMITTED.add(symbol)
                        logger.warning(f"Añadido a blacklist: {symbol}")
                    backoff_sleep(e)
                    break
                except Exception as e:
                    logger.error(f"Compra error {symbol}: {e}")
                    backoff_sleep(e)
                    break
    finally:
        BUY_LOCK.release()

# ───────────── GESTIÓN + ROTACIÓN ─────────────
def gestionar_posiciones():
    if not (client and EX_INFO_READY): return
    reg = leer_posiciones()
    if not reg: return
    nuevos = {}
    for symbol, data in reg.items():
        try:
            qty = float(data["qty"])
            buy_price = float(data["buy_price"])
            peak = float(data.get("peak", buy_price))
            quote = data["quote"]
            if qty <= 0: continue

            price = obtener_precio(symbol)
            if not price:
                nuevos[symbol] = data; continue

            change = (price - buy_price) / buy_price
            peak = max(peak, price)
            trailing_active = (peak - buy_price) / buy_price >= TRAIL_ACTIVATE
            trail_hit = trailing_active and (price <= peak * (1 - TRAIL_PCT))

            # RSI
            kl = safe_get_klines(symbol, Client.KLINE_INTERVAL_5MINUTE, 60)
            if not kl:
                nuevos[symbol] = data; continue
            closes = [float(k[4]) for k in kl]
            rsi = calculate_rsi(closes, RSI_PERIOD)

            # Neto tras fee
            fee_rate = get_commission_rate(symbol)
            net_now = expected_net_after_fee(buy_price, price, qty, fee_rate)

            # Señales de salida (solo vendemos si neto >= 0 o si SL)
            tp = change >= TAKE_PROFIT and net_now >= 0
            rotate = change >= ROTATE_PROFIT and net_now >= 0
            ob = rsi >= RSI_SELL_OVERBOUGHT and net_now >= 0
            sl = change <= STOP_LOSS  # SL ignora net (sal de pérdidas)
            debe_vender = tp or sl or ob or trail_hit or rotate

            if debe_vender:
                step, _, _ = get_filter_values(symbol)
                free_now = free_base_qty(symbol)
                sell_qty = min(qty, free_now) * 0.999
                qty_q = quantize_qty(sell_qty, step)
                if qty_q <= 0:
                    logger.info(f"{symbol}: saldo libre insuficiente para vender (free={free_now:.8f}, reg={qty:.8f})")
                    continue

                # verifica minNotional para venta
                _, _, min_notional = get_filter_values(symbol)
                if (price * qty_q) < min_notional * 1.02:
                    logger.info(f"{symbol}: venta no cumple minNotional (qty={qty_q}).")
                    continue

                client.order_market_sell(symbol=symbol, quantity=qty_q)
                realized = expected_net_after_fee(buy_price, price, qty_q, fee_rate)
                total_pnl = actualizar_pnl_diario(realized)
                motivo = ("TP" if tp else ("SL" if sl else ("RSI" if ob else ("TRAIL" if trail_hit else "ROTATE"))))
                enviar_telegram(
                    f"🔴 Venta {symbol} qty={qty_q} @ {price:.8f} ({change*100:.2f}%) "
                    f"Motivo:{motivo} RSI:{rsi:.1f} | PnL neto:{realized:.4f} {quote} | Hoy:{total_pnl:.4f}"
                )
                # ROTACIÓN inmediata en la misma quote
                balances = holdings_por_asset()
                reg_tmp = leer_posiciones()
                reg_tmp.pop(symbol, None)
                escribir_posiciones(reg_tmp)
                _ = comprar_oportunidad_for_quote(quote, reg_tmp, balances)
            else:
                data["peak"] = float(peak)
                nuevos[symbol] = data

        except BinanceAPIException as e:
            logger.error(f"Gestion error {symbol}: {e}")
            backoff_sleep(e); nuevos[symbol] = data
        except Exception as e:
            logger.error(f"Gestion error {symbol}: {e}")
            backoff_sleep(e); nuevos[symbol] = data
    escribir_posiciones(nuevos)

# ───────────── LIMPIEZA / CONSOLIDACIÓN / RESUMEN ─────────────
def limpiar_dust():
    if not (client and EX_INFO_READY): return
    try:
        reg = leer_posiciones()
        activos_reg = {SYMBOL_MAP[s]["base"] for s in reg.keys() if s in SYMBOL_MAP}
        bals = holdings_por_asset()
        for asset, qty in bals.items():
            if asset in PREFERRED_QUOTES or qty <= 0: continue
            if asset in activos_reg: continue
            if asset in STABLES:  # evitar base estable
                continue
            sym, q = find_best_route(asset, PREFERRED_QUOTES)
            if not sym or sym in NOT_PERMITTED: continue
            if SYMBOL_MAP[sym]["base"] in STABLES: continue
            price = obtener_precio(sym)
            if not price: continue
            step, _, _ = get_filter_values(sym)
            qty_sell = quantize_qty(qty, step)
            if qty_sell <= 0: continue
            _, _, min_notional = get_filter_values(sym)
            if (price * qty_sell) < min_notional * 1.02: continue
            try:
                client.order_market_sell(symbol=sym, quantity=qty_sell)
                enviar_telegram(f"🧹 Limpieza: vendido {qty_sell} {asset} -> {q}")
            except BinanceAPIException as e:
                if getattr(e, "code", None) == -2010:
                    NOT_PERMITTED.add(sym)
                    logger.warning(f"Blacklist por no permitido (limpieza): {sym}")
                else:
                    logger.debug(f"No se pudo limpiar {asset}: {e}")
                backoff_sleep(e)
    except Exception as e:
        logger.debug(f"limpiar_dust error: {e}")

def consolidar_a_quote(target_quote="USDC"):
    if not (client and EX_INFO_READY): return
    try:
        bals = holdings_por_asset()
        for asset, qty in bals.items():
            if qty <= 0: continue
            if asset == target_quote or asset in STABLES:
                continue
            sym = asset + target_quote
            if sym not in SYMBOL_MAP or SYMBOL_MAP[sym]["status"] != "TRADING":
                continue
            price = obtener_precio(sym)
            if not price: continue
            step, _, _ = get_filter_values(sym)
            qty_sell = quantize_qty(qty * 0.999, step)
            if qty_sell <= 0: continue
            _, _, min_notional = get_filter_values(sym)
            if (price * qty_sell) < min_notional * 1.02: 
                continue
            try:
                client.order_market_sell(symbol=sym, quantity=qty_sell)
                enviar_telegram(f"🔄 Consolidación: vendido {qty_sell} {asset} -> {target_quote} via {sym}")
            except BinanceAPIException as e:
                if getattr(e, "code", None) == -2010:
                    NOT_PERMITTED.add(sym)
                else:
                    logger.info(f"No se pudo consolidar {asset} via {sym}: {e}")
                backoff_sleep(e)
    except Exception as e:
        logger.info(f"consolidar_a_quote error: {e}")

def resumen_diario():
    try:
        if client:
            cuenta = client.get_account()
        else:
            cuenta = {"balances": []}
        pnl = cargar_json(PNL_DIARIO_FILE, {})
        d = hoy_str(); pnl_hoy = pnl.get(d, 0.0)
        mensaje = [f"📊 Resumen diario ({d}, {TZ_NAME}):", f"PNL hoy: {pnl_hoy:.4f}", "Balances:"]
        for b in cuenta.get("balances", []):
            total = float(b.get("free",0)) + float(b.get("locked",0))
            if total >= 0.001:
                mensaje.append(f"• {b['asset']}: {total:.6f}")
        enviar_telegram("\n".join(mensaje))
        cutoff = (datetime.now(TIMEZONE) - timedelta(days=14)).date().isoformat()
        pnl2 = {k: v for k, v in pnl.items() if k >= cutoff}
        guardar_json(pnl2, PNL_DIARIO_FILE)
    except Exception as e:
        logger.warning(f"Resumen diario error: {e}")

# ───────────── LOOP / SCHEDULER ─────────────
def init_loop():
    global EX_INFO_READY
    while True:
        if client is None:
            init_binance_client()
        if client and not EX_INFO_READY:
            load_exchange_info()
        time.sleep(10)

def next_order_size():
    import random
    return round(random.uniform(USD_MIN, USD_MAX), 2)

def run_bot():
    enviar_telegram(
        f"🤖 Bot spot activo (rotación continua + fees). Compras cada 20s, gestión cada 10s. "
        f"TP {TAKE_PROFIT*100:.1f}%, ROTATE {ROTATE_PROFIT*100:.1f}%, Trail {TRAIL_ACTIVATE*100:.1f}/{TRAIL_PCT*100:.1f}%. "
        f"Órdenes {USD_MIN}–{USD_MAX}. LLM {'ON' if (LLM_ENABLED and OPENAI_API_KEY) else 'OFF'}."
    )
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(gestionar_posiciones, 'interval', seconds=10, max_instances=1)
    scheduler.add_job(comprar_oportunidad,  'interval', seconds=20, max_instances=2, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(limpiar_dust,         'interval', minutes=5, max_instances=1)
    scheduler.add_job(lambda: consolidar_a_quote("USDC"), 'interval', minutes=2, max_instances=1)
    scheduler.add_job(resumen_diario,       'cron', hour=RESUMEN_HORA_LOCAL, minute=0)
    scheduler.add_job(load_exchange_info,   'interval', minutes=15)
    scheduler.start()

def main():
    threading.Thread(target=init_loop, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    run_http_server()

if __name__ == "__main__":
    main()
