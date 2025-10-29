# trade_notifier.py

import requests
import threading
import time
import datetime

# =======================
# 🔧 CONFIG
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
# 📢 TELEGRAM
# =======================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping message.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=payload, timeout=10)

        if response.status_code != 200:
            print("❌ Telegram Error:", response.status_code, response.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)


# =======================
# 🟩 TRADE ENTRY LOGGING
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, status: str, filled_price: float = None):
    """Send entry updates in two stages — creation & fill"""

    # If this symbol trade already exists and is closed → skip any fill notification
    if symbol in trades and trades[symbol].get("closed", False):
        print(f"⚠️ Skipping fill for {symbol} — trade already closed.")
        return

    # 1️⃣ Order created
    if status == "NEW" and notified_orders.get(order_id) != "NEW":
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

    # 2️⃣ Order fully filled
    elif status == "FILLED" and notified_orders.get(order_id) != "FILLED":
        # If somehow this trade was already marked closed, skip
        if symbol in trades and trades[symbol].get("closed", False):
            print(f"⚠️ Skipping duplicate FILLED message for closed trade {symbol}")
            return

        message = f"""#<b>{symbol}</b> All entry targets achieved ✅
Average Entry Price: <b>{filled_price}</b> 💵"""
        send_telegram_message(message)
        notified_orders[order_id] = "FILLED"

        # Save trade record
        trades[symbol] = {
            "side": side,
            "entry_price": filled_price,
            "order_id": order_id,
            "closed": False,
            "exit_price": None,
            "pnl": 0,
            "pnl_percent": 0
        }


# =======================
# 🟥 TRADE EXIT LOGGING
# =======================
def log_trade_exit(symbol: str, order_id: str, filled_price: float):
    """Record and notify a trade exit"""

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
    trade["exit_price"] = filled_price
    trade["closed"] = True

    # Calculate PnL
    qty = TRADE_AMOUNT
    entry_price = trade.get("entry_price", filled_price)

    if trade["side"] == "BUY":
        pnl = (filled_price - entry_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((filled_price - entry_price) / entry_price) * 100 * LEVERAGE
    elif trade["side"] == "SELL":
        pnl = (entry_price - filled_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((entry_price - filled_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl = pnl_percent = 0

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    status_icon = "🤑" if pnl > 0 else "⛔️"

    message = f"""📉 <b>Trade Closed</b> {status_icon}
Symbol: <b>#{symbol}</b>
Side: <b>{trade['side']}</b>
--- ⌁ ---
Entry: {trade['entry_price']}
Exit: {trade['exit_price']}
--- ⌁ ---
PnL $: {trade['pnl']}
PnL %: {trade['pnl_percent']}%"""
    send_telegram_message(message)


# =======================
# 📅 DAILY SUMMARY
# =======================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
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


# Start daily summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
