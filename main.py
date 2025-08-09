# -*- coding: utf-8 -*-
import os
import time
import json
import random
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
import requests
import pytz
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia")

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROK_API_KEY = os.getenv("GROK_API_KEY")

if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY]):
    raise ValueError("Faltan variables de entorno: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY")

# Mercado
MONEDA_BASE = "USDC"
MIN_VOLUME = 200_000  # evita pares poco líquidos
MAX_POSICIONES = 7
MIN_SALDO_COMPRA = 10  # Asegurar cumplimiento con minNotional
PORCENTAJE_USDC = 0.30
MAX_POR_ORDEN = 0.15  # cap adicional por orden

# Estrategia
TAKE_PROFIT = 0.015  # 1.5% para operaciones rápidas
STOP_LOSS = -0.015  # -1.5% para limitar pérdidas
COMMISSION_RATE = 0.001
RSI_BUY_MAX = 60  # Más flexible para más compras
RSI_SELL_MIN = 50  # Más sensible para más ventas

# Ritmo / límites
TRADE_COOLDOWN_SEC = 120  # 2 min entre compras del MISMO símbolo
MAX_TRADES_PER_HOUR = 15  # Más operaciones por hora

# Riesgo diario
PERDIDA_MAXIMA_DIARIA = 50  # USDC

# Horarios
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = 23

# Archivos
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# Grok (x.ai)
GROK_COOLDOWN = 60 * 5
_LAST_GROK_TS = 0

# Estado y clientes
client = Client(API_KEY, API_SECRET)
client_openai = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")

# Locks / caches / rate controls
LOCK = threading.RLock()
SYMBOL_CACHE = {}
ULTIMA_COMPRA = {}  # symbol -> ts
ULTIMAS_OPERACIONES = []  # lista de ts globales

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades tiempo / JSON
# ──────────────────────────────────────────────────────────────────────────────
def now_tz():
    return datetime.now(TZ_MADRID)

def get_current_date():
    return now_tz().date().isoformat()

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

def guardar_json(data, file):
    atomic_write_json(data, file)

# ──────────────────────────────────────────────────────────────────────────────
# Reintentos / Red
# ──────────────────────────────────────────────────────────────────────────────
def retry(fn, tries=3, base_delay=0.7, jitter=0.3, exceptions=(Exception,)):
    for i in range(tries):
        try:
            return fn()
        except exceptions as e:
            if i == tries - 1:
                raise
            time.sleep(base_delay * (2 ** i) + random.random() * jitter)

