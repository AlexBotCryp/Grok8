# main.py
import os, json, time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException

# ========= Config por entorno =========
API_KEY        = os.getenv("BINANCE_API_KEY", "")
API_SECRET     = os.getenv("BINANCE_API_SECRET", "")
LIVE_MODE      = os.getenv("LIVE_MODE", "1") == "1"          # 0 => dry-run
BASE_ASSET     = os.getenv("FORCE_BASE", "USDC").upper()     # USDC base

AUTO_CONSOLIDATE   = os.getenv("AUTO_CONSOLIDATE", "1") == "1"
DUST_MODE          = os.getenv("DUST_MODE", "IGNORE").upper()   # IGNORE o SELL
MIN_NOTIONAL_USDC  = Decimal(os.getenv("MIN_NOTIONAL_USDC", "20"))
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", "15"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", "0"))
TP_PCT             = Decimal(os.getenv("TP_PCT", "0.006"))      # 0.6%
SL_PCT             = Decimal(os.getenv("SL_PCT", "0.007"))      # 0.7%
ALLOC_PCT          = Decimal(os.getenv("ALLOC_PCT", "1.0"))     # 100% del USDC libre
STATE_PATH         = os.getenv("STATE_PATH", "state.json")

# Watchlist SOLO USDC (forzamos en tiempo de ejecución que terminen en USDC)
WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,LTCUSDC"
)

# Dinamizadores para que "se mueva"
TRAIL_ARM_PCT      = Decimal(os.getenv("TRAIL_ARM_PCT", "0.003"))      # +0.30% arma trailing
TRAIL_GIVEBACK_PCT = Decimal(os.getenv("TRAIL_GIVEBACK_PCT", "0.0015"))# -0.15% desde el máximo => vender
STALE_MIN          = int(os.getenv("STALE_MIN", "40"))                  # minutos max “plano”
FLAT_PNL_PCT       = Decimal(os.getenv("FLAT_PNL_PCT", "0.0015"))       # ±0.15% se considera plano
BE_EXIT_PCT        = Decimal(os.getenv("BE_EXIT_PCT", "0.0025"))        # +0.25% salida ligera (fees)

# Venta segura (evita insufficient balance por comisiones/decimales)
SAFETY_SELL_PCT    = Decimal(os.getenv("SAFETY_SELL_PCT", "0.999"))     # vende 99.9%

# Telegram
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TG_OFF     = os.getenv("TELEGRAM_DISABLE", "0") == "1"

# ========= Logger =========
LOG_PREFIX = lambda lvl: f"{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {lvl} -"
def info(msg): print(f"{LOG_PREFIX('INFO')} {msg}")
def warn(msg): print(f"{LOG_PREFIX('WARNING')} {msg}")
def err (msg): print(f"{LOG_PREFIX('ERROR')} {msg}")

# ========= Telegram =========
def tg_notify(text: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True):
    if TG_OFF or not TG_TOKEN or not TG_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text[:3900],
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                time.sleep(min(max(wait, 2), 10))
                continue
            print(f"[TG] HTTP {r.status_code}: {r.text}")
            return False
        except Exception as e:
            print(f"[TG] Error: {e}")
            time.sleep(1.5)
    return False

# ========= Cliente Binance =========
if not API_KEY or not API_SECRET:
    warn("Claves Binance no configuradas. Estás en modo lectura hasta que las añadas.")
client = Client(API_KEY, API_SECRET)

# ========= Estado persistente =========
def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        warn(f"No se pudo guardar estado: {e}")

state = load_state()
state.setdefault("base_asset", BASE_ASSET)
state.setdefault("open_symbol", None)
state.setdefault("open_qty", 0.0)      # qty neta
state.setdefault("avg_price", 0.0)
state.setdefault("last_trade_ts", 0)
state.setdefault("opened_ts", 0)       # epoch segs
state.setdefault("high_water", 0.0)    # mejor PnL desde apertura

