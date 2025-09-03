import os, time, json, logging, requests, pytz, numpy as np, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from time import monotonic
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ───────────── Helpers robustos para entorno ─────────────
def _env_str(name, default=""):
    val = os.getenv(name)
    return default if val is None else str(val).strip()

def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    s = str(raw).strip().replace(",", ".")
    try:
        return float(s)
    except Exception:
        logging.getLogger("bot-spot").warning(f"[ENV] {name}='{raw}' inválido. Usando {default}.")
        return float(default)

def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(str(raw).strip())
    except Exception:
        logging.getLogger("bot-spot").warning(f"[ENV] {name}='{raw}' inválido. Usando {default}.")
        return int(default)

# ───────── CONFIG ─────────
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-spot")

API_KEY = _env_str("BINANCE_API_KEY")
API_SECRET = _env_str("BINANCE_API_SECRET")
TELEGRAM_TOKEN = _env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env_str("TELEGRAM_CHAT_ID")
PORT = _env_int("PORT", 10000)

# Estrategia (prudente)
TAKE_PROFIT_MIN = _env_float("TAKE_PROFIT_MIN", 0.012)      # 1.2% mínimo
STOP_LOSS_MIN   = _env_float("STOP_LOSS_MIN",   -0.012)     # -1.2% mínimo
ATR_TP_MULT     = _env_float("ATR_TP_MULT", 1.8)            # TP ≈ 1.8 * ATR%
ATR_SL_MULT     = _env_float("ATR_SL_MULT", 1.4)            # SL ≈ 1.4 * ATR%
ATR_TRAIL_MULT  = _env_float("ATR_TRAIL_MULT", 1.0)         # trailing ≈ 1.0 * ATR%
TRAIL_PCT_FLOOR = _env_float("TRAIL_PCT", 0.005)            # 0.5% mínimo (acepta '0,005')

MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 3)
TRADE_FRACTION      = _env_float("TRADE_FRACTION", 1.0)      # *** 100% del saldo de la quote ***
MIN_EUR_ORDER       = _env_float("MIN_EUR_ORDER", 20.0)      # *** mínimo 20 € por orden ***

# Ritmo / control de riesgo
COOLDOWN_PER_QUOTE_SEC = _env_int("COOLDOWN_PER_QUOTE_SEC", 120)  # 2 min
MAX_TRADES_PER_HOUR    = _env_int("MAX_TRADES_PER_HOUR", 8)
DAILY_MAX_LOSS_EUR     = _env_float("DAILY_MAX_LOSS_EUR", 30.0)   # corta compras si se pierde ≥ 30€ en el día
MAX_HOLD_MIN           = _env_int("MAX_HOLD_MIN", 120)            # salida por tiempo si no despega

# Universo / filtros técnicos
PREFERRED_QUOTES = [q.strip().upper() for q in _env_str("PREFERRED_QUOTES", "USDT,USDC").split(",") if q.strip()]
SYMBOL_WHITELIST = [a.strip().upper() for a in _env_str(
    "SYMBOL_WHITELIST", "BTC,ETH,SOL,BNB,LINK,TON,AVAX,XRP,ADA,DOGE"
).split(",") if a.strip()]

MIN_QUOTE_VOLUME = _env_float("MIN_QUOTE_VOLUME", 80000)    # liquidez alta
RSI_PERIOD       = _env_int("RSI_PERIOD", 14)
RSI_BUY_MIN      = _env_float("RSI_BUY_MIN", 45.0)
RSI_BUY_MAX      = _env_float("RSI_BUY_MAX", 65.0)
EMA_FAST         = _env_int("EMA_FAST", 20)
EMA_SLOW         = _env_int("EMA_SLOW", 50)
ATR_PERIOD       = _env_int("ATR_PERIOD", 14)

# Comisión / slippage
COMMISSION_DEFAULT   = _env_float("COMMISSION_DEFAULT", 0.001)  # 0.1%
SLIPPAGE_BUFFER_PCT  = _env_float("SLIPPAGE_BUFFER_PCT", 0.0005)

# Filtros (NOTIONAL/minQty)
NOTIONAL_BUFFER = _env_float("NOTIONAL_BUFFER", 1.03)
MIN_QTY_BUFFER  = _env_float("MIN_QTY_BUFFER", 1.0)

