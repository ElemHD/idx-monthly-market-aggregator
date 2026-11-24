"""
Daily-to-monthly market data parser and panel aggregator for Indonesia Stock Exchange (IDX) equities.
Produces Stata-ready CSV and multi-sheet Excel outputs with balanced calendar grid lag and lead shifts.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = [
    'ticker',
    'yearmonth',
    'close_end',
    'close_mean',
    'volume_mean',
    'volume_sum',
    'trading_days',
    'ret_t',
    'vol_t',
    'illiq_t',
    'maxret_t',
    'rev_t',
    'mom_t',
    'ln_price_t',
    'ret_t_plus_1',
    'illiq_t_plus_1',
    'vol_t_plus_1',
]


def detect_delimiter(file_path: Path) -> str:
    """Detect whether the CSV header uses a comma or semicolon delimiter."""
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        first_line = f.readline()
    if ';' in first_line and ',' not in first_line:
        return ';'
    semicolons = first_line.count(';')
    commas = first_line.count(',')
    if semicolons > commas:
        return ';'
    return ','


def find_column(df_columns: List[str], candidates: List[str]) -> Optional[str]:
    """Return the first DataFrame column matching any candidate name (case-insensitive)."""
    cols_lower = {c.strip().lower(): c for c in df_columns}
    for cand in candidates:
        cand_lower = cand.strip().lower()
        if cand_lower in cols_lower:
            return cols_lower[cand_lower]
    return None


def resolve_input_file(input_arg: Optional[str] = None) -> Path:
    """Locate Raw_DailyPrices.csv from an explicit path, environment variable, or local working directories."""
    if input_arg and os.path.exists(input_arg):
        return Path(input_arg).resolve()

    env_input = os.environ.get("MARKET_DATA_INPUT")
    if env_input and os.path.exists(env_input):
        return Path(env_input).resolve()

    search_paths = [
        Path("Raw_DailyPrices.csv"),
        Path("data/Raw_DailyPrices.csv"),
        Path("../Dataset/Raw_DailyPrices.csv"),
        Path("../Raw_DailyPrices.csv"),
    ]
    for p in search_paths:
        if p.exists():
            return p.resolve()

    raise FileNotFoundError(
        "Could not find Raw_DailyPrices.csv in default relative locations. "
        "Please provide --input /path/to/Raw_DailyPrices.csv."
    )


def process_market_data(
    input_file: Path,
    out_dir: Path,
    secondary_out_dir: Optional[Path] = None,
    skip_excel: bool = False,
    obs_start: str = "2021-01",
    obs_end: str = "2024-12",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Aggregate daily equity trades into a monthly firm-period panel with calendar-aligned lags and leads."""
    t_start = time.time()
    print(f"[1/6] Reading raw input file: {input_file} ...")
    sep = detect_delimiter(input_file)
    print(f"      Detected delimiter: {repr(sep)}")

    df = pd.read_csv(input_file, sep=sep, low_memory=False)
    print(f"      Successfully read {len(df):,} rows and {len(df.columns)} columns in {time.time()-t_start:.2f}s.")

    ticker_col = find_column(df.columns, ['ticker', 'code', 'symbol', 'kode_emiten'])
    date_col = find_column(df.columns, ['date', 'datetime', 'timestamp', 'tgl_transaksi'])
    close_col = find_column(df.columns, ['close_price', 'close', 'adj_close', 'adj close'])
    vol_col = find_column(df.columns, ['volume', 'vol', 'vol_saham'])

    if not all([ticker_col, date_col, close_col, vol_col]):
        missing = []
        if not ticker_col: missing.append("ticker")
        if not date_col: missing.append("date")
        if not close_col: missing.append("close")
        if not vol_col: missing.append("volume")
        raise ValueError(f"Required columns not found in CSV: {missing}. Available: {list(df.columns)}")

    print(f"      Mapped columns: ticker='{ticker_col}', date='{date_col}', close='{close_col}', volume='{vol_col}'")

    print("[2/6] Standardizing and cleaning daily trades (FR-01) ...")
    # Strip Yahoo Finance .JK exchange suffix so tickers match standard IDX issuer codes.
    clean_df = pd.DataFrame({
        'ticker': df[ticker_col].astype(str).str.strip().str.upper().str.replace(r'\.JK$', '', regex=True),
        'date': pd.to_datetime(df[date_col], errors='coerce'),
        'close': pd.to_numeric(df[close_col], errors='coerce'),
        'volume': pd.to_numeric(df[vol_col], errors='coerce'),
    })

    # Exclude non-trading days and invalid prices to prevent artificial zero-return streaks in volatility and Amihud metrics.
    clean_df = clean_df.dropna(subset=['ticker', 'date', 'close', 'volume'])
    clean_df = clean_df[(clean_df['volume'] > 0) & (clean_df['close'] > 0) & (clean_df['ticker'] != '')]

    clean_df = clean_df.sort_values(['ticker', 'date']).drop_duplicates(subset=['ticker', 'date'], keep='last')
    print(f"      Active trading rows remaining: {len(clean_df):,} across {clean_df['ticker'].nunique()} tickers.")
    print(f"      Trading date range: {clean_df['date'].min().strftime('%Y-%m-%d')} to {clean_df['date'].max().strftime('%Y-%m-%d')}.")

    print("[3/6] Computing intra-month daily metrics (FR-02) ...")
    clean_df['daily_ret'] = clean_df.groupby('ticker')['close'].pct_change()

    # Scale daily trading value to IDR millions to keep Amihud illiquidity ratios within standard econometric bounds.
    clean_df['tv_mil'] = (clean_df['close'] * clean_df['volume']) / 1e6

    clean_df['amihud_daily'] = np.where(
        (clean_df['tv_mil'] > 0) & clean_df['daily_ret'].notna(),
        (clean_df['daily_ret'].abs() / clean_df['tv_mil']) * 1e6,
        np.nan
    )
    clean_df['yearmonth'] = clean_df['date'].dt.strftime('%Y-%m')

    print("[4/6] Collapsing daily trades to monthly emiten level (FR-03) ...")
    # Use sample standard deviation (ddof=1); months with a single active trading day yield NaN for vol_t.
    monthly = clean_df.groupby(['ticker', 'yearmonth']).agg(
        close_end=('close', 'last'),
        close_mean=('close', 'mean'),
        volume_mean=('volume', 'mean'),
        volume_sum=('volume', 'sum'),
        trading_days=('close', 'count'),
        vol_t=('daily_ret', lambda x: x.std(ddof=1)),
        maxret_t=('daily_ret', 'max'),
        illiq_t=('amihud_daily', 'mean')
    ).reset_index()
    print(f"      Collapsed into {len(monthly):,} monthly emiten-period observations.")

    print("[5/6] Building complete calendar grid for cross-month lags and leads (FR-04) ...")
    all_tickers = monthly['ticker'].unique()
    min_period = pd.Period(monthly['yearmonth'].min(), freq='M')
    max_period = pd.Period(monthly['yearmonth'].max(), freq='M')
    all_months = [str(p) for p in pd.period_range(min_period, max_period, freq='M')]

    # Reindex onto a full (ticker x calendar month) grid so shift(k) steps across true calendar months.
    # Otherwise, shifting across suspended trading months would link non-adjacent months and corrupt returns.
    grid_idx = pd.MultiIndex.from_product([all_tickers, all_months], names=['ticker', 'yearmonth'])
    grid = monthly.set_index(['ticker', 'yearmonth']).reindex(grid_idx)

    grp = grid.groupby(level='ticker')
    lag1_close = grp['close_end'].shift(1)
    lag12_close = grp['close_end'].shift(12)

    grid['ret_t'] = (grid['close_end'] / lag1_close) - 1
    grid['rev_t'] = grp['ret_t'].shift(1)

    # Standard 12-1 month momentum skips month t to separate medium-term continuation from short-term reversal.
    grid['mom_t'] = (lag1_close / lag12_close) - 1
    grid['ln_price_t'] = np.where(grid['close_end'] > 0, np.log(grid['close_end']), np.nan)

    grid['ret_t_plus_1'] = grp['ret_t'].shift(-1)
    grid['illiq_t_plus_1'] = grp['illiq_t'].shift(-1)
    grid['vol_t_plus_1'] = grp['vol_t'].shift(-1)

    full_df = grid.reset_index()
    for col in OUTPUT_COLUMNS:
        if col not in ['ticker', 'yearmonth']:
            full_df[col] = full_df[col].replace([np.inf, -np.inf], np.nan)

    # Drop synthetic grid rows from suspended months after computing calendar-accurate lags and leads.
    active_df = full_df[full_df['trading_days'].notna()].copy()
    active_df['trading_days'] = active_df['trading_days'].astype(int)

    full_df_ordered = active_df[OUTPUT_COLUMNS].copy()

    obs_df = full_df_ordered[
        (full_df_ordered['yearmonth'] >= obs_start) & (full_df_ordered['yearmonth'] <= obs_end)
    ].copy()

    summary_df = obs_df.groupby('yearmonth').agg(
        num_tickers=('ticker', 'nunique'),
        total_obs=('ticker', 'count'),
        avg_trading_days=('trading_days', 'mean'),
        min_trading_days=('trading_days', 'min'),
        max_trading_days=('trading_days', 'max'),
        mean_close_end=('close_end', 'mean'),
        mean_volume_sum=('volume_sum', 'mean'),
        mean_ret_t=('ret_t', 'mean'),
        mean_illiq_t=('illiq_t', 'mean'),
        mean_vol_t=('vol_t', 'mean')
    ).reset_index()

    for c in ['avg_trading_days', 'mean_close_end', 'mean_volume_sum', 'mean_ret_t', 'mean_illiq_t', 'mean_vol_t']:
        summary_df[c] = summary_df[c].round(4)

    print("[6/6] Exporting Stata-ready CSV and Excel outputs ...")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_filename = "parsed_monthly_market_variables_2021_2024.csv"
    excel_filename = "Market_Variables_Monthly_Stata.xlsx"

    # Empty strings ensure Stata's import delimited parses missing numeric cells as system missing (.).
    csv_path = out_dir / csv_filename
    obs_df.to_csv(csv_path, index=False, na_rep="")
    print(f"      [OK] CSV Master saved: {csv_path} ({len(obs_df):,} rows)")

    if secondary_out_dir:
        try:
            secondary_out_dir.mkdir(parents=True, exist_ok=True)
            sec_csv_path = secondary_out_dir / csv_filename
            obs_df.to_csv(sec_csv_path, index=False, na_rep="")
            print(f"      [OK] Secondary CSV saved: {sec_csv_path}")
        except Exception as e:
            print(f"      [WARN] Could not write secondary CSV: {e}")

    if not skip_excel:
        excel_path = out_dir / excel_filename
        print(f"      Writing multi-sheet Excel file to {excel_path} (this may take ~20-30s) ...")
        t_excel = time.time()
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            obs_df.to_excel(writer, sheet_name='OBS_2021_2024', index=False)
            full_df_ordered.to_excel(writer, sheet_name='FULL_2020_2025', index=False)
            summary_df.to_excel(writer, sheet_name='SUMMARY_STATS', index=False)
        print(f"      [OK] Multi-Sheet Excel saved in {time.time()-t_excel:.2f}s: {excel_path}")

        if secondary_out_dir:
            try:
                sec_excel_path = secondary_out_dir / excel_filename
                with pd.ExcelWriter(sec_excel_path, engine='openpyxl') as writer:
                    obs_df.to_excel(writer, sheet_name='OBS_2021_2024', index=False)
                    full_df_ordered.to_excel(writer, sheet_name='FULL_2020_2025', index=False)
                    summary_df.to_excel(writer, sheet_name='SUMMARY_STATS', index=False)
                print(f"      [OK] Secondary Excel saved: {sec_excel_path}")
            except Exception as e:
                print(f"      [WARN] Could not write secondary Excel: {e}")

    total_time = time.time() - t_start
    print(f"\n=======================================================")
    print(f" PARSER FINISHED SUCCESSFULLY IN {total_time:.2f} SECONDS")
    print(f" Observations in Primary Panel: {len(obs_df):,}")
    print(f" Unique Tickers: {obs_df['ticker'].nunique()}")
    print(f" Uniqueness check (ticker x yearmonth): {'PASS (100% Unique)' if not obs_df.duplicated(['ticker', 'yearmonth']).any() else 'FAIL'}")
    print(f"=======================================================\n")

    return obs_df, full_df_ordered, summary_df


