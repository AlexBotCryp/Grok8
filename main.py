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
MIN_VOLUME = 1_000_000  # Mayor liquidez
MAX_POSICIONES = 4  # Menos posiciones
MIN_SALDO_COMPRA = 50  # Trades más grandes
PORCENTAJE_USDC = 0.25  # Más % por trade
ALLOWED_SYMBOLS = ['BTCUSDC', 'ETHUSDC', 'SOLUSDC', 'BNBUSDC', 'XRPUSDC', 'DOGEUSDC', 'ADAUSDC']  # Top por volumen 2025
# Estrategia
TAKE_PROFIT = 0.03  # 3%
STOP_LOSS = -0.02  # -2%
COMMISSION_RATE = 0.002  # Conservador con slippage
RSI_BUY_MAX = 50  # Menos estricto para comprar más frecuentemente
RSI_SELL_MIN = 55
MIN_NET_GAIN_ABS = 0.5  # Ganancia neta mínima absoluta
# Ritmo / límites
TRADE_COOLDOWN_SEC = 300  # 5 min para más movimiento
MAX_TRADES_PER_HOUR = 4  # Un poco más
# Riesgo diario
PERDIDA_MAXIMA_DIARIA = 50  # USDC
# Horarios
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = 23
# Archivos
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"
# Grok (x.ai)
GROK_CONSULTA_FRECUENCIA = 1  # Más frecuente
consulta_contador = 0
_LAST_GROK_TS = 0
# Yield on idle USDC
MIN_RESERVE_USDC = 100  # Reserva mínima en spot para operaciones inmediatas
USDC_PRODUCT_ID = None
# Estado y clientes
client = Client(API_KEY, API_SECRET)
client_openai = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
# Locks / caches / rate controls
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()
ULTIMA_COMPRA = {}
ULTIMAS_OPERACIONES = []
DUST_THRESHOLD = 1.0  # USDC para dust
# ──────────────────────────────────────────────────────────────────────────────
# Utilidades tiempo / JSON (sin cambios)
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
# Reintentos / Red (sin cambios)
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
# PnL diario / Riesgo (agrego fees estimadas)
# ──────────────────────────────────────────────────────────────────────────────
def actualizar_pnl_diario(realized_pnl, fees=0.1):  # Fee flat por trade
    with LOCK:
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        if today not in pnl_data:
            pnl_data[today] = 0
        pnl_data[today] += float(realized_pnl) - fees
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
# Yield management functions
# ──────────────────────────────────────────────────────────────────────────────
def get_usdc_flexible_product_id():
    global USDC_PRODUCT_ID
    if USDC_PRODUCT_ID is None:
        try:
            products = retry(lambda: client.get_savings_flexible_product_list(asset=MONEDA_BASE, status='ALL', featured='ALL', size=5))
            for product in products:
                if product['asset'] == MONEDA_BASE:
                    USDC_PRODUCT_ID = product['productId']
                    break
            if USDC_PRODUCT_ID is None:
                logger.error("No se encontró producto Flexible Savings para USDC.")
        except Exception as e:
            logger.error(f"Error obteniendo producto USDC: {e}")
    return USDC_PRODUCT_ID

def get_savings_balance():
    try:
        product_id = get_usdc_flexible_product_id()
        if product_id is None:
            return 0.0
        positions = retry(lambda: client.get_savings_flexible_product_position(asset=MONEDA_BASE))
        for pos in positions:
            if pos['productId'] == product_id:
                return float(pos['totalAmount'])
        return 0.0
    except Exception as e:
        logger.error(f"Error obteniendo balance en savings: {e}")
        return 0.0

def subscribe_to_savings(amount: float):
    if amount <= 0:
        return
    try:
        product_id = get_usdc_flexible_product_id()
        if product_id is None:
            return
        retry(lambda: client.savings_purchase_flexible(product_id=product_id, amount=amount))
        logger.info(f"Subscrito {amount:.2f} {MONEDA_BASE} a Flexible Savings.")
        enviar_telegram(f"💰 Subscrito {amount:.2f} {MONEDA_BASE} a yield (Flexible Savings).")
    except Exception as e:
        logger.error(f"Error subscribiendo a savings: {e}")

