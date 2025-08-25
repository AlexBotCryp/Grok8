def comprar_oportunidad():
    """
    Intenta gastar el saldo disponible en las QUOTES preferidas en bucle,
    hasta agotar (prácticamente) el efectivo o llegar al MAX_OPEN_POSITIONS.
    Primero usa candidatos por RSI; si no hay y USE_FALLBACK=True, usa fallback.
    """
    reg = leer_posiciones()
    if len(reg) >= MAX_OPEN_POSITIONS:
        logger.info("Máximo de posiciones abiertas alcanzado.")
        return

    balances = holdings_por_asset()
    # Trabajamos quote por quote para no dejar dinero "parado"
    for quote in PREFERRED_QUOTES:
        if len(reg) >= MAX_OPEN_POSITIONS:
            break

        disponible = float(balances.get(quote, 0.0))
        # Intentamos gastar siempre que tengamos al menos 15
        intentos_seguridad = 0
        while disponible >= USD_MIN and len(reg) < MAX_OPEN_POSITIONS:
            intentos_seguridad += 1
            if intentos_seguridad > 10:
                # Evitar bucles infinitos por minNotional/precision
                break

            # Escaneamos candidatos que coticen en esta quote
            candidatos = [c for c in scan_candidatos() if c["quote"] == quote and c["symbol"] not in reg]
            elegido = candidatos[0] if candidatos else None

            # Fallback: si no hay candidato RSI, elige BTC/ETH/SOL en esta quote
            if not elegido and USE_FALLBACK:
                for base in FALLBACK_SYMBOLS:
                    sym = symbol_exists(base, quote)
                    if sym and sym not in reg and SYMBOL_MAP[sym]["status"] == "TRADING":
                        # Pequeña verificación de klines para no comprar a ciegas
                        try:
                            kl = safe_get_klines(sym, Client.KLINE_INTERVAL_5MINUTE, 30)
                            closes = [float(k[4]) for k in kl]
                            rsi = calculate_rsi(closes, RSI_PERIOD)
                            # ventana amplia para poder entrar y no dejar saldo ocioso
                            if 35 <= rsi <= 65:
                                elegido = {"symbol": sym, "quote": quote, "rsi": rsi, "lastPrice": closes[-1], "quoteVolume": 0}
                                break
                        except Exception:
                            continue

            if not elegido:
                logger.info(f"Sin candidato para {quote}; se mantiene el saldo por ahora.")
                break

            symbol = elegido["symbol"]
            price = obtener_precio(symbol)
            step, tick, _ = get_filter_values(symbol)

            usd_orden = min(next_order_size(), disponible)  # usa lo que haya
            qty = usd_orden / price
            qty = quantize_qty(qty, step)

            # Rechequea notional
            if qty <= 0 or not min_notional_ok(symbol, price, qty):
                # si no alcanza minNotional, subimos tamaño si hay saldo
                usd_orden = min(max(usd_orden * 1.3, USD_MIN), disponible)
                qty = quantize_qty(usd_orden / price, step)
                if qty <= 0 or not min_notional_ok(symbol, price, qty):
                    logger.info(f"{symbol}: no alcanza minNotional con saldo disponible.")
                    break

            try:
                order = client.order_market_buy(symbol=symbol, quantity=qty)
                buy_price = price
                reg[symbol] = {
                    "qty": float(qty),
                    "buy_price": float(buy_price),
                    "quote": quote,
                    "ts": datetime.now(TIMEZONE).isoformat()
                }
                escribir_posiciones(reg)
                enviar_telegram(f"🟢 Compra {symbol} qty={qty} @ {buy_price:.8f} {quote} | RSI {round(elegido.get('rsi', 50),1)}")
                logger.info(f"Comprado {symbol} qty={qty} a {buy_price}")

                # Actualiza disponible y balances para seguir gastando
                balances = holdings_por_asset()
                disponible = float(balances.get(quote, 0.0))
            except BinanceAPIException as e:
                logger.error(f"Error comprando {symbol}: {e}")
                backoff_sleep(e)
                # si fue error por filtros/minNotional, salimos del while
                break
