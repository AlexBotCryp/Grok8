import os
import time
import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# =========================
# Configuración
# =========================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Moneda base de referencia para PnL y puente de emergencia
BASE_ASSET = os.getenv("BASE_ASSET", "USDC")

# Activa enrutado cripto→cripto (1/true) o fuerza pasar por BASE_ASSET
SMART_C2C = os.getenv("SMART_C2C", "1").lower() in ("1", "true", "yes")

# Fee por defecto si la API no devuelve tasas (taker), 0.1% = 0.001
DEFAULT_TAKER_FEE = float(os.getenv("DEFAULT_TAKER_FEE", "0.001"))

# Lista de símbolos a vigilar (puedes ajustar)
WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,DOGEUSDC,TRXUSDC,XRPUSDC,ADAUSDC"
).split(",") if s.strip()]

SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "15"))

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# =========================
# Cliente
# =========================
client = Client(API_KEY, API_SECRET)


# =========================
# Utilidades de mercado
# =========================
class MarketMeta:
    def __init__(self):
        self.symbols_info: Dict[str, dict] = {}
        self.asset_pairs: Dict[Tuple[str, str], str] = {}  # (base,quote) -> SYMBOL
        self.pair_by_assets: Dict[str, List[str]] = {}     # asset -> [symbols]
        self.fee_cache: Dict[str, float] = {}              # symbol -> taker fee

    def load(self):
        logging.info("✅ Exchange info cargada.")
        ex = client.get_exchange_info()
        for s in ex["symbols"]:
            if s.get("status") != "TRADING":
                continue
            symbol = s["symbol"]
            base = s["baseAsset"]
            quote = s["quoteAsset"]
            self.symbols_info[symbol] = s
            self.asset_pairs[(base, quote)] = symbol
            self.pair_by_assets.setdefault(base, []).append(symbol)
            self.pair_by_assets.setdefault(quote, []).append(symbol)

    def symbol_exists(self, base: str, quote: str) -> Optional[str]:
        return self.asset_pairs.get((base, quote))

    def get_filters(self, symbol: str) -> dict:
        info = self.symbols_info[symbol]
        f = {flt["filterType"]: flt for flt in info["filters"]}
        return f

    def step_round(self, qty: float, step: float) -> float:
        return math.floor(qty / step) * step

    def price_round(self, price: float, tick: float) -> float:
        return math.floor(price / tick) * tick

    def get_taker_fee(self, symbol: str) -> float:
        if symbol in self.fee_cache:
            return self.fee_cache[symbol]
        try:
            fees = client.get_trade_fee(symbol=symbol)
            # Binance retorna lista con dicts como {"symbol": "...", "maker": "0.00100000", "taker":"0.00100000"}
            if fees and isinstance(fees, list):
                taker = float(fees[0].get("taker", DEFAULT_TAKER_FEE))
            else:
                taker = DEFAULT_TAKER_FEE
        except Exception:
            taker = DEFAULT_TAKER_FEE
        self.fee_cache[symbol] = taker
        return taker


market = MarketMeta()


# =========================
# Balances y “dust”
# =========================
def get_balances_free() -> Dict[str, float]:
    acc = client.get_account()
    out = {}
    for b in acc["balances"]:
        free = float(b["free"])
        if free > 0:
            out[b["asset"]] = free
    return out


def get_price(symbol: str) -> float:
    return float(client.get_symbol_ticker(symbol=symbol)["price"])


def min_trade_ok(symbol: str, qty: float, side: str) -> bool:
    """Verifica MIN_NOTIONAL y LOT_SIZE aprox con precio actual."""
    try:
        f = market.get_filters(symbol)
        price = get_price(symbol)
        notional = qty * price
        min_notional = float(f.get("MIN_NOTIONAL", {}).get("minNotional", "0"))
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        qty_rounded = market.step_round(qty, step)
        return qty_rounded > 0 and notional >= min_notional
    except Exception:
        return False


