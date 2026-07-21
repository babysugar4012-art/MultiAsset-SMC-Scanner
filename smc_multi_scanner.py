import os
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, time

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSETS = {
    "EUR/USD": "EURUSD=X",
    "XAU/USD (Gold)": "GC=F",
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "USD/JPY": "JPY=X",
    "GBP/JPY": "GBPJPY=X",
    "XAG/USD (Silver)": "SI=F"
}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, data=payload)

def is_in_killzone():
    # London & New York Active Trading Window (06:00 UTC to 20:00 UTC)
    # Crypto (BTC/ETH) bypasses session timing due to 24/7 liquidity
    now_utc = datetime.utcnow().time()
    london_ny_start = time(6, 0)
    london_ny_end = time(20, 0)
    return london_ny_start <= now_utc <= london_ny_end

def analyze_institutional_smc(symbol_name, ticker):
    df_4h = yf.download(ticker, period="60d", interval="1h", progress=False)
    df_15m = yf.download(ticker, period="5d", interval="15m", progress=False)

    if len(df_4h) < 50 or len(df_15m) < 30:
        return

    # Check Kill Zone Timing (Skip Forex/Metals outside high volume hours)
    if "BTC" not in symbol_name and "ETH" not in symbol_name:
        if not is_in_killzone():
            return

    # --- 1. 4H HTF BIAS ANALYSIS ---
    htf_high = df_4h['High'].iloc[-20:-2].max()
    htf_low = df_4h['Low'].iloc[-20:-2].min()
    htf_close = df_4h['Close'].iloc[-1]

    htf_bullish = htf_close > (htf_high + htf_low) / 2
    htf_bearish = not htf_bullish

    # --- 2. 15M LTF STRUCTURE & SWEEP ---
    df_15m['ATR'] = (df_15m['High'] - df_15m['Low']).rolling(14).mean()
    
    c0_close = df_15m['Close'].iloc[-1]
    c1_high, c1_low, c1_close = df_15m['High'].iloc[-2], df_15m['Low'].iloc[-2], df_15m['Close'].iloc[-2]
    c2_high, c2_low = df_15m['High'].iloc[-3], df_15m['Low'].iloc[-3]
    c3_high, c3_low = df_15m['High'].iloc[-4], df_15m['Low'].iloc[-4]
    atr = df_15m['ATR'].iloc[-1]

    swing_high_15m = df_15m['High'].iloc[-22:-2].max()
    swing_low_15m = df_15m['Low'].iloc[-22:-2].min()

    bull_sweep = (c1_low < swing_low_15m) and (c1_close > swing_low_15m)
    bear_sweep = (c1_high > swing_high_15m) and (c1_close < swing_high_15m)

    bull_mss = bull_sweep and (c1_close > c2_high)
    bear_mss = bear_sweep and (c1_close < c2_low)

    # --- 3. ORDER BLOCK & FVG CONFLUENCE ---
    bull_fvg = c1_low > c3_high
    bear_fvg = c1_high < c3_low

    # Identify 15M Order Block (Last opposite candle before displacement)
    c2_open, c2_close_val = df_15m['Open'].iloc[-3], df_15m['Close'].iloc[-3]
    bull_ob = (c2_close_val < c2_open) and bull_mss  # Bearish candle before bullish displacement
    bear_ob = (c2_close_val > c2_open) and bear_mss  # Bullish candle before bearish displacement

    # --- 4. OPTIMAL TRADE ENTRY (OTE / DISCOUNT-PREMIUM FILTER) ---
    swing_range = swing_high_15m - swing_low_15m
    discount_level = swing_low_15m + (swing_range * 0.382) # Entry in bottom 38.2% (Discount)
    premium_level = swing_high_15m - (swing_range * 0.382)  # Entry in top 38.2% (Premium)

    in_discount = c0_close <= discount_level
    in_premium = c0_close >= premium_level

    # --- FINAL HIGH-CONFLUENCE VALIDATION ---
    valid_buy = htf_bullish and bull_mss and (bull_fvg or bull_ob) and in_discount
    valid_sell = htf_bearish and bear_mss and (bear_fvg or bear_ob) and in_premium

    if valid_buy or valid_sell:
        direction = "INSTITUTIONAL BUY (4H + OB + OTE)" if valid_buy else "INSTITUTIONAL SELL (4H + OB + OTE)"
        sl = (c1_low - (atr * 0.2)) if valid_buy else (c1_high + (atr * 0.2))
        risk = abs(c0_close - sl)
        tp = (c0_close + (risk * 3.0)) if valid_buy else (c0_close - (risk * 3.0))

        msg = (f"⚡ HIGH-CONFLUENCE SMC ALERT ⚡\n\n"
               f"Asset: {symbol_name}\nDirection: {direction}\nPrice: {round(c0_close, 4)}\n\n"
               f"✓ 4H HTF Bias Confirmed\n"
               f"✓ Kill Zone Volume Active\n"
               f"✓ 15m Liquidity Sweep & MSS\n"
               f"✓ Order Block / FVG Confluence\n"
               f"✓ OTE Pricing ({'Discount Zone' if valid_buy else 'Premium Zone'})\n\n"
               f"📍 Entry: {round(c0_close, 4)}\n🛡️ Stop Loss: {round(sl, 4)}\n🎯 Take Profit (1:3): {round(tp, 4)}")
        send_telegram(msg)

for name, ticker_code in ASSETS.items():
    try:
        analyze_institutional_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
