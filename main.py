import os, json, time, threading, sys
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_DOWN
import ssl
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# logs sin buffer
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

print(">> Boot sequence starting...", flush=True)

import numpy as np
from dateutil import tz
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.streams import ThreadedWebsocketManager

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

STATE_PATH = "state.json"

# ===================== Utilidades =====================
def env(key, default=None): return os.getenv(key, default)

def parse_float(s, default=0.0):
    try: return float(str(s).replace(",", "."))
    except Exception: return float(default)

def now_ts():
    dt = datetime.now(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def load_state():
    if not os.path.exists(STATE_PATH): return {"positions": {}, "pnl_history": {}, "tokens_used": 0}
    with open(STATE_PATH, "r") as f: return json.load(f)

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)

# ===================== Mini HTTP server =====================
def start_http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                body = f"OK {now_ts()}".encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(body)
            else: self.send_response(404); self.end_headers()
        def log_message(self, fmt, *args): return
    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[HTTP] Listening on 0.0.0.0:{port}", flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ===================== Telegram =====================
def tg_http(method, params: dict):
    token = env("TG_BOT_TOKEN"); 
    if not token: return False, {"error": "Falta TG_BOT_TOKEN"}, None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try: js = json.loads(body)
            except Exception: js = {"ok": False, "raw": body}
            ok = (resp.status == 200) and bool(js.get("ok"))
            return ok, (None if ok else js), (js if ok else None)
    except Exception as e: return False, {"error": repr(e)}, None

def tg_send(msg: str):
    chat = env("TG_CHAT_ID"); 
    if not chat: return False, "CHAT_ID vacío"
    ok, err, _ = tg_http("sendMessage", {"chat_id": chat, "text": msg[:4000]})
    if not ok: print(f"[TG] ERROR -> {err}", flush=True)
    return ok, (None if ok else err)

# ===================== Configuración =====================
BINANCE_API_KEY = env("BINANCE_API_KEY"); BINANCE_API_SECRET = env("BINANCE_API_SECRET")
SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "BTCUSDC,ETHUSDC,SOLUSDC,DOGEUSDC,TRXUSDC").split(",") if s.strip()]
INTERVAL = env("INTERVAL", "1m"); CANDLES = int(env("CANDLES", "200"))
RSI_LEN=int(env("RSI_LEN", "14")); EMA_FAST=int(env("EMA_FAST","9")); EMA_SLOW=int(env("EMA_SLOW","21"))
VOL_SPIKE=parse_float(env("VOL_SPIKE","0.90"),0.90)
MIN_EXPECTED_GAIN_PCT=parse_float(env("MIN_EXPECTED_GAIN_PCT","0.0003"),0.0003)
REQUIRE_VOL_SPIKE=env("REQUIRE_VOL_SPIKE","false").lower()=="true"
TAKE_PROFIT_PCT=parse_float(env("TAKE_PROFIT_PCT","0.006"),0.006)
STOP_LOSS_PCT=parse_float(env("STOP_LOSS_PCT","0.008"),0.008)
TRAIL_PCT=parse_float(env("TRAIL_PCT","0.004"),0.004)
MIN_ORDER_USD=parse_float(env("MIN_ORDER_USD","20"),20)
ALLOCATION_PCT=parse_float(env("ALLOCATION_PCT","1.0"),1.0)
DAILY_MAX_LOSS_USD=parse_float(env("DAILY_MAX_LOSS_USD","25"),25)
FEE_PCT=parse_float(env("FEE_PCT","0.001"),0.001)
LOOP_SECONDS=int(env("LOOP_SECONDS","60"))
DEBUG=env("DEBUG","true").lower()=="true"

# ===================== Clientes =====================
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ===================== Indicadores =====================
def ema(arr,period):
    arr=np.asarray(arr,float); k=2/(period+1); out=np.zeros_like(arr); out[0]=arr[0]
    for i in range(1,len(arr)): out[i]=arr[i]*k+out[i-1]*(1-k)
    return out

def rsi(arr,period=14):
    arr=np.asarray(arr,float); delta=np.diff(arr)
    up=np.where(delta>0,delta,0.0); down=np.where(delta<0,-delta,0.0)
    roll_up=ema(up,period); roll_down=ema(down,period)
    rs=np.divide(roll_up,roll_down,out=np.zeros_like(roll_up),where=roll_down!=0)
    rsi_vals=100-(100/(1+rs)); rsi_vals=np.insert(rsi_vals,0,50.0)
    return rsi_vals

def atr_pct(highs,lows,closes,period=14):
    highs,lows,closes=map(lambda x:np.asarray(x,float),(highs,lows,closes))
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    if not trs:return 0.0
    atr=ema(np.array(trs),period)[-1]; return atr/float(closes[-1])

# ===================== WS buffers =====================
MARKET={}; MARKET_LOCK=threading.Lock()

def _ensure_symbol_ws(sym):
    with MARKET_LOCK:
        if sym not in MARKET: MARKET[sym]={"o":[],"h":[],"l":[],"c":[],"v":[],"ready":False}

