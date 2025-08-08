# -*- coding: utf-8 -*-
import os
import time
import json
import math
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
MIN_VOLUME = 50_000           # volumen cotizado mínimo para considerar par
MAX_POSICIONES = 7            # máximo de posiciones abiertas
MIN_SALDO_COMPRA = 5          # no operar por debajo de esto
PORCENTAJE_USDC = 0.30        # % del saldo disponible por operación (cap abajo)
MAX_POR_ORDEN = 0.15          # cap por orden del saldo (adicional de seguridad)

# Estrategia
TAKE_PROFIT = 0.03            # 3%
STOP_LOSS = -0.03             # -3%
COMMISSION_RATE = 0.001       # 0.1% aprox.
RSI_BUY_MAX = 45              # comprar si RSI < 45 (o momentum fuerte)
RSI_SELL_MIN = 65             # vender si RSI > 65 (además de TP/SL)

# Riesgo diario
PERDIDA_MAXIMA_DIARIA = 50    # en USDC

# Horarios
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = 23             # 23:00 Madrid

# Archivos persistencia
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

# Grok (x.ai) - cooldown por tiempo
GROK_COOLDOWN = 60 * 5        # 5 min
_LAST_GROK_TS = 0

# Estado y clientes
client = Client(API_KEY, API_SECRET)
client_openai = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")

# Lock intra-proceso para evitar carreras entre jobs
LOCK = threading.RLock()

# Cache símbolos
SYMBOL_CACHE = {}

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
    # atómico
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
    info = retry(lambda: client.get_symbol_info(symbol))
    lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
    pricef = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
    min_notional_f = next((f for f in info['filters'] if f['filterType'] in ('MIN_NOTIONAL', 'NOTIONAL')), None)
    meta = {
        "stepSize": Decimal(lot['stepSize']),
        "tickSize": Decimal(pricef['tickSize']),
        "minNotional": Decimal(min_notional_f.get('minNotional', '0.0')) if min_notional_f else Decimal('0'),
        "baseAsset": info['baseAsset'],
        "quoteAsset": info['quoteAsset'],
    }
    SYMBOL_CACHE[symbol] = meta
    return meta

def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    # baja al múltiplo permitido
    if step <= 0:
        return qty
    steps = (qty / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * step).normalize()

def meets_min_notional(symbol, price: Decimal, qty: Decimal) -> bool:
    meta = load_symbol_info(symbol)
    notional = price * qty
    return notional >= meta['minNotional']

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
def rsi_wilder(closes, period=14):
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
    global _LAST_GROK_TS
    now = time.time()
    if now - _LAST_GROK_TS < GROK_COOLDOWN:
        return "no"
    try:
        resp = client_openai.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0
        )
        _LAST_GROK_TS = time.time()
        return (resp.choices[0].message.content or "").strip().lower()
    except Exception as e:
        logger.error(f"Error Grok: {e}")
        return "no"

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
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        try:
            cuenta = retry(lambda: client.get_account())
            for b in cuenta['balances']:
                asset = b['asset']
                free = float(b['free'])
                if asset != MONEDA_BASE and free > 0.0001:
                    symbol = asset + MONEDA_BASE
                    # filtra pares raros/ilíquidos
                    try:
                        t = safe_get_ticker(symbol)
                    except Exception:
                        continue
                    precio_actual = float(t['lastPrice'])
                    if symbol not in registro:
                        pm = precio_medio_si_hay(symbol) or precio_actual
                        registro[symbol] = {
                            "cantidad": round(free, 8),
                            "precio_compra": float(pm),
                            "timestamp": now_tz().isoformat(),
                            "from_cartera": True
                        }
                        logger.info(f"Posición agregada: {symbol} {free} a {pm} (actual {precio_actual})")
            guardar_json(registro, REGISTRO_FILE)
        except BinanceAPIException as e:
            logger.error(f"Error inicializando registro: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Selección de criptos
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos(max_candidates=30):
    try:
        tickers = retry(lambda: client.get_ticker())
        candidates = [
            t for t in tickers
            if t["symbol"].endswith(MONEDA_BASE)
            and not t["symbol"].startswith(MONEDA_BASE)         # evita pares USDCUSDC etc
            and "DOWN" not in t["symbol"] and "UP" not in t["symbol"]  # evita leveraged
            and float(t.get("quoteVolume", 0)) > MIN_VOLUME
        ]
        filtered = []
        for t in candidates[:max_candidates]:
            symbol = t["symbol"]
            klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=60))
            closes = [float(k[4]) for k in klines]
            if len(closes) < 20:
                continue
            rsi = rsi_wilder(closes)
            precio = float(t["lastPrice"])
            # Ganancia neta estimada con comisiones
            ganancia_bruta = precio * TAKE_PROFIT
            comision_compra = precio * COMMISSION_RATE
            comision_venta = (precio * (1 + TAKE_PROFIT)) * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
            if ganancia_neta > 0:
                t['rsi'] = rsi
                filtered.append(t)
        # Ordena por momentum (subida %) y RSI bajo (o momentum alto)
        return sorted(filtered, key=lambda x: (float(x.get("priceChangePercent", 0)), -x.get('rsi', 50)), reverse=True)
    except BinanceAPIException as e:
        logger.error(f"Error obteniendo tickers: {e}")
        return []

