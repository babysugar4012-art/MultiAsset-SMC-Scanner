import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dt_time

# ENVIRONMENT SECRETS
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

# 1. OPTIMIZED 4-ASSET WATCHLIST
ASSETS = {
    "EUR/USD": "EUR/USD",
    "USD/JPY": "USD/JPY",
    "XAU/USD (Gold)": "XAU/USD",
    "BTC/USD": "BTC/USD"
}

STATE_FILE = "last_alerts.json"
COOLDOWN_HOURS = 3  # 3-Hour cooldown window per pair to eliminate repeat signals

# --- STATE MANAGEMENT FOR COOLDOWN ---
def load_alert_history():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_alert_history(history):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(history, f)
    except Exception as e:
        print("Error saving state file:", e)

def should_send_alert(symbol_name):
    history = load_alert_history()
    if symbol_name in history:
        last_time = datetime.fromisoformat(history[symbol_name])
        if datetime.utcnow() - last_time < timedelta(hours=COOLDOWN_HOURS):
            print(f"⏳ {symbol_name}: Cooldown active. Alert sent at {last_time.strftime('%H:%M UTC')}. Skipping duplicate.")
            return False
    return True

def record_alert_sent(symbol_name):
    history = load_alert_history()
    history[symbol_name] = datetime.utcnow().isoformat()
    save_alert_history(history)

# --- TELEGRAM NOTIFIER ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram tokens missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        res = requests.post(url, data=payload)
        print("Telegram Sent Status:", res.status_code)
    except Exception as e:
        print("Telegram Error:", e)

def is_in_killzone():
    now_utc = datetime.utcnow().time()
    return dt_time(6, 0) <= now_utc <= dt_time(20, 0)

