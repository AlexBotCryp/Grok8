# main.py
import os, json, time, math
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException

# ============ CONFIG ENTORNO ============
API_KEY        = os.getenv("BINANCE_API_KEY", "")
API_SECRET     = os.getenv("BINANCE_API_SECRET", "")
LIVE_MODE      = os.getenv("LIVE_MODE", "1") == "1"

BASE_ASSET     = os.getenv("FORCE_BASE", "USDC").upper()
AUTO_CONSOLIDATE   = os.getenv("AUTO_CONSOLIDATE", "1") == "1"   # consolida restos a USDC
DUST_MODE          = os.getenv("DUST_MODE", "IGNORE").upper()    # IGNORE o SELL
MIN_NOTIONAL_USDC  = Decimal(os.getenv("MIN_NOTIONAL_USDC", "20"))

SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", "15"))
STATE_PATH         = os.getenv("STATE_PATH", "state.json")

# ======= ESTRATEGIA (anti-churn y con ATR) =======
# Comisiones (taker) para cubrir ida/vuelta:
FEE_TAKER_PCT   = Decimal(os.getenv("FEE_TAKER_PCT", "0.001"))   # 0.10% por lado típico
MIN_PROFIT_USDC = Decimal(os.getenv("MIN_PROFIT_USDC", "2.0"))   # beneficio mínimo NETO por trade

# Sizing / capital
ALLOC_PCT       = Decimal(os.getenv("ALLOC_PCT", "1.0"))         # 100% del USDC libre

# Volatilidad y señales
ATR_PERIOD      = int(os.getenv("ATR_PERIOD", "14"))
MIN_ATR_PCT     = Decimal(os.getenv("MIN_ATR_PCT", "0.0015"))    # 0.15% mínimo de volatilidad para operar
TP_ATR_MULT     = Decimal(os.getenv("TP_ATR_MULT", "1.2"))       # TP = avg + 1.2 * ATR
SL_ATR_MULT     = Decimal(os.getenv("SL_ATR_MULT", "1.0"))       # SL = avg - 1.0 * ATR
TRAIL_ARM_MULT  = Decimal(os.getenv("TRAIL_ARM_MULT", "0.8"))    # arma trailing cuando pnl >= 0.8*ATR/avg (%)
TRAIL_GIVEBACK  = Decimal(os.getenv("TRAIL_GIVEBACK", "0.5"))    # cede 0.5*ATR/avg desde el pico => venta

# Control de tiempos
MIN_HOLD_MIN        = int(os.getenv("MIN_HOLD_MIN", "20"))       # tiempo mínimo en posición
SYMBOL_COOLDOWN_MIN = int(os.getenv("SYMBOL_COOLDOWN_MIN", "60"))# no re-entrar antes de X minutos en el mismo símbolo
MAX_HOLD_MIN        = int(os.getenv("MAX_HOLD_MIN", "240"))      # límite “blando” de 4h

# Watchlist SOLO USDC (liquidez alta)
WATCHLIST = os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC,LTCUSDC"
)

# Telegram
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TG_OFF     = os.getenv("TELEGRAM_DISABLE", "0") == "1"

# ============ LOG ============
LOG_PREFIX = lambda lvl: f"{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {lvl} -"
def info(msg): print(f"{LOG_PREFIX('INFO')} {msg}")
def warn(msg): print(f"{LOG_PREFIX('WARNING')} {msg}")
def err (msg): print(f"{LOG_PREFIX('ERROR')} {msg}")

def tg_notify(text: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True):
    if TG_OFF or not TG_TOKEN or not TG_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text[:3900], "parse_mode": parse_mode,
               "disable_web_page_preview": disable_web_page_preview}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200: return True
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                time.sleep(min(max(wait,2),10)); continue
            print(f"[TG] HTTP {r.status_code}: {r.text}"); return False
        except Exception as e:
            print(f"[TG] Error: {e}"); time.sleep(1.5)
    return False

# ============ BINANCE ============
if not API_KEY or not API_SECRET:
    warn("Claves Binance no configuradas. Modo lectura.")
