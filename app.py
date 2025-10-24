from flask import Flask, request, jsonify
import requests, hmac, hashlib, time
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
    if http_method == "POST":
        return requests.post(url, headers=headers).json()
    elif http_method == "DELETE":
        return requests.delete(url, headers=headers).json()
    else:
        return requests.get(url, headers=headers).json()

def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except:
        pass

# ===== Order Execution =====

def open_position(symbol, side, price):
    set_leverage_and_margin(symbol)
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": calculate_quantity(symbol, TRADE_AMOUNT),
    }
    return binance_signed_request("POST", "/fapi/v1/order", params)

def close_position(symbol, side, price):
    # use LIMIT order to exit
    close_side = "SELL" if side == "BUY" else "BUY"
    params = {
        "symbol": symbol,
        "side": close_side,
        "type": "LIMIT",
        "price": price,
        "quantity": calculate_quantity(symbol, TRADE_AMOUNT),
        "timeInForce": "GTC"
    }
    return binance_signed_request("POST", "/fapi/v1/order", params)

def calculate_quantity(symbol, usdt_value):
    price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
    price = float(price_data["price"])
    qty = round(usdt_value / price, 3)
    return qty

# ===== Webhook =====

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        
        if "LONG" in comment and "EXIT" not in comment:
            r = open_position(symbol, "BUY", close_price)
        elif "SHORT" in comment and "EXIT" not in comment:
            r = open_position(symbol, "SELL", close_price)
        elif "EXIT_LONG" in comment:
            r = close_position(symbol, "BUY", close_price)
        elif "EXIT_SHORT" in comment:
            r = close_position(symbol, "SELL", close_price)
        else:
            r = {"error": "Unknown comment"}
            
        return jsonify({"status": "ok", "response": r})
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
