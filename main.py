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

# Logging — DEBUG for tracking
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia")

# Config — for 160 USDC, momentum scalping
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""
MONEDA_BASE = "USDC"
MIN_VOLUME = 100_000
MAX_POSICIONES = 5
MIN_SALDO_COMPRA = 5
PORCENTAJE_USDC = 0.2
ALLOWED_SYMBOLS = ['DOGEUSDC', 'SHIBUSDC', 'ADAUSDC', 'XRPUSDC', 'MATICUSDC', 'TRXUSDC', 'VETUSDC', 'HBARUSDC']
TAKE_PROFIT = 0.012
STOP_LOSS = -0.01
COMMISSION_RATE = 0.001
RSI_BUY_MAX = 45
RSI_SELL_MIN = 55
MIN_NET_GAIN_ABS = 0.2
TRAILING_STOP = 0.005
TRADE_COOLDOWN_SEC = 15
MAX_TRADES_PER_HOUR = 40
PERDIDA_MAXIMA_DIARIA = 30
TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = 23
REGISTRO_FILE = "registro.json"
PNL_DIARIO_FILE = "pnl_diario.json"

client = Client(API_KEY, API_SECRET)
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()
ULTIMA_COMPRA = {}
ULTIMAS_OPERACIONES = []
DUST_THRESHOLD = 0.5
TICKERS_CACHE = {}

def get_cached_ticker(symbol):
    now = time.time()
    if symbol in TICKERS_CACHE and now - TICKERS_CACHE[symbol]['ts'] < 60:
        logger.debug(f"Usando ticker cacheado para {symbol}")
        return TICKERS_CACHE[symbol]['data']
    try:
        t = retry(lambda: client.get_ticker(symbol=symbol), tries=5, base_delay=0.5)
        if t and float(t.get('lastPrice', 0)) > 0:
            TICKERS_CACHE[symbol] = {'data': t, 'ts': now}
            logger.debug(f"Ticker actualizado para {symbol}: {t['lastPrice']}")
            return t
    except Exception as e:
        logger.error(f"Error cache ticker {symbol}: {e}")
    return None

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

def enviar_telegram(mensaje: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM DESACTIVADO] {mensaje}")
        return
    try:
        def _send():
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje[:4000]}
            )
            resp.raise_for_status()
        retry(_send, tries=3, base_delay=0.8)
    except Exception as e:
        logger.error(f"Telegram falló: {e}")

def retry(fn, tries=5, base_delay=0.7, jitter=0.3, exceptions=(Exception,)):
    for i in range(tries):
        try:
            return fn()
        except exceptions as e:
            logger.warning(f"Retry {i+1}/{tries} falló: {e}")
            if i == tries - 1:
                raise
            time.sleep(base_delay * (2 ** i) + random.random() * jitter)

def debug_balances():
    try:
        cuenta = retry(lambda: client.get_account(), tries=10, base_delay=1.0)
        logger.info("=== BALANCES EN CARTERA ===")
        total_value = 0
        for b in cuenta['balances']:
            total = float(b['free']) + float(b['locked'])
            if total > 0.0001:
                logger.info(f"{b['asset']}: free={b['free']}, locked={b['locked']}, total={total}")
                if b['asset'] == MONEDA_BASE:
                    total_value += total
                elif b['asset'] + MONEDA_BASE in ALLOWED_SYMBOLS:
                    ticker = safe_get_ticker(b['asset'] + MONEDA_BASE)
                    if ticker:
                        total_value += total * float(ticker['lastPrice'])
        logger.info(f"Valor total estimado: {total_value:.2f} {MONEDA_BASE}")
        enviar_telegram(f"🤖 Inicio bot - Valor total cartera: {total_value:.2f} {MONEDA_BASE}")
        return total_value
    except Exception as e:
        logger.error(f"Error debug balances: {e}")
        return 0

def actualizar_pnl_diario(realized_pnl, fees=0.1):
    with LOCK:
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        if today not in pnl_data:
            pnl_data[today] = 0
        pnl_data[today] += float(realized_pnl) - fees
        guardar_json(pnl_data, PNL_DIARIO_FILE)
        logger.debug(f"PnL actualizado: {today} = {pnl_data[today]}")
        return pnl_data[today]