# ──────────────────────────────────────────────────────────────────────────────
# Trading
# ──────────────────────────────────────────────────────────────────────────────
def comprar():
    if not puede_comprar():
        logger.info("Límite de pérdida diaria alcanzado. No se comprará más hoy.")
        return
    with LOCK:
        try:
            saldo = safe_get_balance(MONEDA_BASE)
            logger.info(f"Saldo {MONEDA_BASE} disponible: {saldo:.2f}")
            if saldo < MIN_SALDO_COMPRA:
                logger.info("Saldo insuficiente para comprar.")
                return

            # sizing por orden
            cantidad_usdc = min(saldo * PORCENTAJE_USDC, saldo * MAX_POR_ORDEN)

            criptos = mejores_criptos()
            registro = cargar_json(REGISTRO_FILE)
            if len(registro) >= MAX_POSICIONES:
                logger.info("Máximo de posiciones abiertas alcanzado.")
                return

            compradas = 0
            for cripto in criptos:
                if compradas >= 1:  # 1 compra por ciclo
                    break
                symbol = cripto["symbol"]
                if symbol in registro:
                    continue

                try:
                    ticker = safe_get_ticker(symbol)
                    precio = Decimal(str(ticker["lastPrice"]))
                    change_percent = float(cripto.get("priceChangePercent", 0))
                    volume = float(cripto.get("quoteVolume", 0))
                    rsi = float(cripto.get("rsi", 50))

                    meta = load_symbol_info(symbol)
                    qty_raw = Decimal(str(cantidad_usdc)) / precio
                    qty = quantize_qty(qty_raw, meta["stepSize"])
                    if qty <= Decimal('0'):
                        continue
                    # Asegura minNotional
                    if not meets_min_notional(symbol, precio, qty):
                        min_qty = (meta["minNotional"] / precio).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                        qty = quantize_qty(min_qty, meta["stepSize"])
                        if qty <= Decimal('0'):
                            logger.info(f"{symbol} no cumple minNotional. Saltando.")
                            continue

                    # chequeo de señal (RSI bajo o momentum) + Grok
                    prompt = (
                        f"Analiza {symbol}: Precio {float(precio):.6f}, Cambio {change_percent:.2f}%, "
                        f"Volumen {volume:.2f}, RSI {rsi:.2f}. ¿Comprar con {cantidad_usdc:.2f} {MONEDA_BASE}? "
                        f"Responde solo 'sí' o 'no'. Prioriza RSI < {RSI_BUY_MAX}."
                    )
                    grok_response = consultar_grok(prompt)

                    # econ estimada
                    ganancia_bruta = float(precio) * float(qty) * TAKE_PROFIT
                    comision_compra = float(precio) * float(qty) * COMMISSION_RATE
                    comision_venta = float(precio) * (1 + TAKE_PROFIT) * float(qty) * COMMISSION_RATE
                    ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)

                    cond_compra = ((rsi < RSI_BUY_MAX) or (change_percent > 0.5))
                    if cond_compra and (ganancia_neta > 0) and ('sí' in grok_response or (time.time() - _LAST_GROK_TS < GROK_COOLDOWN)):
                        orden = retry(lambda: client.order_market_buy(symbol=symbol, quantity=float(qty)), tries=2, base_delay=0.6)
                        logger.info(f"Orden de compra: {orden}")
                        registro[symbol] = {
                            "cantidad": float(qty),
                            "precio_compra": float(precio),
                            "timestamp": now_tz().isoformat()
                        }
                        guardar_json(registro, REGISTRO_FILE)
                        enviar_telegram(f"🟢 Comprado {symbol} - {float(qty):.8f} a {float(precio):.6f} {MONEDA_BASE}. RSI: {rsi:.2f}. Grok: {grok_response}")
                        compradas += 1
                    else:
                        logger.info(f"No se compra {symbol}: RSI {rsi:.2f}, Grok: {grok_response}, Ganancia neta {ganancia_neta:.4f}")
                except BinanceAPIException as e:
                    logger.error(f"Error comprando {symbol}: {e}")
                    continue
        except BinanceAPIException as e:
            logger.error(f"Error general en compra: {e}")