def enviar_telegram(mensaje: str):
    try:
        def _send():
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje[:4000]}
            )
            resp.raise_for_status()
        retry(_send, tries=3, base_delay=0.8)
    except Exception as e:
        logger.error(f"Telegram fallo: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# PnL diario / Riesgo
# ──────────────────────────────────────────────────────────────────────────────
def actualizar_pnl_diario(realized_pnl):
    with LOCK:
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        if today not in pnl_data:
            pnl_data[today] = 0
        pnl_data[today] += float(realized_pnl)
        guardar_json(pnl_data, PNL_DIARIO_FILE)
        return pnl_data[today]

def pnl_hoy():
    pnl_data = cargar_json(PNL_DIARIO_FILE)
    return pnl_data.get(get_current_date(), 0)

def puede_comprar():
    return pnl_hoy() > -PERDIDA_MAXIMA_DIARIA

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
def load_symbol_info(symbol):
    if symbol in SYMBOL_CACHE:
        return SYMBOL_CACHE[symbol]
    try:
        info = retry(lambda: client.get_symbol_info(symbol))
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        market_lot = next((f for f in info['filters'] if f['filterType'] == 'MARKET_LOT_SIZE'), None)
        pricef = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        notional_f = next((f for f in info['filters'] if f['filterType'] in ('NOTIONAL','MIN_NOTIONAL')), None)
        def D(x): return Decimal(x)
        meta = {
            "stepSize": D(lot['stepSize']),
            "minQty": D(lot.get('minQty', '0')),
            "marketStepSize": D(market_lot['stepSize']) if market_lot else D(lot['stepSize']),
            "marketMinQty": D(market_lot.get('minQty', lot.get('minQty', '0'))) if market_lot else D(lot.get('minQty', '0')),
            "tickSize": D(pricef['tickSize']),
            "minNotional": D(notional_f.get('minNotional', '0')) if notional_f else D('0'),
            "applyToMarket": bool(notional_f.get('applyToMarket', True)) if notional_f else True,
            "baseAsset": info['baseAsset'],
            "quoteAsset": info['quoteAsset'],
        }
        SYMBOL_CACHE[symbol] = meta
        return meta
    except Exception as e:
        logger.error(f"Error cargando info de {symbol}: {e}")
        return None

def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    steps = (qty / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * step).normalize()

def quantize_quote(quote: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return quote
    steps = (quote / tick).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * tick).normalize()

def min_quote_for_market(symbol, price: Decimal) -> Decimal:
    meta = load_symbol_info(symbol)
    if not meta:
        return Decimal('0')
    min_q = meta["minNotional"] if meta["applyToMarket"] else Decimal('0')
    return (min_q * Decimal('1.01')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def safe_get_ticker(symbol):
    return retry(lambda: client.get_ticker(symbol=symbol), tries=3, base_delay=0.5, exceptions=(Exception,))

def safe_get_balance(asset):
    try:
        b = retry(lambda: client.get_asset_balance(asset=asset), tries=3, base_delay=0.5)
        if b is None:
            return 0.0
        return float(b.get('free', 0))
    except Exception:
        return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Indicadores
# ──────────────────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed > 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else np.inf
    rsi = 100 - (100 / (1 + rs))
    upvals = up
    downvals = down
    for d in deltas[period:]:
        upval = max(d, 0)
        downval = -min(d, 0)
        upvals = (upvals * (period - 1) + upval) / period
        downvals = (downvals * (period - 1) + downval) / period
        rs = upvals / downvals if downvals != 0 else np.inf
        rsi = 100 - (100 / (1 + rs))
    return float(rsi)

# ──────────────────────────────────────────────────────────────────────────────
# Grok helper
# ──────────────────────────────────────────────────────────────────────────────
def consultar_grok(prompt):
    global consulta_contador
    consulta_contador += 1
    if consulta_contador % GROK_CONSULTA_FRECUENCIA != 0:
        return "no"  # Skip consulta para ahorrar créditos
    try:
        response = client_openai.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logger.error(f"Error en llamada a Grok API: {e}")
        return "no"  # Fallback a 'no' para mayor seguridad

# ──────────────────────────────────────────────────────────────────────────────
# Registro posiciones / precio medio
# ──────────────────────────────────────────────────────────────────────────────
def precio_medio_si_hay(symbol, lookback_days=30):
    try:
        since = int((now_tz() - timedelta(days=lookback_days)).timestamp() * 1000)
        trades = retry(lambda: client.get_my_trades(symbol=symbol, startTime=since), tries=2, base_delay=0.6)
        buys = [t for t in trades if t.get('isBuyer')]
        if not buys:
            return None
        qty_sum = Decimal('0')
        cost_sum = Decimal('0')
        for t in buys:
            qty = Decimal(t['qty'])
            price = Decimal(t['price'])
            commission = Decimal(t['commission']) if t['commissionAsset'] == MONEDA_BASE else Decimal('0')
            cost_sum += qty * price + commission  # coste en quote
            qty_sum += qty
        if qty_sum > 0:
            return float(cost_sum / qty_sum)
    except Exception as e:
        logger.warning(f"No se pudo calcular precio medio {symbol}: {e}")
    return None

def inicializar_registro():
    registro = cargar_json(REGISTRO_FILE)
    try:
        cuenta = client.get_account()
        for b in cuenta['balances']:
            asset = b['asset']
            free = float(b['free'])
            if asset != MONEDA_BASE and free > 0.001:
                symbol = asset + MONEDA_BASE
                if symbol not in registro:
                    try:
                        ticker = client.get_ticker(symbol=symbol)
                        precio_actual = float(ticker['lastPrice'])
                        registro[symbol] = {
                            "cantidad": free,
                            "precio_compra": precio_actual,
                            "timestamp": datetime.now(TIMEZONE).isoformat()
                        }
                        logger.info(f"Posición existente agregada a registro: {symbol} con cantidad {free} a precio {precio_actual}")
                    except BinanceAPIException as e:
                        logger.error(f"Error al agregar posición existente {symbol}: {e}")
        guardar_json(registro, REGISTRO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error inicializando registro: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Selección de criptos
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos():
    try:
        tickers = client.get_ticker()
        candidates = [t for t in tickers if t["symbol"].endswith(MONEDA_BASE) and float(t.get("quoteVolume", 0)) > MIN_VOLUME]
        filtered = []
        for t in candidates[:30]:
            symbol = t["symbol"]
            klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=15)
            closes = [float(k[4]) for k in klines]
            rsi = calculate_rsi(closes)
            precio = float(t["lastPrice"])
            ganancia_bruta = precio * TAKE_PROFIT
            comision_compra = precio * COMMISSION_RATE
            comision_venta = (precio + ganancia_bruta) * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
            if ganancia_neta > 0:
                t['rsi'] = rsi
                filtered.append(t)
        return sorted(filtered, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
    except BinanceAPIException as e:
        logger.error(f"Error obteniendo tickers: {e}")
        return []

def get_precision(symbol):
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                import math
                return int(-math.log10(float(f['stepSize'])))
        return 4
    except:
        return 4

# ──────────────────────────────────────────────────────────────────────────────
# Trading
# ──────────────────────────────────────────────────────────────────────────────
def comprar():
    if not puede_comprar():
        logger.info("Límite de pérdida diaria alcanzado. No se comprará más hoy.")
        return
    try:
        saldo = float(client.get_asset_balance(asset=MONEDA_BASE)['free'])
        logger.info(f"Saldo USDC disponible: {saldo:.2f}")
        if saldo < MIN_SALDO_COMPRA:
            logger.info("Saldo USDC insuficiente para comprar.")
            return
        cantidad_usdc = saldo * PORCENTAJE_USDC
        criptos = mejores_criptos()
        registro = cargar_json(REGISTRO_FILE)
        if len(registro) >= MAX_POSICIONES:
            logger.info("Máximo de posiciones abiertas alcanzado. No se comprará más.")
            return
        compradas = 0
        for cripto in criptos:
            if compradas >= 1: # Limitar a 1 compra por ciclo
                break
            symbol = cripto["symbol"]
            if symbol in registro:
                continue
            try:
                ticker = client.get_ticker(symbol=symbol)
                precio = float(ticker["lastPrice"])
                change_percent = float(cripto["priceChangePercent"])
                volume = float(cripto["quoteVolume"])
                rsi = cripto.get("rsi", 50)
                prompt = f"Analiza {symbol}: Precio {precio:.4f}, Cambio {change_percent:.2f}%, Volumen {volume:.2f}, RSI {rsi:.2f}. ¿Comprar con {cantidad_usdc:.2f} USDC? Supervisa solo, prioriza RSI < 60. Responde solo con 'sí' o 'no'."
                grok_response = consultar_grok(prompt)
                precision = get_precision(symbol)
                cantidad = round(cantidad_usdc / precio, precision)
                if cantidad <= 0:
                    continue
                ganancia_bruta = (precio * cantidad) * TAKE_PROFIT
                comision_compra = (precio * cantidad) * COMMISSION_RATE
                comision_venta = ((precio * (1 + TAKE_PROFIT)) * cantidad) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
                if rsi < 60 and ganancia_neta > 0:
                    orden = client.order_market_buy(symbol=symbol, quantity=cantidad)
                    logger.info(f"Orden de compra: {orden}")
                    registro[symbol] = {
                        "cantidad": cantidad,
                        "precio_compra": precio,
                        "timestamp": datetime.now(TIMEZONE).isoformat()
                    }
                    guardar_json(registro, REGISTRO_FILE)
                    enviar_telegram(f"🟢 Comprado {symbol} - {cantidad:.{precision}f} a {precio:.4f} USDC. RSI: {rsi:.2f}, Grok: {grok_response}")
                    compradas += 1
                else:
                    logger.info(f"No se compra {symbol}: RSI {rsi:.2f}, Grok: {grok_response}, Ganancia neta proyectada: {ganancia_neta:.4f}")
            except BinanceAPIException as e:
                logger.error(f"Error comprando {symbol}: {e}")
                continue
    except BinanceAPIException as e:
        logger.error(f"Error general en compra: {e}")

def vender_y_convertir():
    registro = cargar_json(REGISTRO_FILE)
    nuevos_registro = {}
    saldo_usdc_antes = float(client.get_asset_balance(asset=MONEDA_BASE)['free'])
    logger.info(f"Saldo USDC antes de vender: {saldo_usdc_antes:.2f}")
    for symbol, data in list(registro.items()):
        try:
            cantidad = data["cantidad"]
            precio_compra = data["precio_compra"]
            ticker = client.get_ticker(symbol=symbol)
            precio_actual = float(ticker["lastPrice"])
            cambio = (precio_actual - precio_compra) / precio_compra
            klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=15)
            closes = [float(k[4]) for k in klines]
            rsi = calculate_rsi(closes)
            prompt = f"Para {symbol}: Precio compra {precio_compra:.4f}, actual {precio_actual:.4f}, cambio {cambio*100:.2f}%, RSI {rsi:.2f}. ¿Vender ahora para convertir? Supervisa solo, prioriza RSI > 50 or ganancia >1.5%. Responde solo con 'sí' o 'no'."
            grok_response = consultar_grok(prompt)
            ganancia_bruta = cantidad * (precio_actual - precio_compra)
            comision_compra = precio_compra * cantidad * COMMISSION_RATE
            comision_venta = precio_actual * cantidad * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - comision_compra - comision_venta
            vender_por_stop_loss = cambio <= STOP_LOSS
            vender_por_profit = (cambio >= TAKE_PROFIT or rsi > 50 or 'sí' in grok_response) and ganancia_neta > 0
            if vender_por_stop_loss or vender_por_profit:
                precision = get_precision(symbol)
                asset = symbol.replace(MONEDA_BASE, '')
                cantidad_disponible = float(client.get_asset_balance(asset=asset)['free']) if client.get_asset_balance(asset=asset) else 0
                cantidad = round(min(cantidad, cantidad_disponible), precision)
                if cantidad <= 0:
                    logger.info(f"No hay saldo suficiente para vender {symbol}")
                    continue
                orden = client.order_market_sell(symbol=symbol, quantity=cantidad)
                logger.info(f"Orden de venta: {orden}")
                realized_pnl = ganancia_neta
                actualizar_pnl_diario(realized_pnl)
                motivo = "Stop-loss" if vender_por_stop_loss else "Take-profit/RSI/Grok/Conversión"
                enviar_telegram(f"🔴 Vendido {symbol} - {cantidad:.{precision}f} a {precio_actual:.4f} (Cambio: {cambio*100:.2f}%) PNL: {realized_pnl:.2f} USDC. Motivo: {motivo}. RSI: {rsi:.2f}, Grok: {grok_response}")
                # Intentar comprar otra inmediatamente para conversión
                comprar()
            else:
                nuevos_registro[symbol] = data
                logger.info(f"No se vende {symbol}: ganancia neta {ganancia_neta:.4f}, RSI {rsi:.2f}, Grok: {grok_response}")
        except BinanceAPIException as e:
            logger.error(f"Error vendiendo {symbol}: {e}")
            nuevos_registro[symbol] = data
    guardar_json(nuevos_registro, REGISTRO_FILE)

def resumen_diario():
    try:
        cuenta = client.get_account()
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy:.2f} USDC\nBalances:\n"
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0.001:
                mensaje += f"{b['asset']}: {total:.4f}\n"
        enviar_telegram(mensaje)
        seven_days_ago = (datetime.now(TIMEZONE) - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error en resumen diario: {e}")

# Inicializar registro con posiciones existentes en la cartera
inicializar_registro()

enviar_telegram("🤖 Bot IA activo con umbrales RSI más sensibles para más operaciones, márgenes ajustados para mercados estables y trading basado en cartera actual.")

scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(comprar, 'interval', minutes=3)
scheduler.add_job(vender_y_convertir, 'interval', minutes=3)
scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0)
scheduler.start()
try:
    while True:
        time.sleep(10)
except (KeyboardInterrupt, SystemExit):
    scheduler.shutdown()