def clean_dust_to_base():
    """Convierte saldos pequeños a BASE_ASSET cuando sea posible (mercado)."""
    balances = get_balances_free()
    if not balances:
        return
    for asset, qty in balances.items():
        if asset in ("BNB", BASE_ASSET):  # BNB para fees, y base no tocar
            continue
        if qty <= 0:
            continue
        # Vender asset→BASE_ASSET si existe par
        direct = market.symbol_exists(asset, BASE_ASSET)
        if not direct:
            continue
        # Calcular cantidad válida
        f = market.get_filters(direct)
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        qty_ok = market.step_round(qty, step)
        if qty_ok <= 0:
            continue
        try:
            if not min_trade_ok(direct, qty_ok, side="SELL"):
                continue
            client.order_market_sell(symbol=direct, quantity=qty_ok)
            logging.info(f"🧹 Limpieza DUST {direct}: vendidas {qty_ok}")
        except BinanceAPIException as e:
            logging.warning(f"[DUST] No se pudo vender {asset}->{BASE_ASSET}: {e.message}")


# =========================
# Enrutado inteligente cripto→cripto
# =========================
def find_best_route(src_asset: str, dst_asset: str) -> Optional[List[Tuple[str, str]]]:
    """
    Devuelve una ruta como lista de (symbol, side) donde side es 'BUY' o 'SELL'
    Empezando desde tener src_asset y terminar poseyendo dst_asset.

    Preferencias:
      1) Directo: (dst/src) como BUY, o (src/dst) como SELL según exista el par.
      2) 1 salto por puentes: USDC o USDT (elige el que exista y con menor fee estimada).
    """
    src_asset = src_asset.upper()
    dst_asset = dst_asset.upper()
    if src_asset == dst_asset:
        return []

    # 1) Directo: ¿existe par (dst/src) -> BUY?
    direct_buy = market.symbol_exists(dst_asset, src_asset)
    if direct_buy:
        return [(direct_buy, "BUY")]

    # 1b) Directo: ¿existe par (src/dst) -> SELL?
    direct_sell = market.symbol_exists(src_asset, dst_asset)
    if direct_sell:
        return [(direct_sell, "SELL")]

    # 2) Puentes candidatos
    bridges = ["USDC", "USDT"]

    candidates = []
    for bridge in bridges:
        # Ruta A: src->bridge (SELL) + dst/bridge (BUY)   (si existen ambos)
        leg1 = market.symbol_exists(src_asset, bridge)  # SELL src/bridge
        leg2 = market.symbol_exists(dst_asset, bridge)  # BUY dst/bridge
        if leg1 and leg2:
            # coste aproximado: fee taker en leg1 + leg2
            fee = market.get_taker_fee(leg1) + market.get_taker_fee(leg2)
            candidates.append(([(leg1, "SELL"), (leg2, "BUY")], fee))

        # Ruta B: bridge/src (BUY) + dst/src (¿SELL?) — no tiene sentido si partimos con src en cartera

    if not candidates:
        return None

    # Elige la de menor fee estimada
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def execute_route_from_asset(src_asset: str, dst_asset: str, portion: float = 1.0) -> bool:
    """
    Ejecuta la ruta para convertir src_asset -> dst_asset usando 'portion' del saldo disponible.
    """
    route = find_best_route(src_asset, dst_asset)
    if route is None:
        logging.warning(f"⚠️ No hay ruta disponible {src_asset} → {dst_asset}")
        return False
    if len(route) == 0:
        logging.info("ℹ️ Ruta vacía (activos iguales).")
        return True

    balances = get_balances_free()
    src_qty = balances.get(src_asset, 0.0) * float(portion)
    if src_qty <= 0:
        logging.warning(f"⚠️ Sin saldo en {src_asset} para ejecutar swap.")
        return False

    # Ejecutar cada paso en orden
    # Nota: cuando la leg es SELL usamos qty en el base del símbolo;
    # cuando es BUY calculamos qty objetivo estimando con el precio de mercado y respetando filtros.
    for symbol, side in route:
        f = market.get_filters(symbol)
        base = market.symbols_info[symbol]["baseAsset"]
        quote = market.symbols_info[symbol]["quoteAsset"]
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))

        try:
            if side == "SELL":
                # Debemos tener saldo en 'base' para vender
                qty_avail = get_balances_free().get(base, 0.0)
                qty = market.step_round(qty_avail, step)
                if qty <= 0 or not min_trade_ok(symbol, qty, side="SELL"):
                    logging.warning(f"⚠️ SELL no cumple mínimos {symbol} qty={qty}")
                    return False
                client.order_market_sell(symbol=symbol, quantity=qty)
                logging.info(f"🔁 SELL {symbol} qty={qty}")

            elif side == "BUY":
                # Necesitamos saldo en 'quote' para comprar 'base'
                price = get_price(symbol)
                balances_now = get_balances_free()
                quote_free = balances_now.get(quote, 0.0)
                if quote_free <= 0:
                    logging.warning(f"⚠️ Sin saldo en {quote} para comprar {base} ({symbol})")
                    return False

                # Cuánto base puedo comprar con todo el quote disponible
                est_qty = (quote_free / price) * 0.999  # ligera reserva
                qty = market.step_round(est_qty, step)
                if qty <= 0 or not min_trade_ok(symbol, qty, side="BUY"):
                    logging.warning(f"⚠️ BUY no cumple mínimos {symbol} qty={qty}")
                    return False
                client.order_market_buy(symbol=symbol, quantity=qty)
                logging.info(f"🔁 BUY  {symbol} qty={qty}")
            else:
                logging.error(f"Side desconocido: {side}")
                return False

            time.sleep(0.4)  # breve respiro para evitar weight issues

        except BinanceAPIException as e:
            logging.error(f"[ROUTE] Falló orden {symbol} {side}: {e.message}")
            return False

    logging.info(f"✅ Swap completado: {src_asset} → {dst_asset} (ruta {route})")
    return True