def kline_handler(msg):
    try:
        k=msg.get("data",{}).get("k",{}); sym=k.get("s"); 
        if not sym:return
        _ensure_symbol_ws(sym)
        o,h,l,c,v=float(k["o"]),float(k["h"]),float(k["l"]),float(k["c"]),float(k["v"])
        closed=bool(k.get("x",False))
        with MARKET_LOCK:
            buf=MARKET[sym]
            if closed or not buf["c"]:
                buf["o"].append(o); buf["h"].append(h); buf["l"].append(l); buf["c"].append(c); buf["v"].append(v)
            else:
                buf["o"][-1]=o; buf["h"][-1]=max(buf["h"][-1],h)
                buf["l"][-1]=min(buf["l"][-1],l); buf["c"][-1]=c; buf["v"][-1]=v
            for key in ("o","h","l","c","v"):
                if len(buf[key])>CANDLES: buf[key]=buf[key][-CANDLES:]
            if len(buf["c"])>=max(60,int(CANDLES*0.6)): buf["ready"]=True
    except Exception as e: print(f"[WS] handler error: {repr(e)}", flush=True)

def fetch_klines(sym,interval,limit):
    with MARKET_LOCK:
        buf=MARKET.get(sym)
        if not buf or not buf["ready"]: raise RuntimeError(f"WS no listo {sym}")
        return buf["o"][-limit:],buf["h"][-limit:],buf["l"][-limit:],buf["c"][-limit:],buf["v"][-limit:]

def get_last_price(sym):
    with MARKET_LOCK:
        buf=MARKET.get(sym)
        if buf and buf["c"]: return float(buf["c"][-1])
    return float(client.get_symbol_ticker(symbol=sym)["price"])

# ===================== Balance =====================
def get_free_usdc():
    bal=client.get_asset_balance(asset="USDC"); return float(bal["free"]) if bal else 0.0

# ===================== Estrategia principal =====================
def evaluate_and_trade():
    st=load_state()
    for sym in SYMBOLS:
        try:
            o,h,l,c,v=fetch_klines(sym,INTERVAL,CANDLES)
            closes=np.array(c,float); vols=np.array(v,float); price=float(closes[-1])
            r=rsi(closes,RSI_LEN); ema_f=ema(closes,EMA_FAST); ema_s=ema(closes,EMA_SLOW)
            vol_base=vols[-50:-1].mean() if len(vols)>50 else (vols.mean() if len(vols) else 0.0)
            vol_ok=vols[-1]>VOL_SPIKE*max(vol_base,1e-9)
            trend_up=ema_f[-1]>ema_s[-1]; rsi_val=r[-1]; atrp=atr_pct(h,l,c,14)

            ema_cross_up=(ema_f[-2]<=ema_s[-2]) and (ema_f[-1]>ema_s[-1])
            base_signal=(trend_up and rsi_val>50) or (rsi_val<35) or ema_cross_up
            if REQUIRE_VOL_SPIKE and not vol_ok: 
                if DEBUG: print(f"[SKIP] {sym} volumen insuficiente", flush=True)
                continue

            if DEBUG: print(f"[SIG] {sym} price={price} rsi={rsi_val:.2f} "
                            f"ema_f={ema_f[-1]:.4f} ema_s={ema_s[-1]:.4f} "
                            f"vol_ok={vol_ok} atr={atrp:.4f} ema_cross={ema_cross_up}", flush=True)

            if not base_signal:
                if DEBUG: print(f"[SKIP] {sym} sin señal base", flush=True)
                continue
            if atrp<MIN_EXPECTED_GAIN_PCT:
                if DEBUG: print(f"[SKIP] {sym} ATR insuf {atrp:.4f}", flush=True)
                continue
            usdc=get_free_usdc()
            if usdc<MIN_ORDER_USD:
                if DEBUG: print(f"[SKIP] {sym} USDC insuf {usdc:.2f}", flush=True)
                continue
            qty=(usdc*ALLOCATION_PCT*0.995)/price
            qty=round(qty,5)
            client.order_market_buy(symbol=sym,quantity=qty)
            st["positions"][sym]={"entry":price,"qty":qty}
            save_state(st)
            tg_send(f"🟢 BUY {sym} {qty} @ {price}")
        except Exception as e: print(f"[ERR] {sym} {repr(e)}", flush=True)

# ===================== Main =====================
def main():
    start_http_server(); tg_send("🤖 Bot iniciado.")
    twm=ThreadedWebsocketManager(api_key=BINANCE_API_KEY,api_secret=BINANCE_API_SECRET); twm.start()
    for s in SYMBOLS: _ensure_symbol_ws(s); twm.start_kline_socket(callback=kline_handler,symbol=s.lower(),interval=INTERVAL)
    while True:
        evaluate_and_trade(); time.sleep(LOOP_SECONDS)

if __name__=="__main__": main()
