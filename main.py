import os, json, time, threading, sys
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import ssl
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# stdout sin buffer
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.streams import ThreadedWebsocketManager

# ===== OpenAI opcional (Grok; desactivado por defecto) =====
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

STATE_PATH = "state.json"

# ------------------ Utils ------------------
def env(k, d=None): return os.getenv(k, d)

def parse_float(s, default=0.0):
    if s is None: return float(default)
    try: return float(str(s).replace(",", "."))
    except Exception: return float(default)

def now_ts():
    dt = datetime.now(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            "positions": {},            # sym -> {entry, qty, best, opened_at}
            "pnl_history": {},
            "tokens_used": 0,
            "last_open": {},            # sym -> iso datetime
            "last_close": {},           # sym -> iso datetime
        }
    with open(STATE_PATH, "r") as f:
        return json.load(f)

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)

def iso_to_dt(s):
    try: return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None

# ------------------ Mini HTTP (Render health) ------------------
def start_http_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                b = f"OK {now_ts()}".encode("utf-8")
                self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
                self.wfile.write(b)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *args, **kwargs): return
    port = int(env("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), H)
    print(f"[HTTP] Listening on 0.0.0.0:{port}", flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ------------------ Telegram (stdlib) ------------------
def tg_http(method, params):
    token = env("TG_BOT_TOKEN")
    if not token: return False, {"error": "Falta TG_BOT_TOKEN"}, None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
            try: js = json.loads(body)
            except Exception: js = {"ok": False, "raw": body}
            ok = (r.status == 200) and bool(js.get("ok"))
            return ok, (None if ok else js), (js if ok else None)
    except Exception as e:
        return False, {"ok": False, "error": repr(e)}, None

def tg_send(msg):
    chat = env("TG_CHAT_ID")
    if not chat: return False, "CHAT_ID vacío"
    ok, err, _ = tg_http("sendMessage", {"chat_id": chat, "text": msg[:4000]})
    if not ok: print(f"[TG] ERROR -> {err}", flush=True)
    return ok, (None if ok else err)

def tg_autotest():
    token = env("TG_BOT_TOKEN"); chat = env("TG_CHAT_ID")
    if not token:
        print("[TG] Desactivado: falta TG_BOT_TOKEN", flush=True); return
    ok, err, data = tg_http("getMe", {})
    if ok:
        me = data.get("result", {})
        print(f"[TG] getMe OK @{me.get('username')}", flush=True)
    else:
        print(f"[TG] getMe ERROR -> {err}", flush=True)
    tg_send(f"🤖 Bot activo {now_ts()}")

# ------------------ Config ------------------
BINANCE_API_KEY = env("BINANCE_API_KEY")
BINANCE_API_SECRET = env("BINANCE_API_SECRET")

# Universo
AUTO_UNIVERSE = env("AUTO_UNIVERSE","true").lower()=="true"
UNIVERSE_MAX = int(env("UNIVERSE_MAX","40"))    # top N por volumen
SYMBOLS = [s.strip().upper() for s in env("SYMBOLS","BTCUSDC,ETHUSDC,SOLUSDC,DOGEUSDC,TRXUSDC,BNBUSDC,LINKUSDC").split(",") if s.strip()]

INTERVAL = env("INTERVAL","1m")
CANDLES  = int(env("CANDLES","200"))

RSI_LEN  = int(env("RSI_LEN","14"))
EMA_FAST = int(env("EMA_FAST","9"))
EMA_SLOW = int(env("EMA_SLOW","21"))

VOL_SPIKE = parse_float(env("VOL_SPIKE","0.90"), 0.90)
REQUIRE_VOL_SPIKE = env("REQUIRE_VOL_SPIKE","false").lower()=="true"
MIN_EXPECTED_GAIN_PCT = parse_float(env("MIN_EXPECTED_GAIN_PCT","0.0003"), 0.0003)  # ≈0.03%

TAKE_PROFIT_PCT = parse_float(env("TAKE_PROFIT_PCT","0.006"), 0.006)  # 0.6%
STOP_LOSS_PCT   = parse_float(env("STOP_LOSS_PCT","0.008"), 0.008)    # 0.8%
TRAIL_PCT       = parse_float(env("TRAIL_PCT","0.004"), 0.004)        # 0.4%

MIN_ORDER_USD   = parse_float(env("MIN_ORDER_USD","5"), 5)            # tickets pequeños si el par lo acepta
ALLOCATION_PCT  = parse_float(env("ALLOCATION_PCT","0.15"), 0.15)     # % por trade (diversifica)
MAX_OPEN_POSITIONS = int(env("MAX_OPEN_POSITIONS","6"))
MAX_SYMBOL_EXPOSURE_PCT = parse_float(env("MAX_SYMBOL_EXPOSURE_PCT","0.35"), 0.35)  # tope por símbolo / equity
MAX_BUYS_PER_LOOP = int(env("MAX_BUYS_PER_LOOP","3"))

COOLDOWN_MIN = int(env("COOLDOWN_MIN","10"))           # min tras cerrar para reentrar mismo símbolo
OPEN_COOLDOWN_MIN = int(env("OPEN_COOLDOWN_MIN","3"))  # min tras abrir para no reabrir enseguida

DAILY_MAX_LOSS_USD = parse_float(env("DAILY_MAX_LOSS_USD","25"), 25)
FEE_PCT = parse_float(env("FEE_PCT","0.001"), 0.001)
LOOP_SECONDS = int(env("LOOP_SECONDS","60"))

DEBUG = env("DEBUG","true").lower()=="true"
SEED_KLINES_ON_START = env("SEED_KLINES_ON_START","true").lower()=="true"
FORCE_BUY_SYMBOL = env("FORCE_BUY_SYMBOL","").strip().upper()
FORCE_BUY_USD = parse_float(env("FORCE_BUY_USD","0"), 0.0)

# Scalping/rotación
SCALP_ENV = os.getenv("SCALP_MIN_PROFIT_PCT")
SCALP_MIN_PROFIT_PCT = parse_float(SCALP_ENV, 2 * FEE_PCT + 0.0002)  # por defecto ≈ comisiones ida+vuelta + 0.02%
ROTATE_FOR_ENTRIES = env("ROTATE_FOR_ENTRIES","true").lower()=="true"

# Router (puentes si no hay par directo)
ROUTER_BRIDGES = [s.strip().upper() for s in env("QUOTE_ASSETS","USDC,USDT,BTC").split(",") if s.strip()]

# Grok (opcional)
GROK_ENABLE = env("GROK_ENABLE","false").lower()=="true"
GROK_BASE_URL = env("GROK_BASE_URL","https://api.x.ai/v1")
GROK_API_KEY = env("GROK_API_KEY")
GROK_MODEL = env("GROK_MODEL","grok")
GROK_MODELS_FALLBACK = [GROK_MODEL,"grok","grok-2-latest","grok-2-mini"]
_current_grok_model_idx = 0
MAX_TOKENS_DAILY = int(env("MAX_TOKENS_DAILY","2000"))

# --------- cliente Binance ---------
print(">> Creando cliente de Binance…", flush=True)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
print(">> Cliente Binance OK", flush=True)

llm = None
if GROK_ENABLE and GROK_API_KEY and OpenAI is not None:
    try:
        llm = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)
        print(f"[GROK] Cliente inicializado (modelo {GROK_MODEL})", flush=True)
    except Exception as e:
        print(f"[GROK] Deshabilitado por init error: {repr(e)}", flush=True)
        GROK_ENABLE = False

# ------------------ Indicadores ------------------
def ema(arr, p):
    arr = np.asarray(arr, float); k = 2/(p+1); out = np.zeros_like(arr); out[0]=arr[0]
    for i in range(1,len(arr)): out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def rsi(arr, period=14):
    arr = np.asarray(arr, float); delta = np.diff(arr)
    up = np.where(delta>0,delta,0.0); down = np.where(delta<0,-delta,0.0)
    roll_up = ema(up, period); roll_down = ema(down, period)
    rs = np.divide(roll_up, roll_down, out=np.zeros_like(roll_up), where=roll_down!=0)
    x = 100 - (100/(1+rs)); x = np.insert(x, 0, 50.0); return x

def atr_pct(H,L,C,period=14):
    H,L,C = map(lambda x: np.asarray(x,float),(H,L,C))
    trs=[]
    for i in range(1,len(C)):
        h,l,c1 = H[i],L[i],C[i-1]
        trs.append(max(h-l, abs(h-c1), abs(l-c1)))
    if not trs: return 0.0
    atr = ema(np.array(trs), period)[-1]
    return atr/float(C[-1])

# ------------------ Mercado / WS ------------------
_symbol_info_cache = {}
MARKET = {}            # sym -> {o,h,l,c,v,ready}
MARKET_LOCK = threading.Lock()

REQUIRED_BARS = max(RSI_LEN, EMA_SLOW, 14) + 2

def get_symbol_info(sym):
    if sym in _symbol_info_cache: return _symbol_info_cache[sym]
    info = client.get_symbol_info(sym)
    if not info: raise RuntimeError(f"Symbol info not found for {sym}")
    f = {flt["filterType"]: flt for flt in info["filters"]}
    step = Decimal(f["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(f["LOT_SIZE"]["minQty"])
    min_notional = Decimal(f.get("MIN_NOTIONAL",{}).get("minNotional","5"))
    _symbol_info_cache[sym] = {"step":step,"min_qty":min_qty,"min_notional":min_notional}
    return _symbol_info_cache[sym]

def round_step(qty, step):
    q = (Decimal(qty)/step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return float(q)

def _ensure_symbol_ws(sym):
    with MARKET_LOCK:
        if sym not in MARKET:
            MARKET[sym] = {"o":[], "h":[], "l":[], "c":[], "v":[], "ready":False}

def kline_handler(msg):
    try:
        k = msg.get("data",{}).get("k",{})
        sym = k.get("s"); 
        if not sym: return
        _ensure_symbol_ws(sym)
        o,h,l,c,v = float(k["o"]),float(k["h"]),float(k["l"]),float(k["c"]),float(k["v"])
        closed = bool(k.get("x", False))
        with MARKET_LOCK:
            buf = MARKET[sym]
            if closed or not buf["c"]:
                buf["o"].append(o); buf["h"].append(h); buf["l"].append(l); buf["c"].append(c); buf["v"].append(v)
            else:
                buf["o"][-1]=o; buf["h"][-1]=max(buf["h"][-1],h)
                buf["l"][-1]=min(buf["l"][-1],l); buf["c"][-1]=c; buf["v"][-1]=v
            for key in ("o","h","l","c","v"):
                if len(buf[key])>CANDLES: buf[key]=buf[key][-CANDLES:]
            buf["ready"] = len(buf["c"]) >= REQUIRED_BARS
    except Exception as e:
        print(f"[WS] handler error: {repr(e)}", flush=True)

def fetch_klines(sym, interval, limit):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if not buf or len(buf.get("c",[])) < REQUIRED_BARS:
            return None
        return (buf["o"][-limit:], buf["h"][-limit:], buf["l"][-limit:], buf["c"][-limit:], buf["v"][-limit:])

def get_last_price(sym, fallback_rest=True):
    with MARKET_LOCK:
        buf = MARKET.get(sym)
        if buf and buf.get("c"):
            return float(buf["c"][-1])
    if fallback_rest:
        return float(client.get_symbol_ticker(symbol=sym)["price"])
    raise RuntimeError(f"no price for {sym}")

# ------------------ Universo automático (opcional) ------------------
def build_auto_universe():
    global SYMBOLS
    try:
        ex = client.get_exchange_info()
        usdc_syms = []
        for s in ex["symbols"]:
            try:
                if s.get("quoteAsset")!="USDC": continue
                if s.get("status")!="TRADING": continue
                if s.get("permissions") and "SPOT" not in s["permissions"]: continue
                sym = s["symbol"]
                usdc_syms.append(sym)
            except Exception:
                continue
        tickers = client.get_ticker()
        vol_map = {t["symbol"]: float(t.get("quoteVolume",0.0)) for t in tickers}
        usdc_syms.sort(key=lambda x: vol_map.get(x,0.0), reverse=True)
        if UNIVERSE_MAX>0: usdc_syms = usdc_syms[:UNIVERSE_MAX]
        if usdc_syms:
            SYMBOLS = usdc_syms
            print(f"[UNIVERSE] AUTO: {len(SYMBOLS)} símbolos USDC top volumen", flush=True)
        else:
            print("[UNIVERSE] AUTO vacío; usando SYMBOLS env", flush=True)
    except Exception as e:
        print(f"[UNIVERSE] fallo auto: {repr(e)}; usando SYMBOLS env", flush=True)

# ------------------ Balance / exposición ------------------
BALANCE_CACHE = {"free": {}, "ts": 0.0}
BALANCE_TTL = 20

def get_all_balances():
    now = time.time()
    if now - BALANCE_CACHE["ts"] < BALANCE_TTL:
        return BALANCE_CACHE["free"]
    acc = client.get_account()
    free = {}
    for b in acc["balances"]:
        amt = float(b["free"])
        if amt > 0: free[b["asset"]] = amt
    BALANCE_CACHE["free"] = free; BALANCE_CACHE["ts"] = now
    return free

def _invalidate_balance_cache():
    BALANCE_CACHE["ts"] = 0.0

def get_free_usdc():
    return get_all_balances().get("USDC", 0.0)

def portfolio_value_usdc():
    bals = get_all_balances()
    total = float(bals.get("USDC", 0.0))
    for asset, qty in bals.items():
        if asset in ("USDC","BUSD","USD"): continue
        if qty <= 0: continue
        sym = f"{asset}USDC"
        try:
            px = get_last_price(sym, fallback_rest=False)
        except Exception:
            try: px = float(client.get_symbol_ticker(symbol=sym)["price"])
            except Exception: px = 0.0
        total += qty * px
    return total

def symbol_exposure_usdc(symbol):
    st = load_state()
    pos = st.get("positions", {}).get(symbol)
    if not pos: return 0.0
    return float(pos["qty"]) * float(pos["entry"])

# ------------------ PnL / riesgo ------------------
def today_key(): return date.today().isoformat()

def add_realized_pnl(amount_usd):
    st = load_state(); d = st.get("pnl_history", {}); day = today_key()
    d[day] = round(d.get(day, 0.0) + float(amount_usd), 6)
    st["pnl_history"] = d; save_state(st)

def reached_daily_loss():
    st = load_state(); pnl = st.get("pnl_history", {}).get(today_key(), 0.0)
    return pnl <= -abs(DAILY_MAX_LOSS_USD)

# ------------------ Órdenes ------------------
def place_buy(sym, quote_qty):
    info = get_symbol_info(sym)
    price = get_last_price(sym)
    qty = quote_qty / price
    qty = round_step(qty, info["step"])
    if qty < float(info["min_qty"]): raise RuntimeError(f"Qty {qty} < min_qty {sym}")
    order = client.order_market_buy(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order, qty, price

def place_buy_with_quote(symbol, quote_qty):
    """Compra BASE usando cantidad de QUOTE directamente (market buy con quoteOrderQty)."""
    info = get_symbol_info(symbol)
    if Decimal(str(quote_qty)) < info["min_notional"]:
        raise RuntimeError(f"quote_qty {quote_qty} < min_notional for {symbol}")
    order = client.order_market_buy(symbol=symbol, quoteOrderQty=round(float(quote_qty), 8))
    _invalidate_balance_cache()
    return order

def place_sell(sym, qty):
    info = get_symbol_info(sym)
    qty = round_step(qty, info["step"])
    order = client.order_market_sell(symbol=sym, quantity=qty)
    _invalidate_balance_cache()
    return order

# ------------------ Helpers de PnL flotante / Rotación ------------------
def unrealized_profit_pct(entry_price: float, current_price: float) -> float:
    buy_net = entry_price * (1 + FEE_PCT)
    sell_net = current_price * (1 - FEE_PCT)
    return (sell_net - buy_net) / buy_net

def sell_best_profitable_position(st) -> bool:
    best_sym = None
    best_up = 0.0
    for sym, pos in st.get("positions", {}).items():
        try:
            price = get_last_price(sym)
            up = unrealized_profit_pct(pos["entry"], price)
            if up > best_up:
                best_up = up
                best_sym = sym
        except Exception:
            continue

    if best_sym is None:
        return False

    scalp_min = SCALP_MIN_PROFIT_PCT
    if best_up >= scalp_min:
        sym = best_sym
        pos = st["positions"][sym]
        qty = pos["qty"]
        price = get_last_price(sym)
        place_sell(sym, qty)
        pnl = (price * (1 - FEE_PCT) - pos["entry"] * (1 + FEE_PCT)) * qty
        add_realized_pnl(pnl)
        st["positions"].pop(sym, None)
        st.setdefault("last_close", {})[sym] = now_ts()
        save_state(st)
        tg_send(f"💡 SELL SCALP {sym} @ {price:.8f} | +{best_up*100:.2f}% neto | PnL ≈ {pnl:.2f} USDC")
        return True
    return False

# ------------------ Seed de velas (REST, una vez) ------------------
def seed_klines_once(symbols, interval, limit=200):
    try:
        for sym in symbols:
            try:
                ks = client.get_klines(symbol=sym, interval=interval, limit=min(limit, 500))
                o,h,l,c,v = [],[],[],[],[]
                for k in ks:
                    o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3])); c.append(float(k[4])); v.append(float(k[5]))
                with MARKET_LOCK:
                    if sym not in MARKET:
                        MARKET[sym] = {"o":[], "h":[], "l":[], "c":[], "v":[], "ready":False}
                    buf = MARKET[sym]
                    buf["o"] = o[-CANDLES:]; buf["h"] = h[-CANDLES:]; buf["l"] = l[-CANDLES:]; buf["c"] = c[-CANDLES:]; buf["v"] = v[-CANDLES:]
                    buf["ready"] = len(buf["c"]) >= REQUIRED_BARS
                if DEBUG:
                    print(f"[SEED] {sym} {len(c)} velas (ready={buf['ready']})", flush=True)
                time.sleep(0.1)
            except Exception as e:
                print(f"[SEED] fallo {sym}: {repr(e)}", flush=True)
    except Exception as e:
        print(f"[SEED] error general: {repr(e)}", flush=True)

# ------------------ Cooldowns ------------------
def cooldown_open_allowed(st, sym):
    ts = st.get("last_open", {}).get(sym)
    if not ts: return True
    dt = iso_to_dt(ts)
    if not dt: return True
    return (datetime.now(timezone.utc) - dt) >= timedelta(minutes=OPEN_COOLDOWN_MIN)

def cooldown_reentry_after_close_allowed(st, sym):
    ts = st.get("last_close", {}).get(sym)
    if not ts: return True
    dt = iso_to_dt(ts)
    if not dt: return True
    return (datetime.now(timezone.utc) - dt) >= timedelta(minutes=COOLDOWN_MIN)

# ------------------ Router de pares (ANY->ANY) ------------------
EX_INFO_CACHE = {"pairs": set(), "base_of": {}, "quote_of": {}}

def refresh_pairs_cache():
    ex = client.get_exchange_info()
    pairs = set()
    base_of = {}
    quote_of = {}
    for s in ex["symbols"]:
        if s.get("status") != "TRADING": 
            continue
        base = s.get("baseAsset"); quote = s.get("quoteAsset")
        sym = s.get("symbol")
        pairs.add(sym)
        base_of.setdefault(base, set()).add((base, quote, sym))
        quote_of.setdefault(quote, set()).add((base, quote, sym))
    EX_INFO_CACHE["pairs"] = pairs
    EX_INFO_CACHE["base_of"] = base_of
    EX_INFO_CACHE["quote_of"] = quote_of

def has_pair(base, quote):
    sym = f"{base}{quote}"
    return sym in EX_INFO_CACHE["pairs"]

def symbol_name(base, quote):
    return f"{base}{quote}"

def estimate_usdc_price(asset):
    if asset == "USDC": return 1.0
    sym = f"{asset}USDC"
    try:
        px = get_last_price(sym, fallback_rest=True)
        return float(px)
    except Exception:
        return 0.0

def buy_base_using_quote(base_asset, quote_asset, quote_amount):
    """
    Intenta comprar 'base_asset' gastando 'quote_asset' (cantidad en unidades del quote).
    Preferimos par directo BASE/QUOTE (market buy con quoteOrderQty).
    Si no existe, intentamos vía puentes.
    """
    # 1) Directo BASE/QUOTE
    sym = symbol_name(base_asset, quote_asset)
    if has_pair(base_asset, quote_asset):
        try:
            place_buy_with_quote(sym, quote_amount * 0.995)  # pequeño margen para fees/lot
            tg_send(f"🔄 BUY {base_asset} usando {quote_amount:.8f} {quote_asset} en {sym}")
            return True
        except Exception as e:
            tg_send(f"⚠️ Fallo compra directa {sym}: {repr(e)}")

    # 2) Vía puentes: quote -> bridge -> base
    #    Primero convertimos quote -> bridge (comprando BRIDGE con QUOTE), luego BRIDGE -> base.
    bals = get_all_balances()
    have_quote = float(bals.get(quote_asset, 0.0))
    quote_amount = min(quote_amount, have_quote)

    for bridge in ROUTER_BRIDGES:
        if bridge == quote_asset:  # ya estás en puente
            # direct bridge -> base
            sym2 = symbol_name(base_asset, bridge)
            if has_pair(base_asset, bridge):
                try:
                    place_buy_with_quote(sym2, quote_amount * 0.995)
                    tg_send(f"🔀 BUY {base_asset} usando {quote_amount:.8f} {bridge} (puente) en {sym2}")
                    return True
                except Exception as e:
                    tg_send(f"⚠️ Fallo {sym2} (bridge->base): {repr(e)}")
            continue

        # quote -> bridge (comprar BRIDGE gastando QUOTE)
        sym1 = symbol_name(bridge, quote_asset)
        if not has_pair(bridge, quote_asset):
            continue
        try:
            place_buy_with_quote(sym1, quote_amount * 0.995)
            # ahora tenemos bridge (al menos parte)
            tg_send(f"🔁 SWAP {quote_asset}->{bridge} via {sym1}")
            # segundo salto: bridge -> base
            sym2 = symbol_name(base_asset, bridge)
            if has_pair(base_asset, bridge):
                # usar TODO el bridge disponible (menos margen)
                bals = get_all_balances()
                bridge_amt = float(bals.get(bridge, 0.0))
                if bridge_amt <= 0:
                    continue
                place_buy_with_quote(sym2, bridge_amt * 0.995)
                tg_send(f"🔀 BUY {base_asset} usando {bridge_amt:.8f} {bridge} en {sym2}")
                return True
        except Exception as e:
            tg_send(f"⚠️ Fallo ruta {sym1}→{base_asset}:{repr(e)}")
            continue
    return False

# ------------------ Estrategia ------------------
def evaluate_and_trade():
    if reached_daily_loss():
        if DEBUG: print("[RISK] límite diario de pérdida", flush=True)
        return

    st = load_state()
    buys_this_loop = 0
    rotated_this_loop = False

    open_positions = len(st.get("positions", {}))
    total_equity = portfolio_value_usdc()
    if DEBUG: print(f"[EQ] Equity total ≈ {total_equity:.2f} USDC | open={open_positions}", flush=True)

    for sym in SYMBOLS:
        data = fetch_klines(sym, INTERVAL, CANDLES)
        if data is None:
            if DEBUG: print(f"[WAIT] {sym} esperando barras (need {REQUIRED_BARS})", flush=True)
            continue

        try:
            o,h,l,c,v = data
            closes = np.array(c, float); vols = np.array(v, float); price = float(closes[-1])

            # Indicadores (RSI rápido in-situ para ahorrar cómputo)
            delta = np.diff(closes)
            up = np.where(delta>0,delta,0.0); down = np.where(delta<0,-delta,0.0)
            roll_up = ema(up, RSI_LEN); roll_down = ema(down, RSI_LEN)
            rs = np.divide(roll_up, roll_down, out=np.zeros_like(roll_up), where=roll_down!=0)
            rsi_val = float(100 - (100/(1+rs[-1])))

            ema_f = ema(closes, EMA_FAST)
            ema_s = ema(closes, EMA_SLOW)
            vol_base = vols[-50:-1].mean() if len(vols)>50 else (vols.mean() if len(vols) else 0.0)
            vol_ok = vols[-1] > VOL_SPIKE * max(vol_base, 1e-9)
            trend_up = ema_f[-1] > ema_s[-1]
            atrp = atr_pct(h, l, c, 14)

            ema_cross_up = (ema_f[-2] <= ema_s[-2]) and (ema_f[-1] > ema_s[-1])
            base_signal = (trend_up and rsi_val > 50) or (rsi_val < 35) or ema_cross_up

            if REQUIRE_VOL_SPIKE and not vol_ok:
                if DEBUG: print(f"[SKIP] {sym} volumen insuficiente", flush=True)
                continue

            if DEBUG:
                print(f"[SIG] {sym} px={price:.6f} rsi={rsi_val:.2f} ema_f={float(ema_f[-1]):.6f} "
                      f"ema_s={float(ema_s[-1]):.6f} vol_ok={vol_ok} atr%={atrp:.4f} "
                      f"cross_up={ema_cross_up} trend_up={trend_up}", flush=True)

            pos = st["positions"].get(sym)
            in_pos = pos is not None

            # ----- Gestión en posición -----
            if in_pos:
                entry = pos["entry"]; qty = pos["qty"]; best = pos.get("best", entry)
                tp_price = entry * (1 + TAKE_PROFIT_PCT)
                sl_price = entry * (1 - STOP_LOSS_PCT)

                if price > best:
                    pos["best"] = price; best = price

                # 1) TP por SCALPING (ganancia neta ≥ umbral)
                up = unrealized_profit_pct(entry, price)
                if up >= SCALP_MIN_PROFIT_PCT:
                    place_sell(sym, qty)
                    pnl = (price * (1 - FEE_PCT) - entry * (1 + FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    st.setdefault("last_close", {})[sym] = now_ts()
                    save_state(st)
                    tg_send(f"💡 SELL SCALP {sym} @ {price:.8f} | +{up*100:.2f}% neto | PnL ≈ {pnl:.2f} USDC")
                    continue

                # 2) TP clásico
                if price >= tp_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    st.setdefault("last_close", {})[sym] = now_ts()
                    save_state(st)
                    tg_send(f"✅ SELL TP {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                # 3) Trailing si va en verde
                if best > entry and price <= best * (1 - TRAIL_PCT):
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    st.setdefault("last_close", {})[sym] = now_ts()
                    save_state(st)
                    tg_send(f"⚠️ SELL TRAIL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                # 4) Stop Loss
                if price <= sl_price:
                    place_sell(sym, qty)
                    pnl = (price*(1-FEE_PCT) - entry*(1+FEE_PCT)) * qty
                    add_realized_pnl(pnl)
                    st["positions"].pop(sym, None)
                    st.setdefault("last_close", {})[sym] = now_ts()
                    save_state(st)
                    tg_send(f"❌ SELL SL {sym} @ {price:.8f} | PnL ≈ {pnl:.2f} USDC")
                    continue

                save_state(st)
                continue

            # ----- Entrada -----
            if not base_signal:
                if DEBUG: print(f"[SKIP] {sym} sin señal base", flush=True)
                continue

            if atrp < MIN_EXPECTED_GAIN_PCT:
                if DEBUG: print(f"[SKIP] {sym} ATR% insuficiente ({atrp:.4f} < {MIN_EXPECTED_GAIN_PCT})", flush=True)
                continue

            if open_positions >= MAX_OPEN_POSITIONS:
                if DEBUG: print(f"[SKIP] {sym} límite global de posiciones ({open_positions}/{MAX_OPEN_POSITIONS})", flush=True)
                continue

            if not cooldown_open_allowed(st, sym):
                if DEBUG: print(f"[SKIP] {sym} cooldown tras abrir", flush=True)
                continue
            if not cooldown_reentry_after_close_allowed(st, sym):
                if DEBUG: print(f"[SKIP] {sym} cooldown tras cerrar", flush=True)
                continue

            sym_exp = symbol_exposure_usdc(sym)
            if total_equity > 0 and (sym_exp / total_equity) >= MAX_SYMBOL_EXPOSURE_PCT:
                if DEBUG: print(f"[SKIP] {sym} exposición {sym_exp/total_equity:.2%} >= cap {MAX_SYMBOL_EXPOSURE_PCT:.0%}", flush=True)
                continue

            if buys_this_loop >= MAX_BUYS_PER_LOOP:
                if DEBUG: print(f"[SKIP] {sym} límite buys loop ({buys_this_loop}/{MAX_BUYS_PER_LOOP})", flush=True)
                continue

            # tamaño deseado en USDC aprox (si no hay USDC, estimamos con el asset pagador)
            usdc = get_free_usdc()
            desired_usdc = 0.995 * max(MIN_ORDER_USD, usdc * ALLOCATION_PCT)
            base_asset = sym.replace("USDC","")

            if DEBUG: print(f"[BAL] USDC libre ≈ {usdc:.2f} | desired_usdc ≈ {desired_usdc:.2f}", flush=True)

            if usdc >= MIN_ORDER_USD:
                # Compra clásica con USDC
                info = get_symbol_info(sym)
                if Decimal(str(desired_usdc)) < info["min_notional"]:
                    desired_usdc = float(info["min_notional"]) + 1.0
                order, qty, raw_entry = place_buy(sym, desired_usdc)
                st["positions"][sym] = {"entry": raw_entry*(1+FEE_PCT), "qty": qty, "best": raw_entry, "opened_at": now_ts()}
                st.setdefault("last_open",{})[sym] = now_ts()
                save_state(st)
                tg_send(f"🟢 BUY {sym} {qty} @ {raw_entry:.8f} | Notional ≈ {qty*raw_entry:.2f} USDC")
                buys_this_loop += 1
                open_positions += 1
                continue

            # === NUEVO: compra base usando otra cripto como "quote" (ANY->BASE) ===
            # elegimos un asset pagador con saldo (excluyendo USDC y el propio base_asset)
            bals = get_all_balances()
            payers = [(a, q) for a, q in bals.items() if a not in ("USDC","BUSD","USD") and a != base_asset and q > 0]

            # intenta pagar con el asset que mejor cubra el desired_usdc (según precio a USDC)
            payers_scored = []
            for a, q in payers:
                px = estimate_usdc_price(a)
                if px <= 0: continue
                payers_scored.append((a, q, px, q*px))  # (asset, qty, px_usdc, notional_usdc)
            payers_scored.sort(key=lambda x: x[3], reverse=True)  # mayor capacidad primero

            bought = False
            for a, q, px, notion in payers_scored:
                # gastaremos hasta cubrir desired_usdc, o la mitad del saldo si es demasiado
                need_quote = min(q*0.98, desired_usdc / max(px, 1e-9))
                if need_quote <= 0:
                    continue

                ok = buy_base_using_quote(base_asset, a, need_quote)
                if ok:
                    # tras comprar base con "a", necesitamos registrar la posición en sym (BASEUSDC)
                    # obtenemos precio actual de sym para fijar entry (con comisión)
                    raw_entry = get_last_price(sym)
                    # estimar qty base disponible: consultamos balance de base
                    new_bal = get_all_balances()
                    base_qty = float(new_bal.get(base_asset, 0.0))
                    # cantidad "nueva" aproximada: tomamos un 98% del incremento (simplificado)
                    # para no hacernos un lío, si ya teníamos algo del base_asset, igualará; no pasa nada.
                    if base_qty <= 0:
                        continue
                    # Redondeo a step del par BASEUSDC
                    info = get_symbol_info(sym)
                    base_qty_rd = round_step(base_qty, info["step"])
                    if base_qty_rd <= 0:
                        continue
                    st["positions"][sym] = {"entry": raw_entry*(1+FEE_PCT), "qty": base_qty_rd, "best": raw_entry, "opened_at": now_ts()}
                    st.setdefault("last_open",{})[sym] = now_ts()
                    save_state(st)
                    tg_send(f"🟢 BUY {sym} usando {a} (sin USDC) | qty≈{base_qty_rd}")
                    buys_this_loop += 1
                    open_positions += 1
                    bought = True
                    break

            if bought:
                continue

            # Si no pudo comprar con otras cripto, intenta rotar (vender la mejor en verde) para liberar USDC
            if ROTATE_FOR_ENTRIES and not rotated_this_loop:
                did = sell_best_profitable_position(st)
                rotated_this_loop = did

        except BinanceAPIException as be:
            if be.code == -1003:
                tg_send("⛔️ Peso REST alto. Usamos WS; espera a que se levante.")
            else:
                tg_send(f"⚠️ BinanceAPIException {sym}: {be.status_code} {be.message}")
            time.sleep(3)
        except Exception as e:
            print(f"[ERR] {sym} {repr(e)}", flush=True)
            tg_send(f"⚠️ Error {sym}: {repr(e)}")

# ------------------ Heartbeat ------------------
def heartbeat():
    while True:
        print(f"[HB] alive {now_ts()} — symbols: {','.join(SYMBOLS)}", flush=True)
        time.sleep(60)

# ------------------ Main ------------------
def main():
    print(f"[BOOT] {now_ts()} — starting...", flush=True)

    # Construye cache de pares para routing ANY->ANY
    refresh_pairs_cache()

    # Universo auto (opcional)
    if AUTO_UNIVERSE:
        build_auto_universe()

    start_http_server()
    tg_autotest()

    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    for s in SYMBOLS:
        _ensure_symbol_ws(s)
        twm.start_kline_socket(callback=kline_handler, symbol=s.lower(), interval=INTERVAL)
    print(f"[WS] Streams iniciados: {len(SYMBOLS)}", flush=True)

    if SEED_KLINES_ON_START:
        seed_klines_once(SYMBOLS, INTERVAL, limit=CANDLES)

    # Compra forzada opcional (quítalo después)
    if FORCE_BUY_SYMBOL and FORCE_BUY_SYMBOL in SYMBOLS and FORCE_BUY_USD > 0:
        try:
            info = get_symbol_info(FORCE_BUY_SYMBOL)
            desired = max(MIN_ORDER_USD, FORCE_BUY_USD)
            if Decimal(str(desired)) < info["min_notional"]:
                desired = float(info["min_notional"]) + 1.0
            order, qty, raw_entry = place_buy(FORCE_BUY_SYMBOL, desired)
            st = load_state()
            st["positions"][FORCE_BUY_SYMBOL] = {"entry": raw_entry*(1+FEE_PCT), "qty": qty, "best": raw_entry, "opened_at": now_ts()}
            st.setdefault("last_open",{})[FORCE_BUY_SYMBOL] = now_ts()
            save_state(st)
            tg_send(f"🧪 BUY FORZADO {FORCE_BUY_SYMBOL} {qty} @ {raw_entry:.8f} | Notional ≈ {qty*raw_entry:.2f} USDC")
            print(f"[FORCE] buy {FORCE_BUY_SYMBOL} ok", flush=True)
        except Exception as e:
            print(f"[FORCE] fallo: {repr(e)}", flush=True)
            tg_send(f"⚠️ BUY FORZADO falló: {repr(e)}")

    threading.Thread(target=heartbeat, daemon=True).start()

    while True:
        try:
            evaluate_and_trade()
        except Exception as e:
            print(f"[MAIN LOOP] error: {repr(e)}", flush=True)
            tg_send(f"🔥 Loop error: {repr(e)}")
        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    main()