# =========================
# Estrategia (simple placeholder)
# =========================
def pick_target_asset() -> Optional[str]:
    """
    Lógica mínima: si no tienes posiciones (solo {BASE_ASSET,BNB}),
    no hace nada. Si tienes una posición cripto distinta a BASE_ASSET
    y SMART_C2C está activo y TARGET_ASSET env var está definida,
    intenta rotar a TARGET_ASSET sin pasar por USDC si hay ruta.
    """
    env_target = os.getenv("TARGET_ASSET", "").upper().strip()
    if not env_target:
        return None
    return env_target


def current_position_assets() -> List[str]:
    bals = get_balances_free()
    # ignora BASE_ASSET y BNB (para fees)
    return [a for a, q in bals.items() if q > 0 and a not in (BASE_ASSET, "BNB")]


def loop():
    logging.info("🤖 Bot iniciado. Escaneando…")
    market.load()

    # Limpia “dust” hacia BASE_ASSET una vez al iniciar (si no quieres, coméntalo)
    clean_dust_to_base()

    while True:
        try:
            # Puedes poner tu lógica de señales aquí.
            # Para demostrar el cripto→cripto, usamos TARGET_ASSET si se define.
            if SMART_C2C:
                dst = pick_target_asset()
                if dst:
                    holds = current_position_assets()
                    if holds and dst not in holds:
                        # Elige la primera posición actual como origen
                        src = holds[0]
                        if src != dst:
                            logging.info(f"♻️ Intento de rotación {src} → {dst} (C2C)")
                            ok = execute_route_from_asset(src, dst, portion=1.0)
                            if not ok:
                                logging.warning("No se pudo completar la rotación C2C.")
                            else:
                                # Opcional: si quieres consolidar sobras (ej., restos de puente), puedes limpiar
                                pass

            time.sleep(SLEEP_SECONDS)

        except KeyboardInterrupt:
            logging.info("⏹️ Bot detenido por usuario.")
            break
        except Exception as e:
            logging.exception(f"Error en loop principal: {e}")
            time.sleep(3)


if __name__ == "__main__":
    loop()
