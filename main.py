import os, time, json, logging, requests, pytz, numpy as np, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
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

# Estrategia
USD_MIN = float(os.getenv("USD_MIN", "15"))
USD_MAX = float(os.getenv("USD_MAX", "20"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.05"))          # 5%
STOP_LOSS = float(os.getenv("STOP_LOSS", "-0.02"))             # -2%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "200000"))
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "1"))
RESUMEN_HORA_LOCAL = int(os.getenv("RESUMEN_HORA", "23"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "42"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "58"))
RSI_SELL_OVERBOUGHT = float(os.getenv("RSI_SELL_OVERBOUGHT", "68"))

USE_FALLBACK = os.getenv("USE_FALLBACK", "true").lower() == "true"
FALLBACK_SYMBOLS = [s.strip().upper() for s in os.getenv("FALLBACK_SYMBOLS", "BTC,ETH,SOL").split(",") if s.strip()]

TZ_NAME = os.getenv("TZ", "Europe/Madrid")
TIMEZONE = pytz.timezone(TZ_NAME)

PREFERRED_QUOTES = [q.strip().upper() for q in os.getenv("PREFERRED_QUOTES", "USDC,USDT,FDUSD,TUSD,BUSD").split(",") if q.strip()]

# Stables y blacklist
STABLES = [s.strip().upper() for s in os.getenv(
    "STABLES",
    "USDT,USDC,FDUSD,TUSD,BUSD,DAI,USDP,USTC,EUR,TRY,GBP,BRL,ARS"
).split(",") if s.strip()]
NOT_PERMITTED = set()  # símbolos baneados por -2010

REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# ───────────── ESTADO GLOBAL ─────────────
client = None
EX_INFO_READY = False
SYMBOL_MAP = {}  # symbol -> {base, quote, status, filters, isSpotAllowed, permissions}

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

# ───────────── HTTP para Render ─────────────
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
                "isSpotAllowed": s.get("isSpotTradingAllowed", False),
                "permissions": s.get("permissions", []),
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

