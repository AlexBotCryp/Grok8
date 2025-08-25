import os
import time
import json
import logging
import requests
import pytz
import numpy as np
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ─────────────────────────  CONFIG  ─────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-spot")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Para Render (Web Service)
PORT = int(os.getenv("PORT", "10000"))  # Render inyecta PORT

if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
    raise ValueError("Faltan variables: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

# Preferimos cotizar en USDC, pero podemos rutear por otros si es necesario
PREFERRED_QUOTES = os.getenv("PREFERRED_QUOTES", "USDC,USDT,FDUSD,TUSD,BUSD").split(",")
PREFERRED_QUOTES = [q.strip().upper() for q in PREFERRED_QUOTES if q.strip()]

# Estrategia
USD_MIN = float(os.getenv("USD_MIN", "15"))
USD_MAX = float(os.getenv("USD_MAX", "20"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.05"))            # 5%
STOP_LOSS = float(os.getenv("STOP_LOSS", "-0.02"))               # -2%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "200000"))
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "1"))
RESUMEN_HORA_LOCAL = int(os.getenv("RESUMEN_HORA", "23"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "42"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "58"))
RSI_SELL_OVERBOUGHT = float(os.getenv("RSI_SELL_OVERBOUGHT", "68"))

# Fallback para no dejar saldo parado si no hay señal RSI
USE_FALLBACK = os.getenv("USE_FALLBACK", "true").lower() == "true"
FALLBACK_SYMBOLS = os.getenv("FALLBACK_SYMBOLS", "BTC,ETH,SOL").split(",")
FALLBACK_SYMBOLS = [s.strip().upper() for s in FALLBACK_SYMBOLS if s.strip()]

# Zona horaria
TZ_NAME = os.getenv("TZ", "Europe/Madrid")
TIMEZONE = pytz.timezone(TZ_NAME)

# Persistencia
REGISTRO_FILE = "registro.json"       # posiciones abiertas {symbol: {qty, buy_price, quote, ts}}
PNL_DIARIO_FILE = "pnl_diario.json"   # {YYYY-MM-DD: pnl_en_quote}

# Cliente
client = Client(API_KEY, API_SECRET)

# Estructuras globales (exchangeInfo, filtros, mapa de símbolos)
EX_INFO = None
SYMBOL_MAP = {}  # { "BTCUSDC": {"base":"BTC","quote":"USDC","status":"TRADING","filters":{...}} , ... }

# ─────────────────────────  HTTP SERVER (Render)  ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/" or self.path == "/health":
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            logger.warning(f"HealthHandler error: {e}")

def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"HTTP server escuchando en 0.0.0.0:{PORT}")
    server.serve_forever()