def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        nuevos_registro = {}
        saldo_usdc_antes = safe_get_balance(MONEDA_BASE)
        logger.info(f"Saldo {MONEDA_BASE} antes de vender: {saldo_usdc_antes:.2f}")

        for symbol, data in list(registro.items()):
            try:
                cantidad_reg = Decimal(str(data["cantidad"]))
                precio_compra = Decimal(str(data["precio_compra"]))
                ticker = safe_get_ticker(symbol)
                precio_actual = Decimal(str(ticker["lastPrice"]))
                cambio = (precio_actual - precio_compra) / precio_compra

                # RSI actual
                klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=60))
                closes = [float(k[4]) for k in klines]
                rsi = rsi_wilder(closes)

                prompt = (
                    f"Para {symbol}: Precio compra {float(precio_compra):.6f}, actual {float(precio_actual):.6f}, "
                    f"cambio {float(cambio)*100:.2f}%, RSI {rsi:.2f}. ¿Vender ahora? Prioriza RSI > {RSI_SELL_MIN} "
                    f"o ganancia >= {TAKE_PROFIT*100:.1f}%. Responde solo 'sí' o 'no'."
                )
                grok_response = consultar_grok(prompt)

                ganancia_bruta = float(cantidad_reg) * (float(precio_actual) - float(precio_compra))
                comision_compra = float(precio_compra) * float(cantidad_reg) * COMMISSION_RATE
                comision_venta = float(precio_actual) * float(cantidad_reg) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - comision_compra - comision_venta

                vender_por_stop = float(cambio) <= STOP_LOSS
                vender_por_profit = (float(cambio) >= TAKE_PROFIT or rsi > RSI_SELL_MIN or 'sí' in grok_response) and ganancia_neta > 0

                if vender_por_stop or vender_por_profit:
                    meta = load_symbol_info(symbol)
                    asset = symbol.replace(MONEDA_BASE, '')
                    cantidad_wallet = Decimal(str(safe_get_balance(asset)))
                    qty = quantize_qty(min(cantidad_reg, cantidad_wallet), meta["stepSize"])
                    if qty <= Decimal('0'):
                        logger.info(f"No hay saldo suficiente para vender {symbol}")
                        continue
                    orden = retry(lambda: client.order_market_sell(symbol=symbol, quantity=float(qty)), tries=2, base_delay=0.6)
                    logger.info(f"Orden de venta: {orden}")
                    realized_pnl = ganancia_neta
                    total_hoy = actualizar_pnl_diario(realized_pnl)
                    motivo = "Stop-loss" if vender_por_stop else "Take-profit/RSI/Grok"
                    enviar_telegram(
                        f"🔴 Vendido {symbol} - {float(qty):.8f} a {float(precio_actual):.6f} "
                        f"(Cambio: {float(cambio)*100:.2f}%) PnL: {realized_pnl:.2f} {MONEDA_BASE}. "
                        f"Motivo: {motivo}. RSI: {rsi:.2f}. PnL hoy: {total_hoy:.2f}"
                    )
                    # Intentar rotar a otra oportunidad
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
        cuenta = retry(lambda: client.get_account())
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy_v = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy_v:.2f} {MONEDA_BASE}\nBalances:\n"
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0.001:
                mensaje += f"{b['asset']}: {total:.6f}\n"
        enviar_telegram(mensaje)

        # conservar solo últimos 7 días
        seven_days_ago = (now_tz() - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error en resumen diario: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Inicio
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Inicializar registro en base a cartera existente
    inicializar_registro()

    enviar_telegram("🤖 Bot IA activo: RSI(Wilder), control de riesgo diario, sizing seguro y verificación de mínimos Binance.")

    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    # Jobs cada 3 minutos, con lock interno para no pisarse
    scheduler.add_job(comprar, 'interval', minutes=3, id="comprar")
    scheduler.add_job(vender_y_convertir, 'interval', minutes=3, seconds=90, id="vender")  # desfase para evitar coincidencia exacta
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl")
    scheduler.start()

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
