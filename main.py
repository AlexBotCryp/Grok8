import os
import time
import json
import requests
import pytz
import logging
import numpy as np
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuración
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROK_API_KEY = os.getenv("GROK_API_KEY")

if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY]):
    raise ValueError("Faltan variables de entorno: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY")

# Configura el cliente de OpenAI con la clave de Grok
client_openai = OpenAI(
    api_key=GROK_API_KEY,
    base_url="https://api.x.ai/v1"
)

client = Client(API_KEY, API_SECRET)

PORCENTAJE_USDC = 0.5  # Reducido a 50% para transiciones suaves
TAKE_PROFIT = 0.005  # 0.5%
STOP_LOSS = -0.010  # -1.0%
PERDIDA_MAXIMA_DIARIA = 50
MONEDA_BASE = "USDC"
RESUMEN_HORA = 23
MIN_VOLUME = 50000
MIN_SALDO_COMPRA = 5
COMMISSION_RATE = 0.001
TIMEZONE = pytz.timezone("UTC")

# Archivos
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

def enviar_telegram(mensaje):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}
        )
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Error enviando mensaje a Telegram: {e}")

def cargar_json(file):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return {}

def guardar_json(data, file):
    with open(file, "w") as f:
        json.dump(data, f)

def get_current_date():
    return datetime.now(TIMEZONE).date().isoformat()

def actualizar_pnl_diario(realized_pnl):
    pnl_data = cargar_json(PNL_DIARIO_FILE)
    today = get_current_date()
    if today not in pnl_data:
        pnl_data[today] = 0
    pnl_data[today] += realized_pnl
    guardar_json(pnl_data, PNL_DIARIO_FILE)
    return pnl_data[today]

def puede_comprar():
    pnl_data = cargar_json(PNL_DIARIO_FILE)
    today = get_current_date()
    pnl_hoy = pnl_data.get(today, 0)
    return pnl_hoy > -PERDIDA_MAXIMA_DIARIA

def calculate_rsi(closes, period=14):
    if len(closes) <= period:
        return 50  # Valor neutral si no hay suficientes datos
    deltas = np.diff(closes)
    gain = np.where(deltas > 0, deltas, 0)
    loss = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
    rsi = 100 - (100 / (1 + rs))
    for i in range(period, len(gain)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
        rsi = 100 - (100 / (1 + rs))
    return rsi

def mejores_criptos():
    try:
        tickers = client.get_ticker()
        candidates = [t for t in tickers if t["symbol"].endswith(MONEDA_BASE) and float(t.get("quoteVolume", 0)) > MIN_VOLUME]
        filtered = []
        for t in candidates[:15]:
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

def consultar_grok(prompt):
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
        for cripto in criptos:
            symbol = cripto["symbol"]
            if symbol in registro:
                continue
            try:
                ticker = client.get_ticker(symbol=symbol)
                precio = float(ticker["lastPrice"])
                change_percent = float(cripto["priceChangePercent"])
                volume = float(cripto["quoteVolume"])
                rsi = cripto.get("rsi", 50)
                prompt = f"Analiza {symbol}: Precio {precio:.4f}, Cambio {change_percent:.2f}%, Volumen {volume:.2f}, RSI {rsi:.2f}. ¿Comprar con {cantidad_usdc:.2f} USDC? Supervisa solo, prioriza RSI < 70. Responde solo con 'sí' o 'no'."
                grok_response = consultar_grok(prompt)
                precision = get_precision(symbol)
                cantidad = round(cantidad_usdc / precio, precision)
                if cantidad <= 0:
                    continue
                ganancia_bruta = (precio * cantidad) * TAKE_PROFIT
                comision_compra = (precio * cantidad) * COMMISSION_RATE
                comision_venta = ((precio * (1 + TAKE_PROFIT)) * cantidad) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
                if rsi < 70 and ganancia_neta > 0 and 'sí' in grok_response:
                    orden = client.order_market_buy(symbol=symbol, quantity=cantidad)
                    logger.info(f"Orden de compra: {orden}")
                    registro[symbol] = {
                        "cantidad": cantidad,
                        "precio_compra": precio,
                        "timestamp": datetime.now(TIMEZONE).isoformat()
                    }
                    guardar_json(registro, REGISTRO_FILE)
                    enviar_telegram(f"🟢 Comprado {symbol} - {cantidad:.{precision}f} a {precio:.4f} USDC. RSI: {rsi:.2f}, Grok: {grok_response}")
                    break
                else:
                    logger.info(f"No se compra {symbol}: RSI {rsi:.2f}, Grok: {grok_response}, Ganancia neta proyectada: {ganancia_neta:.4f}")
            except BinanceAPIException as e:
                logger.error(f"Error comprando {symbol}: {e}")
                continue
    except BinanceAPIException as e:
        logger.error(f"Error general en compra: {e}")

def vender():
    registro = cargar_json(REGISTRO_FILE)
    nuevos_registro = {}
    saldo_usdc = float(client.get_asset_balance(asset=MONEDA_BASE)['free'])
    logger.info(f"Saldo USDC antes de vender: {saldo_usdc:.2f}")
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
            prompt = f"Para {symbol}: Precio compra {precio_compra:.4f}, actual {precio_actual:.4f}, cambio {cambio*100:.2f}%, RSI {rsi:.2f}. ¿Vender ahora? Supervisa solo, prioriza RSI > 70. Responde solo con 'sí' o 'no'."
            grok_response = consultar_grok(prompt)
            ganancia_bruta = cantidad * (precio_actual - precio_compra)
            comision_compra = precio_compra * cantidad * COMMISSION_RATE
            comision_venta = precio_actual * cantidad * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - comision_compra - comision_venta
            vender_por_stop_loss = cambio <= STOP_LOSS
            vender_por_profit = (cambio >= TAKE_PROFIT or rsi > 70 or 'sí' in grok_response) and ganancia_neta > 0
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
                motivo = "Stop-loss" if vender_por_stop_loss else "Take-profit/RSI/Grok"
                enviar_telegram(f"🔴 Vendido {symbol} - {cantidad:.{precision}f} a {precio_actual:.4f} (Cambio: {cambio*100:.2f}%) PNL: {realized_pnl:.2f} USDC. Motivo: {motivo}. RSI: {rsi:.2f}, Grok: {grok_response}")
                comprar()  # Reinicia compra tras venta
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

enviar_telegram("🤖 Bot IA activo con RSI como prioridad y Grok como supervisor.")
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(comprar, 'interval', minutes=2)
scheduler.add_job(vender, 'interval', minutes=2)
scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0)
scheduler.start()
try:
    while True:
        time.sleep(10)
except (KeyboardInterrupt, SystemExit):
    scheduler.shutdown()