# ─────────────────────────  UTILIDADES  ─────────────────────────
def enviar_telegram(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram error: {e}")

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

def backoff_sleep(e: BinanceAPIException):
    # Manejo simple para -1003 “request weight”
    if getattr(e, "code", None) == -1003:
        logger.warning("Rate limit / weight alto. Pausa 120s.")
        time.sleep(120)
    else:
        time.sleep(2)

def load_exchange_info():
    global EX_INFO, SYMBOL_MAP
    EX_INFO = client.get_exchange_info()
    for s in EX_INFO["symbols"]:
        symbol = s["symbol"]
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        SYMBOL_MAP[symbol] = {
            "base": s["baseAsset"],
            "quote": s["quoteAsset"],
            "status": s["status"],
            "filters": filters
        }

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

def quantize_price(price, tick: Decimal):
    if tick == 0:
        return price
    d = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    return float(d)

def safe_get_klines(symbol, interval, limit):
    while True:
        try:
            return client.get_klines(symbol=symbol, interval=interval, limit=limit)
        except BinanceAPIException as e:
            logger.warning(f"Klines error {symbol}: {e}")
            backoff_sleep(e)

def safe_get_ticker_24h():
    while True:
        try:
            return client.get_ticker()
        except BinanceAPIException as e:
            logger.warning(f"get_ticker error: {e}")
            backoff_sleep(e)

def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    arr = np.array(closes, dtype=float)
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    return float(rsi)

# ─────────────────────────  ESTRATEGIA  ─────────────────────────
def scan_candidatos():
    candidatos = []
    tickers = safe_get_ticker_24h()

    symbols_ok = {s for s, meta in SYMBOL_MAP.items()
                  if meta["status"] == "TRADING" and meta["quote"] in PREFERRED_QUOTES}

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
            closes = [float(k[4]) for k in kl]
            if len(closes) < RSI_PERIOD + 2:
                continue
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

def leer_posiciones():
    return cargar_json(REGISTRO_FILE, {})

def escribir_posiciones(reg):
    guardar_json(reg, REGISTRO_FILE)

def holdings_por_asset():
    acc = client.get_account()
    res = {}
    for b in acc["balances"]:
        total = float(b["free"]) + float(b["locked"])
        if total > 0:
            res[b["asset"]] = total
    return res

def obtener_precio(symbol):
    while True:
        try:
            t = client.get_ticker(symbol=symbol)
            return float(t["lastPrice"])
        except BinanceAPIException as e:
            logger.warning(f"precio error {symbol}: {e}")
            backoff_sleep(e)

def get_filter_values_cached(symbol):
    return get_filter_values(symbol)

def min_notional_ok(symbol, price, qty):
    _, _, min_notional = get_filter_values_cached(symbol)
    return (price * qty) >= min_notional * 1.02

def next_order_size():
    import random
    return round(random.uniform(USD_MIN, USD_MAX), 2)

def comprar_oportunidad():
    reg = leer_posiciones()
    if len(reg) >= MAX_OPEN_POSITIONS:
        logger.info("Máximo de posiciones abiertas alcanzado.")
        return

    balances = holdings_por_asset()
    for quote in PREFERRED_QUOTES:
        if len(reg) >= MAX_OPEN_POSITIONS:
            break

        disponible = float(balances.get(quote, 0.0))
        intentos_seguridad = 0
        while disponible >= USD_MIN and len(reg) < MAX_OPEN_POSITIONS:
            intentos_seguridad += 1
            if intentos_seguridad > 10:
                break

            candidatos = [c for c in scan_candidatos() if c["quote"] == quote and c["symbol"] not in reg]
            elegido = candidatos[0] if candidatos else None

            if not elegido and USE_FALLBACK:
                for base in FALLBACK_SYMBOLS:
                    sym = symbol_exists(base, quote)
                    if sym and sym not in reg and SYMBOL_MAP[sym]["status"] == "TRADING":
                        try:
                            kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 30)
                            closes = [float(k[4]) for k in kl]
                            rsi = calculate_rsi(closes, RSI_PERIOD)
                            if 35 <= rsi <= 65:
                                elegido = {"symbol": sym, "quote": quote, "rsi": rsi, "lastPrice": closes[-1], "quoteVolume": 0}
                                break
                        except Exception:
                            continue

            if not elegido:
                logger.info(f"Sin candidato para {quote}; saldo se mantiene hasta próximo ciclo.")
                break

            symbol = elegido["symbol"]
            price = obtener_precio(symbol)
            step, tick, _ = get_filter_values_cached(symbol)

            usd_orden = min(next_order_size(), disponible)
            qty = usd_orden / price
            qty = quantize_qty(qty, step)

            if qty <= 0 or not min_notional_ok(symbol, price, qty):
                usd_orden = min(max(usd_orden * 1.3, USD_MIN), disponible)
                qty = quantize_qty(usd_orden / price, step)
                if qty <= 0 or not min_notional_ok(symbol, price, qty):
                    logger.info(f"{symbol}: no alcanza minNotional con saldo disponible.")
                    break

            try:
                client.order_market_buy(symbol=symbol, quantity=qty)
                buy_price = price
                reg[symbol] = {
                    "qty": float(qty),
                    "buy_price": float(buy_price),
                    "quote": quote,
                    "ts": datetime.now(TIMEZONE).isoformat()
                }
                escribir_posiciones(reg)
                enviar_telegram(f"🟢 Compra {symbol} qty={qty} @ {buy_price:.8f} {quote} | RSI {round(elegido.get('rsi', 50),1)}")
                logger.info(f"Comprado {symbol} qty={qty} a {buy_price}")
                balances = holdings_por_asset()
                disponible = float(balances.get(quote, 0.0))
            except BinanceAPIException as e:
                logger.error(f"Error comprando {symbol}: {e}")
                backoff_sleep(e)
                break