# ───────────── ESTRATEGIA ─────────────
def scan_candidatos():
    if not (client and EX_INFO_READY):
        return []
    candidatos = []
    tickers = safe_get_ticker_24h()
    if not tickers:
        return []

    # Sólo símbolos spot activos, con quote preferida, base NO stable y no en blacklist
    symbols_ok = set()
    for sym, meta in SYMBOL_MAP.items():
        if (meta["status"] == "TRADING"
            and meta["quote"] in PREFERRED_QUOTES
            and meta.get("isSpotAllowed", False)
            and ("SPOT" in meta.get("permissions", []))
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
        reduced.extend([t for _, t in arr[:80]])

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

def leer_posiciones():  return cargar_json(REGISTRO_FILE, {})
def escribir_posiciones(reg): guardar_json(reg, REGISTRO_FILE)

def holdings_por_asset():
    if not client: return {}
    try:
        acc = client.get_account()
        res = {}
        for b in acc["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0: res[b["asset"]] = total
        return res
    except Exception as e:
        logger.warning(f"holdings error: {e}")
        backoff_sleep(e); return {}

def obtener_precio(symbol):
    while True:
        try:
            t = client.get_ticker(symbol=symbol)
            return float(t["lastPrice"])
        except Exception as e:
            logger.warning(f"precio error {symbol}: {e}")
            backoff_sleep(e)
            if client is None: break

def get_filter_values_cached(symbol): return get_filter_values(symbol)

def min_notional_ok(symbol, price, qty):
    if symbol not in SYMBOL_MAP: return False
    _, _, min_notional = get_filter_values_cached(symbol)
    return (price * qty) >= min_notional * 1.02

def next_order_size():
    import random
    return round(random.uniform(USD_MIN, USD_MAX), 2)

def comprar_oportunidad():
    if not (client and EX_INFO_READY): return
    reg = leer_posiciones()
    if len(reg) >= MAX_OPEN_POSITIONS: return
    balances = holdings_por_asset()

    for quote in PREFERRED_QUOTES:
        if len(reg) >= MAX_OPEN_POSITIONS: break
        disponible = float(balances.get(quote, 0.0))
        intentos = 0
        while disponible >= USD_MIN and len(reg) < MAX_OPEN_POSITIONS:
            intentos += 1
            if intentos > 10: break

            # Candidatos por RSI para ESTA quote
            cand_all = scan_candidatos()
            candidatos = [c for c in cand_all if c["quote"] == quote and c["symbol"] not in reg]
            elegido = candidatos[0] if candidatos else None

            # Descarta si base es stable o en blacklist
            if elegido:
                base_asset = SYMBOL_MAP[elegido["symbol"]]["base"]
                if base_asset in STABLES or elegido["symbol"] in NOT_PERMITTED:
                    elegido = None

            # Fallback a BTC/ETH/SOL… (nunca stables)
            if not elegido and USE_FALLBACK:
                for base in FALLBACK_SYMBOLS:
                    if base in STABLES:  # por si alguien mete USDT, etc.
                        continue
                    sym = symbol_exists(base, quote)
                    if (sym and sym not in reg and sym not in NOT_PERMITTED):
                        meta = SYMBOL_MAP[sym]
                        if (meta.get("isSpotAllowed", False) and "SPOT" in meta.get("permissions", [])
                            and meta["base"] not in STABLES):
                            try:
                                kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 30)
                                if not kl: continue
                                closes = [float(k[4]) for k in kl]
                                rsi = calculate_rsi(closes, RSI_PERIOD)
                                if 35 <= rsi <= 65:
                                    elegido = {"symbol": sym, "quote": quote, "rsi": rsi,
                                               "lastPrice": closes[-1], "quoteVolume": 0}
                                    break
                            except Exception:
                                continue

            if not elegido:
                logger.info(f"Sin candidato para {quote}; saldo se mantiene hasta próximo ciclo.")
                break

            symbol = elegido["symbol"]
            price = obtener_precio(symbol)
            if not price: break
            step, _, _ = get_filter_values_cached(symbol)

            usd_orden = min(next_order_size(), disponible)
            qty = quantize_qty(usd_orden / price, step)

            if qty <= 0 or not min_notional_ok(symbol, price, qty):
                usd_orden = min(max(usd_orden * 1.3, USD_MIN), disponible)
                qty = quantize_qty(usd_orden / price, step)
                if qty <= 0 or not min_notional_ok(symbol, price, qty):
                    logger.info(f"{symbol}: no alcanza minNotional con saldo disponible.")
                    break

            try:
                client.order_market_buy(symbol=symbol, quantity=qty)
                reg[symbol] = {
                    "qty": float(qty),
                    "buy_price": float(price),
                    "quote": quote,
                    "ts": datetime.now(TIMEZONE).isoformat()
                }
                escribir_posiciones(reg)
                enviar_telegram(f"🟢 Compra {symbol} qty={qty} @ {price:.8f} {quote} | RSI {round(elegido.get('rsi', 50),1)}")
                balances = holdings_por_asset()
                disponible = float(balances.get(quote, 0.0))
            except BinanceAPIException as e:
                logger.error(f"Compra error {symbol}: {e}")
                if getattr(e, "code", None) == -2010:
                    NOT_PERMITTED.add(symbol)
                    logger.warning(f"Añadido a blacklist por no permitido: {symbol}")
                backoff_sleep(e)
                break
            except Exception as e:
                logger.error(f"Compra error {symbol}: {e}")
                backoff_sleep(e)
                break

def gestionar_posiciones():
    if not (client and EX_INFO_READY): return
    reg = leer_posiciones()
    if not reg: return
    nuevos = {}
    for symbol, data in reg.items():
        try:
            qty = float(data["qty"]); buy_price = float(data["buy_price"]); quote = data["quote"]
            if qty <= 0: continue
            price = obtener_precio(symbol)
            if not price:
                nuevos[symbol] = data; continue
            change = (price - buy_price) / buy_price
            kl = safe_get_klines(symbol, Client.KLINE_INTERVAL_5MINUTE, 60)
            if not kl:
                nuevos[symbol] = data; continue
            closes = [float(k[4]) for k in kl]
            rsi = calculate_rsi(closes, RSI_PERIOD)

            tp = change >= TAKE_PROFIT
            sl = change <= STOP_LOSS
            ob = rsi >= RSI_SELL_OVERBOUGHT

            if tp or sl or ob:
                step, _, _ = get_filter_values_cached(symbol)
                qty_q = quantize_qty(qty, step)
                if qty_q <= 0: continue
                client.order_market_sell(symbol=symbol, quantity=qty_q)
                realized = (price - buy_price) * qty_q
                total_pnl = actualizar_pnl_diario(realized)
                motivo = "TP" if tp else ("SL" if sl else "RSI")
                enviar_telegram(f"🔴 Venta {symbol} qty={qty_q} @ {price:.8f} ({change*100:.2f}%) Motivo: {motivo} RSI:{rsi:.1f} | PnL: {realized:.4f} {quote} | PnL hoy: {total_pnl:.4f}")
            else:
                nuevos[symbol] = data
        except BinanceAPIException as e:
            logger.error(f"Gestion error {symbol}: {e}")
            backoff_sleep(e); nuevos[symbol] = data
        except Exception as e:
            logger.error(f"Gestion error {symbol}: {e}")
            backoff_sleep(e); nuevos[symbol] = data
    escribir_posiciones(nuevos)

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
            # evitar vender hacia par donde la base sea estable
            if SYMBOL_MAP[sym]["base"] in STABLES: continue
            price = obtener_precio(sym)
            if not price: continue
            step, _, _ = get_filter_values(sym)
            qty_sell = quantize_qty(qty, step)
            if qty_sell <= 0 or not min_notional_ok(sym, price, qty_sell): continue
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

def ciclo():
    try:
        gestionar_posiciones()
        comprar_oportunidad()   # intenta gastar la quote hasta ~0 (dentro de filtros)
        limpiar_dust()
    except Exception as e:
        logger.error(f"Ciclo error: {e}")

# ───────────── ARRANQUE ROBUSTO ─────────────
def init_loop():
    """Reintenta cliente y exchangeInfo en background sin tumbar proceso."""
    global EX_INFO_READY
    while True:
        if client is None:
            init_binance_client()
        if client and not EX_INFO_READY:
            load_exchange_info()
        time.sleep(10)

def run_bot():
    enviar_telegram("🤖 Bot spot activo (sin Grok). RSI 5m, TP 5%, SL 2%, órdenes 15–20 USDC. Sin stables↔stables.")
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(ciclo, 'interval', minutes=INTERVAL_MINUTES, max_instances=1)
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA_LOCAL, minute=0)
    scheduler.add_job(load_exchange_info, 'interval', minutes=15)  # refresco periódico
    scheduler.start()

def main():
    threading.Thread(target=init_loop, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    run_http_server()

if __name__ == "__main__":
    main()
