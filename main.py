import os, time, json, logging, requests, pytz, numpy as np, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from time import monotonic
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ───────── CONFIGURACIÓN ─────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-spot")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", "10000"))  # Render health port

# Estrategia
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.015"))      # 1.5%
STOP_LOSS = float(os.getenv("STOP_LOSS", "-0.02"))          # -2.0%
ROTATE_PROFIT = float(os.getenv("ROTATE_PROFIT", "0.006"))  # 0.6% rotación rápida
TRAIL_ACTIVATE = float(os.getenv("TRAIL_ACTIVATE", "0.007"))# activa trailing a +0.7%
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "0.005"))          # trailing 0.5%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "6"))

MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "30000"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "30"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "70"))
RSI_SELL_OVERBOUGHT = float(os.getenv("RSI_SELL_OVERBOUGHT", "68"))

# Mínimo por orden en euros + porcentaje de saldo a usar (1.0 = 100%)
MIN_EUR_ORDER = float(os.getenv("MIN_EUR_ORDER", "20"))
FULL_BALANCE_SPEND_FRACTION = float(os.getenv("FULL_BALANCE_SPEND_FRACTION", "1.0"))

# Quotes preferidas (pueden ser stables o cripto para rotación directa)
PREFERRED_QUOTES = [q.strip().upper() for q in os.getenv(
    "PREFERRED_QUOTES", "USDC,USDT,BTC,ETH,BNB"
).split(",") if q.strip()]

# Comisión / slippage
COMMISSION_DEFAULT = float(os.getenv("COMMISSION_DEFAULT", "0.001"))  # 0.1%
SLIPPAGE_BUFFER_PCT = float(os.getenv("SLIPPAGE_BUFFER_PCT", "0.0005"))  # 0.05%

# Filtros (NOTIONAL/minQty) buffers
NOTIONAL_BUFFER = float(os.getenv("NOTIONAL_BUFFER", "1.03"))  # +3% margen
MIN_QTY_BUFFER  = float(os.getenv("MIN_QTY_BUFFER", "1.0"))    # 1.0 = igual al minQty

# Para que no se pare
AGGRESSIVE_MODE = os.getenv("AGGRESSIVE_MODE", "true").lower() == "true"
FORCE_BUY_AFTER_SEC = int(os.getenv("FORCE_BUY_AFTER_SEC", "60"))
MAX_BUY_ATTEMPTS_PER_QUOTE = int(os.getenv("MAX_BUY_ATTEMPTS_PER_QUOTE", "12"))

# LLM (opcional) con diagnóstico
LLM_ENABLED = os.getenv("LLM_ENABLED", "false").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_BLOCK_THRESHOLD = float(os.getenv("LLM_BLOCK_THRESHOLD", "10"))
LLM_MAX_CALLS_PER_MIN = int(os.getenv("LLM_MAX_CALLS_PER_MIN", "10"))
_llm_window = {"start": 0.0, "count": 0}
_llm_lock = threading.Lock()

# Zona horaria
TZ_NAME = os.getenv("TZ", "Europe/Madrid")
TIMEZONE = pytz.timezone(TZ_NAME)

# Otros
STABLES = [s.strip().upper() for s in os.getenv(
    "STABLES",
    "USDT,USDC,FDUSD,TUSD,BUSD,DAI,USDP,USTC,EUR,TRY,GBP,BRL,ARS"
).split(",") if s.strip()]
NOT_PERMITTED = set()

REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# Estado
client = None
EX_INFO_READY = False
SYMBOL_MAP = {}
FEE_CACHE = {}
CAND_CACHE_TS = 0.0
CAND_CACHE = []
LAST_BUY_TS = 0.0
BUY_LOCK = threading.Lock()