def pnl_hoy():
    pnl_data = cargar_json(PNL_DIARIO_FILE)
    return pnl_data.get(get_current_date(), 0)

def puede_comprar():
    hoy_pnl = pnl_hoy()
    can_buy = hoy_pnl > -PERDIDA_MAXIMA_DIARIA if hoy_pnl < 0 else hoy_pnl > -(PERDIDA_MAXIMA_DIARIA * 1.5)
    logger.debug(f"Puede comprar? {can_buy}, PnL hoy: {hoy_pnl}, Límite: {PERDIDA_MAXIMA_DIARIA}")
    return can_buy

def reset_diario():
    with LOCK:
        pnl = cargar_json(PNL_DIARIO_FILE)
        hoy = get_current_date()
        if hoy not in pnl:
            pnl[hoy] = 0
            guardar_json(pnl, PNL_DIARIO_FILE)
            logger.info("PnL diario reseteado")

def dec(x: str) -> Decimal:
    try:
        return Decimal(x)
    except (InvalidOperation, TypeError):
        return Decimal('0')

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
        meta = {
            "stepSize": dec(lot.get('stepSize', '0')),
            "minQty": dec(lot.get('minQty', '0')),
            "marketStepSize": dec(market_lot.get('stepSize', '0')) if market_lot else dec(lot.get('stepSize','0')),
            "marketMinQty": dec(market_lot.get('minQty', '0')) if market_lot else dec(lot.get('minQty','0')),
            "tickSize": dec(pricef.get('tickSize', '0')),
            "minNotional": dec(notional_f.get('minNotional', '0')) if notional_f else dec('5'),
            "applyToMarket": bool(notional_f.get('applyToMarket', True)) if notional_f else True,
            "baseAsset": info['baseAsset'],
            "quoteAsset": info['quoteAsset'],
        }
        if meta["marketStepSize"] <= 0 or meta["marketMinQty"] <= 0:
            meta["marketStepSize"] = meta["stepSize"]
            meta["marketMinQty"] = meta["minQty"]
        SYMBOL_CACHE[symbol] = meta
        logger.debug(f"Info símbolo {symbol} cargada: {meta}")
        return meta
    except Exception as e:
        logger.info(f"Error cargando info de {symbol}: {e}")
        INVALID_SYMBOL_CACHE.add(symbol)
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

def min_quote_for_market(symbol) -> Decimal:
    meta = load_symbol_info(symbol)
    if not meta:
        return Decimal('5')
    return (meta["minNotional"] * Decimal('1.01')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def safe_get_ticker(symbol):
    ticker = get_cached_ticker(symbol)
    if ticker:
        logger.debug(f"Ticker {symbol}: precio={ticker['lastPrice']}, volumen={ticker.get('quoteVolume', 0)}")
    return ticker

def safe_get_balance(asset):
    try:
        b = retry(lambda: client.get_asset_balance(asset=asset), tries=10, base_delay=1.0)
        if not b:
            logger.error(f"No se obtuvo balance para {asset}")
            return 0.0
        free = float(b.get('free', 0))
        locked = float(b.get('locked', 0))
        total = free + locked
        logger.debug(f"Balance {asset}: free={free}, locked={locked}, total={total}")
        return free
    except BinanceAPIException as e:
        logger.error(f"Error BinanceAPI obteniendo balance {asset}: {e}")
        return 0.0
    except Exception as e:
        logger.error(f"Error inesperado obteniendo balance {asset}: {e}")
        return 0.0

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

def mejores_criptos(max_candidates=8):
    try:
        candidates = []
        for sym in ALLOWED_SYMBOLS:
            if sym in INVALID_SYMBOL_CACHE:
                continue
            t = get_cached_ticker(sym)
            if not t:
                continue
            vol = float(t.get("quoteVolume", 0) or 0)
            if vol > MIN_VOLUME:
                candidates.append(t)
            time.sleep(0.1)
        filtered = []
        for t in sorted(candidates, key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)[:max_candidates]:
            symbol = t["symbol"]
            klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=100), tries=5)
            closes = [float(k[4]) for k in klines]
            if len(closes) < 15:
                continue
            rsi = calculate_rsi(closes[-15:])
            precio = float(t["lastPrice"])
            ganancia_bruta = precio * TAKE_PROFIT
            comision_compra = precio * COMMISSION_RATE
            comision_venta = (precio * (1 + TAKE_PROFIT)) * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
            if ganancia_neta > MIN_NET_GAIN_ABS and rsi < RSI_BUY_MAX:
                t['rsi'] = rsi
                t['score'] = float(t.get("quoteVolume", 0))
                filtered.append(t)
            time.sleep(0.1)
        sorted_filtered = sorted(filtered, key=lambda x: x.get('score', 0), reverse=True)
        logger.debug(f"Mejores criptos: {[t['symbol'] for t in sorted_filtered]}")
        return sorted_filtered
    except Exception as e:
        logger.error(f"Error obteniendo mejores criptos: {e}")
        return []

