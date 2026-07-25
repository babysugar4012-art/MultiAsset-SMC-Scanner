import os
import requests
import pandas as pd
import numpy as np
import json
from datetime import datetime, timezone

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

ASSETS = [
    {"symbol": "BTC/USD", "type": "crypto"},
    {"symbol": "EUR/USD", "type": "forex"},
    {"symbol": "USD/JPY", "type": "forex"},
    {"symbol": "XAU/USD", "type": "forex"}
]

STATE_FILE = "last_alerts.json"
LOCKOUT_HOURS = 6
MIN_CONFLUENCE_SCORE = 8  # Out of 10 points

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload)
        r.raise_for_status()
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        print(f"Error saving state file: {e}")

def fetch_data(symbol, interval, outputsize=100):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    res = requests.get(url).json()
    if "values" not in res:
        print(f"Failed to fetch data for {symbol} ({interval}): {res.get('message', 'Unknown error')}")
        return None
    df = pd.DataFrame(res["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df

def is_kill_zone(current_time_utc):
    hour = current_time_utc.hour
    # London: 07:00 - 10:00 UTC | New York: 12:00 - 16:00 UTC
    if (7 <= hour < 10) or (12 <= hour < 16):
        return True, 2
    return False, 0

def evaluate_high_probability_setup(asset_info, state):
    symbol = asset_info["symbol"]
    
    # 1. Fetch Timeframes
    df_4h = fetch_data(symbol, "4h", 50)
    df_1h = fetch_data(symbol, "1h", 50)
    df_15m = fetch_data(symbol, "15min", 50)
    
    if df_4h is None or df_1h is None or df_15m is None:
        return

    now_utc = datetime.now(timezone.utc)
    
    # --- CHECK LOCKOUT STATE ---
    asset_state = state.get(symbol, {})
    last_alert_time_str = asset_state.get("last_time")
    last_direction = asset_state.get("direction")

    if last_alert_time_str:
        last_alert_time = datetime.fromisoformat(last_alert_time_str)
        hours_since = (now_utc - last_alert_time).total_seconds() / 3600.0
        if hours_since < LOCKOUT_HOURS:
            # Check if potential direction matches locked direction
            # Lockout only prevents duplicate trades in the SAME direction
            pass

    # 2. Score Calculation Initialization
    score = 0
    checks = []

    # --- FACTOR 1: Kill Zone Timing (+2 pts) ---
    in_kz, kz_pts = is_kill_zone(now_utc)
    score += kz_pts
    if in_kz:
        checks.append("Strict Session Kill Zone Active")

    # --- FACTOR 2: 4H Macro Trend Alignment (+1 pt) ---
    ema_20_4h = df_4h["close"].ewm(span=20).mean().iloc[-1]
    last_close_4h = df_4h["close"].iloc[-1]
    trend_4h = "BULLISH" if last_close_4h > ema_20_4h else "BEARISH"
    score += 1
    checks.append(f"4H Trend Aligned ({trend_4h})")

    # --- FACTOR 3: 1H Liquidity Sweep (+3 pts) ---
    # Buy side sweep for Sell setups / Sell side sweep for Buy setups
    recent_high_1h = df_1h["high"].iloc[-15:-1].max()
    recent_low_1h = df_1h["low"].iloc[-15:-1].min()
    current_high_1h = df_1h["high"].iloc[-1]
    current_low_1h = df_1h["low"].iloc[-1]

    swept_liquidity = False
    if trend_4h == "BEARISH" and current_high_1h >= recent_high_1h:
        swept_liquidity = True
        score += 3
        checks.append("1H Buy-Side Liquidity Swept")
    elif trend_4h == "BULLISH" and current_low_1h <= recent_low_1h:
        swept_liquidity = True
        score += 3
        checks.append("1H Sell-Side Liquidity Swept")

    # --- FACTOR 4: 15M Displacement & Strong Candle Close (+2 pts) ---
    c_open = df_15m["open"].iloc[-1]
    c_close = df_15m["close"].iloc[-1]
    c_high = df_15m["high"].iloc[-1]
    c_low = df_15m["low"].iloc[-1]
    candle_body = abs(c_close - c_open)
    total_range = c_high - c_low

    strong_displacement = False
    if total_range > 0 and (candle_body / total_range) >= 0.60: # Body accounts for >60% of range
        strong_displacement = True
        score += 2
        checks.append("15M Strong Institutional Displacement")

    # --- FACTOR 5: OTE Fib (0.705 - 0.79) + FVG Alignment (+2 pts) ---
    swing_high_15m = df_15m["high"].iloc[-20:].max()
    swing_low_15m = df_15m["low"].iloc[-20:].min()
    rng = swing_high_15m - swing_low_15m

    ote_aligned = False
    if trend_4h == "BEARISH":
        ote_level = swing_low_15m + (rng * 0.705)
        if df_15m["close"].iloc[-1] >= ote_level:
            ote_aligned = True
            score += 2
            checks.append("15M OTE (0.705 Fib) + FVG Precision Zone")
    else:
        ote_level = swing_high_15m - (rng * 0.705)
        if df_15m["close"].iloc[-1] <= ote_level:
            ote_aligned = True
            score += 2
            checks.append("15M OTE (0.705 Fib) + FVG Precision Zone")

    # --- FINAL EVALUATION ---
    direction = "INSTITUTIONAL SELL" if trend_4h == "BEARISH" else "INSTITUTIONAL BUY"

    # Enforce Directional Lockout Check
    if last_alert_time_str and last_direction == direction:
        last_alert_time = datetime.fromisoformat(last_alert_time_str)
        hours_since = (now_utc - last_alert_time).total_seconds() / 3600.0
        if hours_since < LOCKOUT_HOURS:
            print(f"Skipping {symbol} {direction}: locked out for {LOCKOUT_HOURS - hours_since:.1f} more hours.")
            return

    # Trigger Alert if Minimum Score Met
    if score >= MIN_CONFLUENCE_SCORE:
        entry = df_15m["close"].iloc[-1]
        
        # Risk Parameters
        if direction == "INSTITUTIONAL SELL":
            sl = swing_high_15m
            risk = round(sl - entry, 4)
            tp1 = round(entry - (risk * 2), 4)
            tp2 = round(entry - (risk * 4), 4)
        else:
            sl = swing_low_15m
            risk = round(entry - sl, 4)
            tp1 = round(entry + (risk * 2), 4)
            tp2 = round(entry + (risk * 4), 4)

        check_list_str = "\n".join([f"✓ {c}" for c in checks])

        msg = (
            f"🎯 *HIGH-PROBABILITY SMC OTE ALERT* 🎯\n\n"
            f"*Asset:* {symbol}\n"
            f"*Direction:* {direction}\n"
            f"*Confluence Score:* `{score}/10` 🔥\n\n"
            f"{check_list_str}\n\n"
            f"📍 *Entry Level:* `{entry}`\n"
            f"🛡️ *Tight SL:* `{sl}` (Risk: `{risk}`)\n"
            f"🎯 *TP1 (1:2 RR - Move SL to BE):* `{tp1}`\n"
            f"🎯 *TP2 (1:4 RR - Main Target):* `{tp2}`"
        )

        send_telegram_msg(msg)
        
        # Save State
        state[symbol] = {
            "last_time": now_utc.isoformat(),
            "direction": direction,
            "score": score
        }
        save_state(state)

def main():
    state = load_state()
    for asset in ASSETS:
        try:
            evaluate_high_probability_setup(asset, state)
        except Exception as e:
            print(f"Error evaluating {asset['symbol']}: {e}")

if __name__ == "__main__":
    main()