# ───────── Telegram ─────────
def enviar_telegram(msg: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

# ───────── HTTP Health ─────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/health", "/"):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
            if self.path == "/llmtest":
                if not (LLM_ENABLED and OPENAI_API_KEY and OPENAI_MODEL):
                    self.send_response(200); self.end_headers(); self.wfile.write(b"llm_off"); return
                score, reason = llm_score_entry("BTCUSDT","USDT", 65000.0, 50.0, 1_000_000_000.0, "mixta")
                out = json.dumps({"score": score, "reason": reason}).encode()
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(out); return
            self.send_response(404); self.end_headers()
        except Exception as e:
            logger.error(f"Health error: {e}")
            self.send_response(500); self.end_headers()

def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"HTTP server escuchando en 0.0.0.0:{PORT}")
    server.serve_forever()

# ───────── Utilidades ─────────
def cargar_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        return default
    return default

def guardar_json(obj, path):
    try:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except Exception:
        pass

def hoy_str(): return datetime.now(TIMEZONE).date().isoformat()

def actualizar_pnl_diario(delta):
    pnl = cargar_json(PNL_DIARIO_FILE, {})
    d = hoy_str()
    pnl[d] = pnl.get(d, 0) + float(delta)
    guardar_json(pnl, PNL_DIARIO_FILE)
    return pnl[d]

def backoff_sleep(e): time.sleep(2)

# ───────── Binance ─────────
def init_binance_client():
    global client
    if client: return
    if not (API_KEY and API_SECRET):
        logger.error("Faltan BINANCE_API_KEY/BINANCE_API_SECRET"); return
    client = Client(API_KEY, API_SECRET)
    client.ping()
    logger.info("Binance client OK.")

def load_exchange_info():
    global EX_INFO_READY, SYMBOL_MAP
    info = client.get_exchange_info()
    SYMBOL_MAP.clear()
    for s in info["symbols"]:
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        SYMBOL_MAP[s["symbol"]] = {
            "base": s["baseAsset"], "quote": s["quoteAsset"],
            "status": s["status"], "filters": filters
        }
    EX_INFO_READY = True
    logger.info(f"exchangeInfo cargada: {len(SYMBOL_MAP)} símbolos.")

# ───────── Mercado helpers ─────────
def get_filter_values(symbol):
    f = SYMBOL_MAP[symbol]["filters"]
    lot   = f.get("LOT_SIZE", {})
    price = f.get("PRICE_FILTER", {})
    if "MIN_NOTIONAL" in f:
        min_notional = float(f["MIN_NOTIONAL"].get("minNotional", "0"))
    elif "NOTIONAL" in f:
        min_notional = float(f["NOTIONAL"].get("minNotional", "0"))
    else:
        min_notional = 0.0
    step    = Decimal(lot.get("stepSize", "1"))
    min_qty = float(lot.get("minQty", "0"))
    tick    = Decimal(price.get("tickSize", "1"))
    return step, tick, min_notional, min_qty

def step_decimals(step: Decimal) -> int:
    s = format(step, 'f')
    if '.' in s: return len(s.split('.')[1].rstrip('0'))
    return 0