def redeem_from_savings(amount: float, redeem_type='FAST'):
    if amount <= 0:
        return
    try:
        product_id = get_usdc_flexible_product_id()
        if product_id is None:
            return
        retry(lambda: client.savings_flexible_redeem(product_id=product_id, amount=amount, type=redeem_type))
        logger.info(f"Redimido {amount:.2f} {MONEDA_BASE} de Flexible Savings ({redeem_type}).")
        enviar_telegram(f"💸 Redimido {amount:.2f} {MONEDA_BASE} de yield para trading.")
        time.sleep(5)  # Espera para que se refleje en balance spot
    except Exception as e:
        logger.error(f"Error redimiendo de savings: {e}")

def manage_savings():
    try:
        saldo_spot = safe_get_balance(MONEDA_BASE)
        saldo_savings = get_savings_balance()
        if saldo_spot > MIN_RESERVE_USDC + MIN_SALDO_COMPRA:
            excess = saldo_spot - MIN_RESERVE_USDC
            subscribe_to_savings(excess)
        elif saldo_spot < MIN_RESERVE_USDC and saldo_savings > 0:
            to_redeem = min(saldo_savings, MIN_RESERVE_USDC - saldo_spot)
            redeem_from_savings(to_redeem)
    except Exception as e:
        logger.error(f"Error en manage_savings: {e}")
# ──────────────────────────────────────────────────────────────────────────────
# Mercado: info símbolos y precisión (sin cambios)
# ──────────────────────────────────────────────────────────────────────────────
def load_symbol_info(symbol):
    if symbol in INVALID_SYMBOL_CACHE:
        return None
    if symbol in SYMBOL_CACHE:
        return SYMBOL_CACHE[symbol]
    try:
        info = client.get_symbol_info(symbol)
        if info is None:
            logger.info(f"Símbolo {symbol} no disponible en Binance")
            INVALID_SYMBOL_CACHE.add(symbol)
            return None
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        market_lot = next((f for f in info['filters'] if f['filterType'] == 'MARKET_LOT_SIZE'), None)
        pricef = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        notional_f = next((f for f in info['filters'] if f['filterType'] in ('NOTIONAL','MIN_NOTIONAL')), None)
        def D(x): return Decimal(x)
        meta = {
            "stepSize": D(lot['stepSize']),
            "minQty": D(lot.get('minQty', '0')),
            "marketStepSize": D(market_lot['stepSize']) if market_lot and D(market_lot['stepSize']) > 0 else D(lot['stepSize']),
            "marketMinQty": D(market_lot.get('minQty', lot.get('minQty', '0'))) if market_lot and D(market_lot.get('minQty', '0')) > 0 else D(lot.get('minQty', '0')),
            "tickSize": D(pricef['tickSize']),
            "minNotional": D(notional_f.get('minNotional', '0')) if notional_f else D('0'),
            "applyToMarket": bool(notional_f.get('applyToMarket', True)) if notional_f else True,
            "baseAsset": info['baseAsset'],
            "quoteAsset": info['quoteAsset'],
        }
        if meta["marketStepSize"] <= 0 or meta["marketMinQty"] <= 0:
            logger.warning(f"Valores inválidos para {symbol}: marketStepSize {meta['marketStepSize']}, marketMinQty {meta['marketMinQty']}. Usando LOT_SIZE como fallback.")
            meta["marketStepSize"] = meta["stepSize"]
            meta["marketMinQty"] = meta["minQty"]
        SYMBOL_CACHE[symbol] = meta
        return meta
    except Exception as e:
        logger.info(f"Error cargando info de {symbol}: {e}")
        INVALID_SYMBOL_CACHE.add(symbol)
        return None
def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        logger.warning(f"stepSize inválido: {step}. No se cuantiza.")
        return qty
    steps = (qty / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps * step).normalize()