client = Client(API_KEY, API_SECRET)

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f: return json.load(f)
    except Exception: return {}

def save_state(st: dict):
    try:
        with open(STATE_PATH, "w") as f: json.dump(st, f, indent=2)
    except Exception as e: warn(f"No se pudo guardar estado: {e}")

state = load_state()
state.setdefault("base_asset", BASE_ASSET)
state.setdefault("open_symbol", None)     # p.ej. "BTCUSDC"
state.setdefault("open_qty", 0.0)         # qty neta
state.setdefault("avg_price", 0.0)
state.setdefault("opened_ts", 0)
state.setdefault("high_water_pnl", 0.0)   # mejor pnl% desde apertura
state.setdefault("last_trade_ts", 0)
state.setdefault("symbol_cooldowns", {})  # { "BTCUSDC": epoch_ts }

SYMBOL_INFO: Dict[str, dict] = {}

def refresh_exchange_info():
    global SYMBOL_INFO
    ex = client.get_exchange_info()
    SYMBOL_INFO = {s["symbol"]: s for s in ex["symbols"]}

def symbol_exists(symbol: str) -> bool: return symbol in SYMBOL_INFO

def _get_filter(symbol: str, ftype: str) -> Optional[dict]:
    info_s = SYMBOL_INFO.get(symbol); 
    if not info_s: return None
    for f in info_s["filters"]:
        if f["filterType"] == ftype: return f
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

# ======= PRECIOS / VELAS / ATR =======
def get_klines(symbol: str, interval="1m", limit=ATR_PERIOD+30) -> List[Tuple[Decimal,Decimal,Decimal]]:
    raws = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    # devuelve (high, low, close)
    return [(Decimal(str(k[2])), Decimal(str(k[3])), Decimal(str(k[4]))) for k in raws]

def last_price(symbol: str) -> Decimal:
    r = client.get_symbol_ticker(symbol=symbol)
    return Decimal(str(r["price"]))

def calc_atr(symbol: str, period: int = ATR_PERIOD) -> Tuple[Decimal, Decimal]:
    """Devuelve (ATR_abs, ATR_pct_sobre_close)."""
    kl = get_klines(symbol, limit=period+1)
    if len(kl) < period+1: return Decimal("0"), Decimal("0")
    trs = []
    prev_close = kl[0][2]
    for (h,l,c) in kl[1:]:
        tr = max(h-l, abs(h-prev_close), abs(l-prev_close))
        trs.append(tr); prev_close = c
    atr = sum(trs) / Decimal(len(trs))
    last_close = kl[-1][2]
    atr_pct = atr / last_close if last_close > 0 else Decimal("0")
    return atr, atr_pct

# ======= BALANCES / CONSOLIDACIÓN (solo a USDC) =======
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
        if q > 0: bals[b["asset"]] = q
    return bals

def sell_all_to_usdc(asset: str, qty: Decimal) -> bool:
    if asset == BASE_ASSET: return False
    symbol = f"{asset}{BASE_ASSET}"
    if not symbol_exists(symbol): 
        info(f"🚫 Sin par {symbol}. No convierto {asset}."); 
        return False
    px = last_price(symbol)
    notional = qty * px
    ex_min = min_notional(symbol)
    min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if notional < min_needed:
        info(f"🟡 {asset}: notional {notional:.4f} < min {min_needed:.4f}. {('Ignoro' if DUST_MODE=='IGNORE' else 'Intento vender')}")
        if DUST_MODE == "IGNORE": return False
    q_round = round_by_step(qty, lot_step(symbol))
    if q_round <= 0: return False
    if LIVE_MODE:
        try:
            info(f"🔁 Consolida {asset}->{BASE_ASSET} qty={q_round}")
            client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=float(q_round))
            time.sleep(0.7); return True
        except Exception as e:
            err(f"Consolidación fallo {asset}->{BASE_ASSET}: {e}"); tg_notify(f"⚠️ Consolidación {asset}->USDC: <code>{e}</code>")
            return False
    else:
        info(f"[DRY] Vendería {asset}->{BASE_ASSET} qty={q_round}"); return True

