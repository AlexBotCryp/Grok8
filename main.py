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
from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot-ia")

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""
MONEDA_BASE = "USDC"
MIN_VOLUME = Decimal('10000')
MIN_SALDO_COMPRA = Decimal('15')
COMPRA_POR_CRIPTO = Decimal('15')
ALLOWED_SYMBOLS = ['BTCUSDC', 'ETHUSDC', 'SOLUSDC', 'BNBUSDC', 'AVAXUSDC']
TAKE_PROFIT = Decimal('0.05')
STOP_LOSS = Decimal('-0.01')
COMMISSION_RATE = Decimal('0.001')
TRAILING_STOP = Decimal('0.01')
TRADE_COOLDOWN_SEC = 300
PERDIDA_MAXIMA_DIARIA = Decimal('20')
CRITICAL_SALDO = Decimal('3')
NOTIFICATION_COOLDOWN_MIN = 5
NO_BUY_CYCLES_THRESHOLD = 3
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
DUST_THRESHOLD = Decimal('0.1')
TICKERS_CACHE = {}
BALANCES_CACHE = {}
last_no_buy_notification = 0
no_buy_cycles = 0

def get_cached_ticker(symbol):
    now = time.time()
    if symbol in TICKERS_CACHE and now - TICKERS_CACHE[symbol]['ts'] < 300:
        logger.debug(f"Usando ticker cacheado para {symbol}")
        return TICKERS_CACHE[symbol]['data']
    try:
        t = client.get_ticker(symbol=symbol)
        if t and float(t.get('lastPrice', 0)) > 0:
            TICKERS_CACHE[symbol] = {'data': t, 'ts': now}
            logger.debug(f"Ticker actualizado para {symbol}: {t['lastPrice']}")
            return t
    except BinanceAPIException as e:
        logger.error(f"Error Binance fetching ticker {symbol}: {e}")
    except Exception as e:
        logger.error(f"Error fetching ticker {symbol} via API: {e}")
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

def retry(fn, tries=3, base_delay=0.5, jitter=0.3, exceptions=(Exception,)):
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
        cuenta = retry(lambda: client.get_account(), tries=3, base_delay=0.5)
        logger.info("=== BALANCES EN CARTERA ===")
        total_value = Decimal('0')
        posiciones = cargar_json(REGISTRO_FILE)
        mensaje = f"🤖 Inicio bot - Posiciones abiertas: {len(posiciones)}\n"
        for sym, data in posiciones.items():
            mensaje += f"{sym}: {data['cantidad']:.8f} @ {data['precio_compra']:.6f}\n"
        for b in cuenta['balances']:
            total = Decimal(str(float(b['free']) + float(b['locked'])))
            if total > Decimal('0.0001'):
                logger.info(f"{b['asset']}: free={b['free']}, locked={b['locked']}, total={total}")
                BALANCES_CACHE[b['asset']] = {'free': Decimal(b['free']), 'locked': Decimal(b['locked'])}
                if b['asset'] == MONEDA_BASE:
                    total_value += total
                elif b['asset'] + MONEDA_BASE in ALLOWED_SYMBOLS:
                    ticker = get_cached_ticker(b['asset'] + MONEDA_BASE)
                    if ticker:
                        total_value += total * Decimal(ticker['lastPrice'])
        logger.info(f"Valor total estimado: {total_value:.2f} {MONEDA_BASE}")
        mensaje += f"Valor total cartera: {total_value:.2f} {MONEDA_BASE}"
        enviar_telegram(mensaje)
        return total_value
    except BinanceAPIException as e:
        logger.error(f"Error Binance en debug balances: {e}")
        enviar_telegram(f"⚠️ Error en balances: {e}")
    except Exception as e:
        logger.error(f"Error debug balances: {e}")
        enviar_telegram(f"⚠️ Error en balances: {e}")
    return Decimal('0')

def actualizar_pnl_diario(realized_pnl, fees=Decimal('0.1')):
    with LOCK:
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        if today not in pnl_data:
            pnl_data[today] = 0
        pnl_data[today] += float(realized_pnl) - float(fees)
        guardar_json(pnl_data, PNL_DIARIO_FILE)
        logger.debug(f"PnL actualizado: {today} = {pnl_data[today]}")
        return pnl_data[today]