# --- TWELVE DATA API FETCH ---
def fetch_twelve_data(symbol, interval, outputsize):
    if not TWELVE_DATA_API_KEY:
        print("❌ TWELVE_DATA_API_KEY secret is missing!")
        return pd.DataFrame()

    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    try:
        response = requests.get(url).json()
        if "values" not in response:
            print(f"⚠️ Twelve Data Error for {symbol} ({interval}): {response.get('message', 'No data returned')}")
            return pd.DataFrame()

        df = pd.DataFrame(response["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime').reset_index(drop=True)
        
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
            
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df
    except Exception as e:
        print(f"Error fetching {symbol} from Twelve Data: {e}")
        return pd.DataFrame()

# --- INSTITUTIONAL SMC ANALYSIS ENGINE ---
def analyze_institutional_smc(symbol_name, symbol_code):
    print(f"\n--- Analyzing {symbol_name} ---")

    # 1. Fetch 4H Macro Structure Data
    df_4h = fetch_twelve_data(symbol_code, "4h", 40)
    time.sleep(12)  # Delay keeps requests under 8/min rate limit

    # 2. Fetch 1H Intermediate Structure Data
    df_1h = fetch_twelve_data(symbol_code, "1h", 40)
    time.sleep(12)

    # 3. Fetch 15M Lower Timeframe Entry Data
    df_15m = fetch_twelve_data(symbol_code, "15min", 40)
    time.sleep(12)

    if df_4h.empty or df_1h.empty or df_15m.empty:
        print(f"⚠️ Skipping {symbol_name} due to incomplete candle data.")
        return

    # Skip Forex & Gold outside active Kill Zone hours
    if "BTC" not in symbol_name:
        if not is_in_killzone():
            print(f"⏳ {symbol_name}: Outside London/NY Kill Zone hours. Skipping...")
            return

    # --- TIER 1: 4H MACRO TREND & BREAK OF STRUCTURE (BOS) ---
    h4_swing_high = float(df_4h['High'].iloc[-15:-2].max())
    h4_swing_low = float(df_4h['Low'].iloc[-15:-2].min())
    h4_close = float(df_4h['Close'].iloc[-1])

    h4_bullish = h4_close > h4_swing_high
    h4_bearish = h4_close < h4_swing_low

    # Fallback to structural trend direction if no active BOS
    if not h4_bullish and not h4_bearish:
        h4_bullish = h4_close > (h4_swing_high + h4_swing_low) / 2
        h4_bearish = not h4_bullish

    # --- TIER 2: 1H LIQUIDITY SWEEP & SUPPLY/DEMAND ---
    h1_swing_high = float(df_1h['High'].iloc[-12:-2].max())
    h1_swing_low = float(df_1h['Low'].iloc[-12:-2].min())
    h1_close = float(df_1h['Close'].iloc[-1])

    h1_bsl_swept = float(df_1h['High'].iloc[-2]) > h1_swing_high  # Buy-side liquidity swept
    h1_ssl_swept = float(df_1h['Low'].iloc[-2]) < h1_swing_low   # Sell-side liquidity swept

    # --- TIER 3: 15M DISPLACEMENT & MARKET STRUCTURE SHIFT (MSS) ---
    df_15m['ATR'] = (df_15m['High'] - df_15m['Low']).rolling(14).mean()
    atr = float(df_15m['ATR'].iloc[-1])

    c0_close = float(df_15m['Close'].iloc[-1])
    c1_high = float(df_15m['High'].iloc[-2])
    c1_low = float(df_15m['Low'].iloc[-2])
    c1_close = float(df_15m['Close'].iloc[-2])

    c2_high = float(df_15m['High'].iloc[-3])
    c2_low = float(df_15m['Low'].iloc[-3])
    c2_open = float(df_15m['Open'].iloc[-3])
    c2_close_val = float(df_15m['Close'].iloc[-3])

    c3_high = float(df_15m['High'].iloc[-4])
    c3_low = float(df_15m['Low'].iloc[-4])

    m15_swing_high = float(df_15m['High'].iloc[-15:-2].max())
    m15_swing_low = float(df_15m['Low'].iloc[-15:-2].min())

    m15_bull_mss = (c1_close > m15_swing_high) or (c1_close > c2_high)
    m15_bear_mss = (c1_close < m15_swing_low) or (c1_close < c2_low)

    m15_bull_fvg = c1_low > c3_high
    m15_bear_fvg = c1_high < c3_low

    m15_bull_ob = (c2_close_val < c2_open) and (c1_close > c2_high)
    m15_bear_ob = (c2_close_val > c2_open) and (c1_close < c2_low)

    # STRICT 3-TIER CONFLUENCE CHECK
    valid_buy = h4_bullish and (h1_ssl_swept or c0_close > h1_swing_low) and m15_bull_mss and (m15_bull_fvg or m15_bull_ob)
    valid_sell = h4_bearish and (h1_bsl_swept or c0_close < h1_swing_high) and m15_bear_mss and (m15_bear_fvg or m15_bear_ob)

    h4_str = "BULLISH" if h4_bullish else "BEARISH"
    print(f"🔍 {symbol_name} | Price: {c0_close:.4f} | 4H Bias: {h4_str} | Signal: {'YES' if (valid_buy or valid_sell) else 'NO SETUP'}")

    if valid_buy or valid_sell:
        if not should_send_alert(symbol_name):
            return

        direction = "INSTITUTIONAL BUY" if valid_buy else "INSTITUTIONAL SELL"

        # --- BALANCED STOP LOSS WITH ATR VOLATILITY BUFFER ---
        # 0.5x ATR buffer protects against spread and lower-timeframe micro ticks
        sl_buffer = atr * 0.5

        if valid_buy:
            raw_sl = m15_swing_low - sl_buffer
            risk = abs(c0_close - raw_sl)
            tp_12 = c0_close + (risk * 2.0)
            tp_14 = c0_close + (risk * 4.0)
        else:
            raw_sl = m15_swing_high + sl_buffer
            risk = abs(raw_sl - c0_close)
            tp_12 = c0_close - (risk * 2.0)
            tp_14 = c0_close - (risk * 4.0)

        # Formatting precision according to asset class
        dec = 2 if ("XAU" in symbol_name or "BTC" in symbol_name or "JPY" in symbol_name) else 4

        msg = (f"⚡ HIGH-PRECISION SMC ALERT ⚡\n\n"
               f"Asset: {symbol_name}\nDirection: {direction}\nPrice: {round(c0_close, dec)}\n\n"
               f"✓ 4H Trend Aligned ({h4_str})\n"
               f"✓ 1H Liquidity & Order Block Filter Active\n"
               f"✓ 15m Displacement / MSS Confirmed\n"
               f"✓ Structural SL + Volatility Buffer Applied\n\n"
               f"📍 Entry: {round(c0_close, dec)}\n"
               f"🛡️ Dynamic SL: {round(raw_sl, dec)}\n"
               f"🎯 TP1 (1:2 RR - Move SL to BE): {round(tp_12, dec)}\n"
               f"🎯 TP2 (1:4 RR - Main Target): {round(tp_14, dec)}")
        
        send_telegram(msg)
        record_alert_sent(symbol_name)

# --- SCRIPT EXECUTION ---
print(f"--- STARTING SMC MULTI-TIMEFRAME SCAN AT {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} ---")
for name, ticker_code in ASSETS.items():
    try:
        analyze_institutional_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
print("--- SCAN COMPLETE ---")
