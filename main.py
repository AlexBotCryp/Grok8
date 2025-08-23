# -*- coding: utf-8 -*-
import os
import time
import json
import random
import logging
import threading
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta
import requests
import pytz
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from openai import OpenAI
except Exception as e:
    OpenAI = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""
GROK_API_KEY = os.getenv("GROK_API_KEY") or ""
GROK_BASE_URL = "https://api.x.ai/v1"
ENABLE_GROK_ROTATION = True
GROK_CONSULTA_FRECUENCIA_MIN = 30
consulta_contador = 0
_LAST_GROK_TS = 0

for var, name in [(API_KEY, "BINANCE_API_KEY"), (API_SECRET, "BINANCE_API_SECRET")]:
    if not var:
        raise ValueError(f"Falta variable de entorno: {name}")

logger.info(f"Variables de entorno configuradas: BINANCE_API_KEY={bool(API_KEY)}, TELEGRAM_TOKEN={bool(TELEGRAM_TOKEN)}, GROK_API_KEY={bool(GROK_API_KEY)}")

MONEDA_BASE = "USDC"
MIN_VOLUME = 1_000_000
MAX_POSICIONES = 1
MIN_SALDO_COMPRA = 50
PORCENTAJE_USDC = 1.0
ALLOWED_SYMBOLS = ['BTCUSDC', 'ETHUSDC', 'SOLUSDC', 'BNBUSDC', 'XRPUSDC', 'DOGEUSDC', 'ADAUSDC']
TAKE_PROFIT = 0.03
STOP_LOSS = -0.03
COMMISSION_RATE = 0.001
RSI_BUY_MAX = 35
RSI_SELL_MIN = 70
MIN_NET_GAIN_ABS = 0.5
TRAILING_STOP = 0.015
TRADE_COOLDOWN_SEC = 300
MAX_TRADES_PER_HOUR = 6
PERDIDA_MAXIMA_DIARIA = 50
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = 23
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

client = Client(API_KEY, API_SECRET)
client_openai = None

if ENABLE_GROK_ROTATION and GROK_API_KEY and OpenAI is not None:
    try:
        client_openai = OpenAI(
            api_key=GROK_API_KEY,
            base_url=GROK_BASE_URL
        )
        logger.info("Grok inicializado correctamente.")
    except Exception as e:
        logger.warning(f"No se pudo inicializar Grok: {e}")
        client_openai = None
else:
    logger.info("Grok desactivado: falta GROK_API_KEY o biblioteca OpenAI.")
    ENABLE_GROK_ROTATION = False

LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()
ULTIMA_COMPRA = {}
ULTIMAS_OPERACIONES = []
DUST_THRESHOLD = 0.5
TICKERS_CACHE = {}

# FUNCIONES OMITIDAS DEL BLOQUE ANTERIOR: retry, now_tz, enviar_telegram, cargar_json...
# Aquí retomamos desde la lógica principal:

def loop_trading():
    while True:
        try:
            for symbol in ALLOWED_SYMBOLS:
                evaluar_operacion(symbol)
        except Exception as e:
            logger.error(f"Error en loop trading: {e}")
        time.sleep(60)

def evaluar_operacion(symbol):
    ticker = client.get_ticker(symbol=symbol)
    price = float(ticker['lastPrice'])
    closes = obtener_cierres(symbol)
    rsi = calcular_rsi(closes)
    ema = calcular_ema(closes)

    logger.info(f"[{symbol}] Precio: {price}, RSI: {rsi:.2f}, EMA: {ema:.2f}")

    if rsi < RSI_BUY_MAX:
        logger.info(f"Señal de compra para {symbol}")
        comprar(symbol, price)
    elif rsi > RSI_SELL_MIN:
        logger.info(f"Señal de venta para {symbol}")
        vender(symbol, price)

def obtener_cierres(symbol, limit=100):
    klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=limit)
    return [float(k[4]) for k in klines]

def calcular_rsi(cierres, periodo=14):
    delta = np.diff(cierres)
    ganancia = delta[delta > 0].sum() / periodo
    perdida = -delta[delta < 0].sum() / periodo
    rs = ganancia / perdida if perdida != 0 else 0
    return 100 - (100 / (1 + rs))

def calcular_ema(cierres, periodo=10):
    return np.mean(cierres[-periodo:])

def comprar(symbol, price):
    usdc_disponible = float(client.get_asset_balance(asset=MONEDA_BASE)['free'])
    cantidad = (usdc_disponible * PORCENTAJE_USDC) / price
    orden = client.order_market_buy(symbol=symbol, quantity=round(cantidad, 6))
    enviar_telegram(f"🟢 COMPRA ejecutada: {symbol} a {price:.4f}")
    logger.info(f"Orden de compra: {orden}")

def vender(symbol, price):
    base_asset = symbol.replace(MONEDA_BASE, '')
    cantidad = float(client.get_asset_balance(asset=base_asset)['free'])
    if cantidad < 0.0001:
        return
    orden = client.order_market_sell(symbol=symbol, quantity=round(cantidad, 6))
    enviar_telegram(f"🔴 VENTA ejecutada: {symbol} a {price:.4f}")
    logger.info(f"Orden de venta: {orden}")

def iniciar_bot():
    enviar_telegram("🤖 Bot cripto IA iniciado")
    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(loop_trading, 'interval', seconds=60)
    scheduler.start()
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    iniciar_bot()
