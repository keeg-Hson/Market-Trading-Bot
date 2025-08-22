# backtest.py

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from data_utils import load_spy_daily_data
from datetime import timedelta, datetime
import subprocess

#auto update SPY data before anything else
subprocess.run(["python3", "update_spy_data.py"])

import os, json

# ─── Trade exit resolution ────────────────────────────────────────────────
def _resolve_bar_exit_long(bar, tp_px, sl_px):
    hi = bar.get("High"); lo = bar.get("Low")
    hit_tp = pd.notna(hi) and hi >= tp_px
    hit_sl = pd.notna(lo) and lo <= sl_px
    if hit_tp and hit_sl:         # don't decide here
        return "AMBIG", None, True
    if hit_tp: return "TP", tp_px, False
    if hit_sl: return "SL", sl_px, False
    return None, None, False

# ─── Trade exit resolution for shorts ─────────────────────────────────────
def _resolve_bar_exit_short(bar, tp_px, sl_px):
    hi = bar.get("High"); lo = bar.get("Low")
    hit_tp = pd.notna(lo) and lo <= tp_px    # profit down
    hit_sl = pd.notna(hi) and hi >= sl_px    # stop up
    if hit_tp and hit_sl:
        return "AMBIG", None, True
    if hit_tp: return "TP", tp_px, False
    if hit_sl: return "SL", sl_px, False
    return None, None, False




def _load_predictions(prefer_full: bool = True) -> pd.DataFrame:
    """
    Load predictions, preferring the full dump if available.
    Returns a dataframe with Timestamp (naive) and a normalized Date column.
    De-duplicates by Date keeping the last row (most recent signal of the day).
    """
    full_path  = "logs/predictions_full.csv"
    daily_path = "logs/daily_predictions.csv"

    path = full_path if (prefer_full and os.path.exists(full_path)) else daily_path
    if not os.path.exists(path):
        raise FileNotFoundError(f"No predictions file found at {path}")

    preds = pd.read_csv(path)
    # Timestamp → datetime (naive), then a daily Date key
    if "Timestamp" in preds.columns:
        preds["Timestamp"] = pd.to_datetime(preds["Timestamp"], errors="coerce").dt.tz_localize(None)
    else:
        # fall back to any Date column present
        preds["Timestamp"] = pd.to_datetime(preds.get("Date", pd.NaT), errors="coerce").dt.tz_localize(None)

    preds = preds.dropna(subset=["Timestamp"]).copy()
    preds["Date"] = preds["Timestamp"].dt.normalize()

    # If there are multiple rows per Date, keep the last one (often the most recent append)
    preds = preds.sort_values(["Date", "Timestamp"]).drop_duplicates(subset=["Date"], keep="last")

    # Avoid collisions later
    preds = preds.loc[:, ~preds.columns.duplicated(keep="first")]
    return preds


def _load_best_thresholds(
    csv_path="logs/threshold_search.csv",
    json_path="configs/best_thresholds.json",
    min_trades=10,
    objective_col="score",
):
    # Prefer JSON if present
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                cfg = json.load(f)
            return cfg.get("confidence_thresh"), cfg.get("crash_thresh"), cfg.get("spike_thresh")
        except Exception as e:
            print(f"⚠️ Could not read {json_path}: {e}")

    # Fallback to CSV leaderboard
    try:
        df = pd.read_csv(csv_path)
        if "trades" in df.columns:
            df = df[df["trades"] >= min_trades]
        if df.empty:
            return None, None, None
        row = df.sort_values(objective_col, ascending=False).iloc[0]
        return row.get("confidence_thresh"), row.get("crash_thresh"), row.get("spike_thresh")
    except FileNotFoundError:
        print(f"⚠️ {csv_path} not found — run a threshold sweep first.")
    except Exception as e:
        print(f"⚠️ Could not read {csv_path}: {e}")

    return None, None, None



#sets a reference portfolio size to simulate dollar returns from % returns.





