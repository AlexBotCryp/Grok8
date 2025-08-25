import os
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
import time
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
import requests
from decimal import Decimal, ROUND_DOWN
import pandas as pd

# Configuración de logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Obtener credenciales de variables de entorno
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID]):
    raise ValueError("Faltan variables de entorno: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    params = {'chat_id': CHAT_ID, 'text': message}
    try:
        response = requests.post(url, params=params)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Error enviando Telegram: {e}")

# Función para liquidar AVAX (salir con 100% de cartera)
def liquidate_avax():
    try:
        balance = client.get_asset_balance(asset='AVAX')
        free = float(balance['free'])
        if free > 0.01:  # Mínimo arbitrario
            symbol = 'AVAXUSDC'
            info = client.get_symbol_info(symbol)
            lot_filter = [f for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0]
            step_size = float(lot_filter['stepSize'])
            quantity = float(Decimal(str(free)).quantize(Decimal(str(step_size)), ROUND_DOWN))
            if quantity >= float(lot_filter['minQty']):
                order = client.create_order(
                    symbol=symbol,
                    side='SELL',
                    type='MARKET',
                    quantity=quantity
                )
                logging.info(f"Vendido {quantity} AVAX a USDC")
                send_telegram(f"Vendido {quantity} AVAX a USDC exitosamente.")
    except BinanceAPIException as e:
        logging.error(f"Error liquidando AVAX: {e}")
        send_telegram(f"Error liquidando AVAX: {e}")
    except Exception as e:
        logging.error(f"Error inesperado: {e}")
        send_telegram(f"Error inesperado: {e}")

# Par de trading para la nueva estrategia
PAIR = 'ETHUSDC'

# Función para obtener medias móviles
def get_moving_averages():
    try:
        klines = client.get_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=200)
        closes = [float(k[4]) for k in klines]  # Precio de cierre
        df = pd.DataFrame({'close': closes})
        ma50 = df['close'].rolling(window=50).mean().iloc[-1]
        ma200 = df['close'].rolling(window=200).mean().iloc[-1]
        return ma50, ma200
    except Exception as e:
        logging.error(f"Error obteniendo datos: {e}")
        raise

# Función principal de trading (se ejecuta cada 5 min)
def trade_strategy():
    try:
        ma50, ma200 = get_moving_averages()
        eth_balance = float(client.get_asset_balance(asset='ETH')['free'])
        usdc_balance = float(client.get_asset_balance(asset='USDC')['free'])
        holding_eth = eth_balance > 0.01  # Mínimo arbitrario

        logging.debug(f"MA50: {ma50}, MA200: {ma200}, Holding ETH: {holding_eth}, USDC: {usdc_balance}")

        if ma50 > ma200 and not holding_eth and usdc_balance > 10:  # Señal alcista, comprar con 100%
            price = float(client.get_symbol_ticker(symbol=PAIR)['price'])
            info = client.get_symbol_info(PAIR)
            lot_filter = [f for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0]
            step_size = float(lot_filter['stepSize'])
            min_qty = float(lot_filter['minQty'])
            quantity = float(Decimal(str(usdc_balance / price * 0.995)).quantize(Decimal(str(step_size)), ROUND_DOWN))  # 0.995 para margen
            if quantity >= min_qty:
                order = client.create_order(
                    symbol=PAIR,
                    side='BUY',
                    type='MARKET',
                    quantity=quantity
                )
                logging.info(f"Comprado {quantity} ETH a {price}")
                send_telegram(f"Comprado {quantity} ETH (señal alcista MA crossover).")
        
        elif ma50 < ma200 and holding_eth:  # Señal bajista, vender 100%
            info = client.get_symbol_info(PAIR)
            lot_filter = [f for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0]
            step_size = float(lot_filter['stepSize'])
            quantity = float(Decimal(str(eth_balance)).quantize(Decimal(str(step_size)), ROUND_DOWN))
            if quantity >= float(lot_filter['minQty']):
                order = client.create_order(
                    symbol=PAIR,
                    side='SELL',
                    type='MARKET',
                    quantity=quantity
                )
                logging.info(f"Vendido {quantity} ETH")
                send_telegram(f"Vendido {quantity} ETH (señal bajista MA crossover).")
    
    except BinanceAPIException as e:
        logging.warning(f"Retry por error: {e}")
        time.sleep(1)  # Pequeño delay
        # Puedes agregar más retries si quieres, como en tu log original
    except Exception as e:
        logging.error(f"Error en estrategia: {e}")
        send_telegram(f"Error en estrategia: {e}")

# Ejecutar liquidación inicial una vez
liquidate_avax()

# Configurar scheduler para ejecutar cada 5 minutos
scheduler = BlockingScheduler()
scheduler.add_job(trade_strategy, 'interval', minutes=5)

# Ejecutar una vez al inicio
trade_strategy()

try:
    scheduler.start()
except (KeyboardInterrupt, SystemExit):
    pass