def base_from_symbol(symbol: str) -> str:
    if symbol.endswith(MONEDA_BASE):
        return symbol[:-len(MONEDA_BASE)]
    meta = load_symbol_info(symbol)
    return meta["baseAsset"] if meta else symbol.replace(MONEDA_BASE, "")

def precio_medio_si_hay(symbol, lookback_days=30):
    try:
        since = int((now_tz() - timedelta(days=lookback_days)).timestamp() * 1000)
        trades = retry(lambda: client.get_my_trades(symbol=symbol, startTime=since), tries=5, base_delay=0.6)
        buys = [t for t in trades if t.get('isBuyer')]
        if not buys:
            return None
        qty_sum = Decimal('0')
        cost_sum = Decimal('0')
        for t in buys:
            qty = dec(t['qty'])
            price = dec(t['price'])
            comm = dec(t.get('commission', '0'))
            comm_asset = t.get('commissionAsset', '')
            if comm_asset == MONEDA_BASE:
                cost_sum += qty * price + comm
            else:
                cost_sum += qty * price
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
            cuenta = retry(lambda: client.get_account(), tries=10, base_delay=1.0)
            logger.debug(f"Cuenta obtenida: {len(cuenta['balances'])} activos")
            for b in cuenta['balances']:
                asset = b['asset']
                free = float(b['free'])
                if free > 0.0000001:
                    logger.debug(f"Detectado {asset}: {free} free")
                if asset != MONEDA_BASE and free > 0.0000001:
                    symbol = asset + MONEDA_BASE
                    if symbol not in ALLOWED_SYMBOLS:
                        continue
                    if symbol in INVALID_SYMBOL_CACHE:
                        continue
                    if not load_symbol_info(symbol):
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
                            "from_cartera": True,
                            "high_since_buy": float(precio_actual)
                        }
                        logger.info(f"Posición inicial: {symbol} {free} a {pm} (last {precio_actual})")
                    except Exception:
                        continue
                elif asset == MONEDA_BASE:
                    logger.info(f"Saldo {MONEDA_BASE}: {free}")
            guardar_json(registro, REGISTRO_FILE)
        except BinanceAPIException as e:
            logger.error(f"Error inicializando registro: {e}")

def market_sell_with_fallback(symbol: str, qty: Decimal, meta: dict):
    attempts = 0
    last_err = None
    q = quantize_qty(qty, meta["marketStepSize"])
    while attempts < 3 and q > Decimal('0'):
        try:
            order = retry(lambda: client.order_market_sell(symbol=symbol, quantity=format(q, 'f')), tries=5, base_delay=0.6)
            logger.debug(f"Venta exitosa {symbol}: qty={q}")
            return order
        except BinanceAPIException as e:
            last_err = e
            if e.code == -1013:
                q = q - meta["marketStepSize"]
                q = quantize_qty(q, meta["marketStepSize"])
                attempts += 1
                logger.warning(f"{symbol}: ajustando qty por LOT_SIZE, intento {attempts}, qty={q}")
                continue
            raise
    if last_err:
        raise last_err

