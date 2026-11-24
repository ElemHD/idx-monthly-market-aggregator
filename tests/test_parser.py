"""Unit tests for the daily-to-monthly market data aggregation pipeline."""
import io
import numpy as np
import pandas as pd
from pathlib import Path

from market_data_parser import (
    OUTPUT_COLUMNS,
    detect_delimiter,
    find_column,
    process_market_data,
)


def test_detect_delimiter(tmp_path):
    comma_csv = tmp_path / "comma.csv"
    comma_csv.write_text("ticker,date,close_price,volume\nTLKM,2021-01-04,3500,1000\n", encoding="utf-8")
    assert detect_delimiter(comma_csv) == ","

    semi_csv = tmp_path / "semi.csv"
    semi_csv.write_text("ticker;date;close_price;volume\nTLKM;2021-01-04;3500;1000\n", encoding="utf-8")
    assert detect_delimiter(semi_csv) == ";"


def test_find_column():
    cols = ["Kode_Emiten", "Tgl_Transaksi", "Adj_Close", "Vol_Saham"]
    assert find_column(cols, ["ticker", "code", "kode_emiten"]) == "Kode_Emiten"
    assert find_column(cols, ["close", "adj_close"]) == "Adj_Close"
    assert find_column(cols, ["missing_col"]) is None


def test_pipeline_logic(tmp_path):
    # Synthetic fixture covering suspension gaps (Ticker A in 2021-03), single-day trading (Ticker B),
    # .JK suffix stripping, zero-volume exclusion, and chronological deduplication.
    data = """ticker,date,close_price,volume
A,2021-01-04,100,1000000
A,2021-01-05,105,2000000
A,2021-01-05,110,2000000
A,2021-01-06,100,0
A,2021-02-01,120,1000000
A,2021-04-01,150,1000000
B,2021-01-10,500,100000
 tlkm.jk ,2021-01-04,3000,500000
"""
    raw_csv = tmp_path / "raw.csv"
    raw_csv.write_text(data, encoding="utf-8")

    out_dir = tmp_path / "output"
    obs_df, full_df, summary_df = process_market_data(
        input_file=raw_csv,
        out_dir=out_dir,
        secondary_out_dir=None,
        skip_excel=True,
        obs_start="2021-01",
        obs_end="2021-04"
    )

    assert "TLKM" in obs_df['ticker'].values
    assert " tlkm.jk " not in obs_df['ticker'].values
    assert list(obs_df.columns) == OUTPUT_COLUMNS
    assert not obs_df.duplicated(subset=['ticker', 'yearmonth']).any()

    # Sample standard deviation requires at least 2 observations; a 1-day month must yield NaN.
    b_jan = obs_df[(obs_df['ticker'] == 'B') & (obs_df['yearmonth'] == '2021-01')].iloc[0]
    assert b_jan['trading_days'] == 1
    assert np.isnan(b_jan['vol_t'])

    # In 2021-01, duplicate date keeps last (110) and zero-volume row is dropped.
    a_jan = obs_df[(obs_df['ticker'] == 'A') & (obs_df['yearmonth'] == '2021-01')].iloc[0]
    assert a_jan['close_end'] == 110.0
    assert a_jan['trading_days'] == 2

    a_feb = obs_df[(obs_df['ticker'] == 'A') & (obs_df['yearmonth'] == '2021-02')].iloc[0]
    assert np.isclose(a_feb['ret_t'], (120.0 / 110.0) - 1.0)
    assert np.isclose(a_feb['rev_t'], a_jan['ret_t'] if not np.isnan(a_jan['ret_t']) else 0) or np.isnan(a_feb['rev_t'])

    # Suspended months (zero active trades) are excluded from the final panel.
    assert len(obs_df[(obs_df['ticker'] == 'A') & (obs_df['yearmonth'] == '2021-03')]) == 0

    # Because 2021-03 was suspended, 2021-04 ret_t and rev_t must be NaN rather than bridging across 2021-02.
    a_apr = obs_df[(obs_df['ticker'] == 'A') & (obs_df['yearmonth'] == '2021-04')].iloc[0]
    assert np.isnan(a_apr['ret_t']), f"Expected NaN for ret_t after suspension, got {a_apr['ret_t']}"
    assert np.isnan(a_apr['rev_t']), f"Expected NaN for rev_t after suspension, got {a_apr['rev_t']}"

    assert np.isclose(a_jan['ret_t_plus_1'], a_feb['ret_t'])
    assert np.isnan(a_feb['ret_t_plus_1'])


def test_no_inf_in_output(tmp_path):
    data = """ticker,date,close_price,volume
INF,2021-01-04,10,100
INF,2021-01-05,20,100
"""
    raw_csv = tmp_path / "raw.csv"
    raw_csv.write_text(data, encoding="utf-8")

    out_dir = tmp_path / "output"
    obs_df, _, _ = process_market_data(
        input_file=raw_csv,
        out_dir=out_dir,
        skip_excel=True,
    )
    for col in OUTPUT_COLUMNS:
        if col not in ['ticker', 'yearmonth']:
            assert not np.isinf(obs_df[col]).any()
