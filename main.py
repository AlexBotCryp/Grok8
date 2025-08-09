```python
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
GROK_CONSULTA_FRECUENCIA = 5
consulta_contador = 0
_LAST_GROK_TS = 0

# Estado y clientes
client = Client(API_KEY, API_SECRET)
client_openai = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")

# Locks / caches / rate controls
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()  # Cache para símbolos inválidos
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
    global consulta_contador, _LAST_GROK_TS
    consulta_contador += 1
    now = time.time()
    if now - _LAST_GROK_TS < GROK_CONSULTA_FRECUENCIA * 60:
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
# Selección de criptos
# ──────────────────────────────────────────────────────────────────────────────
def mejores_criptos(max_candidates=30):
    try:
        tickers = retry(lambda: client.get_ticker())
        candidates = [
            t for t in tickers
            if t["symbol"].endswith(MONEDA_BASE)
            and not t["symbol"].startswith(MONEDA_BASE)
            and "DOWN" not in t["symbol"] and "UP" not in t["symbol"]
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
        return sorted(filtered, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
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
        saldo = safe_get_balance(MONEDA_BASE)
        logger.info(f"Saldo {MONEDA_BASE} disponible: {saldo:.2f}")
        if saldo < MIN_SALDO_COMPRA:
            logger.info("Saldo insuficiente para comprar.")
            return
        cantidad_usdc = saldo * PORCENTAJE_USDC
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
                    if Decimal(str(saldo)) >= min_quote:
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
                if rsi < RSI_BUY_MAX and ganancia_neta > 0:
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
                    logger.info(f"{symbol}: cantidad {float(qty):.8f} < marketMinQty {float(meta['marketMinQty']):.8f} (stepSize {float(meta['marketStepSize']):.8f}). Dust, saltando.")
                    dust_positions.append(symbol)
                    continue
                precio_ref = precio_actual
                if meta["applyToMarket"] and meta["minNotional"] > 0 and precio_ref > 0:
                    notional_est = qty * precio_ref
                    if notional_est < meta["minNotional"]:
                        logger.info(f"{symbol}: notional estimado {float(notional_est):.6f} < minNotional {float(meta['minNotional']):.6f}. Dust, saltando.")
                        dust_positions.append(symbol)
                        continue
                ganancia_bruta = float(qty) * (float(precio_actual) - float(precio_compra))
                comision_compra = float(precio_compra) * float(qty) * COMMISSION_RATE
                comision_venta = float(precio_actual) * float(qty) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                vender_por_stop = float(cambio) <= STOP_LOSS
                vender_por_profit = float(cambio) >= TAKE_PROFIT or rsi > RSI_SELL_MIN
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
                        comprar()
                    except BinanceAPIException as e:
                        logger.error(f"Error vendiendo {symbol}: {e}. Cantidad {float(qty):.8f}, marketMinQty {float(meta['marketMinQty']):.8f}, stepSize {float(meta['marketStepSize']):.8f}")
                        dust_positions.append(symbol)
                        continue
                else:
                    nuevos_registro[symbol] = data
                    logger.info(f"No se vende {symbol}: RSI {rsi:.2f}, Ganancia neta {ganancia_neta:.4f}")
            except (BinanceAPIException, TypeError, ValueError) as e:
                logger.error(f"Error vendiendo {symbol}: {e}")
                nuevos_registro[symbol] = data
            except Exception as e:
                logger.error(f"Error inesperado vendiendo {symbol}: {e}")
                nuevos_registro[symbol] = data
        limpio = {sym: d for sym, d in nuevos_registro.items() if sym not in dust_positions}
        guardar_json(limpio, REGISTRO_FILE)
        if dust_positions:
            enviar_telegram(f"🧹 Limpiado dust: {', '.join(dust_positions)}")

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
    enviar_telegram("🤖 Bot IA restaurado a la versión de alta frecuencia de esta mañana, con correcciones para LOT_SIZE, ganancia_neta, símbolos inválidos, precisión, manejo robusto de errores, y zona horaria correcta.")
    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', minutes=2, id="comprar")
    scheduler.add_job(vender_y_convertir, 'interval', minutes=3, id="vender")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl")
    scheduler.start()
    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
```
