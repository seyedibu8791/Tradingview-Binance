from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading
from config import *

app = Flask(__name__)

# ===== Binance Helpers =====
def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if http_method == "POST":
            return requests.post(url, headers=headers).json()
        elif http_method == "DELETE":
            return requests.delete(url, headers=headers).json()
        else:
            return requests.get(url, headers=headers).json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}

def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)

# ===== Symbol Info & Tick/Step Size =====
SYMBOL_INFO_CACHE = {}

def get_symbol_info(symbol):
    if symbol in SYMBOL_INFO_CACHE:
        return SYMBOL_INFO_CACHE[symbol]
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            SYMBOL_INFO_CACHE[symbol] = s
            return s
    return None

def get_tick_size(symbol):
    info = get_symbol_info(symbol)
    if not info:
        return 0.01
    tick_size = float([f["tickSize"] for f in info["filters"] if f["filterType"] == "PRICE_FILTER"][0])
    return tick_size

def round_price(symbol, price):
    tick_size = get_tick_size(symbol)
    return round(price / tick_size) * tick_size

def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        return round(qty, 3)
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    qty = (qty // step_size) * step_size
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)

def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = usdt_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except:
        return 0.001

# ===== Stop-Loss =====
def stop_loss_position(symbol, side, entry_price):
    close_side = "SELL" if side == "BUY" else "BUY"

    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or float(pos_data[0]["positionAmt"]) == 0:
        print(f"⚠️ No open position for {symbol}, skipping stop-loss.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)

    if side == "BUY":
        stop_price_raw = entry_price * (1 - STOPLOSS_PERCENT / 100)
    else:
        stop_price_raw = entry_price * (1 + STOPLOSS_PERCENT / 100)

    stop_price = round_price(symbol, stop_price_raw)

    params = {
        "symbol": symbol,
        "side": close_side,
        "quantity": qty,
        "stopPrice": stop_price,
        "type": "STOP_MARKET",
        "reduceOnly": True
    }

    r = binance_signed_request("POST", "/fapi/v1/order", params)
    print(f"⛔ Stop-Loss {close_side}: {symbol}, Qty: {qty}, Stop Price: {stop_price}")
    return r

# ===== Trailing Stop =====
def trailing_stop_position(symbol, side, entry_price):
    close_side = "SELL" if side == "BUY" else "BUY"

    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or float(pos_data[0]["positionAmt"]) == 0:
        print(f"⚠️ No open position for {symbol}, skipping trailing stop.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)

    if side == "BUY":
        activation_price_raw = entry_price * (1 + TRAILING_ACTIVATION_PERCENT / 100)
    else:
        activation_price_raw = entry_price * (1 - TRAILING_ACTIVATION_PERCENT / 100)

    activation_price = round_price(symbol, activation_price_raw)

    params = {
        "symbol": symbol,
        "side": close_side,
        "quantity": qty,
        "activationPrice": activation_price,
        "callbackRate": TRAILING_CALLBACK_PERCENT,
        "reduceOnly": True
    }

    r = binance_signed_request("POST", "/fapi/v1/trailingStop", params)
    print(f"⏳ Trailing STOP {close_side}: {symbol}, Qty: {qty}, Activation: {activation_price}, Callback: {TRAILING_CALLBACK_PERCENT}%")
    return r

# ===== Open Position =====
def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })

    filled_price = float(response.get("avgFillPrice") or (response.get("fills", [{}])[0].get("price")))
    print(f"📊 {side} ENTRY: {symbol}, Qty: {qty}, Filled Price: {filled_price}")

    # Place stop-loss and trailing stop
    stop_loss_position(symbol, side, filled_price)
    trailing_stop_position(symbol, side, filled_price)
    return response

# ===== Webhook Endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"

        if comment == "BUY_ENTRY":
            r = open_position(symbol, "BUY")
        elif comment == "SELL_ENTRY":
            r = open_position(symbol, "SELL")
        else:
            r = {"error": "Unknown comment"}

        return jsonify({"status": "ok", "response": r})
    except Exception as e:
        return jsonify({"error": str(e)})

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-Ping Thread =====
PING_INTERVAL = 5 * 60  # 5 minutes
def self_ping():
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping, daemon=True).start()

# ===== Run Flask =====
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