def format_qty(symbol: str, qty_float: float) -> str:
    """Cuantiza por stepSize y devuelve string con decimales exactos (evita -1100)."""
    step, _, _, _ = get_filter_values(symbol)
    try:
        q = (Decimal(str(qty_float)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    except InvalidOperation:
        q = Decimal(0)
    decs = step_decimals(step)
    s = f"{q:.{decs}f}"
    if '.' in s:
        s = s.rstrip('0')
        if s.endswith('.'):
            s = s + '0' * max(1, decs)
    return s

def obtener_precio(symbol):
    t = client.get_ticker(symbol=symbol)
    return float(t["lastPrice"])

def safe_get_klines(symbol, interval, limit):
    return client.get_klines(symbol=symbol, interval=interval, limit=limit)

def calculate_rsi(closes, period=14):
    if len(closes) <= period: return 50.0
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

def min_usd_to_quote_amount(quote: str, usd_amount: float) -> float:
    if quote in ("USDT","USDC"): return usd_amount
    sym = quote + "USDT"
    if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"] == "TRADING":
        p = obtener_precio(sym)
        if p and p > 0: return usd_amount / p
    return usd_amount

def min_eur_to_quote_amount(quote: str, eur_amount: float) -> float:
    """
    EUR -> unidades de la quote (USDT/USDC/BTC/ETH/BNB...).
    Usa EURUSDT / EURUSDC como referencia y QUOTEUSDT/QUOTEUSDC para otras quotes.
    Si no hay precios válidos, devuelve 0 para forzar SKIP.
    """
    refs = []
    if "EURUSDT" in SYMBOL_MAP and SYMBOL_MAP["EURUSDT"]["status"] == "TRADING":
        try: refs.append(obtener_precio("EURUSDT"))
        except Exception: pass
    if "EURUSDC" in SYMBOL_MAP and SYMBOL_MAP["EURUSDC"]["status"] == "TRADING":
        try: refs.append(obtener_precio("EURUSDC"))
        except Exception: pass
    eur_usd = max([p for p in refs if p and p > 0], default=0.0)
    if eur_usd <= 0: return 0.0

    if quote == "EUR": return eur_amount
    if quote in ("USDT","USDC"): return eur_amount * eur_usd

    for base in ("USDT","USDC"):
        sym = quote + base
        if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"] == "TRADING":
            try:
                q_usd = obtener_precio(sym)
                if q_usd and q_usd > 0:
                    return (eur_amount * eur_usd) / q_usd
            except Exception:
                continue
    return 0.0

# ───────── Fees / PnL neto ─────────
def get_commission_rate(symbol: str) -> float:
    try:
        if symbol in FEE_CACHE: return FEE_CACHE[symbol]
        fees = client.get_trade_fee(symbol=symbol)
        if isinstance(fees, list) and fees:
            taker = float(fees[0].get("takerCommission", COMMISSION_DEFAULT))
        elif isinstance(fees, dict):
            taker = float(fees.get("takerCommission", COMMISSION_DEFAULT))
        else:
            taker = COMMISSION_DEFAULT
        if taker <= 0 or taker > 0.01: taker = COMMISSION_DEFAULT
        FEE_CACHE[symbol] = taker
        return taker
    except Exception:
        return COMMISSION_DEFAULT

def expected_net_after_fee(buy_price: float, cur_price: float, qty: float, fee_rate: float) -> float:
    gross = qty * (cur_price - buy_price)
    fees  = fee_rate * (buy_price * qty) + fee_rate * (cur_price * qty)
    slip  = SLIPPAGE_BUFFER_PCT * (cur_price * qty)
    return gross - fees - slip

# ───────── LLM (ChatGPT) con diagnóstico ─────────
def llm_rate_ok() -> bool:
    if not (LLM_ENABLED and OPENAI_API_KEY and OPENAI_MODEL): return False
    now = monotonic()
    with _llm_lock:
        if _llm_window["start"] == 0.0: _llm_window["start"] = now
        if now - _llm_window["start"] > 60.0:
            _llm_window["start"] = now; _llm_window["count"] = 0
        if _llm_window["count"] < LLM_MAX_CALLS_PER_MIN:
            _llm_window["count"] += 1; return True
        return False

def llm_score_entry(symbol: str, quote: str, price: float, rsi: float, vol_quote: float, trend_hint: str):
    # si LLM desactivado o sin credenciales => score alto para no bloquear
    if not (LLM_ENABLED and OPENAI_API_KEY and OPENAI_MODEL):
        return 75.0, "llm_off"

    # rate limit local
    if not llm_rate_ok():
        return 60.0, "llm_ratelimit_local"

    try:
        prompt = (
            "Eres un asistente de trading spot intradía. Evalúa si conviene ENTRAR ahora.\n"
            "Considera RSI(14), fuerza reciente y volumen; evita rupturas exhaustas.\n"
            f"Par: {symbol} (quote {quote}) | precio {price:.8f} | RSI {rsi:.1f} | Vol24h {vol_quote:.0f} | tendencia 5m {trend_hint}.\n"
            'Devuelve SOLO JSON como {"score":0-100,"reason":"breve"}'
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 60,
            "response_format": {"type": "json_object"}  # fuerza JSON válido
        }
        url = f"{OPENAI_BASE_URL}/chat/completions"
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        body_preview = ""
        try:
            body_preview = resp.text[:300]
        except Exception:
            body_preview = "<no body>"
        if resp.status_code != 200:
            logger.error(f"[LLM] HTTP {resp.status_code} body={body_preview}")
            if resp.status_code == 401: return 75.0, "llm_unauthorized"
            if resp.status_code == 404: return 70.0, "llm_model_not_found"
            if resp.status_code == 429: return 65.0, "llm_rate_limited"
            return 70.0, f"llm_http_{resp.status_code}"

        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        j = json.loads(text)
        score = float(j.get("score", 75))
        reason = str(j.get("reason", "ok"))[:140]
        return max(0.0, min(100.0, score)), reason
    except Exception as e:
        logger.error(f"[LLM] exception: {e}")
        return 75.0, "llm_error_fallback"

# ───────── Scan/candidatos ─────────
def safe_get_ticker_24h():
    return client.get_ticker()

def scan_candidatos():
    if not EX_INFO_READY: return []
    candidatos, tickers = [], safe_get_ticker_24h()
    if not tickers: return []
    symbols_ok = set()
    for sym, meta in SYMBOL_MAP.items():
        if (meta["status"]=="TRADING"
            and meta["quote"] in PREFERRED_QUOTES
            and meta["base"] not in STABLES
            and sym not in NOT_PERMITTED):
            symbols_ok.add(sym)
    by_quote = {q: [] for q in PREFERRED_QUOTES}
    for t in tickers:
        sym = t["symbol"]
        if sym not in symbols_ok: continue
        q = SYMBOL_MAP[sym]["quote"]
        vol = float(t.get("quoteVolume", 0.0))
        if vol >= MIN_QUOTE_VOLUME: by_quote[q].append((vol, t))
    reduced = []
    for q, arr in by_quote.items():
        arr.sort(key=lambda x:x[0], reverse=True)
        reduced.extend([t for _,t in arr[:200]])
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
        except Exception:
            continue
    candidatos.sort(key=lambda x: (x["quoteVolume"], -abs(x["rsi"] - (RSI_BUY_MIN+RSI_BUY_MAX)/2)), reverse=True)
    return candidatos

def get_candidates_cached():
    global CAND_CACHE_TS, CAND_CACHE
    now = time.time()
    if (now - CAND_CACHE_TS) < 30 and CAND_CACHE:
        return CAND_CACHE
    items = scan_candidatos()
    CAND_CACHE = items; CAND_CACHE_TS = now
    return items

# ───────── Posiciones / balances ─────────
def leer_posiciones():  return cargar_json(REGISTRO_FILE, {})
def escribir_posiciones(reg): guardar_json(reg, REGISTRO_FILE)

def holdings_por_asset():
    acc = client.get_account()
    res = {}
    for b in acc.get("balances", []):
        total = float(b.get("free",0)) + float(b.get("locked",0))
        if total > 0: res[b["asset"]] = total
    return res

def free_base_qty(symbol: str) -> float:
    base = SYMBOL_MAP[symbol]["base"]
    bal = client.get_asset_balance(asset=base) or {}
    return float(bal.get("free", 0.0))

# ───────── Sizing con notional/minQty ─────────
def compute_order_qty(symbol: str, quote: str, price: float, quote_available: float, prefer_quote_amount: float):
    step, _, min_notional, min_qty = get_filter_values(symbol)
    target_spend = max(prefer_quote_amount, min_notional * NOTIONAL_BUFFER)
    spend = min(quote_available, target_spend)
    if spend <= 0 or price <= 0:
        return 0.0, 0.0
    try:
        raw = Decimal(str(spend)) / Decimal(str(price))
        qty_dec = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    except InvalidOperation:
        return 0.0, 0.0
    qty = float(qty_dec)
    if qty < (min_qty * MIN_QTY_BUFFER): return 0.0, 0.0
    if (qty * price) < (min_notional * NOTIONAL_BUFFER): return 0.0, 0.0
    return qty, float(spend)

def quotes_ordenadas_por_saldo(balances: dict) -> list:
    vals = []
    for q in PREFERRED_QUOTES:
        amt = float(balances.get(q,0.0))
        if amt <= 0: continue
        if q in ("USDT","USDC"): usd = amt
        else:
            sym = q + "USDT"
            if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"]=="TRADING":
                p = obtener_precio(sym) or 0.0
                usd = amt * p if p>0 else 0.0
            else: usd = 0.0
        vals.append((usd, q))
    vals.sort(reverse=True)
    return [q for _,q in vals] or PREFERRED_QUOTES

# ───────── Comprar oportunidad ─────────
def comprar_oportunidad():
    """
    Abre nuevas posiciones si:
      - Hay saldo en una quote ≥ MIN_EUR_ORDER (equivalente en esa quote)
      - Pasa filtros de notional/minQty y TP neto > 0 tras fees/slippage
      - LLM (si activo) no bloquea (umbral muy permisivo)
    """
    global LAST_BUY_TS
    if not EX_INFO_READY: return
    if not BUY_LOCK.acquire(blocking=False): return
    try:
        reg = leer_posiciones()
        if len(reg) >= MAX_OPEN_POSITIONS: return
        balances = holdings_por_asset()
        quotes = quotes_ordenadas_por_saldo(balances)

        now = time.time()
        tiempo_sin_comprar = now - LAST_BUY_TS if LAST_BUY_TS>0 else 1e9

        for quote in quotes:
            if len(reg) >= MAX_OPEN_POSITIONS: break
            disponible = float(balances.get(quote, 0.0))
            if disponible <= 0: continue

            # Mínimo 20 € convertidos a la quote y usar 100% del saldo
            min_eur_quote = min_eur_to_quote_amount(quote, MIN_EUR_ORDER)
            if min_eur_quote <= 0:
                logger.info(f"[SKIP] No hay precio EUR->{quote} fiable; salto la quote.")
                continue
            if disponible < min_eur_quote:
                logger.info(f"[SKIP] {quote}: saldo {disponible:.8f} < mínimo {MIN_EUR_ORDER}€ eq ({min_eur_quote:.8f} {quote}).")
                continue
            prefer_amount = max(min_eur_quote, disponible * FULL_BALANCE_SPEND_FRACTION)

            intentos = 0
            while disponible > 0 and len(reg) < MAX_OPEN_POSITIONS:
                intentos += 1
                if intentos > MAX_BUY_ATTEMPTS_PER_QUOTE: break

                cand_all = get_candidates_cached()
                candidatos = [c for c in cand_all if c["quote"] == quote and c["symbol"] not in reg]
                elegido = candidatos[0] if candidatos else None

                if not elegido and AGGRESSIVE_MODE and tiempo_sin_comprar >= FORCE_BUY_AFTER_SEC:
                    tickers = safe_get_ticker_24h() or []
                    top = []
                    for t in tickers:
                        sym = t["symbol"]
                        if sym in NOT_PERMITTED or sym not in SYMBOL_MAP: continue
                        meta = SYMBOL_MAP[sym]
                        if (meta["status"]=="TRADING" and meta["quote"]==quote and meta["base"] not in STABLES):
                            vol = float(t.get("quoteVolume",0.0) or 0.0)
                            top.append((vol, sym))
                    top.sort(reverse=True)
                    if top:
                        elegido = {"symbol": top[0][1], "quote": quote, "rsi": 50.0, "lastPrice": 0, "quoteVolume": top[0][0]}
                        logger.info(f"[FORCE] Elegido top volumen {quote}: {elegido['symbol']}")

                if not elegido:
                    break

                symbol = elegido["symbol"]
                if symbol in NOT_PERMITTED or SYMBOL_MAP[symbol]["status"]!="TRADING":
                    break

                price = obtener_precio(symbol)
                if not price: break

                # LLM (opcional) muy permisivo
                proceed_llm = True
                if LLM_ENABLED and OPENAI_API_KEY:
                    score, reason = llm_score_entry(
                        symbol, quote, price,
                        float(elegido.get("rsi", 50.0)),
                        float(elegido.get("quoteVolume", 0.0)),
                        "mixta"
                    )
                    logger.info(f"[LLM] {symbol} score={score:.1f} {reason}")
                    if score <= LLM_BLOCK_THRESHOLD: proceed_llm = False
                if not proceed_llm:
                    if len(candidatos) > 1:
                        candidatos = candidatos[1:]; continue
                    break

                # Sizing y validaciones
                qty, _spend = compute_order_qty(symbol, quote, price, disponible, prefer_amount)
                if qty <= 0:
                    logger.info(f"[NOTIONAL] {symbol}: no alcanza minNotional/minQty con prefer={prefer_amount:.8f} {quote}.")
                    break

                # TP neto positivo tras fees/slippage
                fee_rate = get_commission_rate(symbol)
                net_tp = expected_net_after_fee(price, price*(1+TAKE_PROFIT), qty, fee_rate)
                if net_tp <= 0:
                    logger.info(f"[NET] {symbol}: TP neto <= 0, descarto entrada.")
                    break

                qty_str = format_qty(symbol, qty)  # evita -1100
                try:
                    orden = client.order_market_buy(symbol=symbol, quantity=qty_str)
                    filled = float(orden.get("executedQty", qty))
                    last_price = obtener_precio(symbol) or price
                    reg[symbol] = {
                        "qty": filled,
                        "buy_price": float(last_price),
                        "peak": float(last_price),
                        "quote": quote,
                        "ts": datetime.now(TIMEZONE).isoformat()
                    }
                    escribir_posiciones(reg)
                    LAST_BUY_TS = time.time()
                    enviar_telegram(f"🟢 Compra {symbol} qty={qty_str} (~{filled:.8f}) @ {last_price:.8f} {quote} | fee={fee_rate*100:.2f}%")
                    balances = holdings_por_asset()
                    disponible = float(balances.get(quote, 0.0))
                except BinanceAPIException as e:
                    logger.error(f"Compra error {symbol}: {e}")
                    if getattr(e, "code", None) == -2010:
                        NOT_PERMITTED.add(symbol); logger.warning(f"Blacklist: {symbol}")
                    backoff_sleep(e); break
                except Exception as e:
                    logger.error(f"Compra error {symbol}: {e}")
                    backoff_sleep(e); break
    finally:
        try: BUY_LOCK.release()
        except Exception: pass

# ───────── Gestión / rotación ─────────
def gestionar_posiciones():
    if not EX_INFO_READY: return
    reg = leer_posiciones()
    if not reg: return
    nuevos = {}
    for symbol, data in reg.items():
        try:
            qty = float(data["qty"]); buy = float(data["buy_price"])
            peak = float(data.get("peak", buy)); quote = data["quote"]
            price = obtener_precio(symbol)
            if not price: nuevos[symbol]=data; continue
            change = (price - buy)/buy
            peak = max(peak, price)
            trailing_active = (peak - buy)/buy >= TRAIL_ACTIVATE
            trail_hit = trailing_active and (price <= peak*(1-TRAIL_PCT))

            kl = safe_get_klines(symbol, Client.KLINE_INTERVAL_5MINUTE, 60)
            if not kl: nuevos[symbol]=data; continue
            closes = [float(k[4]) for k in kl]
            rsi = calculate_rsi(closes, RSI_PERIOD)

            fee_rate = get_commission_rate(symbol)
            net_now = expected_net_after_fee(buy, price, qty, fee_rate)

            tp = change >= TAKE_PROFIT and net_now >= 0
            rotate = change >= ROTATE_PROFIT and net_now >= 0
            ob = rsi >= RSI_SELL_OVERBOUGHT and net_now >= 0
            sl = change <= STOP_LOSS
            sell_flag = tp or sl or ob or trail_hit or rotate

            if sell_flag:
                step, _, min_notional, min_qty = get_filter_values(symbol)
                free_now = free_base_qty(symbol)
                sell_qty = min(qty, free_now) * 0.999
                if sell_qty <= 0: continue
                qty_str = format_qty(symbol, sell_qty)
                if (price * float(qty_str)) < (min_notional * NOTIONAL_BUFFER):
                    logger.info(f"{symbol}: venta no cumple minNotional."); continue
                client.order_market_sell(symbol=symbol, quantity=qty_str)
                realized = expected_net_after_fee(buy, price, float(qty_str), fee_rate)
                total = actualizar_pnl_diario(realized)
                motivo = ("TP" if tp else ("SL" if sl else ("RSI" if ob else ("TRAIL" if trail_hit else "ROTATE"))))
                enviar_telegram(
                    f"🔴 Venta {symbol} qty={qty_str} @ {price:.8f} ({change*100:.2f}%) "
                    f"Motivo:{motivo} RSI:{rsi:.1f} | PnL neto:{realized:.4f} {quote} | Hoy:{total:.4f}"
                )
                # elimina posición y rota en la MISMA quote (cripto→cripto)
                reg2 = leer_posiciones(); reg2.pop(symbol, None); escribir_posiciones(reg2)
                comprar_oportunidad()
            else:
                data["peak"] = float(peak)
                nuevos[symbol] = data
        except BinanceAPIException as e:
            logger.error(f"Gestion error {symbol}: {e}"); backoff_sleep(e); nuevos[symbol]=data
        except Exception as e:
            logger.error(f"Gestion error {symbol}: {e}"); backoff_sleep(e); nuevos[symbol]=data
    escribir_posiciones(nuevos)

# ───────── Limpieza / resumen ─────────
def limpiar_dust():
    # opcional: vender restos minúsculos; lo dejamos vacío para evitar gastos innecesarios
    pass

def resumen_diario():
    try:
        cuenta = client.get_account() if client else {"balances":[]}
        pnl = cargar_json(PNL_DIARIO_FILE, {})
        d = hoy_str(); pnl_hoy = pnl.get(d, 0.0)
        mensaje = [f"📊 Resumen diario ({d}, {TZ_NAME}):", f"PNL hoy: {pnl_hoy:.4f}", "Balances:"]
        for b in cuenta.get("balances", []):
            total = float(b.get("free",0)) + float(b.get("locked",0))
            if total >= 0.001: mensaje.append(f"• {b['asset']}: {total:.6f}")
        enviar_telegram("\n".join(mensaje))
        cutoff = (datetime.now(TIMEZONE) - timedelta(days=14)).date().isoformat()
        pnl2 = {k:v for k,v in pnl.items() if k >= cutoff}; guardar_json(pnl2, PNL_DIARIO_FILE)
    except Exception as e:
        logger.warning(f"Resumen diario error: {e}")

# ───────── Scheduler / main ─────────
def run_bot():
    enviar_telegram(
        f"🤖 Bot activo: min {MIN_EUR_ORDER}€ por orden, gasto {FULL_BALANCE_SPEND_FRACTION*100:.0f}% del saldo quote, "
        f"TP {TAKE_PROFIT*100:.1f}% ROTATE {ROTATE_PROFIT*100:.1f}% SL {STOP_LOSS*100:.1f}%."
    )
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(gestionar_posiciones, 'interval', seconds=10, max_instances=1)
    scheduler.add_job(comprar_oportunidad,  'interval', seconds=20, max_instances=2, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(resumen_diario,       'cron', hour=23, minute=0)
    scheduler.add_job(load_exchange_info,   'interval', minutes=15)
    scheduler.start()

def main():
    init_binance_client(); load_exchange_info()
    threading.Thread(target=run_http_server, daemon=True).start()
    run_bot()
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