def quantize_quote(quote: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        logger.warning(f"tickSize inválido: {tick}. No se cuantiza.")
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
    try:
        ticker = retry(lambda: client.get_ticker(symbol=symbol), tries=3, base_delay=0.5, exceptions=(Exception,))
        if ticker and float(ticker.get('lastPrice', 0)) <= 0:
            logger.info(f"Precio inválido (cero) para {symbol}")
            return None
        return ticker
    except Exception as e:
        logger.error(f"Error obteniendo ticker para {symbol}: {e}")
        return None
def safe_get_balance(asset):
    try:
        b = retry(lambda: client.get_asset_balance(asset=asset), tries=3, base_delay=0.5)
        if b is None:
            return 0.0
        return float(b.get('free', 0))
    except Exception as e:
        logger.error(f"Error obteniendo balance para {asset}: {e}")
        return 0.0
# ──────────────────────────────────────────────────────────────────────────────
# Indicadores (sin cambios)
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
# Grok helper (sin cambios)
# ──────────────────────────────────────────────────────────────────────────────
def consultar_grok(prompt):
    global consulta_contador, _LAST_GROK_TS
    consulta_contador += 1
    now = time.time()
    if now - _LAST_GROK_TS < GROK_CONSULTA_FRECUENCIA * 60:
        return "no"
    try:
        resp = client_openai.chat.completions.create(
            model="grok-beta",
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
# Registro posiciones / precio medio (sin cambios)
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
            cost_sum += qty * price + commission
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
                if asset != MONEDA_BASE and free > 0.0000001:
                    symbol = asset + MONEDA_BASE
                    if symbol not in ALLOWED_SYMBOLS:  # Solo top
                        continue
                    if symbol in INVALID_SYMBOL_CACHE:
                        logger.info(f"Omitiendo {symbol}: par no disponible en Binance (cache)")
                        continue
                    if not load_symbol_info(symbol):
                        logger.info(f"Omitiendo {symbol}: par no disponible en Binance")
                        continue
                    try:
                        t = safe_get_ticker(symbol)
                        if not t:
                            continue
                        precio_actual = float(t['lastPrice'])
                        pm = precio_medio_si_hay(symbol) or precio_actual
                        registro[symbol] = {
                            "cantidad": float(free),
                            "precio_compra": float(pm),
                            "timestamp": now_tz().isoformat(),
                            "from_cartera": True
                        }
                        logger.info(f"Posición inicial: {symbol} {free} a {pm} (last {precio_actual})")
                    except Exception:
                        continue
            guardar_json(registro, REGISTRO_FILE)
        except BinanceAPIException as e:
            logger.error(f"Error inicializando registro: {e}")
# ──────────────────────────────────────────────────────────────────────────────
# Nueva: Liquidar cartera al inicio
# ──────────────────────────────────────────────────────────────────────────────
def liquidar_cartera():
    enviar_telegram("🔥 Liquidando cartera actual para reinicio con estrategia mejorada.")
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        dust_positions = []
        for symbol, data in list(registro.items()):
            try:
                ticker = safe_get_ticker(symbol)
                if not ticker:
                    continue
                precio_actual = Decimal(str(ticker["lastPrice"]))
                meta = load_symbol_info(symbol)
                if not meta:
                    continue
                asset = symbol.replace(MONEDA_BASE, '')
                cantidad_wallet = Decimal(str(safe_get_balance(asset)))
                if cantidad_wallet <= 0:
                    dust_positions.append(symbol)
                    continue
                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                    dust_positions.append(symbol)
                    continue
                if meta["applyToMarket"] and meta["minNotional"] > 0:
                    notional_est = qty * precio_actual
                    if notional_est < meta["minNotional"]:
                        dust_positions.append(symbol)
                        continue
                # Fuerza venta, usa str(format(qty, 'f')) para evitar scientific notation
                orden = retry(lambda: client.order_market_sell(symbol=symbol, quantity=format(qty, 'f')), tries=2, base_delay=0.6)
                logger.info(f"Orden de liquidación: {orden}")
                precio_compra = Decimal(str(data["precio_compra"]))
                ganancia_bruta = float(qty) * (float(precio_actual) - float(precio_compra))
                comision_compra = float(precio_compra) * float(qty) * COMMISSION_RATE
                comision_venta = float(precio_actual) * float(qty) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                total_hoy = actualizar_pnl_diario(ganancia_neta)
                enviar_telegram(f"🔥 Liquidado {symbol} - PnL: {ganancia_neta:.2f} {MONEDA_BASE}. PnL hoy: {total_hoy:.2f}")
            except Exception as e:
                logger.error(f"Error liquidando {symbol}: {e}")
                dust_positions.append(symbol)
        limpio = {sym: d for sym, d in registro.items() if sym not in dust_positions}
        guardar_json(limpio, REGISTRO_FILE)
        if dust_positions:
            enviar_telegram(f"🧹 Dust liquidado: {', '.join(dust_positions)}")
# ──────────────────────────────────────────────────────────────────────────────
# Selección de criptos (filtrado a top, orden por volumen)
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos(max_candidates=10):
    try:
        tickers = retry(lambda: client.get_ticker())
        candidates = [
            t for t in tickers
            if t["symbol"] in ALLOWED_SYMBOLS  # Solo top
            and float(t.get("quoteVolume", 0)) > MIN_VOLUME
            and t["symbol"] not in INVALID_SYMBOL_CACHE
        ]
        filtered = []
        for t in candidates[:max_candidates]:
            symbol = t["symbol"]
            klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=15))
            closes = [float(k[4]) for k in klines]
            if len(closes) < 15:
                continue
            rsi = calculate_rsi(closes)
            precio = float(t["lastPrice"])
            ganancia_bruta = precio * TAKE_PROFIT
            comision_compra = precio * COMMISSION_RATE
            comision_venta = (precio * (1 + TAKE_PROFIT)) * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
            if ganancia_neta > 0:
                t['rsi'] = rsi
                filtered.append(t)
        return sorted(filtered, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)  # Por volumen
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
    try:
        saldo_spot = safe_get_balance(MONEDA_BASE)
        saldo_savings = get_savings_balance()
        saldo_total = saldo_spot + saldo_savings
        logger.info(f"Saldo total {MONEDA_BASE} (spot + savings): {saldo_total:.2f}")
        if saldo_total < MIN_SALDO_COMPRA:
            logger.info("Saldo total insuficiente para comprar.")
            return
        cantidad_usdc = saldo_total * PORCENTAJE_USDC
        if saldo_spot < cantidad_usdc:
            to_redeem = cantidad_usdc - saldo_spot
            if saldo_savings >= to_redeem:
                redeem_from_savings(to_redeem)
                saldo_spot = safe_get_balance(MONEDA_BASE)  # Actualizar
            else:
                cantidad_usdc = saldo_spot  # Usar lo disponible en spot
        criptos = mejores_criptos()
        registro = cargar_json(REGISTRO_FILE)
        if len(registro) >= MAX_POSICIONES:
            logger.info("Máximo de posiciones abiertas alcanzado. No se comprará más.")
            return
        compradas = 0
        now_ts = time.time()
        global ULTIMAS_OPERACIONES
        ULTIMAS_OPERACIONES = [t for t in ULTIMAS_OPERACIONES if now_ts - t < 3600]
        if len(ULTIMAS_OPERACIONES) >= MAX_TRADES_PER_HOUR:
            logger.info("Tope de operaciones por hora alcanzado. No se compra en este ciclo.")
            return
        for cripto in criptos:
            if compradas >= 1:
                break
            symbol = cripto["symbol"]
            if symbol in registro:
                continue
            last = ULTIMA_COMPRA.get(symbol, 0)
            if now_ts - last < TRADE_COOLDOWN_SEC:
                logger.info(f"{symbol}: en cooldown de compra.")
                continue
            try:
                ticker = safe_get_ticker(symbol)
                if not ticker:
                    continue
                precio = Decimal(str(ticker["lastPrice"]))
                if precio <= 0:
                    logger.info(f"{symbol}: precio inválido ({float(precio):.6f}). Saltando.")
                    continue
                rsi = cripto.get("rsi", 50)
                meta = load_symbol_info(symbol)
                if not meta:
                    continue
                min_quote = min_quote_for_market(symbol, precio)
                quote_to_spend = Decimal(str(cantidad_usdc))
                if quote_to_spend < min_quote:
                    if Decimal(str(saldo_spot)) >= min_quote:
                        quote_to_spend = min_quote
                    else:
                        logger.info(f"{symbol}: no alcanza minNotional ({float(min_quote):.2f} {MONEDA_BASE}). Saltando.")
                        continue
                quote_to_spend = quantize_quote(quote_to_spend, meta["tickSize"])
                cantidad = float(quote_to_spend) / float(precio)
                ganancia_bruta = float(precio) * cantidad * TAKE_PROFIT
                comision_compra = float(precio) * cantidad * COMMISSION_RATE
                comision_venta = float(precio) * (1 + TAKE_PROFIT) * cantidad * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
                if rsi < RSI_BUY_MAX and ganancia_neta > MIN_NET_GAIN_ABS:
                    orden = retry(
                        lambda: client.create_order(
                            symbol=symbol,
                            side="BUY",
                            type="MARKET",
                            quoteOrderQty=float(quote_to_spend)
                        ),
                        tries=2, base_delay=0.6
                    )
                    logger.info(f"Orden de compra: {orden}")
                    with LOCK:
                        registro = cargar_json(REGISTRO_FILE)
                        registro[symbol] = {
                            "cantidad": cantidad,
                            "precio_compra": float(precio),
                            "timestamp": now_tz().isoformat()
                        }
                        guardar_json(registro, REGISTRO_FILE)
                    enviar_telegram(f"🟢 Comprado {symbol} por {float(quote_to_spend):.2f} {MONEDA_BASE} a ~{float(precio):.6f}. RSI: {rsi:.2f}")
                    compradas += 1
                    ULTIMA_COMPRA[symbol] = now_ts
                    ULTIMAS_OPERACIONES.append(now_ts)
                else:
                    logger.info(f"No se compra {symbol}: RSI {rsi:.2f}, Ganancia neta {ganancia_neta:.4f}")
            except BinanceAPIException as e:
                logger.error(f"Error comprando {symbol}: {e}")
                continue
            except Exception as e:
                logger.error(f"Error inesperado comprando {symbol}: {e}")
                continue
        manage_savings()  # Después de comprar, manejar excess
    except Exception as e:
        logger.error(f"Error general en compra: {e}")