def force_base_position_usdc():
    bals = all_free_balances()
    non_base = {a:q for a,q in bals.items() if a != BASE_ASSET and q > 0}
    if non_base:
        info(f"🧹 Consolidación: {len(non_base)} activos ≠ {BASE_ASSET}.")
        for a,q in non_base.items(): sell_all_to_usdc(a,q)
    usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {usdc:.2f}")
    # limpiar estado de posición
    state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
    state["opened_ts"]=0; state["high_water_pnl"]=0.0
    save_state(state)
    info("✅ Posición base forzada: USDC. Sin posición abierta.")
    tg_notify("✅ Iniciado y consolidado a <b>USDC</b>. Sin posición abierta.")

# ======= ÓRDENES con qty neta y venta segura =======
def place_market_buy(symbol: str, usdc_to_spend: Decimal):
    px = last_price(symbol)
    if px <= 0: info(f"⛔ {symbol} sin precio."); tg_notify(f"⛔ {symbol} sin precio."); return None, None, None
    # Notional mínimo
    ex_min = min_notional(symbol); min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if usdc_to_spend < min_needed:
        info(f"⛔ Notional insuficiente {usdc_to_spend:.2f}<{min_needed:.2f}"); return None,None,None
    # Qty requerida (redondeo por step)
    step = lot_step(symbol)
    req_qty = usdc_to_spend / px
    req_qty = round_by_step(req_qty, step) if step>0 else req_qty
    if req_qty <= 0: info("⛔ Qty=0"); return None,None,None

    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=float(req_qty))
            base_asset = symbol.replace(BASE_ASSET,"")
            filled_qty = Decimal("0"); commission_in_base = Decimal("0")
            if "fills" in o:
                for f in o["fills"]:
                    filled_qty += Decimal(str(f.get("qty","0")))
                    if f.get("commissionAsset")==base_asset:
                        commission_in_base += Decimal(str(f.get("commission","0")))
            net_qty = filled_qty - commission_in_base
            if net_qty <= 0: net_qty = free_balance(base_asset)
            info(f"✅ COMPRA {symbol} qty_neta={net_qty} notional≈{(net_qty*px):.2f}")
            tg_notify(f"🟢 <b>COMPRA</b> {symbol}\nQty neta: <code>{net_qty}</code>\nPrecio≈<code>{px:.6f}</code>\nNotional≈<code>{(net_qty*px):.2f} USDC</code>")
            return o, net_qty, px
        except BinanceAPIException as be:
            err(f"BUY {symbol}: {be.message}"); tg_notify(f"⛔ BUY {symbol}: <code>{be.message}</code>"); return None,None,None
        except Exception as e:
            err(f"BUY {symbol}: {e}"); tg_notify(f"⛔ BUY {symbol}: <code>{e}</code>"); return None,None,None
    else:
        info(f"[DRY] BUY {symbol} qty={req_qty} notional≈{(req_qty*px):.2f}"); return {"dry":True}, req_qty, px

SAFETY_SELL_PCT = Decimal(os.getenv("SAFETY_SELL_PCT", "0.999"))
def place_market_sell(symbol: str, qty_hint: Decimal):
    base_asset = symbol.replace(BASE_ASSET,""); free_asset = free_balance(base_asset)
    qty_raw = min(qty_hint, free_asset) * SAFETY_SELL_PCT
    q = round_by_step(qty_raw, lot_step(symbol)) if lot_step(symbol)>0 else qty_raw
    if q <= 0: info(f"⛔ Venta 0 {symbol} (free={free_asset}, hint={qty_hint})"); return None
    if LIVE_MODE:
        try:
            o = client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=float(q))
            px = last_price(symbol)
            info(f"✅ VENTA {symbol} qty={q} notional≈{(q*px):.2f}")
            tg_notify(f"🔴 <b>VENTA</b> {symbol}\nQty: <code>{q}</code>\nPrecio≈<code>{px:.6f}</code>\nNotional≈<code>{(q*px):.2f} USDC</code>")
            return o
        except BinanceAPIException as be:
            err(f"SELL {symbol}: {be.message}"); tg_notify(f"⛔ SELL {symbol}: <code>{be.message}</code>"); return None
        except Exception as e:
            err(f"SELL {symbol}: {e}"); tg_notify(f"⛔ SELL {symbol}: <code>{e}</code>"); return None
    else:
        info(f"[DRY] SELL {symbol} qty={q}"); return {"dry":True}