# ─── Trade rules ────────────────────────────────────────────────────────────────
ENTRY_SHIFT   = 1    # enter at next bar’s open
#EXIT_SHIFT    = 1+3    # exit at day+3 close  <-- match label window
POSITION_SIZE = 1.0  # 1x notional per trade
CAPITAL_BASE  = 100_000  #initial capital base for dollar-return tracking

def run_backtest(window_days: int | None = None,
                 crash_thresh: float | None = None,
                 spike_thresh: float | None = None,
                 confidence_thresh: float | None = None,
                 simulate_mode: bool = False,
                 lookahead: int = 3,
                 tp_atr: float = 0.5,
                 sl_atr: float = 0.5,
                 allow_overlap: bool = True,
                 ambig_policy: str = "sl_first",   # 'sl_first' | 'tp_first' | 'skip' | 'close_dir' | 'random'
                 rng_seed: int = 7,
                 fee_bps: float = 0.5,   # 0.005% per fill
                 slip_bps: float = 1.0,  # 0.01% per fill
                 atr_len: int = 14,         
                 trend_len: int = 50  

                 ):
    """
    End-to-end backtest:
      - loads predictions (logs/daily_predictions.csv)
      - loads SPY history and normalizes duplicate/multiindex columns
      - optional pre-filter by confidence
      - optional explicit class thresholds (else use model-provided labels)
      - joins by Date and simulates ENTRY_SHIFT/EXIT_SHIFT trade rules
      - returns (trades_df, metrics_dict, simulate_mode)
    """
    # ── 1) Load predictions ─────────────────────────────────────────────────────
    preds = _load_predictions(prefer_full=True)


    # ── 2) Load & normalize SPY ─────────────────────────────────────────────────
    spy_df = load_spy_daily_data()

    # 2a) Flatten MultiIndex like ('Open','SPY') → 'Open'
    if isinstance(spy_df.columns, pd.MultiIndex):
        spy_df.columns = [c[0] if isinstance(c, tuple) else c for c in spy_df.columns]

    # 2b) Rename stringified tuple columns → canonical names (handles .1/.2 too)
    rename_map = {
        "('Open', 'SPY')": "Open",
        "('High', 'SPY')": "High",
        "('Low', 'SPY')": "Low",
        "('Close', 'SPY')": "Close",
        "('Adj Close', 'SPY')": "Adj Close",
        "('Volume', 'SPY')": "Volume",
    }
    new_cols = []
    for col in map(str, spy_df.columns):
        base = col.replace(".1", "").replace(".2", "").replace(".3", "")
        new_cols.append(rename_map.get(base, col))
    spy_df.columns = new_cols

    # 2c) Coalesce duplicates per name by picking the series with FEWEST NaNs
    def _pick_best(series_list):
        nan_counts = [s.isna().sum() for s in series_list]
        return series_list[int(np.argmin(nan_counts))]

    canonical = {}
    for name in set(spy_df.columns):
        idxs = [i for i, c in enumerate(spy_df.columns) if c == name]
        if len(idxs) == 1:
            canonical[name] = spy_df.iloc[:, idxs[0]]
        else:
            series_list = [spy_df.iloc[:, i] for i in idxs]
            canonical[name] = _pick_best(series_list)

    spy_df = pd.DataFrame(canonical, index=spy_df.index)

    # 2d) Keep only OHLCV we need and enforce daily DatetimeIndex
    needed = [c for c in ["Open", "Close", "High", "Low", "Volume"] if c in spy_df.columns]
    spy_df = spy_df[needed].copy()
    if not isinstance(spy_df.index, pd.DatetimeIndex):
        spy_df.index = pd.to_datetime(spy_df.index, errors="coerce")
    spy_df.index = spy_df.index.floor("D")

  

    

    # ── 3) Align predictions to available SPY dates + optional window ──────────
    preds = preds.dropna(subset=["Date"])
    preds = preds[preds["Date"].isin(spy_df.index)].copy()

    if window_days:
        cutoff = preds["Date"].max() - pd.Timedelta(days=window_days)
        preds = preds[preds["Date"] >= cutoff]
        spy_df = spy_df[spy_df.index >= cutoff]

    # ── 4) Remove any stray price columns from preds to avoid join collisions ──
    preds = preds.loc[:, ~preds.columns.duplicated(keep="first")]
    price_cols = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
    drop_cols = [c for c in preds.columns if c in price_cols]
    if drop_cols:
        print(f"ℹ️ Dropping price cols from preds to avoid join collision: {drop_cols}")
    preds = preds.drop(columns=drop_cols, errors="ignore")




    # ── 5) Optional pre-filter by probability confidence ───────────────────────
    # if confidence_thresh is not None:
    #     preds = preds[
    #         (preds["Crash_Conf"] >= confidence_thresh) |
    #         (preds["Spike_Conf"] >= confidence_thresh)
    #     ]

    # ── 6) Optional explicit thresholds (else use model labels already in file)
    # if (crash_thresh is not None) or (spike_thresh is not None):
    #     print(f"📊 Applying explicit thresholds — "
    #           f"Crash ≥ {crash_thresh if crash_thresh is not None else '—'}, "
    #           f"Spike ≥ {spike_thresh if spike_thresh is not None else '—'}")
    #     preds["Prediction"] = 0
    #     if crash_thresh is not None:
    #         preds.loc[preds["Crash_Conf"] >= crash_thresh, "Prediction"] = 1
    #     if spike_thresh is not None:
    #         preds.loc[preds["Spike_Conf"] >= spike_thresh, "Prediction"] = 2
    # else:
    #     print("ℹ️ Using model-provided class labels (no explicit crash/spike thresholds).")


    # ── 7) (Optional) Simulate mode: inject some spikes for plumbing tests ─────
    if simulate_mode:
        print("🧪 Simulate mode ON — injecting fake spike predictions.")
        valid_idx = preds.index  # already aligned to SPY dates
        if len(valid_idx) >= 10:
            inject_points = np.linspace(0, len(valid_idx) - 3, 10, dtype=int)
            for i in inject_points:
                preds.loc[valid_idx[i], "Prediction"] = 2

    # ── 8) Join on Date and drop rows with missing prices ──────────────────────
    df = (
        preds
        .set_index("Date")
        .join(spy_df[["Open", "High", "Low", "Close"]], how="inner")  #08/21: addition of High/Low
        .sort_index()
        .reset_index()
    )


    # Ensure numeric prices and drop any row that can’t be used
    df["Open"]  = pd.to_numeric(df["Open"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    bad = df[["Open", "Close"]].isna().any(axis=1)
    if bad.any():
        print(f"⚠️ Dropping {int(bad.sum())} rows with NaN/invalid Open/Close after join.")
        df = df[~bad]
    # Also coerce the rest so comparisons don’t silently fail
    for c in ("High", "Low", "Crash_Conf", "Spike_Conf", "Confidence"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # after join + numeric cleanup
    # ---- Single canonical filter + explicit thresholds (AFTER JOIN) ----
    if {"Crash_Conf","Spike_Conf"}.issubset(df.columns):
        # Start with everything kept
        mask = pd.Series(True, index=df.index)

        # a) Pre-filter by overall confidence (too-many-signals guard)
        if confidence_thresh is not None:
            mask &= (df["Crash_Conf"] >= confidence_thresh) | (df["Spike_Conf"] >= confidence_thresh)

        # b) If explicit thresholds are provided, rewrite Prediction deterministically
        #    Otherwise, we use model-provided labels already present in df['Prediction'].
        if (crash_thresh is not None) or (spike_thresh is not None):
            print(
                f"📊 Applying explicit thresholds — "
                f"Crash ≥ {crash_thresh if crash_thresh is not None else '—'}, "
                f"Spike ≥ {spike_thresh if spike_thresh is not None else '—'}"
            )

            # Start neutral
            df["Prediction"] = 0

            # Convenience handles
            c = df["Crash_Conf"] if "Crash_Conf" in df.columns else pd.Series(0.0, index=df.index)
            s = df["Spike_Conf"] if "Spike_Conf" in df.columns else pd.Series(0.0, index=df.index)

            # Apply one-sided thresholds
            if crash_thresh is not None:
                df.loc[c >= crash_thresh, "Prediction"] = 1
            if spike_thresh is not None:
                df.loc[s >= spike_thresh, "Prediction"] = 2

            # Tie: both exceed their respective thresholds → pick higher confidence
            # (If one threshold is None, we use 1.1 so that condition is False for that side.)
            ct = crash_thresh if crash_thresh is not None else 1.1
            st = spike_thresh if spike_thresh is not None else 1.1
            both = (c >= ct) & (s >= st)
            df.loc[both, "Prediction"] = np.where(s[both] >= c[both], 2, 1)
        else:
            print("ℹ️ Using model-provided class labels (no explicit crash/spike thresholds).")


        # c) If you want to *also* enforce per-class minimums on the resulting labels:

        if crash_thresh is not None:
            mask &= (df["Prediction"] != 1) | (df["Crash_Conf"] >= crash_thresh)
        if spike_thresh is not None:
            mask &= (df["Prediction"] != 2) | (df["Spike_Conf"] >= spike_thresh)

        before = len(df)
        df = df[mask].copy()
        after_mask = len(df)

        # Per-class minimum confidence for the final label
        gate_crash = (df["Prediction"] != 1) | (df["Crash_Conf"] >= (crash_thresh if crash_thresh is not None else 0.6))
        gate_spike = (df["Prediction"] != 2) | (df["Spike_Conf"] >= (spike_thresh if spike_thresh is not None else 0.9))
        df = df[gate_crash & gate_spike].copy()
        after_gate = len(df)

        # Regime filter: longs only in up-trend, shorts only in down-trend
        trend_ma = spy_df["Close"].rolling(trend_len).mean()
        df["Trend_MA50"] = trend_ma.reindex(df["Date"]).to_numpy()  

        df = df[df["Trend_MA50"].notna()]  # ignore early bars with no MA

        df = df[
            ((df["Prediction"] == 2) & (df["Close"] >= df["Trend_MA50"])) |  # long in up-trend
            ((df["Prediction"] == 1) & (df["Close"] <  df["Trend_MA50"])) |  # short in down-trend
            (df["Prediction"] == 0)                                          # keep neutrals for alignment
        ].sort_values("Date").reset_index(drop=True)

        print(
            f"🧹 Removed {before - after_mask} via mask, "
            f"{after_mask - after_gate} via class gate, "
            f"{after_gate - len(df)} via regime."
        )
    # ---- End single canonical filter ----
    n_trades = int((df["Prediction"].isin([1,2])).sum())
    print(f"✅ After filters: {len(df)} rows kept, {n_trades} trade signals in window.")








    # ── 9) Diagnostics ─────────────────────────────────────────────────────────
    print("\n🔎 Confidence Range Stats:")
    if not preds.empty:
        print("📈 Raw Spike confidence range:", preds["Spike_Conf"].min(), "→", preds["Spike_Conf"].max())
        print("📉 Raw Crash confidence range:", preds["Crash_Conf"].min(), "→", preds["Crash_Conf"].max())
    else:
        print("ℹ️ No prediction rows after filtering.")

    print("\n📊 Joined Prediction Candidates (1 or 2):")
    print(df[df["Prediction"].isin([1, 2])].head())

    print("\n🔍 Prediction Counts:")
    print(df["Prediction"].value_counts())

    print("\n🗓️  Date range in predictions:", preds["Date"].min() if not preds.empty else None,
          "→", preds["Date"].max() if not preds.empty else None)
    print("📅 Date range in SPY data:    ", spy_df.index.min(), "→", spy_df.index.max())

    if not df.empty and all(df["Prediction"] == 0):
        print("⚠️  All predictions are '0' — consider lowering thresholds.")

    # ── 10) Build trades (label-aligned exits, conservative TP/SL) ────────────────

    # --- exit config derived from function args ---
    LOOKAHEAD = max(1, int(lookahead))
    TP_ATR    = float(tp_atr)
    SL_ATR    = float(sl_atr)

    # Wilder-style ATR in DOLLARS with configurable length
    hl = (spy_df["High"] - spy_df["Low"]).abs()
    hc = (spy_df["High"] - spy_df["Close"].shift(1)).abs()
    lc = (spy_df["Low"]  - spy_df["Close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/atr_len, adjust=False).mean()

    # align to joined df dates
    df["ATR_dol"] = atr.reindex(df["Date"]).to_numpy()
    df["ATR_dol"] = pd.to_numeric(df["ATR_dol"], errors="coerce").replace([np.inf,-np.inf], np.nan)
    # optional: trim ultra-low ATR tails to avoid microscopic bands
    df["ATR_dol"] = df["ATR_dol"].fillna(np.nanmedian(df["ATR_dol"]))
    df["ATR_dol"] = df["ATR_dol"].clip(lower=np.nanpercentile(df["ATR_dol"], 5))




    trades_list = []
    ambig_bars = 0
    #rng = np.random.default_rng(rng_seed)

    i = 0
    N = len(df)
    rng = np.random.default_rng(rng_seed)
    while i < N:
        row = df.iloc[i]
        raw_sig = row.get("Prediction", 0)
        sig = int(raw_sig) if pd.notna(raw_sig) else 0

        if sig not in (1, 2):
            i += 1
            continue

        entry_idx = i + ENTRY_SHIFT
        if entry_idx >= N:
            break

        entry = df.iloc[entry_idx]
        look_end = min(entry_idx + LOOKAHEAD, N)
        if look_end <= entry_idx:
            i += 1
            continue
        look = df.iloc[entry_idx:look_end]

        entry_px = float(entry["Open"])
        atr_dol  = float(entry.get("ATR_dol", 0.0)) if pd.notna(entry.get("ATR_dol")) else 0.0

        if sig == 2:   # long
            tp_px = entry_px + TP_ATR * atr_dol
            sl_px = entry_px - SL_ATR * atr_dol
        else:          # short
            tp_px = entry_px - TP_ATR * atr_dol
            sl_px = entry_px + SL_ATR * atr_dol

        exit_px, exit_ts = None, None


        for _, bar in look.iterrows():
            if sig == 2:
                tag, px, ambig = _resolve_bar_exit_long(bar, tp_px, sl_px)
            else:
                tag, px, ambig = _resolve_bar_exit_short(bar, tp_px, sl_px)

            if ambig:
                ambig_bars += 1
                if ambig_policy == "sl_first":
                    tag, px = "SL", sl_px
                elif ambig_policy == "tp_first":
                    tag, px = "TP", tp_px
                elif ambig_policy == "close_dir":
                    # use bar direction as a cheap proxy for path
                    op = bar.get("Open", np.nan); cl = bar.get("Close", np.nan)
                    if pd.notna(op) and pd.notna(cl) and cl >= op:
                        tag, px = ("TP", tp_px) if sig == 2 else ("SL", sl_px)  # long up bar favors TP; short up bar favors SL
                    else:
                        tag, px = ("SL", sl_px) if sig == 2 else ("TP", tp_px)
                elif ambig_policy == "random":
                    tag, px = ("TP", tp_px) if rng.random() < 0.5 else ("SL", sl_px)
                elif ambig_policy == "skip":
                    # ignore this bar; keep scanning later bars in look window
                    continue

            if tag in ("TP", "SL") and pd.notna(px):
                exit_px, exit_ts = float(px), bar.get("Timestamp", pd.NaT)
                break

            if ambig_bars:
                print(f"ℹ️ Ambiguous TP/SL bars encountered: {ambig_bars} (policy={ambig_policy})")


        if exit_px is None:
            last = look.iloc[-1]
            exit_px = float(last["Close"])
            exit_ts = last.get("Timestamp", pd.NaT)

        # round-trip cost multiplier (applies to both entry & exit prices)
        c = (fee_bps + slip_bps) / 1e4
        cost_mult = (1 - c) / (1 + c)

        if sig == 2:  # long
            # gross = (exit_px / entry_px) - 1.0   # keep if you want diagnostics
            ret = (exit_px / entry_px) * cost_mult - 1.0
        else:         # short
            # gross = (entry_px / exit_px) - 1.0
            ret = (entry_px / exit_px) * cost_mult - 1.0


        trades_list.append({
            "signal_time": row.get("Timestamp", pd.NaT),
            "sig":         sig,
            "entry_time":  entry.get("Timestamp", pd.NaT),
            "exit_time":   exit_ts,
            "entry_price": entry_px,
            "exit_price":  exit_px,
            "return_pct":  ret * POSITION_SIZE
        })

        # jump past the window if overlapping trades are disabled
        i = (look_end if not allow_overlap else i + 1)

    if ambig_bars:
        print(f"ℹ️ Ambiguous TP/SL bars resolved conservatively: {ambig_bars}")

    if not trades_list:
        print("⚠️  No trades generated in this backtest.")
        zero = {
            "trades": 0, "total_return": 0.0, "annualized_return": 0.0, "sharpe": 0.0,
            "avg_return": 0.0, "median_return": 0.0, "win_rate": 0.0,
            "avg_long": 0.0, "avg_short": 0.0, "max_drawdown": 0.0, "profit_factor": 0.0
        }
        return pd.DataFrame(), zero, simulate_mode




    # ── 11) Compute metrics ────────────────────────────────────────────────────
    trades = pd.DataFrame(trades_list).set_index("signal_time")

    # equity assumes full notional each trade (compounded)
    trades["equity_curve"] = (1.0 + trades["return_pct"]).cumprod()
    trades["cash_curve"]   = CAPITAL_BASE * trades["equity_curve"]
    trades["dollar_return"] = trades["return_pct"] * CAPITAL_BASE

    total_return = trades["equity_curve"].iloc[-1] - 1.0

    # annualize by elapsed trading days between first signal and last exit
    if len(trades) > 1:
        start = pd.to_datetime(trades.index.min(), errors="coerce")
        end   = pd.to_datetime(trades["exit_time"].max(), errors="coerce")
        if pd.notna(start) and pd.notna(end) and end > start:
            elapsed_days = max(1, (end - start).days)
            annualized_return = (1.0 + total_return) ** (252.0 / elapsed_days) - 1.0
        else:
            annualized_return = np.nan
    else:
        annualized_return = np.nan

    # Calculate Sharpe ratio using average holding period
    returns = trades["return_pct"]
    hold_days = (
        pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"])
    ).dt.days.clip(lower=1)
    avg_hold = hold_days.mean() if not hold_days.empty else 1.0
    print(f"🕒 Avg hold (days): {avg_hold:.2f}")


    sigma = returns.std(ddof=0)
    if sigma == 0 or np.isnan(sigma):
        sharpe = np.nan
    else:
        sharpe = (returns.mean() / sigma) * np.sqrt(252.0 / avg_hold)
    
    # Warn if too few trades
    if len(trades) < 5:
        print("⚠️  Very few trades — annualized return and Sharpe ratio may be misleading.")

    avg_return    = returns.mean()
    median_return = returns.median()
    win_rate      = (returns > 0).mean()

    avg_long  = trades.loc[trades["sig"] == 2, "return_pct"].mean() if (trades["sig"] == 2).any() else 0.0
    avg_short = trades.loc[trades["sig"] == 1, "return_pct"].mean() if (trades["sig"] == 1).any() else 0.0

    trades["peak"] = trades["equity_curve"].cummax()
    trades["drawdown"] = trades["equity_curve"] / trades["peak"] - 1.0
    max_drawdown = trades["drawdown"].min()


    # Save log
    cols = ["sig","entry_time","exit_time","entry_price","exit_price",
            "return_pct","dollar_return","equity_curve","peak","drawdown","cash_curve"]
    trades[cols].to_csv("logs/trade_log.csv", float_format="%.5f")

    gross_win  = trades.loc[trades["dollar_return"] > 0, "dollar_return"].sum()
    gross_loss = -trades.loc[trades["dollar_return"] < 0, "dollar_return"].sum()
    profit_factor = gross_win / gross_loss if gross_loss != 0 else np.inf

    metrics = {
        "trades": len(trades), "total_return": total_return,
        "annualized_return": annualized_return, "sharpe": sharpe,
        "avg_return": avg_return, "median_return": median_return, "win_rate": win_rate,
        "avg_long": avg_long, "avg_short": avg_short,
        "max_drawdown": max_drawdown, "profit_factor": profit_factor
    }


    return trades, metrics, simulate_mode



# ─── Threshold optimizer ───────────────────────────────────────────────────────

import itertools
import math

def optimize_thresholds(
    window_days: int | None = None,
    grid: dict | None = None,
    min_trades: int = 20,
    objective: str = "avg_dollar_return",
) -> tuple[float | None, float | None, float | None, dict]:
    """
    Grid-search thresholds to maximize chosen objective.
    Returns: (confidence_thresh, crash_thresh, spike_thresh, best_metrics)
    """
    # Default search space (tighten/loosen as you like)
    if grid is None:
        grid = {
            "confidence_thresh": [None, 0.60, 0.70, 0.80, 0.85, 0.90],
            "crash_thresh":      [None, 0.60, 0.70, 0.80, 0.85, 0.90],
            "spike_thresh":      [None, 0.60, 0.70, 0.80, 0.85, 0.90],
        }

    keys = ["confidence_thresh", "crash_thresh", "spike_thresh"]
    combos = list(itertools.product(*(grid[k] for k in keys)))

    rows = []
    best_val = -math.inf
    best_combo = (None, None, None)
    best_metrics = {}

    for conf, crash, spike in combos:
        trades, metrics, _ = run_backtest(
            window_days=window_days,
            crash_thresh=crash,
            spike_thresh=spike,
            confidence_thresh=conf,
            simulate_mode=False,
        )

        n = metrics.get("trades", 0)
        if n < min_trades:
            val = -math.inf  # avoid overfitting to tiny samples
        else:
            if objective == "avg_dollar_return":
                val = trades["dollar_return"].mean() if not trades.empty else -math.inf
            elif objective == "total_profit":
                val = trades["dollar_return"].sum() if not trades.empty else -math.inf
            elif objective == "win_rate":
                val = (trades["return_pct"] > 0).mean() if not trades.empty else -math.inf
            else:
                # fallback: profit factor
                val = metrics.get("profit_factor", 0.0)

        rows.append({
            "confidence_thresh": conf,
            "crash_thresh": crash,
            "spike_thresh": spike,
            "trades": n,
            "objective": objective,
            "score": val,
            **metrics
        })

        if val > best_val:
            best_val = val
            best_combo = (conf, crash, spike)
            best_metrics = metrics

    # Save the sweep for review
    pd.DataFrame(rows).sort_values("score", ascending=False).to_csv(
        "logs/threshold_search.csv", index=False
    )
    print(f"🔎 Threshold search complete. Best score={best_val:.6f} "
          f"@ conf={best_combo[0]} crash={best_combo[1]} spike={best_combo[2]}")
    print("📄 Wrote: logs/threshold_search.csv")

    return (*best_combo, best_metrics)

def sweep_params_big():
    from itertools import product
    import pandas as pd
    grids = {
        "confidence_thresh": [0.80, 0.85],
        "crash_thresh":      [0.60, 0.70, 0.75],
        "spike_thresh":      [0.90, 0.95, 0.975],
        "lookahead":         [3, 5],
        "tp_atr":            [1.25, 1.5],
        "sl_atr":            [0.75, 1.0],
        "allow_overlap":     [False],
        "ambig_policy":      ["close_dir"],
    }
    keys = list(grids.keys())
    rows = []
    for vals in product(*[grids[k] for k in keys]):
        kw = dict(zip(keys, vals))
        _, m, _ = run_backtest(window_days=365*12, fee_bps=2.0, slip_bps=3.0, **kw)
        rows.append({**kw, **m})
    df = pd.DataFrame(rows)
    out = "logs/param_sweep.csv"
    df.sort_values(["sharpe","max_drawdown"], ascending=[False,True]).to_csv(out, index=False)
    print("Wrote", out)











# ─── Main entrypoint ───────────────────────────────────────────────────────────


if __name__ == "__main__":
    trades, m, simulate_mode = run_backtest(
        lookahead=5,
        tp_atr=1.25,      # or 1.5 for lower DD
        sl_atr=0.75,      # or 1.0 if TP=1.5
        allow_overlap=False,
        ambig_policy="close_dir",
        confidence_thresh=0.80,
        crash_thresh=0.70,
        spike_thresh=0.95,
        fee_bps=2.0,      # round-trip fees (bps)
        slip_bps=3.0      # round-trip slippage (bps)
    )

    print("\n📈 Backtest Report")
    print(f"  Trades taken:       {m['trades']}")
    print(f"  Total return:       {m['total_return']:.2%}")
    print(f"  Annualized return:  {m['annualized_return']:.2%}")
    print(f"  Sharpe ratio (252d):{m['sharpe']:.2f}\n")
    print(f"  Max drawdown:        {m['max_drawdown']:.2%}")

    print(f"  Avg long:           {m['avg_long']:.5f}")
    print(f"  Avg short:          {m['avg_short']:.5f}")



    if not trades.empty:
        # print final balance + profit
        final_balance = trades["cash_curve"].iloc[-1]
        print(f"  Final capital:      ${final_balance:,.2f}")
        print(f"  Net profit:         ${final_balance - CAPITAL_BASE:,.2f}")


    print("\nSample trades:")
    if trades.empty:
        print("  (no trades to show)")
    else:
        print(trades.head())

        # Rename equity column for consistency
        trades = trades.rename(columns={"equity_curve": "Equity"})
        trades["Drawdown %"] = trades["drawdown"] * 100  # in percent terms

        # Ensure expected plot columns are present
        if "equity_curve" in trades.columns:
            trades["Equity"] = trades["equity_curve"]  # manually copy
        if "drawdown" in trades.columns:
            trades["Drawdown %"] = trades["drawdown"] * 100  # convert to percent

        # Combine both plots in one window using subplots
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

        # Equity Curve
        trades["Equity"].plot(ax=ax1, title="Equity Curve")
        ax1.set_ylabel("Cumulative Return")
        ax1.grid(True)

        # Drawdown Plot
        trades["Drawdown %"].plot(ax=ax2, title="Drawdown (%)", color='red', linestyle='--')
        ax2.set_ylabel("Drawdown")
        ax2.axhline(0, color="black", linewidth=0.5)
        ax2.grid(True)

        plt.xlabel("Signal Time")
        plt.tight_layout()
        plt.show()

        # Save the equity + drawdown plot to file
        fig.savefig("logs/equity_drawdown_plot.png", dpi=300)
        print("📸 Saved equity and drawdown chart to logs/equity_drawdown_plot.png")



    if simulate_mode:  #callout
        print("\n⚠️ NOTE: This was a simulated run with injected predictions.")