# ========= Exchange info / filtros =========
SYMBOL_INFO: Dict[str, dict] = {}

def refresh_exchange_info():
    global SYMBOL_INFO
    ex = client.get_exchange_info()
    SYMBOL_INFO = {s["symbol"]: s for s in ex["symbols"]}

def symbol_exists(symbol: str) -> bool:
    return symbol in SYMBOL_INFO

def _get_filter(symbol: str, ftype: str) -> Optional[dict]:
    info_s = SYMBOL_INFO.get(symbol)
    if not info_s: return None
    for f in info_s["filters"]:
        if f["filterType"] == ftype:
            return f
    return None

def lot_step(symbol: str) -> Decimal:
    f = _get_filter(symbol, "LOT_SIZE")
    return Decimal(str(f["stepSize"])) if f else Decimal("0")

def price_tick(symbol: str) -> Decimal:
    f = _get_filter(symbol, "PRICE_FILTER")
    return Decimal(str(f["tickSize"])) if f else Decimal("0")

def min_notional(symbol: str) -> Decimal:
    f = _get_filter(symbol, "NOTIONAL") or _get_filter(symbol, "MIN_NOTIONAL")
    if f:
        v = f.get("minNotional") or f.get("notional") or "0"
        return Decimal(str(v))
    return Decimal("0")