# No pararse del todo
AGGRESSIVE_MODE       = _env_str("AGGRESSIVE_MODE", "true").lower() == "true"
FORCE_BUY_AFTER_SEC   = _env_int("FORCE_BUY_AFTER_SEC", 300)      # 5 min sin comprar -> considerar force
MAX_BUY_ATTEMPTS_PER_QUOTE = _env_int("MAX_BUY_ATTEMPTS_PER_QUOTE", 3)

# LLM (opcional; por defecto apagado)
LLM_ENABLED      = _env_str("LLM_ENABLED", "false").lower() == "true"
OPENAI_API_KEY   = _env_str("OPENAI_API_KEY", "")
OPENAI_BASE_URL  = _env_str("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL     = _env_str("OPENAI_MODEL", "gpt-4o-mini")
LLM_BLOCK_THRESHOLD  = _env_float("LLM_BLOCK_THRESHOLD", 10.0)
LLM_MAX_CALLS_PER_MIN = _env_int("LLM_MAX_CALLS_PER_MIN", 6)
_llm_window = {"start": 0.0, "count": 0}
_llm_lock   = threading.Lock()

# Zona horaria
TZ_NAME  = _env_str("TZ", "Europe/Madrid")
TIMEZONE = pytz.timezone(TZ_NAME)

# Otros
STABLES = [s.strip().upper() for s in _env_str(
    "STABLES","USDT,USDC,FDUSD,TUSD,BUSD,DAI,USDP,USTC,EUR,TRY,GBP,BRL,ARS"
).split(",") if s.strip()]
NOT_PERMITTED = set()

REGISTRO_FILE   = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# Estado
client = None
EX_INFO_READY = False
SYMBOL_MAP = {}
FEE_CACHE = {}
CAND_CACHE, CAND_CACHE_TS = [], 0.0
LAST_BUY_TS = 0.0
BUY_LOCK = threading.Lock()
LAST_BUY_BY_QUOTE = {}  # cooldown por quote
TRADES_THIS_HOUR = {"start": 0.0, "count": 0}

# ───────── Telegram ─────────
def enviar_telegram(msg: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True},
                      timeout=10)
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

# ───────── HTTP health ─────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/health","/"):
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

# ───────── Utilidades persistencia ─────────
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

def actualizar_pnl_diario(delta_eur):
    pnl = cargar_json(PNL_DIARIO_FILE, {})
    d = hoy_str()
    pnl[d] = pnl.get(d, 0.0) + float(delta_eur)
    guardar_json(pnl, PNL_DIARIO_FILE)
    return pnl[d]

# ───────── Binance init ─────────
def init_binance_client():
    global client
    if client: return
    if not (API_KEY and API_SECRET):
        logger.error("Faltan BINANCE_API_KEY/BINANCE_API_SECRET"); return
    client = Client(API_KEY, API_SECRET); client.ping()
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
    """Cuantiza por stepSize y devuelve string de cantidad EXACTO (evita -1100)."""
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

def ema(arr, n):
    arr = np.array(arr, dtype=float)
    k = 2/(n+1)
    ema_vals = [arr[0]]
    for x in arr[1:]:
        ema_vals.append(ema_vals[-1] + k*(x - ema_vals[-1]))
    return np.array(ema_vals)

