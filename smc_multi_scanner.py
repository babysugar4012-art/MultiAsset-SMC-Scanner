import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# ==========================================
# 1. CONFIGURATION & STATE TRACKING
# ==========================================
ACTIVE_POSITIONS = {}
COOLDOWN_HOURS = 4  # Prevent re-signaling same pair within 4 hours

class SMCScanner:
    def __init__(self, symbol):
        self.symbol = symbol

    def is_on_cooldown(self, current_time):
        """Prevents signal spamming on Telegram."""
        if self.symbol in ACTIVE_POSITIONS:
            last_signal_time = ACTIVE_POSITIONS[self.symbol]
            if current_time - last_signal_time < timedelta(hours=COOLDOWN_HOURS):
                return True
        return False

    # ==========================================
    # 2. HIGHER TIMEFRAME (4H) DISCOUNT/PREMIUM
    # ==========================================
    def check_htf_structure(self, df_4h):
        """
        Determines if 4H is Bullish/Bearish AND ensures price isn't over-extended.
        Discount = < 50% of 4H Swing (Valid for BUY)
        Premium  = > 50% of 4H Swing (Valid for SELL)
        """
        recent_high = df_4h['high'].tail(50).max()
        recent_low = df_4h['low'].tail(50).min()
        equilibrium = (recent_high + recent_low) / 2
        
        current_close = df_4h['close'].iloc[-1]

        # 4H Market Structure Direction (simplified via swing highs/lows)
        htf_trend = "BULLISH" if current_close > equilibrium else "BEARISH"

        is_in_discount = current_close < equilibrium
        is_in_premium = current_close > equilibrium

        return {
            "htf_trend": htf_trend,
            "is_discount": is_in_discount,
            "is_premium": is_in_premium,
            "eq_price": equilibrium
        }

    # ==========================================
    # 3. INDUCEMENT (IDM) & LIQUIDITY SWEEP
    # ==========================================
    def check_inducement_sweep(self, df_15m):
        """
        Ensures the liquidity sweep was of a MAJOR swing high/low, 
        not just minor internal candle noise.
        """
        # Define Swing Low (IDM Low) as low lower than 5 candles left & right
        df_15m['swing_low'] = df_15m['low'][(df_15m['low'] == df_15m['low'].rolling(11, center=True).min())]
        df_15m['swing_high'] = df_15m['high'][(df_15m['high'] == df_15m['high'].rolling(11, center=True).max())]

        last_candles = df_15m.tail(5)
        
        # Check if recent wick swept below a recognized 15m Major Swing Low
        major_lows = df_15m['swing_low'].dropna().iloc[-4:-1] # Ignore current candle
        major_highs = df_15m['swing_high'].dropna().iloc[-4:-1]

        swept_sell_side_idm = False
        swept_buy_side_idm = False

        if not major_lows.empty:
            lowest_idm = major_lows.min()
            # Wick pierces major low, but candle closes back above (True Liquidity Grab)
            if (last_candles['low'].min() < lowest_idm) and (last_candles['close'].iloc[-1] > lowest_idm):
                swept_sell_side_idm = True

        if not major_highs.empty:
            highest_idm = major_highs.max()
            if (last_candles['high'].max() > highest_idm) and (last_candles['close'].iloc[-1] < highest_idm):
                swept_buy_side_idm = True

        return swept_sell_side_idm, swept_buy_side_idm

    # ==========================================
    # 4. ENTRY LOGIC & SIGNAL GENERATION
    # ==========================================
    def analyze_setup(self, df_4h, df_15m, df_5m, current_time):
        if self.is_on_cooldown(current_time):
            return None  # Skip scan, on cooldown

        htf = self.check_htf_structure(df_4h)
        swept_sell_idm, swept_buy_idm = self.check_inducement_sweep(df_15m)

        # -------------------
        # BUY CONFLUENCE
        # -------------------
        # Rule 1: 4H must be Bullish OR in a deep 4H Discount Zone
        # Rule 2: Must have swept SELL-SIDE Inducement (IDM Liquidity)
        if htf['is_discount'] and swept_sell_idm:
            # Check 5m/15m FVG Displacement
            if self.has_valid_fvg(df_5m, direction="BUY"):
                score = self.calculate_confluence_score(htf, swept_sell_idm, True)
                if score >= 8:
                    ACTIVE_POSITIONS[self.symbol] = current_time
                    return self.format_telegram_signal("BUY", score, df_5m)

        # -------------------
        # SELL CONFLUENCE
        # -------------------
        # Rule 1: Must be in Premium Zone
        # Rule 2: Must have swept BUY-SIDE Inducement
        if htf['is_premium'] and swept_buy_idm:
            if self.has_valid_fvg(df_5m, direction="SELL"):
                score = self.calculate_confluence_score(htf, swept_buy_idm, True)
                if score >= 8:
                    ACTIVE_POSITIONS[self.symbol] = current_time
                    return self.format_telegram_signal("SELL", score, df_5m)

        return None

    def has_valid_fvg(self, df, direction="BUY"):
        """Checks for 3-bar Fair Value Gap with displacement."""
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        if direction == "BUY":
            return c3['low'] > c1['high']  # Bullish FVG
        else:
            return c3['high'] < c1['low']  # Bearish FVG

    def calculate_confluence_score(self, htf, idm_swept, fvg_valid):
        score = 0
        if htf['is_discount'] or htf['is_premium']: score += 3
        if idm_swept: score += 4  # Heavy weight on Major IDM Sweep
        if fvg_valid: score += 3
        return score

    def format_telegram_signal(self, side, score, df):
        entry = df['close'].iloc[-1]
        sl = df['low'].tail(5).min() if side == "BUY" else df['high'].tail(5).max()
        tp = entry + (abs(entry - sl) * 2.5) if side == "BUY" else entry - (abs(entry - sl) * 2.5)
        
        return f"🚨 **NEW SIGNAL: {self.symbol} ({side})**\n" \
               f"📊 Confluence Score: {score}/10\n" \
               f"🔹 Entry: {entry:.2f}\n" \
               f"🛑 SL: {sl:.2f}\n" \
               f"🎯 TP (1:2.5): {tp:.2f}\n" \
               f"⚡ State: HTF Discount + Major IDM Swept"