# ======= ESTRATEGIA (ATR + beneficio mínimo neto) =======
def expected_net_profit_ok(symbol: str, entry_px: Decimal, qty: Decimal, atr_abs: Decimal) -> bool:
    """Comprueba que el objetivo TP (entry + TP_ATR_MULT*ATR) deje >= MIN_PROFIT_USDC tras fees ida+vuelta."""
    tp_px = entry_px + TP_ATR_MULT * atr_abs
    gross = (tp_px - entry_px) * qty
    fees = (entry_px * qty + tp_px * qty) * FEE_TAKER_PCT   # ida + vuelta
    net  = gross - fees
    return net >= MIN_PROFIT_USDC

def symbol_on_cooldown(symbol: str) -> bool:
    cd = state.get("symbol_cooldowns", {})
    last = cd.get(symbol, 0)
    if not last: return False
    wait = SYMBOL_COOLDOWN_MIN*60 - int(time.time()-last)
    if wait > 0:
        info(f"⏳ Cooldown {symbol} {wait}s.")
        return True
    return False

def open_position(symbol: str, usdc_free: Decimal):
    # ATR filtro
    atr_abs, atr_pct = calc_atr(symbol, ATR_PERIOD)
    if atr_pct < MIN_ATR_PCT:
        info(f"🧊 {symbol} ATR% bajo ({float(atr_pct)*100:.2f}%) < {float(MIN_ATR_PCT)*100:.2f}%"); 
        return
    # compra sólo si TP esperado supera fees + MIN_PROFIT_USDC
    px = last_price(symbol)
    to_spend = (usdc_free * ALLOC_PCT)
    # Ensure notional ok
    ex_min = min_notional(symbol); min_needed = ex_min if ex_min > 0 else MIN_NOTIONAL_USDC
    if to_spend < min_needed:
        info(f"⛔ Notional insuficiente para {symbol}"); return
    # qty tentativa para check de beneficio
    step = lot_step(symbol)
    qty_tent = round_by_step(to_spend/px, step) if step>0 else (to_spend/px)
    if qty_tent <= 0: info("⛔ qty_tent=0"); return
    if not expected_net_profit_ok(symbol, px, qty_tent, atr_abs):
        info(f"❎ {symbol} TP esperado no cubre fees + MIN_PROFIT_USDC."); 
        return

    order, net_qty, entry_px = place_market_buy(symbol, to_spend)
    if order and net_qty and entry_px:
        state["open_symbol"] = symbol
        state["open_qty"]    = float(net_qty)
        state["avg_price"]   = float(entry_px)
        state["opened_ts"]   = int(time.time())
        state["high_water_pnl"] = 0.0
        save_state(state)