def executed_qty_from_order(order_resp) -> float:
    try:
        fills = order_resp.get('fills') or []
        if fills:
            qty = sum(float(f.get('qty', 0)) for f in fills if f)
            logger.debug(f"Cantidad ejecutada desde fills: {qty}")
            return qty
    except Exception:
        pass
    try:
        executed_quote = float(order_resp.get('cummulativeQuoteQty', 0))
        price = float(order_resp.get('price') or 0) or float(order_resp.get('fills', [{}])[0].get('price', 0) or 0)
        if executed_quote and price:
            qty = executed_quote / price
            logger.debug(f"Cantidad ejecutada desde quote: {qty}")
            return qty
    except Exception:
        pass
    return 0.0

def comprar():
    if not puede_comprar():
        logger.info("Límite de pérdida diaria alcanzado. No se comprará más hoy.")
        return
    try:
        saldo_spot = safe_get_balance(MONEDA_BASE)
        logger.debug(f"Saldo {MONEDA_BASE} disponible: {saldo_spot}")
        if saldo_spot < MIN_SALDO_COMPRA:
            logger.info(f"Saldo {MONEDA_BASE} insuficiente: {saldo_spot} < {MIN_SALDO_COMPRA}")
            return
        cantidad_usdc = saldo_spot * PORCENTAJE_USDC
        criptos = mejores_criptos()
        if not criptos:
            logger.info("No hay criptos candidatas para comprar.")
            return
        registro = cargar_json(REGISTRO_FILE)
        if len(registro) >= MAX_POSICIONES:
            logger.info("Máximo de posiciones abiertas alcanzado.")
            return
        now_ts = time.time()
        global ULTIMAS_OPERACIONES
        ULTIMAS_OPERACIONES = [t for t in ULTIMAS_OPERACIONES if now_ts - t < 3600]
        if len(ULTIMAS_OPERACIONES) >= MAX_TRADES_PER_HOUR:
            logger.info("Tope de operaciones por hora alcanzado.")
            return
        compradas = 0
        for cripto in criptos:
            if compradas >= MAX_POSICIONES - len(registro):
                break
            symbol = cripto["symbol"]
            if symbol in registro:
                continue
            last = ULTIMA_COMPRA.get(symbol, 0)
            if now_ts - last < TRADE_COOLDOWN_SEC:
                continue
            ticker = safe_get_ticker(symbol)
            if not ticker:
                continue
            precio = dec(str(ticker["lastPrice"]))
            if precio <= 0:
                continue
            rsi = cripto.get("rsi", 50)
            meta = load_symbol_info(symbol)
            if not meta:
                continue
            min_quote = min_quote_for_market(symbol)
            quote_to_spend = dec(str(cantidad_usdc)) * (1 - COMMISSION_RATE)
            if quote_to_spend < min_quote:
                logger.info(f"{symbol}: no alcanza minNotional ({float(min_quote):.2f} {MONEDA_BASE}).")
                continue
            quote_to_spend = quantize_quote(quote_to_spend, meta["tickSize"])
            if rsi < RSI_BUY_MAX:
                try:
                    logger.debug(f"Intentando comprar {symbol} con {quote_to_spend} {MONEDA_BASE}")
                    orden = retry(
                        lambda: client.create_order(
                            symbol=symbol,
                            side="BUY",
                            type="MARKET",
                            quoteOrderQty=format(quote_to_spend, 'f')
                        ),
                        tries=5, base_delay=1.0
                    )
                    executed_qty = executed_qty_from_order(orden)
                    if executed_qty <= 0:
                        executed_qty = float(quote_to_spend) / float(precio)
                    with LOCK:
                        registro = cargar_json(REGISTRO_FILE)
                        registro[symbol] = {
                            "cantidad": executed_qty,
                            "precio_compra": float(precio),
                            "timestamp": now_tz().isoformat(),
                            "high_since_buy": float(precio)
                        }
                        guardar_json(registro, REGISTRO_FILE)
                    enviar_telegram(f"🟢 Comprado {symbol} por {float(quote_to_spend):.2f} {MONEDA_BASE} a ~{float(precio):.6f}. RSI: {rsi:.2f}.")
                    logger.info(f"Compra exitosa: {symbol}, qty={executed_qty}, precio={precio}")
                    compradas += 1
                    ULTIMA_COMPRA[symbol] = now_ts
                    ULTIMAS_OPERACIONES.append(now_ts)
                except BinanceAPIException as e:
                    logger.error(f"Error comprando {symbol}: {e}")
                except Exception as e:
                    logger.error(f"Error inesperado comprando {symbol}: {e}")
            else:
                logger.info(f"No se compra {symbol}: RSI {rsi:.2f} > {RSI_BUY_MAX}")
            time.sleep(0.3)
    except Exception as e:
        logger.error(f"Error general en compra: {e}")