def rsi(closes, period=14):
    if len(closes) <= period: return 50.0
    arr = np.array(closes, dtype=float)
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period]); avg_loss = np.mean(losses[:period])
    if avg_loss == 0: return 100.0
    rs = avg_gain/avg_loss
    r = 100 - (100/(1+rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
        if avg_loss == 0: return 100.0
        rs = avg_gain/avg_loss
        r = 100 - (100/(1+rs))
    return float(r)

def atr(highs, lows, closes, period=14):
    highs = np.array(highs, dtype=float)
    lows  = np.array(lows, dtype=float)
    closes= np.array(closes, dtype=float)
    trs = [highs[0]-lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    trs = np.array(trs)
    if len(trs) < period: return float(np.mean(trs))
    return float(np.mean(trs[-period:]))

def min_eur_to_quote_amount(quote: str, eur_amount: float) -> float:
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

# Conversión PnL a EUR aprox (quote -> EUR)
def quote_to_eur(quote: str, amount_quote: float) -> float:
    if amount_quote == 0: return 0.0
    if quote == "EUR": return amount_quote
    if quote == "USDT":
        usdt = amount_quote
    else:
        sym = quote + "USDT"
        if sym in SYMBOL_MAP and SYMBOL_MAP[sym]["status"] == "TRADING":
            p = obtener_precio(sym)
            usdt = amount_quote * (p if p > 0 else 0.0)
        else:
            usdt = 0.0
    if usdt == 0.0: return 0.0
    if "EURUSDT" in SYMBOL_MAP:
        eurusdt = obtener_precio("EURUSDT")
        return usdt / eurusdt if eurusdt and eurusdt > 0 else 0.0
    return 0.0

# Fees / PnL neto
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

# LLM (opcional, no bloquea si falla)
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

def llm_score_entry(symbol: str, quote: str, price: float, rsi_v: float, vol_quote: float, trend_hint: str):
    if not llm_rate_ok(): return 70.0, "skip"
    try:
        prompt = (
            "Eres un asistente de trading spot intradía. Evalúa si conviene ENTRAR.\n"
            f"Par: {symbol} (quote {quote}) precio {price:.8f} RSI {rsi_v:.1f} Vol24h {vol_quote:.0f} tendencia {trend_hint}.\n"
            'Responde SOLO JSON {"score":0-100,"reason":"breve"}'
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": OPENAI_MODEL, "messages":[{"role":"user","content":prompt}],
                   "temperature":0.2, "max_tokens":60, "response_format":{"type":"json_object"}}
        resp = requests.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error(f"[LLM] HTTP {resp.status_code} body={resp.text[:300]}")
            return 70.0, f"http_{resp.status_code}"
        data = resp.json()
        j = json.loads(data["choices"][0]["message"]["content"])
        score = float(j.get("score", 70)); reason = str(j.get("reason","ok"))[:140]
        return max(0, min(100, score)), reason
    except Exception as e:
        logger.error(f"[LLM] exception: {e}")
        return 70.0, "llm_error_fallback"

# Scan/candidatos
def safe_get_ticker_24h():
    return client.get_ticker()

def scan_candidatos():
    if not EX_INFO_READY: return []
    out, tickers = [], safe_get_ticker_24h()
    if not tickers: return []

    # universo líquido whitelist + quotes preferidas
    symbols_ok = set()
    for sym, meta in SYMBOL_MAP.items():
        if (meta["status"]=="TRADING"
            and meta["quote"] in PREFERRED_QUOTES
            and meta["base"] in SYMBOL_WHITELIST
            and sym not in NOT_PERMITTED):
            symbols_ok.add(sym)

    by_quote = {q: [] for q in PREFERRED_QUOTES}
    for t in tickers:
        sym = t["symbol"]
        if sym not in symbols_ok: continue
        q = SYMBOL_MAP[sym]["quote"]
        vol = float(t.get("quoteVolume", 0.0) or 0.0)
        if vol >= MIN_QUOTE_VOLUME:
            by_quote[q].append((vol, t))

    reduced = []
    for q, arr in by_quote.items():
        arr.sort(key=lambda x:x[0], reverse=True)
        reduced.extend([t for _,t in arr[:150]])  # top 150 por quote

    # filtros técnicos: EMA, RSI, ATR%
    for t in reduced:
        sym = t["symbol"]
        try:
            kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 120)
            if not kl or len(kl) < max(EMA_SLOW+2, ATR_PERIOD+2): continue
            closes = [float(k[4]) for k in kl]
            highs  = [float(k[2]) for k in kl]
            lows   = [float(k[3]) for k in kl]
            efast  = ema(closes, EMA_FAST)
            eslow  = ema(closes, EMA_SLOW)
            if not (efast[-1] > eslow[-1] and closes[-1] > eslow[-1]):  # tendencia y precio sobre EMA lenta
                continue
            r = rsi(closes, RSI_PERIOD)
            if not (RSI_BUY_MIN <= r <= RSI_BUY_MAX):  # RSI moderado
                continue
            a = atr(highs, lows, closes, ATR_PERIOD)
            price = closes[-1]
            atr_pct = a / price if price > 0 else 0.0
            out.append({
                "symbol": sym,
                "quote": SYMBOL_MAP[sym]["quote"],
                "lastPrice": price,
                "quoteVolume": float(t["quoteVolume"]),
                "rsi": r,
                "atr_pct": atr_pct
            })
        except Exception:
            continue

    out.sort(key=lambda x: (x["quoteVolume"], -abs(x["rsi"] - (RSI_BUY_MIN+RSI_BUY_MAX)/2)), reverse=True)
    return out

def get_candidates_cached():
    global CAND_CACHE_TS, CAND_CACHE
    now = time.time()
    if (now - CAND_CACHE_TS) < 60 and CAND_CACHE:
        return CAND_CACHE
    CAND_CACHE = scan_candidatos()
    CAND_CACHE_TS = now
    return CAND_CACHE

# Posiciones / balances
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

# Sizing con notional/minQty
def compute_order_qty(symbol: str, price: float, quote_available: float, prefer_quote_amount: float):
    step, _, min_notional, min_qty = get_filter_values(symbol)
    target_spend = max(prefer_quote_amount, min_notional * NOTIONAL_BUFFER)
    spend = min(quote_available, target_spend)
    if spend <= 0 or price <= 0:
        return 0.0
    try:
        raw = Decimal(str(spend)) / Decimal(str(price))
        qty_dec = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    except InvalidOperation:
        return 0.0
    qty = float(qty_dec)
    if qty < (min_qty * MIN_QTY_BUFFER): return 0.0
    if (qty * price) < (min_notional * NOTIONAL_BUFFER): return 0.0
    return qty

# Trades/hora control
def can_trade_now():
    now = time.time()
    if TRADES_THIS_HOUR["start"] == 0.0 or (now - TRADES_THIS_HOUR["start"]) > 3600:
        TRADES_THIS_HOUR["start"] = now; TRADES_THIS_HOUR["count"] = 0
    return TRADES_THIS_HOUR["count"] < MAX_TRADES_PER_HOUR

def inc_trade_count():
    TRADES_THIS_HOUR["count"] += 1

# Daily max loss control
def daily_ok_to_buy():
    pnl = cargar_json(PNL_DIARIO_FILE, {})
    d = hoy_str()
    loss = pnl.get(d, 0.0)
    return loss > -DAILY_MAX_LOSS_EUR

# Comprar oportunidad (con mínimo 20€ y 100% saldo)
def comprar_oportunidad():
    global LAST_BUY_TS
    if not EX_INFO_READY: return
    if not daily_ok_to_buy(): 
        logger.info("[RISK] Pérdida diaria límite alcanzada: sin nuevas compras hoy.")
        return
    if not can_trade_now():
        logger.info("[RISK] Límite de operaciones por hora alcanzado.")
        return
    if not BUY_LOCK.acquire(blocking=False): return
    try:
        reg = leer_posiciones()
        if len(reg) >= MAX_OPEN_POSITIONS: return
        balances = holdings_por_asset()

        quotes = []
        for q in PREFERRED_QUOTES:
            amt = float(balances.get(q,0.0))
            if amt <= 0: continue
            if q in ("USDT","USDC"): usd = amt
            else:
                sym = q + "USDT"
                usd = amt * (obtener_precio(sym) if sym in SYMBOL_MAP else 0.0)
            quotes.append((usd, q))
        quotes.sort(reverse=True)
        quotes = [q for _,q in quotes] or PREFERRED_QUOTES

        for quote in quotes:
            if len(reg) >= MAX_OPEN_POSITIONS: break
            disponible = float(balances.get(quote, 0.0))
            if disponible <= 0: continue

            # mínimo 20€ en esta quote
            min_eur_quote = min_eur_to_quote_amount(quote, MIN_EUR_ORDER)
            if min_eur_quote <= 0:
                logger.info(f"[SKIP] No hay precio EUR->{quote} fiable.")
                continue
            if disponible < min_eur_quote:
                logger.info(f"[SKIP] {quote}: saldo {disponible:.6f} < mínimo {MIN_EUR_ORDER}€ eq ({min_eur_quote:.6f}).")
                continue

            # cooldown por quote
            if quote in LAST_BUY_BY_QUOTE and (time.time() - LAST_BUY_BY_QUOTE[quote]) < COOLDOWN_PER_QUOTE_SEC:
                continue

            # *** 100% del saldo *** (respetando el mínimo en EUR)
            prefer_amount = max(min_eur_quote, disponible * TRADE_FRACTION)  # TRADE_FRACTION = 1.0

            # candidatos por filtros técnicos
            cand_all = get_candidates_cached()
            cand = [c for c in cand_all if c["quote"] == quote and c["symbol"] not in reg]
            if not cand and AGGRESSIVE_MODE and (time.time() - LAST_BUY_TS) >= FORCE_BUY_AFTER_SEC:
                # force: mayor volumen
                tks = safe_get_ticker_24h() or []
                tps = []
                for t in tks:
                    sym = t["symbol"]
                    if sym not in SYMBOL_MAP or SYMBOL_MAP[sym]["status"]!="TRADING": continue
                    meta = SYMBOL_MAP[sym]
                    if meta["quote"]==quote and meta["base"] in SYMBOL_WHITELIST:
                        try:
                            vol = float(t.get("quoteVolume",0.0))
                            tps.append((vol, sym))
                        except Exception:
                            continue
                tps.sort(reverse=True)
                if tps:
                    sym = tps[0][1]
                    price = obtener_precio(sym); atr_pct = 0.006  # aprox si no hay klines
                    cand = [{"symbol": sym, "quote": quote, "lastPrice": price, "rsi": 50.0, "atr_pct": atr_pct}]

            if not cand:
                continue

            elegido = cand[0]
            symbol  = elegido["symbol"]
            price   = float(elegido["lastPrice"])
            atr_pct = float(elegido.get("atr_pct", 0.006))
            rsi_v   = float(elegido.get("rsi", 50.0))

            # LLM opcional
            if LLM_ENABLED and OPENAI_API_KEY:
                score, reason = llm_score_entry(symbol, quote, price, rsi_v, float(elegido.get("quoteVolume",0.0)), "mixta")
                logger.info(f"[LLM] {symbol} score={score:.1f} {reason}")
                if score <= LLM_BLOCK_THRESHOLD:
                    continue

            qty = compute_order_qty(symbol, price, disponible, prefer_amount)
            if qty <= 0:
                logger.info(f"[NOTIONAL] {symbol}: no alcanza minNotional/minQty con prefer={prefer_amount:.6f} {quote}.")
                continue

            # TP/SL/trail dinámicos (en porcentaje)
            tp_pct = max(TAKE_PROFIT_MIN, ATR_TP_MULT * atr_pct)
            sl_pct = max(abs(STOP_LOSS_MIN), ATR_SL_MULT * atr_pct)  # positivo; guardaremos negativo
            trail_pct = max(TRAIL_PCT_FLOOR, ATR_TRAIL_MULT * atr_pct)

            # check TP neto mínimo tras fees
            fee_rate = get_commission_rate(symbol)
            net_tp = expected_net_after_fee(price, price*(1+tp_pct), qty, fee_rate)
            if net_tp <= 0:
                logger.info(f"[NET] {symbol}: TP neto <= 0, descarto entrada.")
                continue

            qty_str = format_qty(symbol, qty)
            try:
                orden = client.order_market_buy(symbol=symbol, quantity=qty_str)
                filled = float(orden.get("executedQty", qty))
                last_price = obtener_precio(symbol) or price
                reg[symbol] = {
                    "qty": filled,
                    "buy_price": float(last_price),
                    "peak": float(last_price),
                    "quote": quote,
                    "ts": datetime.now(TIMEZONE).isoformat(),
                    "tp_pct": tp_pct,
                    "sl_pct": -sl_pct,          # guardamos negativo
                    "trail_pct": trail_pct,
                    "max_hold_min": MAX_HOLD_MIN
                }
                escribir_posiciones(reg)
                LAST_BUY_TS = time.time()
                LAST_BUY_BY_QUOTE[quote] = time.time()
                inc_trade_count()
                enviar_telegram(f"🟢 Compra {symbol} qty={qty_str} (~{filled:.8f}) @ {last_price:.8f} {quote} | tp~{tp_pct*100:.2f}% sl~{-sl_pct*100:.2f}% trail~{trail_pct*100:.2f}%")
            except BinanceAPIException as e:
                logger.error(f"Compra error {symbol}: {e}")
                if getattr(e, "code", None) == -2010:
                    NOT_PERMITTED.add(symbol)
                time.sleep(2)
            except Exception as e:
                logger.error(f"Compra error {symbol}: {e}")
                time.sleep(2)
    finally:
        try: BUY_LOCK.release()
        except Exception: pass

# Gestión posiciones
def gestionar_posiciones():
    if not EX_INFO_READY: return
    reg = leer_posiciones()
    if not reg: return
    nuevos = {}
    for symbol, data in reg.items():
        try:
            qty = float(data["qty"]); buy = float(data["buy_price"])
            peak = float(data.get("peak", buy)); quote = data["quote"]
            tp_pct = float(data.get("tp_pct", TAKE_PROFIT_MIN))
            sl_pct = float(data.get("sl_pct", -abs(STOP_LOSS_MIN)))
            trail_pct = float(data.get("trail_pct", TRAIL_PCT_FLOOR))
            ts = data.get("ts"); max_hold_min = int(data.get("max_hold_min", MAX_HOLD_MIN))

            price = obtener_precio(symbol)
            if not price:
                nuevos[symbol] = data; continue
            change = (price - buy)/buy
            peak = max(peak, price)

            # tiempo en mercado
            held_min = 0
            try:
                t0 = datetime.fromisoformat(ts)
                held_min = (datetime.now(TIMEZONE) - t0).total_seconds() / 60.0
            except Exception:
                pass

            # trailing
            trail_active = (peak - buy)/buy >= (tp_pct * 0.6)
            trail_hit = trail_active and (price <= peak * (1 - trail_pct))

            fee_rate = get_commission_rate(symbol)
            net_now = expected_net_after_fee(buy, price, qty, fee_rate)

            tp = (change >= tp_pct) and net_now >= 0
            sl = (change <= sl_pct)  # SL ignora net
            time_exit = held_min >= max_hold_min and net_now >= 0
            exit_flag = tp or sl or trail_hit or time_exit

            if exit_flag:
                step, _, min_notional, _ = get_filter_values(symbol)
                free_now = free_base_qty(symbol)
                sell_qty = min(qty, free_now) * 0.999
                if sell_qty <= 0: 
                    nuevos[symbol] = data; continue
                qty_str = format_qty(symbol, sell_qty)
                if (price * float(qty_str)) < (min_notional * NOTIONAL_BUFFER):
                    logger.info(f"{symbol}: venta no cumple minNotional."); nuevos[symbol] = data; continue

                client.order_market_sell(symbol=symbol, quantity=qty_str)
                realized_quote = expected_net_after_fee(buy, price, float(qty_str), fee_rate)
                realized_eur   = quote_to_eur(quote, realized_quote)
                total_today    = actualizar_pnl_diario(realized_eur)
                motivo = ("TP" if tp else ("SL" if sl else ("TRAIL" if trail_hit else "TIME")))
                enviar_telegram(
                    f"🔴 Venta {symbol} qty={qty_str} @ {price:.8f} ({change*100:.2f}%) Motivo:{motivo} "
                    f"PnL neto≈{realized_eur:.2f} EUR | Hoy≈{total_today:.2f} EUR"
                )
            else:
                data["peak"] = float(peak)
                nuevos[symbol] = data
        except BinanceAPIException as e:
            logger.error(f"Gestion error {symbol}: {e}")
            time.sleep(2); nuevos[symbol]=data
        except Exception as e:
            logger.error(f"Gestion error {symbol}: {e}")
            time.sleep(2); nuevos[symbol]=data
    escribir_posiciones(nuevos)

# Resumen
def resumen_diario():
    try:
        cuenta = client.get_account() if client else {"balances":[]}
        pnl = cargar_json(PNL_DIARIO_FILE, {})
        d = hoy_str(); pnl_hoy = pnl.get(d, 0.0)
        msg = [f"📊 Resumen ({d}, {TZ_NAME})", f"PNL hoy ≈ {pnl_hoy:.2f} EUR", "Balances:"]
        for b in cuenta.get("balances", []):
            total = float(b.get("free",0)) + float(b.get("locked",0))
            if total >= 0.001: msg.append(f"• {b['asset']}: {total:.6f}")
        enviar_telegram("\n".join(msg))
        cutoff = (datetime.now(TIMEZONE) - timedelta(days=14)).date().isoformat()
        pnl2 = {k:v for k,v in pnl.items() if k >= cutoff}; guardar_json(pnl2, PNL_DIARIO_FILE)
    except Exception as e:
        logger.warning(f"Resumen diario error: {e}")

# Loop / scheduler
def run_bot():
    enviar_telegram(
        f"🤖 Bot activo: mínimo {MIN_EUR_ORDER}€ por orden · fracción {TRADE_FRACTION*100:.0f}% del saldo quote · "
        f"TP≥max({TAKE_PROFIT_MIN*100:.1f}%, {ATR_TP_MULT}×ATR) · SL≤min({STOP_LOSS_MIN*100:.1f}%, {ATR_SL_MULT}×ATR) · "
        f"trailing≥max({TRAIL_PCT_FLOOR*100:.2f}%, {ATR_TRAIL_MULT}×ATR)."
    )
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(gestionar_posiciones, 'interval', seconds=15, max_instances=1, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(comprar_oportunidad,  'interval', seconds=60, max_instances=1, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(resumen_diario,       'cron', hour=23, minute=0)
    scheduler.add_job(load_exchange_info,   'interval', minutes=20)
    scheduler.start()

def main():
    init_binance_client(); load_exchange_info()
    threading.Thread(target=run_http_server, daemon=True).start()
    run_bot()
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