def manage_position():
    sym = state.get("open_symbol")
    if not sym: return
    try:
        px   = last_price(sym)
        avg  = Decimal(str(state.get("avg_price", 0)))
        qty  = Decimal(str(state.get("open_qty", 0)))
        if avg<=0 or qty<=0:
            info("⚠️ Estado inconsistente, cierro mem-pos."); 
            state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
            state["opened_ts"]=0; state["high_water_pnl"]=0.0; save_state(state); return

        pnl = (px - avg) / avg
        opened_ts = int(state.get("opened_ts", 0))
        minutes_open = int((time.time()-opened_ts)/60) if opened_ts else 0

        # ATR del momento para targets dinámicos
        atr_abs, atr_pct = calc_atr(sym, ATR_PERIOD)
        if atr_abs == 0:
            info("ATR=0, mantengo."); return

        # High-water PnL
        hw = Decimal(str(state.get("high_water_pnl", 0.0)))
        if pnl > hw:
            hw = pnl; state["high_water_pnl"] = float(hw); save_state(state)

        # Targets
        tp_px = avg + TP_ATR_MULT*atr_abs
        sl_px = avg - SL_ATR_MULT*atr_abs
        tp_hit = px >= tp_px
        sl_hit = px <= sl_px

        # Trailing armado?
        trail_arm = (hw >= (TRAIL_ARM_MULT * (atr_abs/avg)))  # comparar en %
        trail_giveback = (trail_arm and ((hw - pnl) >= (TRAIL_GIVEBACK * (atr_abs/avg))))

        # Beneficio real en USDC ahora:
        gross_now = max(Decimal("0"), (px - avg) * qty)
        fees_now  = (avg*qty + px*qty) * FEE_TAKER_PCT
        net_now   = gross_now - fees_now

        # 1) SL por ATR
        if sl_hit and minutes_open >= MIN_HOLD_MIN:
            info(f"🛑 SL {sym} ATR ({float(pnl)*100:.2f}%)")
            if place_market_sell(sym, qty):
                state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
                state["opened_ts"]=0; state["high_water_pnl"]=0.0
                # cooldown símbolo
                cds = state.get("symbol_cooldowns", {}); cds[sym]=int(time.time()); state["symbol_cooldowns"]=cds
                state["last_trade_ts"]=int(time.time()); save_state(state); 
                tg_notify(f"🛑 SL {sym} ({float(pnl)*100:.2f}%)"); 
            return

        # 2) Trailing por ATR (si ya armó y cede)
        if trail_giveback and minutes_open >= MIN_HOLD_MIN:
            # Sólo cerrar si deja NETO suficiente (o al menos > 0) o si reversiona fuerte
            if net_now >= MIN_PROFIT_USDC or hw - pnl >= (TRAIL_GIVEBACK * (atr_abs/avg) * 1.5):
                info(f"🏳️ Trailing {sym} hw={float(hw)*100:.2f}% → pnl={float(pnl)*100:.2f}% (net≈{float(net_now):.2f} USDC)")
                if place_market_sell(sym, qty):
                    state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
                    state["opened_ts"]=0; state["high_water_pnl"]=0.0
                    cds = state.get("symbol_cooldowns", {}); cds[sym]=int(time.time()); state["symbol_cooldowns"]=cds
                    state["last_trade_ts"]=int(time.time()); save_state(state)
                    tg_notify(f"🏳️ Trailing {sym} cierre net≈{float(net_now):.2f} USDC")
                return
            else:
                info(f"⏩ Trailing armado pero neto insuficiente aún (net≈{float(net_now):.2f} USDC)")

        # 3) TP por ATR
        if tp_hit and minutes_open >= MIN_HOLD_MIN:
            # sólo si deja neto >= mínimo
            if expected_net_profit_ok(sym, avg, qty, atr_abs) and net_now >= MIN_PROFIT_USDC:
                info(f"🎯 TP {sym} ATR + net≈{float(net_now):.2f} USDC")
                if place_market_sell(sym, qty):
                    state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
                    state["opened_ts"]=0; state["high_water_pnl"]=0.0
                    cds = state.get("symbol_cooldowns", {}); cds[sym]=int(time.time()); state["symbol_cooldowns"]=cds
                    state["last_trade_ts"]=int(time.time()); save_state(state)
                    tg_notify(f"🎯 TP {sym} net≈{float(net_now):.2f} USDC")
                return
            else:
                info(f"🟡 TP tocado pero neto < mínimo (net≈{float(net_now):.2f} USDC); sigo con trailing")

        # 4) Break-even ampliado (sólo si cumple mínimo neto)
        if minutes_open >= MIN_HOLD_MIN and net_now >= MIN_PROFIT_USDC:
            # si no hay volatilidad suficiente para ATR trailing, aceptamos cierre rentable
            info(f"🔄 BE/Fees OK {sym}: net≈{float(net_now):.2f} USDC (tras fees)")
            if place_market_sell(sym, qty):
                state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
                state["opened_ts"]=0; state["high_water_pnl"]=0.0
                cds = state.get("symbol_cooldowns", {}); cds[sym]=int(time.time()); state["symbol_cooldowns"]=cds
                state["last_trade_ts"]=int(time.time()); save_state(state)
                tg_notify(f"🔄 BE/Fees {sym} net≈{float(net_now):.2f} USDC")
            return

        # 5) Max hold: si pasan 4h y el neto no llega al mínimo y la volatilidad cayó, salir si es leve ganancia
        if minutes_open >= MAX_HOLD_MIN:
            if net_now > Decimal("0") and atr_pct < MIN_ATR_PCT:
                info(f"⏱️ MAX HOLD {sym}: cierro con net≈{float(net_now):.2f} USDC (vol baja)")
                if place_market_sell(sym, qty):
                    state["open_symbol"]=None; state["open_qty"]=0.0; state["avg_price"]=0.0
                    state["opened_ts"]=0; state["high_water_pnl"]=0.0
                    cds = state.get("symbol_cooldowns", {}); cds[sym]=int(time.time()); state["symbol_cooldowns"]=cds
                    state["last_trade_ts"]=int(time.time()); save_state(state)
                    tg_notify(f"⏱️ MAX HOLD {sym} net≈{float(net_now):.2f} USDC")
                return

        info(f"📊 {sym} pnl={float(pnl)*100:.2f}% | hw={float(hw)*100:.2f}% | hold={minutes_open}m | ATR%={float(atr_pct)*100:.2f}% | net≈{float(net_now):.2f} USDC")

    except Exception as e:
        warn(f"manage_position error: {e}")
        tg_notify(f"⚠️ manage_position error: <code>{e}</code>")