def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        nuevos_registro = {}
        dust_positions = []
        for symbol, data in list(registro.items()):
            try:
                precio_compra = dec(str(data["precio_compra"]))
                high_since_buy = dec(str(data.get("high_since_buy", data["precio_compra"])))
                ticker = safe_get_ticker(symbol)
                if not ticker:
                    nuevos_registro[symbol] = data
                    continue
                precio_actual = dec(str(ticker["lastPrice"]))
                cambio = (precio_actual - precio_compra) / (precio_compra if precio_compra != 0 else Decimal('1'))
                klines = retry(lambda: client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=15), tries=5)
                closes = [float(k[4]) for k in klines]
                rsi = calculate_rsi(closes[-15:])
                meta = load_symbol_info(symbol)
                if not meta:
                    nuevos_registro[symbol] = data
                    continue
                if precio_actual > high_since_buy:
                    data["high_since_buy"] = float(precio_actual)
                    guardar_json(registro, REGISTRO_FILE)
                    high_since_buy = precio_actual
                asset = base_from_symbol(symbol)
                cantidad_wallet = dec(str(safe_get_balance(asset)))
                if cantidad_wallet <= 0:
                    dust_positions.append(symbol)
                    continue
                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                    dust_positions.append(symbol)
                    continue
                if meta["applyToMarket"] and meta["minNotional"] > 0 and precio_actual > 0:
                    notional_est = qty * precio_actual
                    if notional_est < meta["minNotional"] or float(notional_est) < DUST_THRESHOLD:
                        dust_positions.append(symbol)
                        continue
                ganancia_bruta = float(qty) * (float(precio_actual) - float(precio_compra))
                comision_compra = float(precio_compra) * float(qty) * COMMISSION_RATE
                comision_venta = float(precio_actual) * float(qty) * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                trailing_trigger = (precio_actual - high_since_buy) / high_since_buy <= -TRAILING_STOP
                vender_por_stop = float(cambio) <= STOP_LOSS or trailing_trigger
                vender_por_profit = float(cambio) >= TAKE_PROFIT and ganancia_neta > MIN_NET_GAIN_ABS
                if vender_por_stop or vender_por_profit:
                    try:
                        orden = market_sell_with_fallback(symbol, qty, meta)
                        logger.info(f"Orden de venta: {orden}")
                        total_hoy = actualizar_pnl_diario(ganancia_neta)
                        motivo = "Stop-loss/Trailing" if vender_por_stop else "Take-profit"
                        enviar_telegram(
                            f"🔴 Vendido {symbol} - {float(qty):.8f} a ~{float(precio_actual):.6f} "
                            f"(Cambio: {float(cambio)*100:.2f}%) PnL: {ganancia_neta:.2f} {MONEDA_BASE}. "
                            f"Motivo: {motivo}. RSI: {rsi:.2f}. PnL hoy: {total_hoy:.2f}."
                        )
                        comprar()
                    except BinanceAPIException as e:
                        logger.error(f"Error vendiendo {symbol}: {e}")
                        dust_positions.append(symbol)
                        continue
                else:
                    nuevos_registro[symbol] = data
                    logger.debug(f"No se vende {symbol}: RSI={rsi:.2f}, Ganancia neta={ganancia_neta:.4f}")
            except Exception as e:
                logger.error(f"Error vendiendo {symbol}: {e}")
                nuevos_registro[symbol] = data
            time.sleep(0.3)
        limpio = {sym: d for sym, d in nuevos_registro.items() if sym not in dust_positions}
        guardar_json(limpio, REGISTRO_FILE)
        if dust_positions:
            enviar_telegram(f"🧹 Limpiado dust: {', '.join(dust_positions)}")
        try:
            registro = cargar_json(REGISTRO_FILE)
            if not registro:
                return
            criptos = mejores_criptos()
            if criptos:
                candidates = [c for c in criptos if c['symbol'] not in registro]
                if candidates:
                    best = candidates[0]
                    best_symbol = best['symbol']
                    best_rsi = best.get('rsi', 50)
                    pos_perfs = []
                    for sym, data in registro.items():
                        ticker = safe_get_ticker(sym)
                        if not ticker:
                            continue
                        price = float(ticker['lastPrice'])
                        buy_price = data['precio_compra']
                        change = (price - buy_price) / buy_price if buy_price else 0
                        klines = retry(lambda: client.get_klines(symbol=sym, interval=Client.KLINE_INTERVAL_5MINUTE, limit=15), tries=5)
                        closes = [float(k[4]) for k in klines]
                        rsi = calculate_rsi(closes[-15:])
                        qty = data['cantidad']
                        ganancia_bruta = qty * (price - buy_price)
                        comision_compra = buy_price * qty * COMMISSION_RATE
                        comision_venta = price * qty * COMMISSION_RATE
                        ganancia_neta = ganancia_bruta - comision_compra - comision_venta
                        pos_perfs.append((sym, change, rsi, ganancia_neta))
                        time.sleep(0.3)
                    if pos_perfs:
                        pos_perfs.sort(key=lambda x: x[1])
                        worst_sym, worst_change, worst_rsi, worst_net = pos_perfs[0]
                        if worst_net < 0 or worst_rsi > best_rsi + 5:
                            try:
                                meta = load_symbol_info(worst_sym)
                                asset = base_from_symbol(worst_sym)
                                cantidad_wallet = dec(str(safe_get_balance(asset)))
                                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                                    del registro[worst_sym]
                                    guardar_json(registro, REGISTRO_FILE)
                                    return
                                orden = market_sell_with_fallback(worst_sym, qty, meta)
                                logger.info(f"Rotación: vendido {worst_sym}: {orden}")
                                total_hoy = actualizar_pnl_diario(worst_net)
                                enviar_telegram(f"🔄 Rotación agresiva: vendido {worst_sym} (PnL neto {worst_net:.2f}). Para {best_symbol}.")
                                del registro[worst_sym]
                                guardar_json(registro, REGISTRO_FILE)
                                comprar()
                            except Exception as e:
                                logger.error(f"Error en venta por rotación {worst_sym}: {e}")
        except Exception as e:
            logger.error(f"Error en bloque de rotación: {e}")