def main():
    parser = argparse.ArgumentParser(
        description="Daily-to-Monthly Market Data Parser & Aggregator (Stata-Ready)"
    )
    parser.add_argument(
        "-i", "--input",
        type=str,
        default=None,
        help="Path to Raw_DailyPrices.csv. If omitted, searches default relative paths."
    )
    parser.add_argument(
        "-o", "--out-dir",
        type=str,
        default=".",
        help="Output directory for generated CSV and Excel files (default: current directory)."
    )
    parser.add_argument(
        "-s", "--secondary-dir",
        type=str,
        default=None,
        help="Optional secondary output directory to mirror generated files."
    )
    parser.add_argument(
        "--skip-excel",
        action="store_true",
        help="Skip Excel generation for fast CSV-only export."
    )
    parser.add_argument(
        "--obs-start",
        type=str,
        default="2021-01",
        help="Start yearmonth of primary observation window (default: 2021-01)."
    )
    parser.add_argument(
        "--obs-end",
        type=str,
        default="2024-12",
        help="End yearmonth of primary observation window (default: 2024-12)."
    )

    args = parser.parse_args()

    try:
        input_file = resolve_input_file(args.input)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir).resolve()
    sec_dir = Path(args.secondary_dir).resolve() if args.secondary_dir else None

    process_market_data(
        input_file=input_file,
        out_dir=out_dir,
        secondary_out_dir=sec_dir,
        skip_excel=args.skip_excel,
        obs_start=args.obs_start,
        obs_end=args.obs_end,
    )


if __name__ == "__main__":
    main()
