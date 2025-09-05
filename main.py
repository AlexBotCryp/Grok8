#!/usr/bin/env python3
import os, time, csv, math, signal, json, threading
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.enums import *
from binance.helpers import round_step_size
from dotenv import load_dotenv
import requests

# ---------------------------
# Utilidades
# ---------------------------

def env_float(name: str, default: float) -> float:
    """Convierte variables con posible coma decimal '0,005' -> 0.005"""
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return float(default)
    return float(str(val).replace(",", ".").strip())

def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else int(default)

def env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y")

def now_ts() -> float:
    return time.time()

def fmt_pct(x: float) -> str:
    return f"{x*100:.3f}%"

def safe_round_down(value: float, step: float) -> float:
    # Redondea hacia abajo al múltiplo de step
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d // s) * s)

# ---------------------------
# Notificaciones (Telegram)
# ---------------------------

class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{self.token}/sendMessage" if token and chat_id else None

    def send(self, msg: str):
        if not self.base:
            return
        try:
            requests.post(self.base, data={"chat_id": self.chat_id, "text": msg})
        except Exception:
            pass

# ---------------------------
# Bot de Micro-Oportunidades
# ---------------------------

class MicroScalpBot:
    def __init__(self):
        load_dotenv()

        # ---- Config
        self.API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
        self.API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
        self.TESTNET = env_bool("BINANCE_TESTNET", False)

        self.QUOTE = os.getenv("QUOTE_ASSET", "USDC").strip().upper()
        wl = os.getenv("WATCHLIST", "")
        self.WATCHLIST = [s.strip().upper() for s in wl.split(",") if s.strip()] or [
            "BTCUSDC","ETHUSDC","SOLUSDC","BNBUSDC","DOGEUSDC","TRXUSDC","XRPUSDC","ADAUSDC"
        ]

        self.SCAN_INTERVAL = env_int("SCAN_INTERVAL_SEC", 5)
        self.MAX_POS = env_int("MAX_POSITIONS", 4)

        self.ALLOC_MODE = os.getenv("ALLOC_MODE", "ALL").strip().upper()
        self.FIXED_TRADE_USD = env_float("FIXED_TRADE_USD", 50.0)
        self.MIN_TRADE_USD = env_float("MIN_TRADE_USD", 20.0)

        self.TARGET_NET_PCT = env_float("TARGET_NET_PCT", 0.004)     # neto tras fees
        self.TRAIL_PCT = env_float("TRAIL_PCT", 0.004)
        self.STOP_LOSS_PCT = env_float("STOP_LOSS_PCT", 0.006)
        self.TIMEOUT_SELL_SEC = env_int("TIMEOUT_SELL_SEC", 900)

        self.MOMENTUM_PCT = env_float("MOMENTUM_PCT", 0.001)
        self.MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", 0.0006)

        self.BALANCE_REFRESH_SEC = env_int("BALANCE_REFRESH_SEC", 30)
        self.LOG_CSV = os.getenv("LOG_TRADES_CSV", "trades.csv")
        self.DRY_RUN = env_bool("DRY_RUN", False)

        self.TELE_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.TELE_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notify = Notifier(self.TELE_TOKEN, self.TELE_CHAT)

        # ---- Cliente Binance
        self.client = Client(self.API_KEY, self.API_SECRET, testnet=self.TESTNET)
        # **Importantísimo**: reducir peso → llamar a endpoints agregados y espaciar refrescos

        # ---- Exchange Info + filtros
        self.filters = {}      # symbol -> {stepSize, minQty, minNotional, tickSize}
        self.base_asset = {}   # symbol -> base asset
        self.quote_asset = {}  # symbol -> quote asset
        self._load_exchange_info()

        # ---- Fees (taker) por símbolo; fallback 0.001
        self.taker_fee = {}  # symbol -> fee pct
        self._load_fees()

        # ---- Estados
        self.last_balance_ts = 0
        self.free_balances = {}     # asset -> free
        self._refresh_balances()    # una vez al inicio

        self.positions = {}         # symbol -> dict(qty, entry, peak, ts, spent_usd)
        self.price_hist = {s: deque(maxlen=12) for s in self.WATCHLIST}  # ~1 min si SCAN=5s

        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.notify.send("🤖 Bot micro-oportunidades iniciado.")

        # CSV header si no existe
        if not os.path.exists(self.LOG_CSV):
            with open(self.LOG_CSV, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ts","action","symbol","qty","price","pnl_pct","note"])

    # --------------------- Inicialización ---------------------

    def _handle_signal(self, *args):
        self._running = False
        self.notify.send("🛑 Bot detenido por señal del sistema.")

    def _load_exchange_info(self):
        info = self.client.get_exchange_info()
        for s in info["symbols"]:
            symbol = s["symbol"]
            if s["status"] != "TRADING":
                continue
            base = s["baseAsset"]; quote = s["quoteAsset"]
            self.base_asset[symbol] = base
            self.quote_asset[symbol] = quote
            f = {"stepSize": None, "minQty": None, "minNotional": None, "tickSize": None}
            for flt in s["filters"]:
                t = flt["filterType"]
                if t == "LOT_SIZE":
                    f["stepSize"] = float(flt["stepSize"])
                    f["minQty"] = float(flt["minQty"])
                elif t in ("MIN_NOTIONAL","NOTIONAL"):
                    f["minNotional"] = float(flt.get("minNotional") or flt.get("minNotional", 0))
                elif t == "PRICE_FILTER":
                    f["tickSize"] = float(flt["tickSize"])
            self.filters[symbol] = f

        # Si WATCHLIST incluye símbolos inexistentes, los filtramos
        self.WATCHLIST = [s for s in self.WATCHLIST if s in self.filters and self.quote_asset.get(s) == self.QUOTE]

    def _load_fees(self):
        try:
            # Llamada agrupada → devuelve lista de fees
            fees = self.client.get_trade_fee()
            for item in fees["tradeFee"]:
                sym = item["symbol"]
                self.taker_fee[sym] = float(item["taker"])
        except Exception:
            pass  # fallback por símbolo cuando haga falta

    def _fee(self, symbol: str) -> float:
        return self.taker_fee.get(symbol, 0.001)  # 0.1% por defecto

    def _refresh_balances(self, force: bool=False):
        if not force and (now_ts() - self.last_balance_ts) < self.BALANCE_REFRESH_SEC:
            return
        acc = self.client.get_account()
        self.free_balances = {b["asset"]: float(b["free"]) for b in acc["balances"]}
        self.last_balance_ts = now_ts()

    def _free(self, asset: str) -> float:
        return float(self.free_balances.get(asset, 0.0))

    # --------------------- Precios y libro ---------------------

    def _all_prices(self) -> dict:
        # Un solo endpoint para todos los tickers (peso 2)
        tickers = self.client.get_symbol_ticker()
        return {t["symbol"]: float(t["price"]) for t in tickers}

    def _all_book_tickers(self) -> dict:
        # Un solo endpoint para todos los bookTickers (peso 2)
        books = self.client.get_orderbook_ticker()
        return {b["symbol"]: (float(b["bidPrice"]), float(b["askPrice"])) for b in books}

    # --------------------- Rounding & filtros ------------------

    def _round_qty(self, symbol: str, qty: float) -> float:
        f = self.filters[symbol]
        step = f["stepSize"] or 0.0
        if step > 0:
            return round_step_size(qty, step)
        # fallback
        return float(f"{qty:.8f}")

    def _min_notional_ok(self, symbol: str, price: float, qty: float) -> bool:
        mn = self.filters[symbol].get("minNotional") or 0.0
        return (price * qty) >= max(mn, self.MIN_TRADE_USD)

    # --------------------- Órdenes -----------------------------

    def _buy_market(self, symbol: str, quote_usd: float):
        if self.DRY_RUN:
            return {"fills":[{"price": str(self._last_price(symbol)), "qty":"0"}], "executedQty":"0"}

        qo = max(quote_usd, self.MIN_TRADE_USD)
        # Binance acepta quoteOrderQty para compra market (gastar X USDC)
        order = self.client.create_order(
            symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET,
            quoteOrderQty=str(qo)
        )
        return order

    def _sell_market(self, symbol: str, qty: float):
        if self.DRY_RUN:
            return {"fills":[{"price": str(self._last_price(symbol)), "qty": str(qty)}], "executedQty": str(qty)}
        order = self.client.create_order(
            symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET,
            quantity=str(qty)
        )
        return order

    # --------------------- Estrategia --------------------------

    def _last_price(self, symbol: str) -> float:
        # Último almacenado en price_hist si existe
        dq = self.price_hist.get(symbol)
        if dq and len(dq) > 0:
            return dq[-1]
        # fallback rápido
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def _record_trade(self, action, symbol, qty, price, pnl_pct=None, note=""):
        with open(self.LOG_CSV, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([datetime.now(timezone.utc).isoformat(), action, symbol, f"{qty:.8f}", f"{price:.8g}", "" if pnl_pct is None else f"{pnl_pct:.6f}", note])

    def _enter_allowed(self):
        return len(self.positions) < self.MAX_POS

    def _calc_net_change(self, symbol: str, entry: float, current: float) -> float:
        # cambia neto considerando fees de compra y venta
        fee = self._fee(symbol)
        net_mult = (current * (1 - fee)) / (entry * (1 + fee))
        return net_mult - 1.0

    def _try_enter(self, symbol: str, bid: float, ask: float):
        # Condiciones de entrada: spread bajo + momentum positivo
        if not self._enter_allowed():
            return

        # momentum simple con histórico local
        hist = self.price_hist[symbol]
        last = hist[-1] if len(hist) else None
        if last is None:
            return  # aún sin histórico

        spread_pct = (ask - bid) / ask if ask > 0 else 1.0
        if spread_pct > self.MAX_SPREAD_PCT:
            return

        momentum_pct = (ask / last) - 1.0
        if momentum_pct < self.MOMENTUM_PCT:
            return

        # Capital disponible
        self._refresh_balances()
        usdc_free = self._free(self.QUOTE)

        if self.ALLOC_MODE == "ALL":
            quote_to_spend = max(usdc_free, 0.0)
        else:
            quote_to_spend = min(usdc_free, self.FIXED_TRADE_USD)

        if quote_to_spend < self.MIN_TRADE_USD:
            return

        # Crear orden de compra
        try:
            order = self._buy_market(symbol, quote_to_spend)
            fills = order.get("fills") or []
            if fills:
                # precio medio ponderado
                total_qty = sum(float(f["qty"]) for f in fills)
                avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / (total_qty or 1.0)
            else:
                # fallback: usar ask
                total_qty = float(order.get("executedQty", "0") or 0)
                avg_price = ask

            # Cantidad real en base que tenemos tras la compra
            self._refresh_balances(force=True)
            base = self.base_asset[symbol]
            base_free = self._free(base)

            if base_free <= 0:
                return

            # Registrar posición
            self.positions[symbol] = {
                "qty": base_free,
                "entry": avg_price,
                "peak": avg_price,
                "ts": now_ts(),
                "spent_usd": quote_to_spend
            }

            self._record_trade("BUY", symbol, base_free, avg_price, note=f"spent≈{quote_to_spend:.2f} {self.QUOTE}")
            self.notify.send(f"🟢 BUY {symbol}\nQty≈{base_free:.6f}\nEntry≈{avg_price:.8g}\nSpread {fmt_pct(spread_pct)}  Mom {fmt_pct(momentum_pct)}")

        except Exception as e:
            self.notify.send(f"⚠️ Error BUY {symbol}: {e}")

    def _try_exit(self, symbol: str, bid: float, ask: float):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        qty = pos["qty"]; entry = pos["entry"]; peak = pos["peak"]; ts = pos["ts"]

        # actualizar pico
        mid = (bid + ask) / 2.0
        if mid > peak:
            pos["peak"] = mid
            peak = mid

        # señales de salida
        net_change = self._calc_net_change(symbol, entry, bid)  # usar bid para salida
        drawdown_from_peak = (peak - bid) / peak if peak > 0 else 0.0
        age = now_ts() - ts

        should_sell = False
        reason = ""

        # 1) objetivo neto
        if net_change >= self.TARGET_NET_PCT:
            should_sell = True
            reason = f"TP net {fmt_pct(self.TARGET_NET_PCT)} alcanzado ({fmt_pct(net_change)})"

        # 2) trailing
        elif drawdown_from_peak >= self.TRAIL_PCT and bid > entry:  # asegúrate de no vender en pérdida por trailing
            should_sell = True
            reason = f"Trailing {fmt_pct(self.TRAIL_PCT)} desde pico"

        # 3) stop-loss duro
        elif ((bid / entry) - 1.0) <= -self.STOP_LOSS_PCT:
            should_sell = True
            reason = f"Stop-loss {fmt_pct(self.STOP_LOSS_PCT)}"

        # 4) timeout: si excede tiempo, intenta cerrar en pequeño positivo neto; si no, salida de seguridad liviana
        elif age >= self.TIMEOUT_SELL_SEC:
            if net_change > 0:
                should_sell = True
                reason = f"Timeout {self.TIMEOUT_SELL_SEC}s con net +"
            elif net_change <= -min(self.STOP_LOSS_PCT/2, 0.003):
                should_sell = True
                reason = f"Timeout SL suave {fmt_pct(min(self.STOP_LOSS_PCT/2, 0.003))}"

        if not should_sell:
            return

        # Vender cantidad redondeada y chequear notional
        qty_to_sell = self._round_qty(symbol, qty)
        if qty_to_sell <= 0:
            return
        if not self._min_notional_ok(symbol, bid, qty_to_sell):
            return  # evitar órdenes que el exchange rechaza

        try:
            order = self._sell_market(symbol, qty_to_sell)
            fills = order.get("fills") or []
            if fills:
                total_qty = sum(float(f["qty"]) for f in fills)
                avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / (total_qty or 1.0)
            else:
                avg_price = bid

            pnl_net = self._calc_net_change(symbol, entry, avg_price)
            self._record_trade("SELL", symbol, qty_to_sell, avg_price, pnl_net, note=reason)
            self.notify.send(
                f"🔴 SELL {symbol}\nQty≈{qty_to_sell:.6f}\nExit≈{avg_price:.8g}\nPnL neto {fmt_pct(pnl_net)}\n{reason}"
            )

            # Refrescar balances y limpiar posición
            self._refresh_balances(force=True)
            del self.positions[symbol]
        except Exception as e:
            self.notify.send(f"⚠️ Error SELL {symbol}: {e}")

    # --------------------- Bucle principal ---------------------

    def run(self):
        while self._running:
            loop_start = now_ts()
            try:
                # Obtener precios y libros de TODO en llamadas agregadas (peso total muy bajo)
                prices = self._all_prices()
                books = self._all_book_tickers()

                # Actualizar históricos de la watchlist
                for sym in self.WATCHLIST:
                    if sym in books:
                        bid, ask = books[sym]
                    else:
                        p = prices.get(sym)
                        if p is None:
                            continue
                        bid, ask = p, p
                    self.price_hist[sym].append((bid + ask) / 2.0)

                # 1) Intentar cerrar posiciones
                for sym in list(self.positions.keys()):
                    if sym not in books:
                        # fallback con último precio
                        p = prices.get(sym)
                        if p is None: 
                            continue
                        self._try_exit(sym, p, p)
                    else:
                        bid, ask = books[sym]
                        self._try_exit(sym, bid, ask)

                # 2) Buscar entradas nuevas
                for sym in self.WATCHLIST:
                    if sym in self.positions:
                        continue
                    if not self._enter_allowed():
                        break
                    if sym not in books:
                        p = prices.get(sym)
                        if p is None: 
                            continue
                        bid = ask = p
                    else:
                        bid, ask = books[sym]
                    self._try_enter(sym, bid, ask)

            except Exception as e:
                self.notify.send(f"⚠️ Loop error: {e}")

            # Dormir hasta completar SCAN_INTERVAL
            elapsed = now_ts() - loop_start
            to_sleep = max(0, self.SCAN_INTERVAL - elapsed)
            time.sleep(to_sleep)

        self.notify.send("✅ Bot finalizado limpiamente.")

# ---------------------------
# MAIN
# ---------------------------
if __name__ == "__main__":
    bot = MicroScalpBot()
    bot.run()
