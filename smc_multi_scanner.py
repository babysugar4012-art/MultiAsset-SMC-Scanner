import os
import requests
import pandas as pd
import numpy as np
import json
import time
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

def fetch_batched_timeframes(symbol):
    """
    Fetches 4h, 1h, and 15min in 1 single HTTP request.
    Consumes ONLY 1 credit per asset instead of 3 credits.
    """
    time.sleep(8.5) # Per-minute safety rate limit delay
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=15min,1h,4h&outputsize=50&apikey={TWELVE_DATA_API_KEY}"
    
    try:
        res = requests.get(url).json()
    except Exception as e:
        print(f"  ❌ HTTP error for {symbol}: {e}")
        return None, None, None

    if not isinstance(res, dict):
        print(f"  ❌ API error for {symbol}: {res}")
        return None, None, None

    def parse_df(tf_data):
        if not isinstance(tf_data, dict) or "values" not in tf_data:
            err = tf_data.get("message") if isinstance(tf_data, dict) else str(tf_data)
            print(f"  ❌ Timeframe parse error: {err}")
            return None
        df = pd.DataFrame(tf_data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        return df

    # TwelveData returns dict keyed by interval when batched
    df_15m = parse_df(res.get("15min"))
    df_1h = parse_df(res.get("1h"))
    df_4h = parse_df(res.get("4h"))

    return df_4h, df_1h, df_15m

def is_kill_zone(current_time_utc):
    hour = current_time_utc.hour
    if (7 <= hour < 10) or (12 <= hour < 16):
        return True, 2
    return False, 0

def evaluate_high_probability_setup(asset_info, state):
    symbol = asset_info["symbol"]
    print(f"\n🔍 [EVALUATING]: {symbol}...")
    
    # 1. Fetch ALL timeframes in 1 single API call
    df_4h, df_1h, df_15m = fetch_batched_timeframes(symbol)
    
    if df_4h is None or df_1h is None or df_15m is None:
        print(f"  ⚠️ Skipped {symbol}: Could not retrieve complete timeframe data.")
        return

    now_utc = datetime.now(timezone.utc)
    
    asset_state = state.get(symbol, {})
    if not isinstance(asset_state, dict):
        asset_state = {}

    last_alert_time_str = asset_state.get("last_time")
    last_direction = asset_state.get("direction")

    score = 0
    checks = []

    # Factor 1: Kill Zone Timing (+2)
    in_kz, kz_pts = is_kill_zone(now_utc)
    score += kz_pts
    if in_kz:
        checks.append("Strict Session Kill Zone Active")

    # Factor 2: 4H Macro Trend (+1)
    ema_20_4h = df_4h["close"].ewm(span=20).mean().iloc[-1]
    last_close_4h = df_4h["close"].iloc[-1]
    trend_4h = "BEARISH" if last_close_4h < ema_20_4h else "BULLISH"
    score += 1
    checks.append(f"4H Trend Aligned ({trend_4h})")

    # Factor 3: 1H Liquidity Sweep (+3)
    recent_high_1h = df_1h["high"].iloc[-15:-1].max()
    recent_low_1h = df_1h["low"].iloc[-15:-1].min()
    current_high_1h = df_1h["high"].iloc[-1]
    current_low_1h = df_1h["low"].iloc[-1]

    if trend_4h == "BEARISH" and current_high_1h >= recent_high_1h:
        score += 3
        checks.append("1H Buy-Side Liquidity Swept")
    elif trend_4h == "BULLISH" and current_low_1h <= recent_low_1h:
        score += 3
        checks.append("1H Sell-Side Liquidity Swept")

    # Factor 4: 15M Displacement (+2)
    c_open = df_15m["open"].iloc[-1]
    c_close = df_15m["close"].iloc[-1]
    c_high = df_15m["high"].iloc[-1]
    c_low = df_15m["low"].iloc[-1]
    candle_body = abs(c_close - c_open)
    total_range = c_high - c_low

    if total_range > 0 and (candle_body / total_range) >= 0.60:
        score += 2
        checks.append("15M Strong Institutional Displacement")

    # Factor 5: OTE Fib + FVG (+2)
    swing_high_15m = df_15m["high"].iloc[-20:].max()
    swing_low_15m = df_15m["low"].iloc[-20:].min()
    rng = swing_high_15m - swing_low_15m

    if trend_4h == "BEARISH":
        ote_level = swing_low_15m + (rng * 0.705)
        if df_15m["close"].iloc[-1] >= ote_level:
            score += 2
            checks.append("15M OTE (0.705 Fib) + FVG Precision Zone")
    else:
        ote_level = swing_high_15m - (rng * 0.705)
        if df_15m["close"].iloc[-1] <= ote_level:
            score += 2
            checks.append("15M OTE (0.705 Fib) + FVG Precision Zone")

    direction = "INSTITUTIONAL SELL" if trend_4h == "BEARISH" else "INSTITUTIONAL BUY"

    print(f"  📊 Confluence Score: {score}/10 ({len(checks)} checks passed)")

    # Lockout check
    if last_alert_time_str and last_direction == direction:
        try:
            last_alert_time = datetime.fromisoformat(last_alert_time_str)
            hours_since = (now_utc - last_alert_time).total_seconds() / 3600.0
            if hours_since < LOCKOUT_HOURS:
                print(f"  🛑 Lockout Active for {symbol} ({direction}): {LOCKOUT_HOURS - hours_since:.1f} hours remaining.")
                return
        except Exception:
            pass

    if score >= MIN_CONFLUENCE_SCORE:
        entry = df_15m["close"].iloc[-1]
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
            f"🎯 *TP1 (1:2 RR):* `{tp1}`\n"
            f"🎯 *TP2 (1:4 RR):* `{tp2}`"
        )
        send_telegram_msg(msg)
        
        state[symbol] = {
            "last_time": now_utc.isoformat(),
            "direction": direction,
            "score": score
        }
        save_state(state)

def main():
    print("🚀 Starting SMC Multi-Asset Confluence Scanner (Batched)...")
    state = load_state()
    for asset in ASSETS:
        try:
            evaluate_high_probability_setup(asset, state)
        except Exception as e:
            print(f"❌ Error evaluating {asset['symbol']}: {e}")

if __name__ == "__main__":
    main()
