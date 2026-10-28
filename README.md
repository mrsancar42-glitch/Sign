# Signal G5 V3

A strict Binance Spot scanner for Telegram. It sends alerts only; it never opens or closes orders.

## Entry logic

- **4H:** primary trend must be clear: EMA20/EMA50 alignment, price on the correct side of EMA50, and EMA20 slope.
- **1H:** same direction as 4H, with DMI confirmation and ADX >= 20.
- **15M:** same direction as 1H/4H, ADX >= 16, a pullback into the EMA20 zone, a directional candle close, RSI in a directional momentum range, and MACD histogram confirmation.
- **Volume:** the mean volume of the last three closed 15M candles must be at least 0.75x the previous 20-candle mean. Stronger volume adds a score point.
- **Risk:** stop beyond recent 15M structure, with a 1.0-2.5 ATR range. The nearest confirmed 1H swing level blocks the entry if it is closer than 2R; otherwise the target is a usable 1H level or a clearly labeled model target of 2.2R.
- **Stale alert guard:** the current ticker price must still be within 0.35 ATR of the signal candle close, and the price must not have already crossed TP/SL.
- **Quality:** only score >= 9/10 signals are sent. No signal is preferable to a weak signal.

## Frequency and duplicate protection

- Maximum 4 signals in one scan and 20 signals per Istanbul local calendar day.
- **Each symbol is sent at most once per Istanbul local day**, regardless of whether the next setup has the same or opposite direction.
- The workflow persists `signal_g5_state.json` by committing it to the repository. This is required for deduplication across GitHub Actions runs.
- When the alert arrives, a coin may already have moved. The stale-price guard reduces this risk but cannot eliminate latency or execution slippage.

## GitHub repository structure

Place these files at the repository root / workflow path exactly as shown:

```text
repository/
├── signal_g5.py
├── signal_g5_state.json
└── .github/
    └── workflows/
        └── signal_g5.yml
```

The downloaded ZIP preserves the `signal_g5/` folder, so copy its `signal_g5.py` to the repository root; put `signal_g5.yml` in `.github/workflows/` and `signal_g5_state.json` in the root.

## Secrets and permissions

Add repository Actions secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`. The workflow needs `contents: write` to commit the state file. If the state commit fails, check **Settings → Actions → General → Workflow permissions** and allow read/write repository permissions.

## Important limits

The score is a rule-based filter, not a probability of profit. R:R does not include exchange fees, spread, slippage or order execution. The strategy must be validated with a historical backtest including fees and an out-of-sample period before relying on it with real funds. Alerts use Binance Spot market data and do not place orders.
