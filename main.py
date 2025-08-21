# -*- coding: utf-8 -*-
"""
Bot UNIVERSAL (Spot Binance) sin moneda base fija.
- Elige HUB estable dinámico (USDT/USDC/FDUSD/TUSD) o el que indiques.
- Consolida TODA la cartera al HUB (vende lo no estable; respeta BNB para comisiones).
- Limpia restos (dust) y saldos por debajo de minNotional.
- Compra/Vende cualquier cripto usando par ASSET/HUB; si no existe, hace puente vía USDT.
- Gestión por ATR (SL/TP/Trailing), control de pérdida diaria por %.

Requisitos:
  python-binance, numpy, pytz, apscheduler, requests
"""

import os, time, json, random, logging, threading, requests
import pytz, numpy as np
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta

from binance.client import Client
from binance.exceptions import BinanceAPIException
from apscheduler.schedulers.background import BackgroundScheduler

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("bot-universal")

# ──────────────────────────────────────────────────────────────────────────────
# ENV / Config
# ──────────────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

# Hub estable: AUTO (elige mejor) o fuerza uno (USDT/USDC/FDUSD/TUSD)
STABLE_HUB = os.getenv("STABLE_HUB", "AUTO").upper()
STABLE_CANDIDATES = [x.strip().upper() for x in os.getenv("STABLE_CANDIDATES","USDT,USDC,FDUSD,TUSD").split(",")]

# Reservar algo de BNB para fees
BNB_FEE_RESERVE = float(os.getenv("BNB_FEE_RESERVE", "0.02"))
# Considerar "restos" por debajo de este valor en USD como dust
DUST_USD = float(os.getenv("DUST_USD", "1.0"))

# Universo de compra (se formará ASSET/HUB); si vacío, se elige por volumen
ALLOWED_ASSETS = [x.strip().upper() for x in os.getenv("ALLOWED_ASSETS","BTC,ETH,SOL,BNB,TON,AVAX,LINK,NEAR,ADA,DOGE,TRX,MATIC,OP,ARB,ATOM").split(",") if x.strip()]

# Filtros / estrategia
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "100000"))
SPREAD_MAX_PCT = float(os.getenv("SPREAD_MAX_PCT", "0.15"))

ATR_MULT_SL = float(os.getenv("ATR_MULT_SL", "1.5"))
ATR_MULT_TP = float(os.getenv("ATR_MULT_TP", "1.0"))
ATR_TRAIL   = float(os.getenv("ATR_TRAIL",   "1.2"))
RSI_BUY_MIN = float(os.getenv("RSI_BUY_MIN", "40"))
RSI_BUY_MAX = float(os.getenv("RSI_BUY_MAX", "62"))
RSI_SELL_MIN = float(os.getenv("RSI_SELL_MIN","58"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.15"))  # ATR/price*100 mínimo

TRADE_COOLDOWN_SEC = int(os.getenv("TRADE_COOLDOWN_SEC", "90"))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "18"))
COOLDOWN_POST_STOP_MIN = int(os.getenv("COOLDOWN_POST_STOP_MIN", "30"))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "6"))

# Riesgo diario por equity (%)
PERDIDA_MAXIMA_DIARIA_PCT = float(os.getenv("PERDIDA_MAXIMA_DIARIA_PCT", "3.0"))

TZ_MADRID = pytz.timezone("Europe/Madrid")
RESUMEN_HORA = int(os.getenv("RESUMEN_HORA", "23"))

# Archivos
REGISTRO_FILE = "registro.json"  # posiciones abiertas (por símbolo ASSETHUB)
STATE_FILE    = "state.json"     # equity de inicio del día y pnl