def round_by_step(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0: return qty
    n_steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return n_steps * step

def round_by_tick(px: Decimal, tick: Decimal) -> Decimal:
    if tick == 0: return px
    n_ticks = (px / tick).to_integral_value(rounding=ROUND_DOWN)
    return n_ticks * tick

# ========= Precios / velas =========
def last_price(symbol: str) -> Decimal:
    r = client.get_symbol_ticker(symbol=symbol)
    return Decimal(str(r["price"]))

def get_klines(symbol: str, interval="1m", limit=60) -> List[dict]:
    raws = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return [{"c": Decimal(str(k[4]))} for k in raws]

def ema(values: List[Decimal], period: int) -> Decimal:
    if not values: return Decimal("0")
    k = Decimal("2") / Decimal(period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (Decimal("1") - k)
    return e

# ========= Balances =========
def free_balance(asset: str) -> Decimal:
    try:
        bal = client.get_asset_balance(asset=asset)
        return Decimal(bal["free"]) if bal else Decimal("0")
    except Exception as e:
        warn(f"No se pudo leer balance de {asset}: {e}")
        return Decimal("0")

def all_free_balances() -> Dict[str, Decimal]:
    bals = {}
    for b in client.get_account()["balances"]:
        q = Decimal(b["free"])
        if q > 0:
            bals[b["asset"]] = q
    return bals

# ========= Consolidación a USDC =========
def sell_all_to_usdc(asset: str, qty: Decimal) -> bool:
    if asset == BASE_ASSET: return False
    symbol = f"{asset}{BASE_ASSET}"
    if not symbol_exists(symbol):
        info(f"🚫 No existe {symbol}, no consolido {asset}.")
        return False
    px = last_price(symbol)
    if px <= 0:
        info(f"🚫 Sin precio para {symbol}.")
        return False
    notional = qty * px
    ex_min = min_notional(symbol)
    min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if notional < min_needed:
        info(f"🟡 {asset}: notional {notional:.4f} < min {min_needed:.4f}. "
             f"{'Ignoro (dust)' if DUST_MODE=='IGNORE' else 'Intento vender igualmente'}")
        if DUST_MODE == "IGNORE":
            return False
    q_round = round_by_step(qty, lot_step(symbol))
    if q_round <= 0:
        info(f"🟡 {asset}: qty tras redondeo es 0. No vendo.")
        return False
    if LIVE_MODE:
        try:
            info(f"🔁 Consolida {asset} -> {BASE_ASSET}: {symbol} qty={q_round}")
            client.create_order(symbol=symbol, side=SIDE_SELL,
                                type=ORDER_TYPE_MARKET, quantity=float(q_round))
            time.sleep(0.7)
            return True
        except Exception as e:
            err(f"Error vendiendo {asset}->{BASE_ASSET}: {e}")
            tg_notify(f"⚠️ Error consolidando <b>{asset}</b>→{BASE_ASSET}: <code>{e}</code>")
            return False
    else:
        info(f"[DRY] Vendería {asset}->{BASE_ASSET} qty={q_round}")
        return True

def force_base_position_usdc():
    refresh_exchange_info()
    bals = all_free_balances()
    non_base = {a: q for a, q in bals.items() if a != BASE_ASSET and q > 0}
    if non_base:
        info(f"🧹 Consolidación: {len(non_base)} activos ≠ {BASE_ASSET} detectados.")
        for a, q in non_base.items():
            sell_all_to_usdc(a, q)
    usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {usdc:.2f}")
    # Estado a sin posición
    state["open_symbol"] = None
    state["open_qty"] = 0.0
    state["avg_price"] = 0.0
    state["opened_ts"] = 0
    state["high_water"] = 0.0
    save_state(state)
    info("✅ Posición base forzada: USDC. Sin posición abierta.")
    tg_notify("✅ Bot iniciado y consolidado a <b>USDC</b>. Sin posición abierta.")

# ========= Señal simple (precio vs EMA20) =========
def score_symbol(symbol: str) -> Tuple[Decimal, dict]:
    try:
        kl = get_klines(symbol, limit=40)
        closes = [k["c"] for k in kl]
        if len(closes) < 20:
            return Decimal("0"), {"reason": "pocas velas"}
        px = closes[-1]
        ema20 = ema(closes[-20:], 20)
        score = (px - ema20) / (ema20 if ema20 != 0 else px)
        return score, {"px": float(px), "ema20": float(ema20)}
    except Exception as e:
        warn(f"{symbol} fallo señal: {e}")
        return Decimal("0"), {"reason":"error señal"}

# ========= Órdenes (compra guarda qty neta, venta usa saldo real + safety) =========
def place_market_buy(symbol: str, usdc_to_spend: Decimal):
    px = last_price(symbol)
    if px <= 0:
        info(f"⛔ {symbol} sin precio válido.")
        tg_notify(f"⛔ {symbol} sin precio válido para comprar.")
        return None, None, None

    ex_min = min_notional(symbol)
    min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if usdc_to_spend < min_needed:
        info(f"⛔ Notional insuficiente ({usdc_to_spend:.2f} < {min_needed:.2f}) para {symbol}.")
        tg_notify(f"⛔ Notional insuficiente para {symbol}: <code>{usdc_to_spend:.2f} &lt; {min_needed:.2f}</code>")
        return None, None, None

    step = lot_step(symbol)
    req_qty = usdc_to_spend / px
    req_qty = round_by_step(req_qty, step) if step > 0 else req_qty
    if req_qty <= 0:
        info(f"⛔ Qty redondeada es 0 para {symbol}.")
        tg_notify(f"⛔ Qty redondeada 0 para {symbol}.")
        return None, None, None

    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_BUY,
                                    type=ORDER_TYPE_MARKET, quantity=float(req_qty))
            # Cantidad neta tras comisión (si la comisión fue en el mismo activo)
            base_asset = symbol.replace(BASE_ASSET, "")
            filled_qty = Decimal("0")
            commission_in_base = Decimal("0")
            if "fills" in o:
                for f in o["fills"]:
                    filled_qty += Decimal(str(f.get("qty", "0")))
                    if f.get("commissionAsset") == base_asset:
                        commission_in_base += Decimal(str(f.get("commission", "0")))
            net_qty = filled_qty - commission_in_base
            if net_qty <= 0:
                # Fallback: leer del balance real
                net_qty = free_balance(base_asset)

            info(f"✅ COMPRA {symbol} qty_neta={net_qty} notional≈{(net_qty*px):.2f}")
            tg_notify(
                f"🟢 <b>COMPRA</b> {symbol}\n"
                f"Qty neta: <code>{net_qty}</code>\n"
                f"Precio≈<code>{px:.6f}</code>\n"
                f"Notional≈<code>{(net_qty*px):.2f} USDC</code>"
            )
            return o, net_qty, px
        except BinanceAPIException as be:
            err(f"Orden BUY falló {symbol}: {be.message}")
            tg_notify(f"⛔ BUY falló {symbol}: <code>{be.message}</code>")
            return None, None, None
        except Exception as e:
            err(f"Orden BUY falló {symbol}: {e}")
            tg_notify(f"⛔ BUY falló {symbol}: <code>{e}</code>")
            return None, None, None
    else:
        info(f"[DRY] BUY {symbol} qty={req_qty} notional≈{(req_qty*px):.2f}")
        tg_notify(f"🟢 [DRY] COMPRA {symbol} qty=<code>{req_qty}</code> px≈<code>{px:.6f}</code>")
        return {"dry": True}, req_qty, px

def place_market_sell(symbol: str, qty_hint: Decimal):
    base_asset = symbol.replace(BASE_ASSET, "")
    free_asset = free_balance(base_asset)

    qty_raw = min(qty_hint, free_asset) * SAFETY_SELL_PCT
    step = lot_step(symbol)
    q = round_by_step(qty_raw, step) if step > 0 else qty_raw

    if q <= 0:
        info(f"⛔ Qty venta 0 para {symbol} (free={free_asset}, hint={qty_hint}).")
        tg_notify(f"⛔ Venta 0 para {symbol} (free={free_asset}, hint={qty_hint}).")
        return None

    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_SELL,
                                    type=ORDER_TYPE_MARKET, quantity=float(q))
            px = last_price(symbol)
            info(f"✅ VENTA {symbol} qty={q} notional≈{(q*px):.2f}")
            tg_notify(
                f"🔴 <b>VENTA</b> {symbol}\n"
                f"Qty: <code>{q}</code>\n"
                f"Precio≈<code>{px:.6f}</code>\n"
                f"Notional≈<code>{(q*px):.2f} USDC</code>"
            )
            return o
        except BinanceAPIException as be:
            err(f"Orden SELL falló {symbol}: {be.message}")
            tg_notify(f"⛔ SELL falló {symbol}: <code>{be.message}</code>\n"
                      f"(free={free_asset} hint={qty_hint} q_try={q})")
            return None
        except Exception as e:
            err(f"Orden SELL falló {symbol}: {e}")
            tg_notify(f"⛔ SELL falló {symbol}: <code>{e}</code>")
            return None
    else:
        px = last_price(symbol)
        info(f"[DRY] SELL {symbol} qty={q} notional≈{(q*px):.2f}")
        tg_notify(f"🔴 [DRY] VENTA {symbol} qty=<code>{q}</code> px≈<code>{px:.6f}</code>")
        return {"dry": True}

