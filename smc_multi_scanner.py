import os
import sys
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# ==========================================
# 1. ENVIRONMENT & CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

SYMBOLS = ["EUR/USD", "XAU/USD", "BTC/USD", "USD/JPY"]
COOLDOWN_HOURS = 4  # Prevent re-signaling same pair within 4 hours
STATE_FILE = "scanner_state.json"

# ==========================================
# 2. STATE MANAGEMENT (PERSISTENT COOLDOWN)
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing. Message skipped.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"❌ Failed to send Telegram alert: {e}")

# ==========================================
# 3. TWELVE DATA API FETCH
# ==========================================
def fetch_twelve_data(symbol, interval, outputsize=100):
    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY is missing from environment variables.")
        
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY
    }
    response = requests.get(url, params=params, timeout=15)
    data = response.json()

    if "values" not in data:
        error_msg = data.get("message", "Unknown API error")
        raise ConnectionError(f"TwelveData API Error for {symbol} ({interval}): {error_msg}")

    df = pd.DataFrame(data["values"])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(float)
        
    return df

# ==========================================
# 4. SMC SCANNER CLASS WITH FIXES
# ==========================================
class SMCScanner:
    def __init__(self, symbol, state):
        self.symbol = symbol
        self.state = state

    def is_on_cooldown(self, current_time):
        """Fix #1: Cooldown check to prevent signal spamming."""
        if self.symbol in self.state:
            last_signal_str = self.state[self.symbol]
            last_signal_time = datetime.fromisoformat(last_signal_str)
            if current_time - last_signal_time < timedelta(hours=COOLDOWN_HOURS):
                return True
        return False

    def check_htf_structure(self, df_4h):
        """Fix #2: Ensure price is in Discount (for Buy) or Premium (for Sell)."""
        recent_high = df_4h['high'].tail(50).max()
        recent_low = df_4h['low'].tail(50).min()
        equilibrium = (recent_high + recent_low) / 2
        current_close = df_4h['close'].iloc[-1]

        return {
            "is_discount": current_close < equilibrium,
            "is_premium": current_close > equilibrium,
            "eq_price": equilibrium
        }

    def check_inducement_sweep(self, df_15m):
        """Fix #3: Must sweep major 15m fractal swing high/low."""
        df_15m['swing_low'] = df_15m['low'][(df_15m['low'] == df_15m['low'].rolling(11, center=True).min())]
        df_15m['swing_high'] = df_15m['high'][(df_15m['high'] == df_15m['high'].rolling(11, center=True).max())]

        last_candles = df_15m.tail(5)
        major_lows = df_15m['swing_low'].dropna().iloc[-4:-1]
        major_highs = df_15m['swing_high'].dropna().iloc[-4:-1]

        swept_sell_side_idm = False
        swept_buy_side_idm = False

        if not major_lows.empty:
            lowest_idm = major_lows.min()
            if (last_candles['low'].min() < lowest_idm) and (last_candles['close'].iloc[-1] > lowest_idm):
                swept_sell_side_idm = True

        if not major_highs.empty:
            highest_idm = major_highs.max()
            if (last_candles['high'].max() > highest_idm) and (last_candles['close'].iloc[-1] < highest_idm):
                swept_buy_side_idm = True

        return swept_sell_side_idm, swept_buy_side_idm

    def has_valid_fvg(self, df, direction="BUY"):
        """5m Fair Value Gap with Displacement."""
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        if direction == "BUY":
            return c3['low'] > c1['high']
        else:
            return c3['high'] < c1['low']

    def analyze_setup(self, df_4h, df_15m, df_5m, current_time):
        if self.is_on_cooldown(current_time):
            print(f"⏳ {self.symbol} is on active signal cooldown. Skipping.")
            return None, False

        htf = self.check_htf_structure(df_4h)
        swept_sell_idm, swept_buy_idm = self.check_inducement_sweep(df_15m)

        # BUY SETUP
        if htf['is_discount'] and swept_sell_idm:
            if self.has_valid_fvg(df_5m, direction="BUY"):
                score = 8
                signal = self.format_telegram_signal("BUY", score, df_5m)
                return signal, True

        # SELL SETUP
        if htf['is_premium'] and swept_buy_idm:
            if self.has_valid_fvg(df_5m, direction="SELL"):
                score = 8
                signal = self.format_telegram_signal("SELL", score, df_5m)
                return signal, True

        return None, False

    def format_telegram_signal(self, side, score, df):
        entry = df['close'].iloc[-1]
        sl = df['low'].tail(5).min() if side == "BUY" else df['high'].tail(5).max()
        risk = abs(entry - sl)
        tp = (entry + (risk * 2.5)) if side == "BUY" else (entry - (risk * 2.5))

        return (
            f"🚨 *NEW SMC SIGNAL: {self.symbol} ({side})*\n"
            f"📊 *Confluence Score:* {score}/10\n\n"
            f"🔹 *Entry:* `{entry:.4f}`\n"
            f"🛑 *SL:* `{sl:.4f}`\n"
            f"🎯 *TP (1:2.5):* `{tp:.4f}`\n\n"
            f"⚡ *Filters Met:* HTF Alignment + Major IDM Swept + 5m FVG"
        )

# ==========================================
# 5. MAIN EXECUTION LOOP
# ==========================================
def main():
    print("🚀 Starting SMC Multi-Asset Scan...")
    sys.stdout.flush()

    state = load_state()
    now = datetime.utcnow()

    for symbol in SYMBOLS:
        print(f"\n🔍 Scanning {symbol}...")
        sys.stdout.flush()

        try:
            # Fetch data across timeframes
            df_4h = fetch_twelve_data(symbol, interval="4h")
            df_15m = fetch_twelve_data(symbol, interval="15m")
            df_5m = fetch_twelve_data(symbol, interval="5m")

            scanner = SMCScanner(symbol, state)
            signal, triggered = scanner.analyze_setup(df_4h, df_15m, df_5m, now)

            if triggered and signal:
                print(f"✅ HIGH CONFLUENCE SETUP FOUND for {symbol}!")
                send_telegram_message(signal)
                state[symbol] = now.isoformat()
            else:
                print(f"⚪ No high-conviction setup for {symbol}.")

        except Exception as e:
            print(f"❌ Error scanning {symbol}: {str(e)}")

        sys.stdout.flush()

    save_state(state)
    print("\n🏁 Scan completed successfully!")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