def gestionar_posiciones():
    reg = leer_posiciones()
    if not reg:
        return

    nuevos = {}
    for symbol, data in reg.items():
        try:
            qty = float(data["qty"])
            buy_price = float(data["buy_price"])
            quote = data["quote"]

            if qty <= 0:
                continue

            price = obtener_precio(symbol)
            change = (price - buy_price) / buy_price

            kl = safe_get_klines(symbol, Client.KLINE_INTERVAL_5MINUTE, 60)
            closes = [float(k[4]) for k in kl]
            rsi = calculate_rsi(closes, RSI_PERIOD)

            tp = change >= TAKE_PROFIT
            sl = change <= STOP_LOSS
            ob = rsi >= RSI_SELL_OVERBOUGHT

            if tp or sl or ob:
                step, tick, _ = get_filter_values_cached(symbol)
                qty_q = quantize_qty(qty, step)
                if qty_q <= 0:
                    continue
                client.order_market_sell(symbol=symbol, quantity=qty_q)
                realized = (price - buy_price) * qty_q
                total_pnl = actualizar_pnl_diario(realized)
                motivo = "TP" if tp else ("SL" if sl else "RSI")
                enviar_telegram(f"🔴 Venta {symbol} qty={qty_q} @ {price:.8f} ({change*100:.2f}%) Motivo: {motivo} RSI:{rsi:.1f} | PnL: {realized:.4f} {quote} | PnL hoy: {total_pnl:.4f}")
                logger.info(f"Vendido {symbol} por {motivo} PnL={realized:.6f} {quote}")
            else:
                nuevos[symbol] = data
        except BinanceAPIException as e:
            logger.error(f"Error gestionando {symbol}: {e}")
            backoff_sleep(e)
            nuevos[symbol] = data
        except Exception as e:
            logger.error(f"Error interno {symbol}: {e}")
            nuevos[symbol] = data

    escribir_posiciones(nuevos)

def limpiar_dust():
    try:
        reg = leer_posiciones()
        activos_reg = {SYMBOL_MAP[s]["base"] for s in reg.keys() if s in SYMBOL_MAP}

        bals = holdings_por_asset()
        for asset, qty in bals.items():
            if asset in PREFERRED_QUOTES or qty <= 0:
                continue
            if asset in activos_reg:
                continue
            sym, q = find_best_route(asset, PREFERRED_QUOTES)
            if not sym:
                continue
            price = obtener_precio(sym)
            step, tick, _ = get_filter_values_cached(sym)
            qty_sell = quantize_qty(qty, step)
            if qty_sell <= 0 or not min_notional_ok(sym, price, qty_sell):
                continue
            try:
                client.order_market_sell(symbol=sym, quantity=qty_sell)
                enviar_telegram(f"🧹 Limpieza: vendido {qty_sell} {asset} -> {q}")
                logger.info(f"Limpieza {asset} via {sym}")
            except BinanceAPIException as e:
                logger.debug(f"No se pudo limpiar {asset}: {e}")
                backoff_sleep(e)
    except Exception as e:
        logger.debug(f"limpiar_dust error: {e}")

def resumen_diario():
    try:
        cuenta = client.get_account()
        pnl = cargar_json(PNL_DIARIO_FILE, {})
        d = hoy_str()
        pnl_hoy = pnl.get(d, 0.0)
        mensaje = [f"📊 Resumen diario ({d}, {TZ_NAME}):", f"PNL hoy: {pnl_hoy:.4f}"]
        mensaje.append("Balances (principales):")
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
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
        comprar_oportunidad()
        limpiar_dust()
    except Exception as e:
        logger.error(f"Ciclo error: {e}")

# ─────────────────────────  ARRANQUE  ─────────────────────────
def run_bot():
    try:
        enviar_telegram("🤖 Bot spot activo (sin Grok). RSI 5m, TP 5%, SL 2%, órdenes 15–20 USDC. 🚍")
        logger.info("Cargando exchange info…")
        load_exchange_info()
        logger.info("Exchange info cargada.")
        scheduler = BackgroundScheduler(timezone=TIMEZONE)
        scheduler.add_job(ciclo, 'interval', minutes=INTERVAL_MINUTES, max_instances=1)
        scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA_LOCAL, minute=0)
        scheduler.start()
    except Exception as e:
        logger.exception(f"Fallo crítico al iniciar el bot: {e}")
        enviar_telegram(f"⚠️ Fallo crítico al iniciar el bot: {e}")

def main():
    # Lanzar bot en hilo
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    # Mantener proceso vivo con servidor HTTP
    run_http_server()

if __name__ == "__main__":
    main()