def resumen_diario():
    try:
        cuenta = retry(lambda: client.get_account(), tries=10, base_delay=1.0)
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy_v = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy_v:.2f} {MONEDA_BASE}\nBalances:\n"
        total_value = 0
        for b in cuenta["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0.001:
                mensaje += f"{b['asset']}: {total:.6f}\n"
                if b['asset'] != MONEDA_BASE:
                    symbol = b['asset'] + MONEDA_BASE
                    ticker = safe_get_ticker(symbol)
                    if ticker:
                        total_value += total * float(ticker['lastPrice'])
                else:
                    total_value += total
            time.sleep(0.3)
        mensaje += f"Valor total estimado: {total_value:.2f} {MONEDA_BASE}"
        enviar_telegram(mensaje)
        logger.info(f"Resumen diario enviado: {mensaje}")
        seven_days_ago = (now_tz() - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error en resumen diario: {e}")

if __name__ == "__main__":
    debug_balances()
    inicializar_registro()
    enviar_telegram("🤖 Bot IA Momentum Scalping: ~160 USDC, compra múltiples altcoins baratas, vende si baja 1% o sube 1.2% neto, 15s checks, fee-aware.")
    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', seconds=15, id="comprar")
    scheduler.add_job(vender_y_convertir, 'interval', seconds=15, id="vender")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl")
    scheduler.start()
    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