# ========= Gestión de posición =========
def position_open() -> bool: return bool(state.get("open_symbol"))
def open_symbol() -> Optional[str]: return state.get("open_symbol")

def manage_position():
    sym = open_symbol()
    if not sym:
        return
    try:
        px  = last_price(sym)
        avg = Decimal(str(state.get("avg_price", 0)))
        qty = Decimal(str(state.get("open_qty", 0)))
        if avg <= 0 or qty <= 0:
            info("⚠️ Estado inconsistente, cierro mem-pos.")
            state["open_symbol"] = None
            state["open_qty"] = 0.0
            state["avg_price"] = 0.0
            state["opened_ts"] = 0
            state["high_water"] = 0.0
            save_state(state)
            return

        pnl = (px - avg) / avg
        opened_ts = int(state.get("opened_ts", 0))
        minutes_open = int((time.time() - opened_ts) / 60) if opened_ts else 0

        # Actualizar high-water (mejor PnL desde apertura)
        hw = Decimal(str(state.get("high_water", 0.0)))
        if pnl > hw:
            hw = pnl
            state["high_water"] = float(hw)
            save_state(state)

        # 1) TP directo
        if pnl >= TP_PCT:
            info(f"🎯 TP {sym} +{float(pnl)*100:.2f}% (avg={avg}, px={px})")
            base_asset = sym.replace(BASE_ASSET, "")
            free_asset = free_balance(base_asset)
            info(f"🧮 Pre-SELL {sym}: hint_qty={qty} | free_asset={free_asset}")
            if place_market_sell(sym, qty):
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["opened_ts"] = 0
                state["high_water"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
                tg_notify(f"🎯 <b>TP</b> {sym} +{float(pnl)*100:.2f}%")
            return

        # 2) Trailing armado y giveback
        if hw >= TRAIL_ARM_PCT and (hw - pnl) >= TRAIL_GIVEBACK_PCT:
            info(f"🏳️ Trailing {sym}: hw={float(hw)*100:.2f}% → pnl={float(pnl)*100:.2f}% "
                 f"| giveback={float(hw-pnl)*100:.2f}%")
            base_asset = sym.replace(BASE_ASSET, "")
            free_asset = free_balance(base_asset)
            info(f"🧮 Pre-SELL {sym}: hint_qty={qty} | free_asset={free_asset}")
            if place_market_sell(sym, qty):
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["opened_ts"] = 0
                state["high_water"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
                tg_notify(f"🏳️ <b>Trailing</b> {sym} hw={float(hw)*100:.2f}% → pnl={float(pnl)*100:.2f}%")
            return

        # 3) SL clásico
        if (-pnl) >= SL_PCT:
            info(f"🛑 SL {sym} -{float(pnl)*100:.2f}% (avg={avg}, px={px})")
            base_asset = sym.replace(BASE_ASSET, "")
            free_asset = free_balance(base_asset)
            info(f"🧮 Pre-SELL {sym}: hint_qty={qty} | free_asset={free_asset}")
            if place_market_sell(sym, qty):
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["opened_ts"] = 0
                state["high_water"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
                tg_notify(f"🛑 <b>SL</b> {sym} -{float(pnl)*100:.2f}%")
            return

        # 4) Break-even/fees si no arma trailing
        if pnl >= BE_EXIT_PCT and Decimal(str(state.get("high_water", 0.0))) < TRAIL_ARM_PCT:
            info(f"🔄 BE/fees exit {sym}: pnl=+{float(pnl)*100:.2f}% (sin armar trailing)")
            base_asset = sym.replace(BASE_ASSET, "")
            free_asset = free_balance(base_asset)
            info(f"🧮 Pre-SELL {sym}: hint_qty={qty} | free_asset={free_asset}")
            if place_market_sell(sym, qty):
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["opened_ts"] = 0
                state["high_water"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
                tg_notify(f"🔄 <b>BE/fees</b> {sym} cierre en +{float(pnl)*100:.2f}%")
            return

        # 5) Salida por estancamiento
        if minutes_open >= STALE_MIN and abs(pnl) < FLAT_PNL_PCT:
            info(f"⏱️ Estancamiento {sym}: {minutes_open} min con pnl={float(pnl)*100:.2f}% → rotación")
            base_asset = sym.replace(BASE_ASSET, "")
            free_asset = free_balance(base_asset)
            info(f"🧮 Pre-SELL {sym}: hint_qty={qty} | free_asset={free_asset}")
            if place_market_sell(sym, qty):
                state["open_symbol"] = None
                state["open_qty"] = 0.0
                state["avg_price"] = 0.0
                state["opened_ts"] = 0
                state["high_water"] = 0.0
                state["last_trade_ts"] = int(time.time())
                save_state(state)
                tg_notify(f"⏱️ <b>Flat exit</b> {sym} tras {minutes_open}m (pnl={float(pnl)*100:.2f}%)")
            return

        # Si nada aplica, solo informar
        info(f"📊 {sym} PnL={float(pnl)*100:.2f}% | hw={float(hw)*100:.2f}% | open_for={minutes_open}m")

    except Exception as e:
        warn(f"manage_position error: {e}")
        tg_notify(f"⚠️ manage_position error: <code>{e}</code>")

# ========= Apertura de posición (solo símbolos USDC válidos) =========
def position_can_open_now() -> bool:
    if COOLDOWN_SEC <= 0: return True
    last = int(state.get("last_trade_ts", 0))
    if last == 0: return True
    wait = COOLDOWN_SEC - int(time.time() - last)
    if wait > 0:
        info(f"⏳ Cooldown activo {wait}s.")
        return False
    return True

def scan_loop():
    info("🤖 Escaneando…")

    # Si hay posición abierta → gestionar
    if position_open():
        manage_position()
        return

    # Sin posición: verificar USDC
    free_usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {free_usdc:.2f}")
    if free_usdc < MIN_NOTIONAL_USDC:
        info(f"⛔ USDC insuficiente para operar (min={MIN_NOTIONAL_USDC}).")
        return

    if not position_can_open_now():
        return

    # Watchlist SOLO USDC y existente en exchange
    syms_conf = [s.strip().upper() for s in WATCHLIST.split(",") if s.strip()]
    symbols = [s for s in syms_conf if s.endswith(BASE_ASSET) and symbol_exists(s)]
    if not symbols:
        info("⛔ WATCHLIST vacía o inválida (USDC).")
        tg_notify("⛔ WATCHLIST vacía o inválida (USDC).")
        return

    # Puntuar y elegir mejor
    best_sym = None
    best_score = Decimal("-999")
    for s in symbols:
        sc, meta = score_symbol(s)
        info(f"[SIG] {s} score={float(sc):.4f} meta={meta}")
        if sc > best_score:
            best_score = sc
            best_sym = s

    if best_sym is None or best_score <= 0:
        info("🟡 Sin señal clara (score<=0). No compro.")
        return

    # Comprar con todo el USDC (según ALLOC_PCT)
    to_spend = free_usdc * ALLOC_PCT
    order, net_qty, px = place_market_buy(best_sym, to_spend)
    if order and net_qty and px:
        state["open_symbol"] = best_sym
        state["open_qty"] = float(net_qty)   # qty neta
        state["avg_price"] = float(px)
        state["opened_ts"] = int(time.time())
        state["high_water"] = 0.0
        state["last_trade_ts"] = int(time.time())
        save_state(state)
    else:
        info("🟠 No se pudo ejecutar compra: revisa logs anteriores.")

# ========= Arranque =========
def main():
    info("🤖 Bot iniciado.")
    refresh_exchange_info()
    info("✅ Exchange info cargada.")
    info(f"🧹 Dust: {'ignorado' if DUST_MODE=='IGNORE' else 'SELL'} (DUST_MODE={DUST_MODE}).")

    # Consolidar todo a USDC si está activo
    if AUTO_CONSOLIDATE:
        force_base_position_usdc()
    else:
        usdc = free_balance(BASE_ASSET)
        info(f"ℹ️ Arranque sin consolidación. {BASE_ASSET} libre: {usdc:.2f}")

    if position_open():
        sym = open_symbol()
        info(f"ℹ️ Sincronizado estado con posición actual: {sym}")
    else:
        info(f"ℹ️ Sin posición abierta. Base: {BASE_ASSET}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scan_loop, "interval",
        seconds=SCAN_INTERVAL_SEC,
        max_instances=1, coalesce=True, misfire_grace_time=10
    )
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        info("🛑 Detenido por usuario.")
    finally:
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    main()
