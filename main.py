#!/usr/bin/env python3
import os, time, csv, math, signal
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from binance.client import Client
from binance.enums import *
from binance.helpers import round_step_size
from dotenv import load_dotenv
import requests
import traceback

# ---------------------------
# Utilidades
# ---------------------------

def env_float(name: str, default: float) -> float:
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

# ---------------------------
# Notificaciones + LOG
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
            requests.post(self.base, data={"chat_id": self.chat_id, "text": msg}, timeout=5)
        except Exception:
            pass

def make_logger(notifier: Notifier):
    def log(msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        # Imprime SIEMPRE a consola (Render Logs)
        print(line, flush=True)
        # Y además Telegram si está configurado
        notifier.send(line)
    return log

# ---------------------------
# Bot de Micro-Oportunidades
# ---------------------------

class MicroScalpBot:
    def __init__(self):
        load_dotenv()

        # ---- Config env
        self.API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
        self.API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
        self.TESTNET = env_bool("BINANCE_TESTNET", False)

        self.QUOTE = os.getenv("QUOTE_ASSET", "USDC").strip().upper()
        wl = os.getenv("WATCHLIST", "")
        self.WATCHLIST = [s.strip().upper() for s in wl.split(",") if s.strip()]
        if not self.WATCHLIST:
            self.WATCHLIST = ["BTCUSDC","ETHUSDC","SOLUSDC","BNBUSDC","DOGEUSDC","TRXUSDC","XRPUSDC","ADAUSDC"]

        self.SCAN_INTERVAL = env_int("SCAN_INTERVAL_SEC", 5)
        self.MAX_POS = env_int("MAX_POSITIONS", 4)

        self.ALLOC_MODE = os.getenv("ALLOC_MODE", "ALL").strip().upper()
        self.FIXED_TRADE_USD = env_float("FIXED_TRADE_USD", 50.0)
        self.MIN_TRADE_USD = env_float("MIN_TRADE_USD", 20.0)

        self.TARGET_NET_PCT = env_float("TARGET_NET_PCT", 0.004)
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
        self.log = make_logger(self.notify)

        # ---- Resumen de arranque a LOGS
        self.log("🚀 Iniciando bot micro-oportunidades…")
        self.log(f"Modo TESTNET={self.TESTNET}  DRY_RUN={self.DRY_RUN}")
        self.log(f"QUOTE={self.QUOTE}  WATCHLIST={','.join(self.WATCHLIST)}")
        self.log(f"SCAN={self.SCAN_INTERVAL}s  MAX_POS={self.MAX_POS}  ALLOC_MODE={self.ALLOC_MODE}")
        self.log(f"TARGET_NET={fmt_pct(self.TARGET_NET_PCT)}  TRAIL={fmt_pct(self.TRAIL_PCT)}  SL={fmt_pct(self.STOP_LOSS_PCT)}")
        if not self.API_KEY or not self.API_SECRET:
            self.log("⚠️ BINANCE_API_KEY/SECRET vacíos: se podrán leer precios pero NO operar. (Pon las claves en Environment).")
        if not (self.TELE_TOKEN and self.TELE_CHAT):
            self.log("ℹ️ Telegram no configurado (TELEGRAM_BOT_TOKEN/CHAT_ID). Enviaré logs solo a consola.")

        # ---- Binance client
        try:
            self.client = Client(self.API_KEY, self.API_SECRET, testnet=self.TESTNET)
            self.log("✅ Cliente Binance creado.")
        except Exception as e:
            self.log(f"❌ Error creando cliente Binance: {e}")
            raise

        # ---- Exchange Info + filtros
        self.filters = {}
        self.base_asset = {}
        self.quote_asset = {}
        self._load_exchange_info()

        # ---- Fees
        self.taker_fee = {}
        self._load_fees()

        # ---- Balances
        self.last_balance_ts = 0
        self.free_balances = {}
        self._refresh_balances(force=True)

        # ---- Estado
        self.positions = {}
        self.price_hist = {s: deque(maxlen=12) for s in self.WATCHLIST}
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # CSV header
        if not os.path.exists(self.LOG_CSV):
            with open(self.LOG_CSV, "w", newline="") as f:
                csv.writer(f).writerow(["ts","action","symbol","qty","price","pnl_pct","note"])

        self.log("🤖 Bot iniciado correctamente. Comenzando escaneo…")

    # --------------------- Inicialización ---------------------

    def _handle_signal(self, *args):
        self._running = False
        self.log("🛑 Señal de parada recibida. Cerrando…")

    def _load_exchange_info(self):
        self.log("⏳ Cargando exchange info…")
        info = self.client.get_exchange_info()
        for s in info["symbols"]:
            if s["status"] != "TRADING": 
                continue
            symbol = s["symbol"]
            base = s["baseAsset"]; quote = s["quoteAsset"]
            self.base_asset[symbol] = base
            self.quote_asset[symbol] = quote
            f = {"stepSize": None, "minQty": None, "minNotional": None, "tickSize": None}
            for flt in s["filters"]:
                t = flt["filterType"]
                if t == "LOT_SIZE":
                    f["stepSize"] = float(flt["stepSize"]); f["minQty"] = float(flt["minQty"])
                elif t in ("MIN_NOTIONAL","NOTIONAL"):
                    f["minNotional"] = float(flt.get("minNotional") or 0)
                elif t == "PRICE_FILTER":
                    f["tickSize"] = float(flt["tickSize"])
            self.filters[symbol] = f

        # Filtra watchlist por quote correcto
        before = list(self.WATCHLIST)
        self.WATCHLIST = [s for s in self.WATCHLIST if s in self.filters and self.quote_asset.get(s) == self.QUOTE]
        if not self.WATCHLIST:
            self.log(f"⚠️ WATCHLIST original {before} no coincide con QUOTE={self.QUOTE}. Pruebo con pares por defecto.")
            self.WATCHLIST = [s for s in ["BTCUSDC","ETHUSDC","SOLUSDC","BNBUSDC","DOGEUSDC","TRXUSDC","XRPUSDC","ADAUSDC"] if self.quote_asset.get(s) == self.QUOTE]
        self.log(f"✅ Watchlist final: {','.join(self.WATCHLIST)}")

    def _load_fees(self):
        try:
            fees = self.client.get_trade_fee()
            for item in fees["tradeFee"]:
                self.taker_fee[item["symbol"]] = float(item["taker"])
            self.log("✅ Fees cargadas.")
        except Exception as e:
            self.log(f"⚠️ No pude cargar fees, uso 0.1% por defecto. Detalle: {e}")

    def _fee(self, symbol: str) -> float:
        return self.taker_fee.get(symbol, 0.001)

    def _refresh_balances(self, force: bool=False):
        if not force and (now_ts() - self.last_balance_ts) < self.BALANCE_REFRESH_SEC:
            return
        try:
            acc = self.client.get_account()
            self.free_balances = {b["asset"]: float(b["free"]) for b in acc["balances"]}
            self.last_balance_ts = now_ts()
        except Exception as e:
            self.log(f"⚠️ Error leyendo balances: {e}")

    def _free(self, asset: str) -> float:
        return float(self.free_balances.get(asset, 0.0))

    # --------------------- Precios -----------------------------

    def _all_prices(self) -> dict:
        tickers = self.client.get_symbol_ticker()
        return {t["symbol"]: float(t["price"]) for t in tickers}

    def _all_book_tickers(self) -> dict:
        books = self.client.get_orderbook_ticker()
        return {b["symbol"]: (float(b["bidPrice"]), float(b["askPrice"])) for b in books}

    # --------------------- Rounding & filtros ------------------

    def _round_qty(self, symbol: str, qty: float) -> float:
        step = (self.filters.get(symbol) or {}).get("stepSize") or 0.0
        if step > 0:
            return float(Decimal(str(qty)) // Decimal(str(step)) * Decimal(str(step)))
        return float(f"{qty:.8f}")

    def _min_notional_ok(self, symbol: str, price: float, qty: float) -> bool:
        mn = (self.filters.get(symbol) or {}).get("minNotional") or 0.0
        return (price * qty) >= max(mn, self.MIN_TRADE_USD)

    # --------------------- Órdenes -----------------------------

    def _buy_market(self, symbol: str, quote_usd: float):
        if self.DRY_RUN:
            self.log(f"(DRY_RUN) BUY {symbol} por ≈{quote_usd:.2f} {self.QUOTE}")
            return {"fills":[{"price":"0","qty":"0"}], "executedQty":"0"}

        qo = max(quote_usd, self.MIN_TRADE_USD)
        return self.client.create_order(
            symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET,
            quoteOrderQty=str(qo)
        )

    def _sell_market(self, symbol: str, qty: float):
        if self.DRY_RUN:
            self.log(f"(DRY_RUN) SELL {symbol} qty≈{qty}")
            return {"fills":[{"price":"0","qty": str(qty)}], "executedQty": str(qty)}
        return self.client.create_order(
            symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET,
            quantity=str(qty)
        )

    # --------------------- Estrategia --------------------------

    def _last_price(self, symbol: str, prices: dict, books: dict) -> float:
        if symbol in books:
            b,a = books[symbol]; return (b+a)/2.0
        return prices.get(symbol, 0.0)

    def _record_trade(self, action, symbol, qty, price, pnl_pct=None, note=""):
        with open(self.LOG_CSV, "a", newline="") as f:
            csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), action, symbol, f"{qty:.8f}", f"{price:.8g}", "" if pnl_pct is None else f"{pnl_pct:.6f}", note])

    def _enter_allowed(self):
        return len(self.positions) < self.MAX_POS

    def _calc_net_change(self, symbol: str, entry: float, current: float) -> float:
        fee = self._fee(symbol)
        net_mult = (current * (1 - fee)) / (entry * (1 + fee))
        return net_mult - 1.0

    def _try_enter(self, symbol: str, bid: float, ask: float):
        if not self._enter_allowed():
            return

        hist = self.price_hist[symbol]
        last = hist[-1] if len(hist) else None
        if last is None:
            return

        spread_pct = (ask - bid) / ask if ask > 0 else 1.0
        if spread_pct > self.MAX_SPREAD_PCT:
            return

        momentum_pct = (ask / last) - 1.0
        if momentum_pct < self.MOMENTUM_PCT:
            return

        self._refresh_balances()
        usdc_free = self._free(self.QUOTE)
        quote_to_spend = usdc_free if self.ALLOC_MODE == "ALL" else min(usdc_free, self.FIXED_TRADE_USD)
        if quote_to_spend < self.MIN_TRADE_USD:
            return

        try:
            order = self._buy_market(symbol, quote_to_spend)
            # mejor usar ask como aproximación si no hay fills (DRY_RUN)
            avg_price = ask
            self._refresh_balances(force=True)
            base = self.base_asset[symbol]
            base_free = self._free(base)

            if base_free <= 0:
                return

            self.positions[symbol] = {
                "qty": base_free,
                "entry": avg_price,
                "peak": avg_price,
                "ts": now_ts(),
                "spent_usd": quote_to_spend
            }
            self._record_trade("BUY", symbol, base_free, avg_price, note=f"spent≈{quote_to_spend:.2f} {self.QUOTE}")
            self.log(f"🟢 BUY {symbol} qty≈{base_free:.6f} entry≈{avg_price:.8g} spread {fmt_pct(spread_pct)} mom {fmt_pct(momentum_pct)}")

        except Exception as e:
            self.log(f"⚠️ Error BUY {symbol}: {e}\n{traceback.format_exc()}")

    def _try_exit(self, symbol: str, bid: float, ask: float):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        qty = pos["qty"]; entry = pos["entry"]; peak = pos["peak"]; ts = pos["ts"]

        mid = (bid + ask) / 2.0
        if mid > peak:
            pos["peak"] = mid
            peak = mid

        net_change = self._calc_net_change(symbol, entry, bid)
        drawdown_from_peak = (peak - bid) / peak if peak > 0 else 0.0
        age = now_ts() - ts

        should_sell = False
        reason = ""

        if net_change >= self.TARGET_NET_PCT:
            should_sell = True; reason = f"TP net {fmt_pct(self.TARGET_NET_PCT)} ({fmt_pct(net_change)})"
        elif drawdown_from_peak >= self.TRAIL_PCT and bid > entry:
            should_sell = True; reason = f"Trailing {fmt_pct(self.TRAIL_PCT)}"
        elif ((bid / entry) - 1.0) <= -self.STOP_LOSS_PCT:
            should_sell = True; reason = f"Stop-loss {fmt_pct(self.STOP_LOSS_PCT)}"
        elif age >= self.TIMEOUT_SELL_SEC:
            if net_change > 0:
                should_sell = True; reason = f"Timeout {self.TIMEOUT_SELL_SEC}s con net +"
            elif net_change <= -min(self.STOP_LOSS_PCT/2, 0.003):
                should_sell = True; reason = f"Timeout SL suave"

        if not should_sell:
            return

        qty_to_sell = self._round_qty(symbol, qty)
        if qty_to_sell <= 0 or not self._min_notional_ok(symbol, bid, qty_to_sell):
            return

        try:
            order = self._sell_market(symbol, qty_to_sell)
            exit_price = bid
            pnl_net = self._calc_net_change(symbol, entry, exit_price)
            self._record_trade("SELL", symbol, qty_to_sell, exit_price, pnl_net, note=reason)
            self.log(f"🔴 SELL {symbol} qty≈{qty_to_sell:.6f} exit≈{exit_price:.8g} PnL net {fmt_pct(pnl_net)} — {reason}")
            del self.positions[symbol]
        except Exception as e:
            self.log(f"⚠️ Error SELL {symbol}: {e}\n{traceback.format_exc()}")

    # --------------------- Bucle principal ---------------------

    def run(self):
        loop_count = 0
        while self._running:
            loop_start = now_ts()
            try:
                prices = self._all_prices()
                books = self._all_book_tickers()

                # histórico
                for sym in self.WATCHLIST:
                    if sym in books:
                        b,a = books[sym]
                        self.price_hist[sym].append((b+a)/2.0)
                    elif sym in prices:
                        p = prices[sym]
                        self.price_hist[sym].append(p)

                # salidas
                for sym in list(self.positions.keys()):
                    if sym in books:
                        b,a = books[sym]
                        self._try_exit(sym, b, a)
                    elif sym in prices:
                        p = prices[sym]
                        self._try_exit(sym, p, p)

                # entradas
                for sym in self.WATCHLIST:
                    if sym in self.positions:
                        continue
                    if not self._enter_allowed():
                        break
                    if sym in books:
                        b,a = books[sym]
                    else:
                        p = prices.get(sym)
                        if not p: 
                            continue
                        b = a = p
                    self._try_enter(sym, b, a)

                loop_count += 1
                if loop_count % max(1, int(60 / max(1, self.SCAN_INTERVAL))) == 0:
                    # 1 vez por minuto, pequeño latido:
                    self._refresh_balances()
                    self.log(f"⏱️ Ciclos={loop_count}  Posiciones={len(self.positions)}  {self.QUOTE} libre≈{self._free(self.QUOTE):.4f}")

            except Exception as e:
                self.log(f"⚠️ Loop error: {e}\n{traceback.format_exc()}")

            elapsed = now_ts() - loop_start
            time.sleep(max(0, self.SCAN_INTERVAL - elapsed))

        self.log("✅ Bot finalizado limpiamente.")

# ---------------------------
# MAIN
# ---------------------------
if __name__ == "__main__":
    bot = MicroScalpBot()
    bot.run()
