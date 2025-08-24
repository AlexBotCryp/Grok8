# -*- coding: utf-8 -*-
# BOT CRIPTO AVANZADO CON GROK, RSI, ROTACIÓN INTELIGENTE Y REINVERSIÓN
# Versión optimizada para Render con recompra automática y gestión de saldo libre

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

# Configuración
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MONEDA_BASE = "USDC"
PORCENTAJE_USDC = Decimal("0.9")
MIN_SALDO_COMPRA = 10
MIN_PROFIT = 0.004  # 0.4%
TRAILING_STOP = 0.015
MAX_POSICIONES = 2
PNL_FILE = "pnl.json"
REGISTRO_FILE = "registro.json"
TZ = pytz.timezone("Europe/Madrid")

client = Client(API_KEY, API_SECRET)
LOCK = threading.RLock()

# Utilidades
def now_tz():
    return datetime.now(TZ)

def cargar_json(f):
    if os.path.exists(f):
        with open(f) as j:
            try: return json.load(j)
            except: pass
    return {}

def guardar_json(data, f):
    with open(f, "w") as j:
        json.dump(data, j, indent=2)

def enviar_telegram(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4000]})
        except Exception as e:
            logger.warning(f"Telegram falló: {e}")

def retry(fn, tries=3, delay=0.6):
    for _ in range(tries):
        try:
            return fn()
        except: time.sleep(delay)
    return None

def get_price(symbol):
    t = retry(lambda: client.get_symbol_ticker(symbol=symbol))
    return Decimal(t["price"]) if t else Decimal("0")

def get_balance(asset):
    b = retry(lambda: client.get_asset_balance(asset))
    return Decimal(b["free"]) if b else Decimal("0")

def get_symbol_info(symbol):
    info = client.get_symbol_info(symbol)
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = Decimal(f["stepSize"])
        if f["filterType"] == "MIN_NOTIONAL":
            min_notional = Decimal(f["minNotional"])
    return step, min_notional

def redondear(qty, step):
    return (qty // step) * step

def calcular_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed > 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else np.inf
    rsi = 100 - (100 / (1 + rs))
    return float(rsi)

def mejores_criptos(max_candidates=10):
    symbols = [s for s in client.get_all_tickers() if s["symbol"].endswith(MONEDA_BASE)]
    symbols = sorted(symbols, key=lambda x: float(x["price"]), reverse=True)
    return [s["symbol"] for s in symbols if "UP" not in s["symbol"] and "DOWN" not in s["symbol"]][:max_candidates]

# Lógica
def comprar():
    with LOCK:
        saldo_spot = get_balance(MONEDA_BASE)
        if saldo_spot < MIN_SALDO_COMPRA:
            logger.info(f"No hay saldo suficiente para comprar: {saldo_spot}")
            return

        registro = cargar_json(REGISTRO_FILE)
        en_uso = list(registro.keys())
        candidatos = mejores_criptos(max_candidates=10)
        disponibles = [s for s in candidatos if s not in en_uso]
        if not disponibles and len(en_uso) < MAX_POSICIONES:
            disponibles = candidatos  # Rotar

        if not disponibles:
            logger.info("No hay criptos disponibles para comprar.")
            return

        symbol = random.choice(disponibles)
        price = get_price(symbol)
        step, min_notional = get_symbol_info(symbol)
        invertir = saldo_spot * PORCENTAJE_USDC
        qty = redondear(invertir / price, step)

        if qty * price < min_notional:
            logger.info(f"{symbol}: qty {qty} * price {price} menor que min_notional")
            return

        try:
            order = client.order_market_buy(symbol=symbol, quantity=float(qty))
            enviar_telegram(f"🟢 COMPRA: {symbol} | Cantidad: {qty} | Precio: {price}")
            registro[symbol] = {
                "qty": float(qty),
                "buy_price": float(price),
                "timestamp": time.time()
            }
            guardar_json(registro, REGISTRO_FILE)
        except Exception as e:
            logger.error(f"Error al comprar {symbol}: {e}")

        if saldo_spot > MIN_SALDO_COMPRA:
            enviar_telegram(f"⚠️ Queda saldo libre sin invertir: {saldo_spot} {MONEDA_BASE}")

def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        if not registro:
            logger.info("Sin posiciones abiertas.")
            return

        vendidos = []
        for symbol, data in list(registro.items()):
            qty = Decimal(data["qty"])
            buy_price = Decimal(data["buy_price"])
            time_held = (time.time() - data["timestamp"]) / 60
            price = get_price(symbol)
            step, _ = get_symbol_info(symbol)
            qty = redondear(qty, step)

            if qty <= 0: continue

            net_gain = (price - buy_price) / buy_price
            trailing = TRAILING_STOP if net_gain > TRAILING_STOP else 0

            if net_gain >= MIN_PROFIT or net_gain < -trailing or time_held > 5:
                try:
                    order = client.order_market_sell(symbol=symbol, quantity=float(qty))
                    base = symbol.replace(MONEDA_BASE, "")
                    enviar_telegram(f"🔴 VENTA: {symbol} | Ganancia: {net_gain*100:.2f}%")
                    vendidos.append(symbol)
                except Exception as e:
                    logger.error(f"Error al vender {symbol}: {e}")

        for s in vendidos:
            registro.pop(s, None)

        guardar_json(registro, REGISTRO_FILE)

def resumen_diario():
    saldo = get_balance(MONEDA_BASE)
    registro = cargar_json(REGISTRO_FILE)
    msg = f"📊 Resumen {now_tz().strftime('%Y-%m-%d')}:\nSaldo USDC: {saldo:.2f}\n"
    for s, d in registro.items():
        msg += f"- {s}: {d['qty']} comprados a {d['buy_price']}\n"
    enviar_telegram(msg)

# Main
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(comprar, 'interval', minutes=5)
    scheduler.add_job(vender_y_convertir, 'interval', minutes=5)
    scheduler.add_job(resumen_diario, 'cron', hour=23, minute=0)
    scheduler.start()
    enviar_telegram("🤖 Bot iniciado y ejecutándose.")
    while True:
        time.sleep(60)
