# trade_notifier.py
import requests
import threading
import time
import datetime

# =======================
# 🔧 CONFIG (HARD-CODED)
# =======================
TELEGRAM_BOT_TOKEN = "8282710007:AAFbcLUwHRrMrBJ5VacJQQFM27qxdCplwO4"
TELEGRAM_CHAT_ID = "-1003281678423"

TRADE_AMOUNT = 50
LEVERAGE = 20

# =======================
# 🧾 STORAGE
# =======================
trades = {}           # {symbol: {...}}
notified_orders = {}  # {order_id: "NEW"/"FILLED"}


# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    """Send formatted text to Telegram chat."""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping message.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=payload, timeout=10)

        if response.status_code != 200:
            print("❌ Telegram Error:", response.status_code, response.text)
        else:
            print(f"✅ Sent Telegram message: {message[:80]}...")
    except Exception as e:
        print("❌ Telegram Exception:", e)


# =======================
# 🟩 TRADE ENTRY LOGGING
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, status: str, pos_data=None, filled_price: float = None):
    """Send both NEW (created) and FILLED (confirmed) messages safely."""

    symbol = symbol.upper()
    side = side.upper()

    # Avoid duplicate or repeated updates
    prev_status = notified_orders.get(order_id)
    if prev_status == status:
        print(f"⚠️ Skipping duplicate {status} message for {symbol}")
        return

    # Ensure filled_price from pos_data if missing
    if filled_price is None and pos_data:
        try:
            if isinstance(pos_data, list) and len(pos_data) > 0:
                filled_price = float(pos_data[0].get("entryPrice", 0))
            elif isinstance(pos_data, dict):
                filled_price = float(pos_data.get("entryPrice", 0))
        except Exception as e:
            print(f"⚠️ Could not parse filled_price from pos_data for {symbol}: {e}")

    # --- 1️⃣ NEW Order Created ---
    if status == "NEW":
        message = f"""📈 <b>Trade Entry</b>
Symbol: <b>#{symbol}</b>
Side: <b>{side}</b>
Leverage: {LEVERAGE}x
--- ⌁ ---
Entry Price: <b>{filled_price or 'Pending Fill'}</b>
--- ⌁ ---
🕐 Waiting for Exit Signal..."""
        send_telegram_message(message)
        notified_orders[order_id] = "NEW"

    # --- 2️⃣ FILLED Order Confirmed ---
    elif status == "FILLED":
        # Skip if trade already closed (race condition)
        if symbol in trades and trades[symbol].get("closed"):
            print(f"⚠️ Skipping FILLED notification — trade already closed for {symbol}")
            return

        message = f"""#<b>{symbol}</b> All entry targets achieved ✅
Average Entry Price: <b>{filled_price}</b> 💵"""
        send_telegram_message(message)
        notified_orders[order_id] = "FILLED"

        trades[symbol] = {
            "side": side,
            "entry_price": filled_price,
            "order_id": order_id,
            "closed": False,
            "exit_price": None,
            "pnl": 0,
            "pnl_percent": 0
        }

    else:
        print(f"⚠️ Unknown order status {status} for {symbol}")


# =======================
# 🟥 TRADE EXIT LOGGING
# =======================
def log_trade_exit(symbol: str, order_id: str, filled_price: float):
    """Record and notify trade exit."""
    symbol = symbol.upper()

    if symbol not in trades:
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": filled_price,
            "closed": True,
            "exit_price": filled_price,
            "pnl": 0,
            "pnl_percent": 0
        }

    trade = trades[symbol]
    if trade["closed"]:
        print(f"⚠️ Skipping duplicate exit for {symbol}")
        return

    trade["exit_price"] = filled_price
    trade["closed"] = True

    qty = TRADE_AMOUNT
    entry_price = trade["entry_price"]
    side = trade["side"]

    # PnL Calculation
    if side == "BUY":
        pnl = (filled_price - entry_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((filled_price - entry_price) / entry_price) * 100 * LEVERAGE
    elif side == "SELL":
        pnl = (entry_price - filled_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((entry_price - filled_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl = pnl_percent = 0

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    icon = "✅" if pnl > 0 else "⛔️"
    tag = "Profit Achieved!" if pnl > 0 else "Ended in Loss!"

    message = f"""{tag} {icon}
PnL: <b>{trade['pnl']}$</b> | {trade['pnl_percent']}%
--- ⌁ ---
Symbol: <b>#{symbol}</b>
--- ⌁ ---
Entry: {trade['entry_price']}
Exit: {trade['exit_price']}"""
    send_telegram_message(message)


# =======================
# 📅 DAILY SUMMARY THREAD
# =======================
def send_daily_summary():
    """Send daily performance summary automatically at midnight (IST)."""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        print(f"🕛 Next summary in {round(sleep_seconds/3600, 2)} hours...")
        time.sleep(sleep_seconds)

        closed_trades = [t for t in trades.values() if t["closed"]]
        total_signals = len(trades)
        profitable = sum(1 for t in closed_trades if t["pnl"] > 0)
        lost = sum(1 for t in closed_trades if t["pnl"] < 0)
        open_trades = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in closed_trades), 2)

        detailed_msg = ""
        for symbol, t in trades.items():
            if t["closed"]:
                icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{symbol} {t['side']} {icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""{detailed_msg}
👇🏻 <b>Signals Summary</b>
➕ Total Signals: {total_signals}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open Trades: {open_trades}
✅ Net PnL %: {net_pnl_percent}%"""

        send_telegram_message(summary_msg)
        trades.clear()


# Start background summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