def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        nuevos_registro = {}
        dust_positions = []
        saldo_usdc_antes = safe_get_balance(MONEDA_BASE)
        logger.info(f"Saldo {MONEDA_BASE} antes de vender: {saldo_usdc_antes:.2f}")
        for symbol, data in list(registro.items()):
            try:
                precio_compra = Decimal(str(data["precio_compra"]))
                ticker = safe_get_ticker(symbol)
                if not ticker:
                    nuevos_registro[symbol] = data
                    continue
                precio_actual = Decimal(str(ticker["lastPrice"]))
                cambio = (precio_actual - precio_compra) / precio_compra
                klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=15))
                closes = [float(k[4]) for k in klines]
                rsi = calculate_rsi(closes)
                meta = load_symbol_info(symbol)
                if not meta:
                    nuevos_registro[symbol] = data
                    continue
                asset = symbol.replace(MONEDA_BASE, '')
                cantidad_wallet = Decimal(str(safe_get_balance(asset)))
                if cantidad_wallet <= 0:
                    logger.info(f"{symbol}: saldo disponible {float(cantidad_wallet):.8f}. Dust, saltando.")
                    dust_positions.append(symbol)
                    continue
                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                    logger.info(f"{symbol}: cantidad {float(qty):.8f} < marketMinQty {float(meta['marketMinQty']):.8f}. Dust, saltando.")
                    dust_positions.append(symbol)
                    continue
                if meta["applyToMarket"] and meta["minNotional"] > 0 and precio_actual > 0:
                    notional_est = qty * precio_actual
                    if notional_est < meta["minNotional"] or float(notional_est) < DUST_THRESHOLD:
                        logger.info(f"{symbol}: notional {float(notional_est):.6f} < threshold. Dust.")
                        dust_positions.append(symbol)
                        continue
                ganancia_bruta = float(qty) * (float(precio_actual) - float(precio_compra))
                comision_compra = float(precio_compra) * float(qty) * COMMISSION_RATE
                comision_venta = float(precio_actual) * float(qty) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                vender_por_stop = float(cambio) <= STOP_LOSS
                vender_por_profit = (float(cambio) >= TAKE_PROFIT or rsi > RSI_SELL_MIN) and ganancia_neta > MIN_NET_GAIN_ABS
                if vender_por_stop or vender_por_profit:
                    try:
                        orden = retry(lambda: client.order_market_sell(symbol=symbol, quantity=float(qty)), tries=2, base_delay=0.6)
                        logger.info(f"Orden de venta: {orden}")
                        total_hoy = actualizar_pnl_diario(ganancia_neta)
                        motivo = "Stop-loss" if vender_por_stop else "Take-profit/RSI"
                        enviar_telegram(
                            f"🔴 Vendido {symbol} - {float(qty):.8f} a ~{float(precio_actual):.6f} "
                            f"(Cambio: {float(cambio)*100:.2f}%) PnL: {ganancia_neta:.2f} {MONEDA_BASE}. "
                            f"Motivo: {motivo}. RSI: {rsi:.2f}. PnL hoy: {total_hoy:.2f}"
                        )
                    except BinanceAPIException as e:
                        logger.error(f"Error vendiendo {symbol}: {e}")
                        dust_positions.append(symbol)
                        continue
                else:
                    nuevos_registro[symbol] = data
                    logger.info(f"No se vende {symbol}: RSI {rsi:.2f}, Ganancia neta {ganancia_neta:.4f}")
            except Exception as e:
                logger.error(f"Error vendiendo {symbol}: {e}")
                nuevos_registro[symbol] = data
        limpio = {sym: d for sym, d in nuevos_registro.items() if sym not in dust_positions}
        guardar_json(limpio, REGISTRO_FILE)
        if dust_positions:
            enviar_telegram(f"🧹 Limpiado dust: {', '.join(dust_positions)}")
        # Rotación con Grok si saldo bajo
        saldo_spot = safe_get_balance(MONEDA_BASE)
        saldo_savings = get_savings_balance()
        saldo_total = saldo_spot + saldo_savings
        if saldo_total < MIN_SALDO_COMPRA:
            criptos = mejores_criptos()
            if criptos:
                candidates = [c for c in criptos if c['symbol'] not in limpio]
                if candidates:
                    best = candidates[0]
                    best_symbol = best['symbol']
                    best_rsi = best['rsi']
                    best_change = best.get('priceChangePercent', '0')
                    pos_perfs = []
                    for sym, data in limpio.items():
                        ticker = safe_get_ticker(sym)
                        if not ticker:
                            continue
                        price = float(ticker['lastPrice'])
                        buy_price = data['precio_compra']
                        change = (price - buy_price) / buy_price
                        klines = retry(lambda: client.get_klines(symbol=sym, interval=Client.KLINE_INTERVAL_1HOUR, limit=15))
                        closes = [float(k[4]) for k in klines]
                        rsi = calculate_rsi(closes)
                        qty = data['cantidad']
                        ganancia_bruta = qty * (price - buy_price)
                        comision_compra = buy_price * qty * COMMISSION_RATE
                        comision_venta = price * qty * COMMISSION_RATE
                        ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                        pos_perfs.append((sym, change, rsi, ganancia_neta))
                    if pos_perfs:
                        pos_perfs.sort(key=lambda x: x[1])  # Peor primero
                        worst_sym, worst_change, worst_rsi, worst_net = pos_perfs[0]
                        prompt = f"Debo vender {worst_sym} con RSI {worst_rsi:.2f}, ganancia neta {worst_net:.4f} para liberar fondos y comprar {best_symbol} con RSI {best_rsi:.2f} y cambio {best_change}%? Responde solo con 'si' o 'no'."
                        respuesta = consultar_grok(prompt)
                        if 'si' in respuesta:
                            try:
                                meta = load_symbol_info(worst_sym)
                                asset = worst_sym.replace(MONEDA_BASE, '')
                                cantidad_wallet = Decimal(str(safe_get_balance(asset)))
                                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                                    del limpio[worst_sym]
                                    guardar_json(limpio, REGISTRO_FILE)
                                    return
                                orden = retry(lambda: client.order_market_sell(symbol=worst_sym, quantity=float(qty)))
                                logger.info(f"Orden de venta por rotación: {orden}")
                                total_hoy = actualizar_pnl_diario(worst_net)
                                enviar_telegram(f"🔄 Vendido {worst_sym} por rotación - PnL: {worst_net:.2f} {MONEDA_BASE}. RSI: {worst_rsi:.2f}. Para comprar {best_symbol}.")
                                del limpio[worst_sym]
                                guardar_json(limpio, REGISTRO_FILE)
                            except Exception as e:
                                logger.error(f"Error en venta por rotación {worst_sym}: {e}")
        manage_savings()  # Después de vender, manejar excess
def resumen_diario():
    try:
        cuenta = retry(lambda: client.get_account())
        saldo_savings = get_savings_balance()
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy_v = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy_v:.2f} {MONEDA_BASE} (fees estimadas incluidas)\nBalances:\n"
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0.001:
                mensaje += f"{b['asset']}: {total:.6f}\n"
        mensaje += f"{MONEDA_BASE} in Savings: {saldo_savings:.2f}\n"
        enviar_telegram(mensaje)
        seven_days_ago = (now_tz() - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error en resumen diario: {e}")
# ──────────────────────────────────────────────────────────────────────────────
# Inicio
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    inicializar_registro()
    liquidar_cartera()  # Vende todo al inicio
    manage_savings()  # Inicializar savings
    enviar_telegram("🤖 Bot IA mejorado: Más movimiento, yield en USDC idle via Flexible Savings (~11-12% APR), liquidación inicial completada.")
    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', minutes=10, id="comprar")
    scheduler.add_job(vender_y_convertir, 'interval', minutes=10, id="vender")
    scheduler.add_job(manage_savings, 'interval', minutes=10, id="manage_savings")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl")
    scheduler.start()
    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
