import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dt_time

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

ASSETS = {
    "EUR/USD": "EUR/USD",
    "XAU/USD (Gold)": "XAU/USD",
    "BTC/USD": "BTC/USD",
    "ETH/USD": "ETH/USD",
    "USD/JPY": "USD/JPY",
    "GBP/JPY": "GBP/JPY"
}

STATE_FILE = "last_alerts.json"
COOLDOWN_HOURS = 2  # Don't repeat the same asset alert for 2 hours

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
        print("Error saving state:", e)

def should_send_alert(symbol_name):
    history = load_alert_history()
    if symbol_name in history:
        last_time = datetime.fromisoformat(history[symbol_name])
        if datetime.utcnow() - last_time < timedelta(hours=COOLDOWN_HOURS):
            print(f"⏳ {symbol_name}: Cooldown active. Sent alert recently ({last_time.strftime('%H:%M UTC')}). Skipping duplicate.")
            return False
    return True

def record_alert_sent(symbol_name):
    history = load_alert_history()
    history[symbol_name] = datetime.utcnow().isoformat()
    save_alert_history(history)

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens missing.")
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

def fetch_twelve_data(symbol, interval, outputsize):
    if not TWELVE_DATA_API_KEY:
        print("❌ TWELVE_DATA_API_KEY secret is missing in GitHub Settings!")
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

def analyze_institutional_smc(symbol_name, symbol_code):
    htf_interval = "4h" if ("BTC" in symbol_name or "ETH" in symbol_name) else "1h"
    
    df_htf = fetch_twelve_data(symbol_code, htf_interval, 50)
    time.sleep(8)
    
    df_15m = fetch_twelve_data(symbol_code, "15min", 50)
    time.sleep(8)

    if df_htf.empty or df_15m.empty:
        print(f"⚠️ Skipping {symbol_name} due to missing data.")
        return

    if "BTC" not in symbol_name and "ETH" not in symbol_name:
        if not is_in_killzone():
            print(f"⏳ {symbol_name}: Outside Kill Zone hours. Skipping...")
            return

    # 1. HTF BIAS
    htf_high = float(df_htf['High'].iloc[-15:-2].max())
    htf_low = float(df_htf['Low'].iloc[-15:-2].min())
    htf_close = float(df_htf['Close'].iloc[-1])

    htf_bullish = htf_close > (htf_high + htf_low) / 2
    htf_bearish = not htf_bullish

    # 2. LTF STRUCTURE
    df_15m['ATR'] = (df_15m['High'] - df_15m['Low']).rolling(14).mean()
    
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
    
    atr = float(df_15m['ATR'].iloc[-1])

    swing_high_15m = float(df_15m['High'].iloc[-18:-2].max())
    swing_low_15m = float(df_15m['Low'].iloc[-18:-2].min())

    bull_sweep = (c1_low < swing_low_15m) and (c1_close > swing_low_15m)
    bear_sweep = (c1_high > swing_high_15m) and (c1_close < swing_high_15m)

    bull_mss = bull_sweep or (c1_close > c2_high)
    bear_mss = bear_sweep or (c1_close < c2_low)

    bull_fvg = c1_low > c3_high
    bear_fvg = c1_high < c3_low

    bull_ob = (c2_close_val < c2_open) and (c1_close > c2_high)
    bear_ob = (c2_close_val > c2_open) and (c1_close < c2_low)

    valid_buy = htf_bullish and bull_mss and (bull_fvg or bull_ob)
    valid_sell = htf_bearish and bear_mss and (bear_fvg or bear_ob)

    htf_str = "BULLISH" if htf_bullish else "BEARISH"
    print(f"🔍 {symbol_name} | Price: {c0_close:.4f} | {htf_interval.upper()} Bias: {htf_str} | Signal: {'YES' if (valid_buy or valid_sell) else 'NO SETUP'}")

    if valid_buy or valid_sell:
        # CHECK FOR DUPLICATES BEFORE SENDING
        if not should_send_alert(symbol_name):
            return

        direction = "INSTITUTIONAL BUY (1:4 RR)" if valid_buy else "INSTITUTIONAL SELL (1:4 RR)"
        sl = (c1_low - (atr * 0.05)) if valid_buy else (c1_high + (atr * 0.05))
        risk = abs(c0_close - sl)
        tp_14 = (c0_close + (risk * 4.0)) if valid_buy else (c0_close - (risk * 4.0))

        msg = (f"⚡ HIGH-PRECISION 1:4 SMC ALERT ⚡\n\n"
               f"Asset: {symbol_name}\nDirection: {direction}\nPrice: {round(c0_close, 4)}\n\n"
               f"✓ {htf_interval.upper()} Trend Aligned\n"
               f"✓ Kill Zone Active\n"
               f"✓ 15m Displacement / Structure Shift\n"
               f"✓ Tight Invalidation Stop Applied\n\n"
               f"📍 Entry: {round(c0_close, 4)}\n🛡️ Tight SL: {round(sl, 4)}\n🎯 Take Profit (1:4 RR): {round(tp_14, 4)}")
        
        send_telegram(msg)
        record_alert_sent(symbol_name)

print(f"--- STARTING SMC SCAN AT {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} ---")
for name, ticker_code in ASSETS.items():
    try:
        analyze_institutional_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
print("--- SCAN COMPLETE ---")