# ──────────────────────────────────────────────────────────────────────────────
# Clientes
# ──────────────────────────────────────────────────────────────────────────────
if not API_KEY or not API_SECRET:
    raise ValueError("Faltan BINANCE_API_KEY / BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET)

# ──────────────────────────────────────────────────────────────────────────────
# Estado / caches
# ──────────────────────────────────────────────────────────────────────────────
LOCK = threading.RLock()
SYMBOL_CACHE = {}
INVALID_SYMBOL_CACHE = set()

ULTIMAS_OPERACIONES = []
ULTIMA_COMPRA = {}
POST_STOP_BAN = {}   # símbolo -> ts desbloqueo

ALL_TICKERS = {}
ALL_TICKERS_TS = 0.0
ALL_TICKERS_TTL = 45

# ──────────────────────────────────────────────────────────────────────────────
# Utils básicos
# ──────────────────────────────────────────────────────────────────────────────
def now_tz(): return datetime.now(TZ_MADRID)
def today_key(): return now_tz().date().isoformat()

def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path,"r") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Error leyendo {path}: {e}")
    return {} if default is None else default

def save_json(data, path):
    tmp = path + ".tmp"
    with open(tmp,"w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def enviar_tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TG OFF] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

def dec(x:str)->Decimal:
    try: return Decimal(x)
    except (InvalidOperation,TypeError): return Decimal('0')

# ──────────────────────────────────────────────────────────────────────────────
# Binance helpers (backoff)
# ──────────────────────────────────────────────────────────────────────────────
def binance_call(fn, *args, tries=6, base_delay=0.9, max_delay=240, **kwargs):
    import re
    att=0
    while True:
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            msg=str(e); code=getattr(e,"code",None); status=getattr(e,"status_code",None)
            m=re.search(r"IP banned until\s+(\d{13})", msg)
            if m:
                ban_until_ms=int(m.group(1)); now_ms=int(time.time()*1000)
                wait_s=max(0,(ban_until_ms-now_ms)/1000.0)+2.0
                log.warning(f"[BAN] Sleep {wait_s:.1f}s")
                time.sleep(min(wait_s,900)); att=0; continue
            if code in (-1003,) or status in (418,429) or "Too many requests" in msg:
                att+=1; 
                if att>tries: raise
                delay=min(max_delay,(2.0**att)*base_delay+random.random())
                log.warning(f"[RATE] Backoff {delay:.1f}s (try {att}/{tries})")
                time.sleep(delay); continue
            att+=1
            if att<=tries:
                delay=min(max_delay,(1.7**att)*base_delay+random.random())
                log.warning(f"[API] Retry {delay:.1f}s (try {att}/{tries})")
                time.sleep(delay); continue
            raise
        except requests.exceptions.RequestException as e:
            att+=1
            if att<=tries:
                delay=min(max_delay,(1.7**att)*base_delay+random.random())
                log.warning(f"[NET] Retry {delay:.1f}s")
                time.sleep(delay); continue
            raise

# ──────────────────────────────────────────────────────────────────────────────
# Mercado / símbolos
# ──────────────────────────────────────────────────────────────────────────────
def refresh_all_tickers(force=False):
    global ALL_TICKERS, ALL_TICKERS_TS
    now=time.time()
    if not force and (now-ALL_TICKERS_TS)<ALL_TICKERS_TTL: return
    data=binance_call(client.get_ticker)
    ALL_TICKERS={t.get("symbol"):t for t in data if t.get("symbol")}
    ALL_TICKERS_TS=now

def load_symbol_info(symbol):
    if symbol in INVALID_SYMBOL_CACHE: return None
    if symbol in SYMBOL_CACHE: return SYMBOL_CACHE[symbol]
    try:
        info=binance_call(client.get_symbol_info, symbol=symbol)
        if info is None:
            INVALID_SYMBOL_CACHE.add(symbol); return None
        filters={f['filterType']:f for f in info['filters']}
        lot=filters.get('LOT_SIZE',{})
        mlot=filters.get('MARKET_LOT_SIZE',None) or lot
        pricef=filters.get('PRICE_FILTER',{})
        notional=filters.get('NOTIONAL',{}) or filters.get('MIN_NOTIONAL',{})
        meta={
            "baseAsset":info['baseAsset'],
            "quoteAsset":info['quoteAsset'],
            "marketStepSize":dec(mlot.get('stepSize','0')),
            "marketMinQty":dec(mlot.get('minQty','0')),
            "tickSize":dec(pricef.get('tickSize','0')),
            "minNotional":dec(notional.get('minNotional','0')) if notional else dec('0'),
            "applyToMarket": bool(notional.get('applyToMarket', True)) if notional else True
        }
        SYMBOL_CACHE[symbol]=meta
        return meta
    except Exception as e:
        log.info(f"Symbol info error {symbol}: {e}")
        INVALID_SYMBOL_CACHE.add(symbol); return None

def quantize_qty(qty:Decimal, step:Decimal)->Decimal:
    if step<=0: return qty
    steps=(qty/step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return (steps*step).normalize()

def min_quote_for_market(symbol)->Decimal:
    meta=load_symbol_info(symbol)
    if not meta: return Decimal('0')
    return (meta["minNotional"]*Decimal('1.02')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def symbol_exists(symbol:str)->bool:
    return load_symbol_info(symbol) is not None

# ──────────────────────────────────────────────────────────────────────────────
# Indicadores (5m por REST)
# ──────────────────────────────────────────────────────────────────────────────
def get_klines(symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=200):
    return binance_call(client.get_klines, symbol=symbol, interval=interval, limit=limit)

def ema(arr, period):
    if len(arr)<period: return float(arr[-1])
    m=2/(period+1.0)
    e=float(np.mean(arr[:period]))
    for x in arr[period:]:
        e=(x-e)*m+e
    return e

def rsi(arr, period=14):
    if len(arr)<period+1: return 50.0
    deltas=np.diff(arr)
    up=deltas.clip(min=0).sum()/period
    down=-deltas.clip(max=0).sum()/period
    rs=up/(down if down!=0 else 1e-9)
    r=100-(100/(1+rs))
    au,ad=up,down
    for d in deltas[period:]:
        upv=max(d,0); dnv=-min(d,0)
        au=(au*(period-1)+upv)/period
        ad=(ad*(period-1)+dnv)/period
        rs=au/(ad if ad!=0 else 1e-9)
        r=100-(100/(1+rs))
    return float(r)

def atr_from_klines(ks, period=14):
    highs=np.array([float(k[2]) for k in ks],dtype=float)
    lows =np.array([float(k[3]) for k in ks],dtype=float)
    closes=np.array([float(k[4]) for k in ks],dtype=float)
    trs=[]
    for i in range(1,len(closes)):
        hl=highs[i]-lows[i]
        hc=abs(highs[i]-closes[i-1])
        lc=abs(lows[i]-closes[i-1])
        trs.append(max(hl,hc,lc))
    if len(trs)<period: return np.mean(trs) if trs else 0.0
    atr=np.mean(trs[:period])
    for tr in trs[period:]:
        atr=(atr*(period-1)+tr)/period
    return float(atr), float(closes[-1])

# ──────────────────────────────────────────────────────────────────────────────
# Estado diario / equity
# ──────────────────────────────────────────────────────────────────────────────
def init_day_state():
    st=load_json(STATE_FILE,{})
    tk=today_key()
    if st.get("day")!=tk:
        st={"day":tk,"start_equity":cartera_value_usdt(),"pnl_today":0.0}
        save_json(st, STATE_FILE)
    return st

def update_pnl_today(delta):
    st=load_json(STATE_FILE,{})
    st.setdefault("day", today_key())
    st["pnl_today"]=round(st.get("pnl_today",0.0)+float(delta),8)
    save_json(st, STATE_FILE)
    return st["pnl_today"]

def daily_risk_ok():
    st=init_day_state()
    start_eq=st.get("start_equity",0.0) or cartera_value_usdt()
    max_loss=start_eq*(PERDIDA_MAXIMA_DIARIA_PCT/100.0)
    return st.get("pnl_today",0.0) > -max_loss + 1e-6

# ──────────────────────────────────────────────────────────────────────────────
# Precios / conversión
# ──────────────────────────────────────────────────────────────────────────────
def price(symbol):
    refresh_all_tickers()
    t=ALL_TICKERS.get(symbol)
    return float(t.get("lastPrice",0) or 0) if t else 0.0

def pair_price(base, quote):
    """Devuelve precio de 1 base en quote (si existe el par directo o inverso o vía USDT)."""
    if base==quote: return 1.0
    direct=f"{base}{quote}"
    if symbol_exists(direct):
        p=price(direct); return p if p>0 else 0.0
    inverse=f"{quote}{base}"
    if symbol_exists(inverse):
        p=price(inverse); return (1.0/p) if p>0 else 0.0
    # puente vía USDT
    if base!="USDT":
        b_usdt=pair_price(base,"USDT")
        if b_usdt<=0: return 0.0
    else:
        b_usdt=1.0
    if quote!="USDT":
        usdt_q=pair_price("USDT",quote)
        if usdt_q<=0: return 0.0
    else:
        usdt_q=1.0
    return b_usdt*usdt_q

# ──────────────────────────────────────────────────────────────────────────────
# Cartera / hub
# ──────────────────────────────────────────────────────────────────────────────
def balances():
    acct=binance_call(client.get_account)
    bals={b['asset']: (float(b['free'])+float(b['locked'])) for b in acct['balances'] if (float(b['free'])+float(b['locked']))>0}
    return bals

def cartera_value_usdt():
    bals=balances()
    total=0.0
    for a,qty in bals.items():
        p=pair_price(a,"USDT")
        total+=qty*(p or 0)
    return total

def pick_hub():
    if STABLE_HUB!="AUTO":
        return STABLE_HUB
    bals=balances()
    # Elige el estable con mayor valor en USDT; si ninguno, devuelve "USDT"
    best="USDT"; best_val=-1.0
    for s in STABLE_CANDIDATES:
        q=bals.get(s,0.0)
        if q<=0: continue
        val=q*pair_price(s,"USDT")
        if val>best_val:
            best_val=val; best=s
    return best

# ──────────────────────────────────────────────────────────────────────────────
# Trade helpers (incluye puente via USDT si hace falta)
# ──────────────────────────────────────────────────────────────────────────────
def market_sell(symbol, qty, meta):
    q=quantize_qty(dec(str(qty)), meta["marketStepSize"])
    if q<=0: return None
    return binance_call(client.order_market_sell, symbol=symbol, quantity=f"{q:.12f}")

def market_buy_quote(symbol, quote_amount):
    # compra a mercado gastando 'quote_amount' (en la moneda cotizada)
    return binance_call(client.create_order, symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=f"{quote_amount:.8f}")

def ensure_pair_and_sell(base_asset, quote_asset):
    """Vende base_asset a quote_asset. Si no hay par directo, hace puente vía USDT."""
    if base_asset==quote_asset:
        return True
    sym=f"{base_asset}{quote_asset}"
    if symbol_exists(sym):
        meta=load_symbol_info(sym)
        bal=asset_free(base_asset)
        if bal<=0: return True
        # minNotional
        p=price(sym); minq=float(min_quote_for_market(sym))
        if bal*p < minq: return False
        resp=market_sell(sym, bal, meta)
        return True if resp else False
    # No directo: vende a USDT, luego convierte USDT->quote
    if base_asset!="USDT":
        if not ensure_pair_and_sell(base_asset, "USDT"): return False
    if quote_asset!="USDT":
        # comprar quote usando USDT
        sym2=f"{quote_asset}USDT"
        if not symbol_exists(sym2):
            # si no existe, nos quedamos en USDT y cambiamos HUB global
            return True
        usdt=asset_free("USDT")
        minq=float(min_quote_for_market(sym2))
        if usdt< (minq or 1e-6): return True
        market_buy_quote(sym2, usdt)  # compra quote gastando todo USDT
    return True

def asset_free(asset):
    try:
        b=binance_call(client.get_asset_balance, asset=asset)
        if not b: return 0.0
        return float(b.get('free',0))
    except Exception:
        return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# Limpieza & consolidación
# ──────────────────────────────────────────────────────────────────────────────
def sweep_dust(hub):
    """Intenta vender saldos pequeños al HUB si superan minNotional; ignora BNB reserva."""
    bals=balances()
    refresh_all_tickers()
    for a,qty in bals.items():
        if a in (hub,"USDT"): continue
        if a=="BNB" and qty<=BNB_FEE_RESERVE: continue
        sym=f"{a}{hub}"
        px=pair_price(a,hub)
        val=qty*(px or 0)
        if val<=0: continue
        # Si menor a DUST_USD, ignorar (no llega a minNotional casi seguro)
        if val < DUST_USD: continue
        if symbol_exists(sym):
            minq=float(min_quote_for_market(sym))
            if val<minq:
                continue
            meta=load_symbol_info(sym)
            try:
                market_sell(sym, qty, meta)
                enviar_tg(f"🧹 Dust -> Vendido {qty:.8f} {a} a {hub}")
            except Exception as e:
                log.warning(f"Dust {a}->{hub} fallo: {e}")
        else:
            # intenta puente via USDT
            try:
                ok=ensure_pair_and_sell(a, hub)
                if ok: enviar_tg(f"🧹 Dust puente: {a} -> {hub}")
            except Exception as e:
                log.warning(f"Dust puente {a}->{hub} fallo: {e}")

def consolidate_to_hub(hub):
    """Vende TODO lo que no sea HUB (y no sea BNB reserva) hacia el HUB."""
    bals=balances()
    refresh_all_tickers()
    for a,qty in bals.items():
        if a==hub: continue
        if a=="BNB" and qty<=BNB_FEE_RESERVE: continue
        if qty<=0: continue
        if a in STABLE_CANDIDATES and a!="USDT" and hub=="USDT":
            # cambiar estable a USDT
            sym=f"{a}USDT"
            if symbol_exists(sym):
                minq=float(min_quote_for_market(sym))
                val=qty*price(sym)
                if val>=minq:
                    try:
                        meta=load_symbol_info(sym)
                        market_sell(sym, qty, meta)
                        continue
                    except Exception as e:
                        log.warning(f"Consolid stable {a}->USDT fallo: {e}")
        # general
        try:
            ok=ensure_pair_and_sell(a, hub)
            if ok: log.info(f"Consolidado {a} -> {hub}")
        except Exception as e:
            log.warning(f"Consolid {a}->{hub} fallo: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Registro posiciones (por símbolo ASSET+HUB)
# ──────────────────────────────────────────────────────────────────────────────
def reg_load(): return load_json(REGISTRO_FILE,{})
def reg_save(r):  save_json(r, REGISTRO_FILE)

def limpiar_registro_dust(min_usd=1.0, hub=None):
    r=reg_load()
    changed=False
    for sym,data in list(r.items()):
        # sym = ASSET+HUB
        px=price(sym) if hub is None else pair_price(sym.replace(hub,""), hub)
        qty=float(data.get("cantidad",0))
        val=qty*(px or 0)
        if val<min_usd or qty<=0:
            r.pop(sym,None); changed=True
    if changed:
        reg_save(r); enviar_tg("🧹 Registro limpiado (<$1).")

# ──────────────────────────────────────────────────────────────────────────────
# Señales de compra
# ──────────────────────────────────────────────────────────────────────────────
def mejores_simbolos(hub, maxc=20):
    refresh_all_tickers()
    cands=[]
    for t in ALL_TICKERS.values():
        sym=t.get("symbol","")
        if not sym.endswith(hub): continue
        base=sym[:-len(hub)]
        if ALLOWED_ASSETS and base not in ALLOWED_ASSETS: continue
        vol=float(t.get("quoteVolume",0) or 0)
        last=float(t.get("lastPrice",0) or 0)
        if vol>=MIN_VOLUME and last>0:
            cands.append((sym, vol))
    cands.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in cands[:maxc]]

def spread_pct(sym):
    # usa bookTicker si quisieras; aquí aproximamos con last (mejorable)
    return 0.0

def comprar(hub):
    log.info("[JOB] comprar()")
    if not daily_risk_ok():
        log.info("Límite de pérdida diaria alcanzado.")
        return
    r=reg_load()
    if r:
        log.info("Ya hay posición abierta; 1 a la vez.")
        return

    hub_bal=asset_free(hub)
    if hub_bal<=5:
        log.info(f"Saldo {hub} insuficiente para comprar: {hub_bal:.2f}")
        return

    now_ts=time.time()
    global ULTIMAS_OPERACIONES
    ULTIMAS_OPERACIONES=[t for t in ULTIMAS_OPERACIONES if now_ts-t<3600]
    if len(ULTIMAS_OPERACIONES)>=MAX_TRADES_PER_HOUR:
        log.info("Tope de trades/hora."); return

    # Explora mejores símbolos y aplica filtros 5m (EMA/RSI/ATR)
    for sym in mejores_simbolos(hub):
        try:
            ks=get_klines(sym, limit=200)
            closes=np.array([float(k[4]) for k in ks],dtype=float)
            if len(closes)<60: continue
            ema50=ema(closes,50)
            ema200=ema(closes,200)
            last=float(closes[-1])
            if not (ema50>ema200 and last>ema50): 
                continue
            rsi14=rsi(closes,14)
            if not (RSI_BUY_MIN<=rsi14<=RSI_BUY_MAX):
                continue
            atr, price_last = atr_from_klines(ks,14)
            atr_pct=(atr/price_last)*100.0 if price_last>0 else 0.0
            if atr_pct<MIN_ATR_PCT: 
                continue

            # Spread (si implementas bookTicker, aquí puedes medirlo)
            if SPREAD_MAX_PCT>0 and spread_pct(sym)>SPREAD_MAX_PCT:
                continue

            # minNotional
            minq=float(min_quote_for_market(sym))
            if hub_bal<minq: 
                log.info(f"{sym}: {hub} {hub_bal:.2f} < minNotional {minq:.2f}")
                continue

            # Compra 100% del HUB
            order=market_buy_quote(sym, hub_bal)
            fills=order.get('fills') or []
            exec_price=last
            qty=sum(float(f['qty']) for f in fills) if fills else (hub_bal/last)
            if fills:
                cost=sum(float(f['price'])*float(f['qty']) for f in fills)
                q=sum(float(f['qty']) for f in fills)
                exec_price=cost/q if q>0 else last

            sl=exec_price - ATR_MULT_SL*atr
            tp=exec_price + ATR_MULT_TP*atr

            with LOCK:
                r=reg_load()
                r[sym]={
                    "cantidad": float(qty),
                    "precio_compra": float(exec_price),
                    "timestamp": now_tz().isoformat(),
                    "high_since_buy": float(exec_price),
                    "sl": float(sl), "tp": float(tp), "be": None
                }
                reg_save(r)

            enviar_tg(f"🟢 BUY {sym} ~{hub_bal:.2f} {hub} @ {exec_price:.6f} | RSI {rsi14:.1f} ATR% {atr_pct:.2f}")
            ULTIMAS_OPERACIONES.append(now_ts)
            break  # 1 compra
        except Exception as e:
            log.error(f"Compra {sym} err: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Gestión de posiciones (SL/TP/Trailing/Time)
# ──────────────────────────────────────────────────────────────────────────────
def gestionar_posiciones():
    log.info("[JOB] gestionar_posiciones()")
    r=reg_load()
    if not r: return

    nuevos={}
    to_clean=[]
    for sym,data in r.items():
        try:
            ks=get_klines(sym, limit=120)
            atr, last = atr_from_klines(ks,14)
            closes=np.array([float(k[4]) for k in ks],dtype=float)
            rsi14=rsi(closes,14)

            qty=float(data.get("cantidad",0))
            if qty<=0:
                to_clean.append(sym); continue

            entry=float(data["precio_compra"])
            high=float(data.get("high_since_buy", entry))
            sl=data.get("sl"); tp=data.get("tp"); be=data.get("be")

            if last>high: high=last
            if be is None and last >= (entry + 0.75*ATR_MULT_TP*atr):
                be = max(entry, entry + 0.05*atr)
            trail = max((high - ATR_TRAIL*atr), (be if be else -1e9))

            hit_sl = last <= (sl if sl is not None else entry - ATR_MULT_SL*atr)
            hit_tp = last >= (tp if tp is not None else entry + ATR_MULT_TP*atr)
            hit_trail = last <= trail

            ts_open = datetime.fromisoformat(data["timestamp"])
            open_secs = (now_tz() - ts_open.replace(tzinfo=TZ_MADRID)).total_seconds()
            too_long = open_secs >= MAX_HOLD_HOURS*3600
            take_rsi = (rsi14 >= RSI_SELL_MIN) and last > entry

            reason=None
            if hit_sl or hit_trail or too_long:
                reason = "SL" if hit_sl else ("TRAIL" if hit_trail else "TIME")
            elif hit_tp or take_rsi:
                reason = "TP/RSI"

            if reason:
                meta=load_symbol_info(sym)
                if not meta:
                    nuevos[sym]=data; continue
                order=market_sell(sym, qty, meta)
                # precio ejecución aprox:
                exec_p=last
                try:
                    fills=order.get('fills') or []
                    if fills:
                        qf=sum(float(f['qty']) for f in fills)
                        cf=sum(float(f['price'])*float(f['qty']) for f in fills)
                        exec_p=cf/qf if qf>0 else last
                except Exception:
                    pass
                pnl=(exec_p - entry)*qty
                total=update_pnl_today(pnl)
                enviar_tg(f"🔴 SELL {sym} {qty:.8f} @ {exec_p:.6f} | {reason} | PnL {pnl:.2f} | PnL hoy {total:.2f}")
                to_clean.append(sym)
            else:
                data["high_since_buy"]=high
                data["sl"]=float(entry - ATR_MULT_SL*atr) if sl is None else float(sl)
                data["tp"]=float(entry + ATR_MULT_TP*atr) if tp is None else float(tp)
                data["be"]=be
                nuevos[sym]=data
        except Exception as e:
            log.error(f"Gestión {sym} err: {e}")
            nuevos[sym]=data
    for s in to_clean:
        if s in nuevos: del nuevos[s]
    reg_save(nuevos)

# ──────────────────────────────────────────────────────────────────────────────
# Jobs utilitarios
# ──────────────────────────────────────────────────────────────────────────────
def resumen_diario():
    try:
        st=init_day_state()
        total=cartera_value_usdt()
        pnl=st.get("pnl_today",0.0)
        enviar_tg(f"📊 Resumen ({today_key()}): PnL {pnl:.2f} USDT | Equity {total:.2f} USDT | Inicio {st.get('start_equity',0):.2f}")
    except Exception as e:
        log.error(f"Resumen err: {e}")

def housekeeping(hub):
    """Consolida y limpia restos periódicamente."""
    try:
        consolidate_to_hub(hub)
        sweep_dust(hub)
        limpiar_registro_dust(min_usd=DUST_USD, hub=hub)
    except Exception as e:
        log.error(f"Housekeeping err: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    hub = pick_hub()
    enviar_tg(f"🤖 Bot Universal activo. HUB: {hub}. Consolidando cartera…")
    consolidate_to_hub(hub)
    sweep_dust(hub)
    init_day_state()
    limpiar_registro_dust(min_usd=DUST_USD, hub=hub)

    scheduler=BackgroundScheduler(timezone=TZ_MADRID)
    scheduler.add_job(lambda: comprar(hub), 'interval', minutes=4, id="comprar")
    scheduler.add_job(gestionar_posiciones, 'interval', minutes=1, id="gestionar")
    scheduler.add_job(lambda: housekeeping(hub), 'interval', minutes=15, id="housekeeping")
    scheduler.add_job(resumen_diario, 'cron', hour=RESUMEN_HORA, minute=0, id="resumen")
    scheduler.add_job(init_day_state, 'cron', hour=0, minute=3, id="reset")
    scheduler.start()

    # Log de jobs
    for job in scheduler.get_jobs():
        log.info(f"[SCHED] {job.id} next at {job.next_run_time}")

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Bot detenido.")
