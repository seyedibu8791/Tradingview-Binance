# app.py

from flask import Flask, request, jsonify
import requests, time, threading
from binance.client import Client
from binance.enums import *
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL,
    TRADE_AMOUNT, LEVERAGE, MARGIN_TYPE,
    MAX_ACTIVE_TRADES, OPPOSITE_CLOSE_DELAY,
    EXIT_MARKET_DELAY, EXIT_LIMIT_TIMEOUT, USE_BAR_HIGH_LOW_FOR_EXIT
)
from trade_notifier import log_trade_entry, log_trade_exit

app = Flask(__name__)

# ==============================
# 🔹 Binance Setup
# ==============================
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
client.API_URL = BASE_URL

# ==============================
# 🔹 Active Trade Tracker
# ==============================
active_trades = {}

# ==============================
# 🔹 Helper Functions
# ==============================

def normalize_symbol(symbol: str) -> str:
    return symbol.strip().replace("/", "").replace("_", "").upper()

def get_symbol_precision(symbol):
    """Get precision details from Binance."""
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return int(s["quantityPrecision"])
    except Exception as e:
        print(f"⚠️ Precision fetch failed for {symbol}: {e}")
    return 3

def get_current_price(symbol):
    """Fetch live price from Binance."""
    try:
        return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        print(f"⚠️ Price fetch error: {e}")
        return None

def close_existing_position(symbol, side):
    """Close opposite side position if any."""
    try:
        position = client.futures_position_information(symbol=symbol)
        pos_amt = float(position[0]['positionAmt'])
        if pos_amt == 0:
            return

        position_side = "BUY" if pos_amt > 0 else "SELL"
        if position_side != side:
            print(f"🔁 Closing opposite {position_side} position for {symbol}")
            opposite_side = SIDE_SELL if position_side == "BUY" else SIDE_BUY
            qty = abs(pos_amt)
            client.futures_create_order(
                symbol=symbol,
                side=opposite_side,
                type=ORDER_TYPE_MARKET,
                quantity=qty
            )
            time.sleep(OPPOSITE_CLOSE_DELAY)
    except Exception as e:
        print(f"⚠️ Error closing opposite position: {e}")

def place_futures_order(symbol, side, order_type, quantity, price=None):
    """Place order on Binance Futures."""
    try:
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity
        }
        if price:
            params["price"] = round(price, 2)
            params["timeInForce"] = TIME_IN_FORCE_GTC

        order = client.futures_create_order(**params)
        return order
    except Exception as e:
        print(f"❌ Order failed for {symbol}: {e}")
        return None

def get_filled_price(order_id, symbol):
    """Retrieve filled price from Binance order history."""
    try:
        trades = client.futures_account_trades(symbol=symbol)
        for t in reversed(trades):
            if t["orderId"] == order_id:
                return float(t["price"])
    except Exception as e:
        print(f"⚠️ Filled price fetch failed: {e}")
    return None

# ==============================
# 🔹 Flask Webhook Endpoints
# ==============================

@app.route("/signal", methods=["POST"])
def handle_signal():
    data = request.get_json(force=True)
    print(f"📩 Signal received: {data}")

    symbol = normalize_symbol(data.get("symbol", ""))
    side = data.get("side", "").upper()
    signal_type = data.get("signal_type", "").upper()
    bar_high = float(data.get("bar_high", 0))
    bar_low = float(data.get("bar_low", 0))

    if not symbol or side not in ["BUY", "SELL"]:
        return jsonify({"error": "Invalid signal"}), 400

    # Limit concurrent trades
    if len(active_trades) >= MAX_ACTIVE_TRADES and signal_type == "ENTRY":
        print(f"🚫 Max active trades reached ({MAX_ACTIVE_TRADES}). Skipping {symbol}.")
        return jsonify({"status": "skipped"}), 200

    # ===== ENTRY SIGNAL =====
    if signal_type == "ENTRY":
        close_existing_position(symbol, side)

        price = get_current_price(symbol)
        qty = round((TRADE_AMOUNT * LEVERAGE) / price, get_symbol_precision(symbol))
        order = place_futures_order(symbol, side, ORDER_TYPE_MARKET, qty)

        if order and order.get("orderId"):
            order_id = order["orderId"]
            filled_price = get_filled_price(order_id, symbol)
            if filled_price:
                active_trades[symbol] = {"side": side, "entry_price": filled_price, "order_id": order_id}
                log_trade_entry(symbol, side, order_id, filled_price)
            else:
                print(f"⚠️ Could not fetch filled price for {symbol}")
        return jsonify({"status": "entry processed"}), 200

    # ===== EXIT SIGNAL =====
    elif signal_type == "EXIT":
        if symbol not in active_trades:
            print(f"⚠️ No active trade found for {symbol}")
            return jsonify({"status": "no active trade"}), 200

        trade = active_trades[symbol]
        entry_side = trade["side"]
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        qty = round((TRADE_AMOUNT * LEVERAGE) / get_current_price(symbol), get_symbol_precision(symbol))

        limit_price = bar_high if entry_side == "BUY" else bar_low
        if not USE_BAR_HIGH_LOW_FOR_EXIT:
            limit_price = get_current_price(symbol)

        print(f"📤 Exit {symbol}: {exit_side} @ {limit_price}")

        # Step 1: Place limit exit
        order = place_futures_order(symbol, exit_side, ORDER_TYPE_LIMIT, qty, limit_price)

        # Step 2: Wait for fill
        filled_price = None
        start = time.time()
        while time.time() - start < EXIT_LIMIT_TIMEOUT:
            time.sleep(1)
            filled_price = get_filled_price(order["orderId"], symbol)
            if filled_price:
                break

        # Step 3: If not filled, execute market exit
        if not filled_price:
            print(f"⚠️ Limit not filled, switching to market after {EXIT_MARKET_DELAY}s...")
            time.sleep(EXIT_MARKET_DELAY)
            market_order = place_futures_order(symbol, exit_side, ORDER_TYPE_MARKET, qty)
            filled_price = get_filled_price(market_order["orderId"], symbol)

        if filled_price:
            log_trade_exit(symbol, order["orderId"], filled_price)
            del active_trades[symbol]
        else:
            print(f"❌ Could not get filled exit price for {symbol}")

        return jsonify({"status": "exit processed"}), 200

    return jsonify({"status": "unknown signal type"}), 400

# ==============================
# 🔹 Run Flask App
# ==============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