# ======= LOOP =======
def scan_loop():
    info("🤖 Escaneando…")

    # 1) Con posición abierta -> gestionar
    if state.get("open_symbol"): 
        manage_position(); 
        return

    # 2) Sin posición: sólo USDC libre
    usdc = free_balance(BASE_ASSET)
    info(f"💰 {BASE_ASSET} libre: {usdc:.2f}")
    if usdc < MIN_NOTIONAL_USDC: 
        info("⛔ USDC insuficiente."); 
        return

    # 3) Lista USDC válida y no en cooldown
    syms_conf = [s.strip().upper() for s in WATCHLIST.split(",") if s.strip()]
    symbols = [s for s in syms_conf if s.endswith(BASE_ASSET) and symbol_exists(s)]
    if not symbols:
        info("⛔ WATCHLIST vacía/ inválida USDC."); tg_notify("⛔ WATCHLIST vacía/ inválida USDC."); return
    symbols = [s for s in symbols if not symbol_on_cooldown(s)]
    if not symbols: 
        info("🟡 Todos en cooldown."); 
        return

    # 4) Ranking simple por ATR% (prioriza volatilidad útil)
    ranked = []
    for s in symbols:
        atr_abs, atr_pct = calc_atr(s, ATR_PERIOD)
        ranked.append((s, atr_pct))
        info(f"[VOL] {s} ATR%={float(atr_pct)*100:.2f}%")
    ranked.sort(key=lambda x: x[1], reverse=True)

    # 5) Probar abrir en orden de mayor ATR% (hasta encontrar uno que cumpla beneficio neto mínimo)
    for s, _ in ranked:
        open_position(s, usdc)
        if state.get("open_symbol"): break  # abrió una

# ======= ARRANQUE =======
def main():
    info("🤖 Bot iniciado.")
    refresh_exchange_info(); info("✅ Exchange info cargada.")
    if AUTO_CONSOLIDATE: force_base_position_usdc()
    else: info(f"ℹ️ Consolidación desactivada. {BASE_ASSET} libre: {free_balance(BASE_ASSET):.2f}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_loop, "interval", seconds=SCAN_INTERVAL_SEC, max_instances=1, coalesce=True, misfire_grace_time=10)
    scheduler.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        info("🛑 Detenido por usuario.")
    finally:
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    main()