def pnl_hoy():
    pnl_data = cargar_json(PNL_DIARIO_FILE)
    return Decimal(str(pnl_data.get(get_current_date(), 0)))

def puede_comprar():
    hoy_pnl = pnl_hoy()
    can_buy = hoy_pnl > -PERDIDA_MAXIMA_DIARIA
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

def dec(x):
    try:
        return Decimal(str(x))
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
            "minNotional": dec(notional_f.get('minNotional', '0')) if notional_f else dec('1'),
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
    except BinanceAPIException as e:
        logger.error(f"Error Binance cargando info de {symbol}: {e}")
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
        return Decimal('1')
    return (meta["minNotional"] * Decimal('1.01')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def safe_get_ticker(symbol):
    now = time.time()
    if symbol in TICKERS_CACHE and now - TICKERS_CACHE[symbol]['ts'] < 300:
        logger.debug(f"Usando ticker cacheado para {symbol}")
        return TICKERS_CACHE[symbol]['data']
    try:
        t = client.get_ticker(symbol=symbol)
        if t and float(t.get('lastPrice', 0)) > 0:
            TICKERS_CACHE[symbol] = {'data': t, 'ts': now}
            logger.debug(f"Ticker actualizado para {symbol}: {t['lastPrice']}")
            return t
    except BinanceAPIException as e:
        logger.error(f"Error Binance fetching ticker {symbol}: {e}")
    except Exception as e:
        logger.error(f"Error fetching ticker {symbol} via API: {e}")
    return None

def safe_get_balance(asset):
    if asset in BALANCES_CACHE:
        return BALANCES_CACHE[asset]['free']
    try:
        b = retry(lambda: client.get_asset_balance(asset=asset), tries=3, base_delay=0.5)
        if not b:
            logger.error(f"No se obtuvo balance para {asset}")
            enviar_telegram(f"⚠️ No se obtuvo balance para {asset}")
            return Decimal('0')
        free = Decimal(str(b.get('free', 0)))
        locked = Decimal(str(b.get('locked', 0)))
        total = free + locked
        BALANCES_CACHE[asset] = {'free': free, 'locked': locked}
        logger.debug(f"Balance {asset}: free={free}, locked={locked}, total={total}")
        return free
    except BinanceAPIException as e:
        logger.error(f"Error BinanceAPI obteniendo balance {asset}: {e}")
        enviar_telegram(f"⚠️ Error en balance {asset}: {e}")
    except Exception as e:
        logger.error(f"Error inesperado obteniendo balance {asset}: {e}")
        enviar_telegram(f"⚠️ Error inesperado en balance {asset}: {e}")
    return Decimal('0')

def mejores_criptos(max_candidates=10):
    try:
        candidates = []
        for sym in ALLOWED_SYMBOLS:
            if sym in INVALID_SYMBOL_CACHE:
                logger.debug(f"Símbolo {sym} en cache inválida, saltando")
                continue
            t = safe_get_ticker(sym)
            if not t:
                logger.debug(f"No se obtuvo ticker para {sym}")
                continue
            vol = Decimal(str(t.get("quoteVolume", 0) or 0))
            if vol > MIN_VOLUME:
                candidates.append(t)
                logger.debug(f"{sym} añadido: volumen={vol}")
            else:
                logger.debug(f"{sym} volumen bajo: {vol} < {MIN_VOLUME}")
            time.sleep(1)
        if not candidates and no_buy_cycles >= NO_BUY_CYCLES_THRESHOLD:
            logger.warning("Forzando compras tras 3 ciclos sin candidatos.")
            return [safe_get_ticker(random.choice(ALLOWED_SYMBOLS)) for _ in range(min(5, len(ALLOWED_SYMBOLS)))]
        if not candidates:
            logger.warning("No hay criptos con volumen suficiente.")
            enviar_telegram("⚠️ No hay criptos con volumen suficiente (>10000 USDC).")
            return []
        filtered = []
        for t in sorted(candidates, key=lambda x: Decimal(str(x.get("quoteVolume", 0) or 0)), reverse=True)[:max_candidates]:
            symbol = t["symbol"]
            precio = dec(t["lastPrice"])
            ganancia_bruta = precio * TAKE_PROFIT
            comision_compra = precio * COMMISSION_RATE
            comision_venta = (precio * (Decimal('1') + TAKE_PROFIT)) * COMMISSION_RATE
            ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
            if ganancia_neta > 0:
                t['score'] = Decimal(str(t.get("quoteVolume", 0)))
                filtered.append(t)
                logger.debug(f"{symbol} añadido a candidatos: ganancia_neta={ganancia_neta}, score={t['score']}")
            else:
                logger.debug(f"{symbol} descartado: ganancia_neta={ganancia_neta}")
            time.sleep(1)
        sorted_filtered = sorted(filtered, key=lambda x: x.get('score', 0), reverse=True)
        logger.debug(f"Mejores criptos: {[t['symbol'] for t in sorted_filtered]}")
        return sorted_filtered
    except BinanceAPIException as e:
        logger.error(f"Error Binance obteniendo mejores criptos: {e}")
        enviar_telegram(f"⚠️ Error Binance obteniendo criptos: {e}")
    except Exception as e:
        logger.error(f"Error obteniendo mejores criptos: {e}")
        enviar_telegram(f"⚠️ Error obteniendo criptos: {e}")
    return []

def base_from_symbol(symbol: str) -> str:
    if symbol.endswith(MONEDA_BASE):
        return symbol[:-len(MONEDA_BASE)]
    meta = load_symbol_info(symbol)
    return meta["baseAsset"] if meta else symbol.replace(MONEDA_BASE, "")

def precio_medio_si_hay(symbol, lookback_days=30):
    try:
        since = int((now_tz() - timedelta(days=lookback_days)).timestamp() * 1000)
        trades = retry(lambda: client.get_my_trades(symbol=symbol, startTime=since), tries=3, base_delay=0.5)
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
    except BinanceAPIException as e:
        logger.warning(f"Error Binance calculando precio medio {symbol}: {e}")
    except Exception as e:
        logger.warning(f"No se pudo calcular precio medio {symbol}: {e}")
    return None

def inicializar_registro():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        try:
            cuenta = retry(lambda: client.get_account(), tries=3, base_delay=0.5)
            logger.debug(f"Cuenta obtenida: {len(cuenta['balances'])} activos")
            for b in cuenta['balances']:
                asset = b['asset']
                free = Decimal(str(b['free']))
                if free > Decimal('0.0000001'):
                    logger.debug(f"Detectado {asset}: {free} free")
                if asset != MONEDA_BASE and free > Decimal('0.0000001'):
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
                        precio_actual = Decimal(t['lastPrice'])
                        pm = precio_medio_si_hay(symbol) or float(precio_actual)
                        registro[symbol] = {
                            "cantidad": float(free),
                            "precio_compra": float(pm),
                            "timestamp": now_tz().isoformat(),
                            "high_since_buy": float(precio_actual)
                        }
                        logger.info(f"Posición inicial: {symbol} {free} a {pm} (last {precio_actual})")
                    except Exception:
                        continue
                elif asset == MONEDA_BASE:
                    logger.info(f"Saldo {MONEDA_BASE}: {free}")
            guardar_json(registro, REGISTRO_FILE)
        except BinanceAPIException as e:
            logger.error(f"Error Binance inicializando registro: {e}")
            enviar_telegram(f"⚠️ Error Binance inicializando registro: {e}")
        except Exception as e:
            logger.error(f"Error inicializando registro: {e}")
            enviar_telegram(f"⚠️ Error inicializando registro: {e}")

def market_sell_with_fallback(symbol: str, qty: Decimal, meta: dict):
    attempts = 0
    last_err = None
    q = quantize_qty(qty, meta["marketStepSize"])
    while attempts < 3 and q > Decimal('0'):
        try:
            order = retry(lambda: client.order_market_sell(symbol=symbol, quantity=format(q, 'f')), tries=3, base_delay=0.5)
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
        executed_quote = float(order_resp.get('cummulativeQuoteQty', 0))
        price = float(order_resp.get('price', 0)) or float(order_resp.get('fills', [{}])[0].get('price', 0) or 0)
        if executed_quote and price:
            qty = executed_quote / price
            logger.debug(f"Cantidad ejecutada desde quote: {qty}")
            return qty
    except Exception:
        pass
    return 0.0

def resumen_diario():
    try:
        cuenta = retry(lambda: client.get_account(), tries=3, base_delay=0.5)
        pnl_data = cargar_json(PNL_DIARIO_FILE)
        today = get_current_date()
        pnl_hoy_v = pnl_data.get(today, 0)
        mensaje = f"📊 Resumen diario ({today}):\nPNL hoy: {pnl_hoy_v:.2f} {MONEDA_BASE}\nBalances:\n"
        total_value = Decimal('0')
        for b in cuenta['balances']:
            total = Decimal(str(float(b['free']) + float(b['locked'])))
            if total > Decimal('0.001'):
                mensaje += f"{b['asset']}: {total:.6f}\n"
                if b['asset'] != MONEDA_BASE:
                    symbol = b['asset'] + MONEDA_BASE
                    ticker = get_cached_ticker(symbol)
                    if ticker:
                        total_value += total * Decimal(ticker['lastPrice'])
                else:
                    total_value += total
            time.sleep(1)
        mensaje += f"Valor total estimado: {total_value:.2f} {MONEDA_BASE}"
        enviar_telegram(mensaje)
        logger.info(f"Resumen diario enviado: {mensaje}")
        seven_days_ago = (now_tz() - timedelta(days=7)).date().isoformat()
        pnl_data = {k: v for k, v in pnl_data.items() if k >= seven_days_ago}
        guardar_json(pnl_data, PNL_DIARIO_FILE)
    except BinanceAPIException as e:
        logger.error(f"Error Binance en resumen diario: {e}")
        enviar_telegram(f"⚠️ Error Binance en resumen diario: {e}")
    except Exception as e:
        logger.error(f"Error en resumen diario: {e}")
        enviar_telegram(f"⚠️ Error en resumen diario: {e}")

def comprar():
    global last_no_buy_notification, no_buy_cycles, TRADE_COOLDOWN_SEC, ULTIMAS_OPERACIONES
    if not puede_comprar():
        logger.info("Límite de pérdida diaria alcanzado. No se comprará más hoy.")
        enviar_telegram("⚠️ Límite de pérdida diaria alcanzado.")
        return
    try:
        saldo_spot = safe_get_balance(MONEDA_BASE)
        logger.debug(f"Saldo {MONEDA_BASE} disponible: {saldo_spot}")
        if saldo_spot < MIN_SALDO_COMPRA:
            logger.info(f"Saldo {MONEDA_BASE} insuficiente: {saldo_spot} < {MIN_SALDO_COMPRA}")
            enviar_telegram(f"⚠️ Saldo insuficiente: {saldo_spot} {MONEDA_BASE}")
            return
        if saldo_spot < CRITICAL_SALDO:
            logger.info(f"Saldo crítico: {saldo_spot} < {CRITICAL_SALDO}. Pausando compras.")
            enviar_telegram(f"⚠️ Saldo crítico: {saldo_spot} {MONEDA_BASE}. Pausando compras.")
            return
        max_compras = int(saldo_spot // COMPRA_POR_CRIPTO)
        if max_compras == 0:
            logger.info(f"No hay suficiente saldo para al menos una compra de {COMPRA_POR_CRIPTO} {MONEDA_BASE}")
            enviar_telegram(f"⚠️ No hay suficiente saldo para al menos una compra de {COMPRA_POR_CRIPTO} {MONEDA_BASE}")
            return
        cantidad_usdc = min(COMPRA_POR_CRIPTO * max_compras, saldo_spot)
        criptos = mejores_criptos(max_candidates=max_compras)
        if not criptos:
            no_buy_cycles += 1
            logger.info("No hay criptos candidatas para comprar.")
            if time.time() - last_no_buy_notification >= NOTIFICATION_COOLDOWN_MIN * 60:
                enviar_telegram("⚠️ No hay criptos candidatas: sin símbolos con volumen suficiente (>10000 USDC).")
                last_no_buy_notification = time.time()
            return
        no_buy_cycles = 0
        registro = cargar_json(REGISTRO_FILE)
        now_ts = time.time()
        ULTIMAS_OPERACIONES = [t for t in ULTIMAS_OPERACIONES if now_ts - t < 3600]
        compradas = 0
        no_compradas_razon = []
        for cripto in criptos:
            symbol = cripto["symbol"]
            last = ULTIMA_COMPRA.get(symbol, 0)
            if now_ts - last < TRADE_COOLDOWN_SEC:
                no_compradas_razon.append(f"{symbol}: en cooldown")
                logger.debug(f"{symbol} en cooldown, saltando")
                continue
            ticker = get_cached_ticker(symbol)
            if not ticker:
                no_compradas_razon.append(f"{symbol}: sin ticker")
                logger.debug(f"No ticker para {symbol}")
                continue
            precio = dec(ticker["lastPrice"])
            if precio <= 0:
                no_compradas_razon.append(f"{symbol}: precio inválido")
                logger.debug(f"{symbol} precio inválido: {precio}")
                continue
            meta = load_symbol_info(symbol)
            if not meta:
                no_compradas_razon.append(f"{symbol}: sin info de símbolo")
                logger.debug(f"No meta para {symbol}")
                continue
            min_notional = min_quote_for_market(symbol)
            quote_to_spend = max(COMPRA_POR_CRIPTO * (Decimal('1') - COMMISSION_RATE), min_notional)
            if quote_to_spend > saldo_spot or compradas * COMPRA_POR_CRIPTO >= saldo_spot:
                no_compradas_razon.append(f"{symbol}: no hay suficiente saldo restante ({saldo_spot} < {quote_to_spend})")
                logger.debug(f"{symbol}: no hay suficiente saldo restante para {quote_to_spend} {MONEDA_BASE}, disponible: {saldo_spot}")
                continue
            quote_to_spend = quantize_quote(quote_to_spend, meta["tickSize"])
            try:
                logger.debug(f"Intentando comprar {symbol} con {quote_to_spend} {MONEDA_BASE}")
                orden = retry(
                    lambda: client.create_order(
                        symbol=symbol,
                        side="BUY",
                        type="MARKET",
                        quoteOrderQty=format(float(quote_to_spend), 'f')
                    ),
                    tries=3, base_delay=0.5
                )
                executed_qty = executed_qty_from_order(orden)
                if executed_qty <= 0:
                    executed_qty = float(quote_to_spend / precio)
                with LOCK:
                    registro = cargar_json(REGISTRO_FILE)
                    registro[symbol] = {
                        "cantidad": executed_qty,
                        "precio_compra": float(precio),
                        "timestamp": now_tz().isoformat(),
                        "high_since_buy": float(precio)
                    }
                    guardar_json(registro, REGISTRO_FILE)
                enviar_telegram(f"🟢 Comprado {symbol} por {float(quote_to_spend):.2f} {MONEDA_BASE} a ~{float(precio):.6f}.")
                logger.info(f"Compra exitosa: {symbol}, qty={executed_qty}, precio={precio}")
                compradas += 1
                ULTIMA_COMPRA[symbol] = now_ts
                ULTIMAS_OPERACIONES.append(now_ts)
                saldo_spot -= quote_to_spend
            except BinanceAPIException as e:
                no_compradas_razon.append(f"{symbol}: error API ({e})")
                logger.error(f"Error Binance comprando {symbol}: {e}")
                enviar_telegram(f"⚠️ Error Binance comprando {symbol}: {e}")
            except Exception as e:
                no_compradas_razon.append(f"{symbol}: error inesperado ({e})")
                logger.error(f"Error inesperado comprando {symbol}: {e}")
                enviar_telegram(f"⚠️ Error inesperado comprando {symbol}: {e}")
            time.sleep(1)
        if compradas == 0 and time.time() - last_no_buy_notification >= NOTIFICATION_COOLDOWN_MIN * 60:
            razon_msg = f"Razones: {', '.join(no_compradas_razon)}" if no_compradas_razon else "Razones no especificadas"
            logger.warning(f"No se realizaron compras en este ciclo. {razon_msg}")
            enviar_telegram(f"⚠️ No se realizaron compras en este ciclo. {razon_msg}")
            last_no_buy_notification = time.time()
    except BinanceAPIException as e:
        logger.error(f"Error Binance general en compra: {e}")
        enviar_telegram(f"⚠️ Error Binance general en compra: {e}")
    except Exception as e:
        logger.error(f"Error general en compra: {e}")
        enviar_telegram(f"⚠️ Error general en compra: {e}")

def vender_y_convertir():
    with LOCK:
        registro = cargar_json(REGISTRO_FILE)
        nuevos_registro = {}
        dust_positions = []
        try:
            for symbol, data in list(registro.items()):
                precio_compra = dec(data["precio_compra"])
                high_since_buy = dec(data.get("high_since_buy", data["precio_compra"]))
                ticker = get_cached_ticker(symbol)
                if not ticker:
                    nuevos_registro[symbol] = data
                    logger.debug(f"No ticker para {symbol}, manteniendo")
                    continue
                precio_actual = dec(ticker["lastPrice"])
                cambio = (precio_actual - precio_compra) / (precio_compra if precio_compra != 0 else Decimal('1'))
                meta = load_symbol_info(symbol)
                if not meta:
                    nuevos_registro[symbol] = data
                    logger.debug(f"No meta para {symbol}, manteniendo")
                    continue
                if precio_actual > high_since_buy:
                    data["high_since_buy"] = float(precio_actual)
                    guardar_json(registro, REGISTRO_FILE)
                    high_since_buy = precio_actual
                asset = base_from_symbol(symbol)
                cantidad_wallet = dec(safe_get_balance(asset))
                if cantidad_wallet <= 0:
                    dust_positions.append(symbol)
                    logger.debug(f"{symbol} sin cantidad, marcando como dust")
                    continue
                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                    dust_positions.append(symbol)
                    logger.debug(f"{symbol} cantidad insuficiente: {qty}")
                    continue
                if meta["applyToMarket"] and meta["minNotional"] > 0 and precio_actual > 0:
                    notional_est = qty * precio_actual
                    if notional_est < meta["minNotional"] or notional_est < DUST_THRESHOLD:
                        dust_positions.append(symbol)
                        logger.debug(f"{symbol} no alcanza minNotional o dust: {notional_est}")
                        continue
                ganancia_bruta = qty * (precio_actual - precio_compra)
                comision_compra = precio_compra * qty * COMMISSION_RATE
                comision_venta = precio_actual * qty * COMMISSION_RATE
                ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
                time_held = (now_tz() - datetime.fromisoformat(data['timestamp'])).total_seconds() / 60
                trailing_trigger = (precio_actual - high_since_buy) / high_since_buy <= -TRAILING_STOP
                vender_por_stop = cambio <= STOP_LOSS or trailing_trigger
                vender_por_profit = cambio >= TAKE_PROFIT
                vender_por_liberacion = cambio <= Decimal('-0.005') and time_held > 15
                if vender_por_stop or vender_por_profit or vender_por_liberacion:
                    try:
                        orden = market_sell_with_fallback(symbol, qty, meta)
                        logger.info(f"Orden de venta: {orden}")
                        total_hoy = actualizar_pnl_diario(ganancia_neta)
                        motivo = "Stop-loss/Trailing" if vender_por_stop else ("Take-profit" if vender_por_profit else "Liberación fondos")
                        enviar_telegram(
                            f"🔴 Vendido {symbol} - {float(qty):.8f} a ~{float(precio_actual):.6f} "
                            f"(Cambio: {float(cambio)*100:.2f}%) PnL: {float(ganancia_neta):.2f} {MONEDA_BASE}. "
                            f"Motivo: {motivo}. PnL hoy: {total_hoy:.2f}."
                        )
                        comprar()
                    except BinanceAPIException as e:
                        logger.error(f"Error Binance vendiendo {symbol}: {e}")
                        enviar_telegram(f"⚠️ Error Binance vendiendo {symbol}: {e}")
                        dust_positions.append(symbol)
                        continue
                    except Exception as e:
                        logger.error(f"Error vendiendo {symbol}: {e}")
                        enviar_telegram(f"⚠️ Error vendiendo {symbol}: {e}")
                        dust_positions.append(symbol)
                        continue
                else:
                    nuevos_registro[symbol] = data
                    logger.debug(f"No se vende {symbol}: Cambio={float(cambio)*100:.2f}%, Ganancia neta={float(ganancia_neta):.4f}")
                time.sleep(1)
            limpo = {sym: d for sym, d in nuevos_registro.items() if sym not in dust_positions}
            guardar_json(limpo, REGISTRO_FILE)
            if dust_positions:
                enviar_telegram(f"🧹 Limpiado dust: {', '.join(dust_positions)}")
        except BinanceAPIException as e:
            logger.error(f"Error Binance en bloque de venta: {e}")
            enviar_telegram(f"⚠️ Error Binance en bloque de venta: {e}")
            guardar_json(nuevos_registro, REGISTRO_FILE)
        except Exception as e:
            logger.error(f"Error en bloque de venta: {e}")
            enviar_telegram(f"⚠️ Error en bloque de venta: {e}")
            guardar_json(nuevos_registro, REGISTRO_FILE)
        try:
            registro = cargar_json(REGISTRO_FILE)
            if not registro:
                return
            criptos = mejores_criptos()
            if criptos:
                candidates = [c for c in criptos]
                if candidates:
                    best = candidates[0]
                    best_symbol = best['symbol']
                    pos_perfs = []
                    for sym, data in registro.items():
                        ticker = get_cached_ticker(sym)
                        if not ticker:
                            continue
                        price = dec(ticker['lastPrice'])
                        buy_price = dec(data['precio_compra'])
                        change = (price - buy_price) / buy_price if buy_price != 0 else 0
                        qty = dec(data['cantidad'])
                        ganancia_bruta = qty * (price - buy_price)
                        comision_compra = buy_price * qty * COMMISSION_RATE
                        comision_venta = price * qty * COMMISSION_RATE
                        ganancia_neta = ganancia_bruta - (comision_compra + comision_venta)
                        time_held = (now_tz() - datetime.fromisoformat(data['timestamp'])).total_seconds() / 60
                        pos_perfs.append((sym, change, ganancia_neta, time_held))
                        time.sleep(1)
                    if pos_perfs:
                        pos_perfs.sort(key=lambda x: x[1])
                        worst_sym, worst_change, worst_net, time_held = pos_perfs[0]
                        if worst_net < 0 or time_held > 30:
                            try:
                                meta = load_symbol_info(worst_sym)
                                asset = base_from_symbol(worst_sym)
                                cantidad_wallet = dec(safe_get_balance(asset))
                                qty = quantize_qty(cantidad_wallet, meta["marketStepSize"])
                                if qty < meta["marketMinQty"] or qty <= Decimal('0'):
                                    del registro[worst_sym]
                                    guardar_json(registro, REGISTRO_FILE)
                                    return
                                orden = market_sell_with_fallback(worst_sym, qty, meta)
                                logger.info(f"Rotación: vendido {worst_sym}: {orden}")
                                total_hoy = actualizar_pnl_diario(worst_net)
                                enviar_telegram(f"🔄 Rotación: vendido {worst_sym} (PnL neto {float(worst_net):.2f}). Para {best_symbol}.")
                                del registro[worst_sym]
                                guardar_json(registro, REGISTRO_FILE)
                                comprar()
                            except BinanceAPIException as e:
                                logger.error(f"Error Binance en rotación {worst_sym}: {e}")
                                enviar_telegram(f"⚠️ Error Binance en rotación: {e}")
                            except Exception as e:
                                logger.error(f"Error en rotación {worst_sym}: {e}")
                                enviar_telegram(f"⚠️ Error en rotación: {e}")
        except BinanceAPIException as e:
            logger.error(f"Error Binance en bloque de rotación: {e}")
            enviar_telegram(f"⚠️ Error Binance en rotación: {e}")
        except Exception as e:
            logger.error(f"Error en bloque de rotación: {e}")
            enviar_telegram(f"⚠️ Error en rotación: {e}")

if __name__ == "__main__":
    debug_balances()
    inicializar_registro()
    enviar_telegram("🤖 Bot IA: Usa $15 por cripto, 100% del saldo, TAKE_PROFIT=5%, 300s checks.")
    scheduler = BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(comprar, 'interval', seconds=TRADE_COOLDOWN_SEC, id="comprar", coalesce=True, max_instances=1)
    scheduler.add_job(vender_y_convertir, 'interval', seconds=TRADE_COOLDOWN_SEC, id="vender", coalesce=True, max_instances=1)
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen", coalesce=True, max_instances=1)
    scheduler.add_job(reset_diario, 'cron', hour=0, minute=5, id="reset_pnl", coalesce=True, max_instances=1)
    scheduler.start()
    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot detenido.")
