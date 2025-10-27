# trade_notifier.py

import requests, threading, time, datetime, os
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

trades = {}  # Store ongoing and closed trades

# ===== Telegram Helper =====
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=payload)
    except Exception as e:
        print("❌ Telegram send failed:", e)

# ===== Trade Logging =====
def log_trade_entry(symbol, side, order_id, filled_price):
    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0
    }
    message = f"""Action: {side} 💹
Symbol: #{symbol}
--- ⌁ ---
Exchange: Binance Futures
Timeframe: 15 Mins
Leverage: {LEVERAGE}X
--- ⌁ ---
☑️ Entry Price: {filled_price}
--- ⌁ ---
⚠️ Wait for Close Signal!"""
    send_telegram_message(message)

def log_trade_exit(symbol, order_id, filled_price):
    if symbol not in trades:
        return

    trade = trades[symbol]
    trade["exit_price"] = filled_price
    trade["closed"] = True

    # Calculate PnL with leverage
    qty = TRADE_AMOUNT  # notional per trade in USD
    if trade["side"] == "BUY":
        pnl = (filled_price - trade["entry_price"]) * qty * LEVERAGE / trade["entry_price"]
        pnl_percent = ((filled_price - trade["entry_price"]) / trade["entry_price"]) * 100 * LEVERAGE
    else:
        pnl = (trade["entry_price"] - filled_price) * qty * LEVERAGE / trade["entry_price"]
        pnl_percent = ((trade["entry_price"] - filled_price) / trade["entry_price"]) * 100 * LEVERAGE

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    status_icon = "✅" if pnl > 0 else "⛔️"
    message = f"""#{symbol} {trade['side']} Closed {status_icon}
Entry Price: {trade['entry_price']}
Exit Price: {trade['exit_price']}
Profit/Loss: {trade['pnl_percent']}%
PnL $: {trade['pnl']}"""
    send_telegram_message(message)

# ===== End of Day Summary =====
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        time.sleep(sleep_seconds)

        total_signals = len(trades)
        profitable = sum(1 for t in trades.values() if t["closed"] and t["pnl"] > 0)
        lost = sum(1 for t in trades.values() if t["closed"] and t["pnl"] < 0)
        cancelled = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in trades.values() if t["closed"]), 2)

        detailed_msg = ""
        for t_sym, t in trades.items():
            if t["closed"]:
                status_icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{t_sym} {t['side']} Closed {status_icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""{detailed_msg}
👇🏻Signals Summary
➕Total Signals Sent out - {total_signals}
✔️Profitable Signals - {profitable}
✖️Total Signals Lost - {lost}
◼️Trade cancelled without being executed - {cancelled}
✅✅Net Profit - {net_pnl_percent}%"""
        send_telegram_message(summary_msg)
        trades.clear()  # Reset for next day

# Start daily summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
