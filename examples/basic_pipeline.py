# -----------------------------------------------------------------------------
# examples/basic_pipeline.py
#
# Demonstrates the core @watch decorator on a realistic multi-step pipeline.
#
# Run:
#   python examples/basic_pipeline.py
# -----------------------------------------------------------------------------

import numpy as np
import pandas as pd

from watcher import watch, session

# -----------------------------------------------------------------------------
# Seed for reproducibility
# -----------------------------------------------------------------------------
rng = np.random.default_rng(42)

# -----------------------------------------------------------------------------
# Synthetic dataset — 1 000 000 rows, realistic e-commerce shape
# -----------------------------------------------------------------------------
N = 1_000_000

raw = pd.DataFrame(
    {
        "order_id":    np.arange(N),
        "customer_id": rng.integers(1, 50_000, size=N),
        "product_id":  rng.integers(1, 5_000,  size=N),
        "status":      rng.choice(["active", "cancelled", "pending", None], size=N, p=[0.6, 0.2, 0.15, 0.05]),
        "revenue":     rng.uniform(5.0, 500.0, size=N).round(2),
        "created_at":  pd.date_range("2023-01-01", periods=N, freq="1min"),
    }
)

# Sprinkle some nulls into revenue to make the null-delta report interesting
null_mask = rng.random(N) < 0.03
raw.loc[null_mask, "revenue"] = np.nan

print(f"Input: {len(raw):,} rows\n")

# -----------------------------------------------------------------------------
# Pipeline steps — each decorated with @watch
# -----------------------------------------------------------------------------


@watch(track_memory="off")
def drop_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows where any core field is null."""
    return df.dropna(subset=["status", "revenue"])


@watch(track_memory="off")
def filter_active(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only active orders."""
    return df[df["status"] == "active"].copy()


@watch(track_memory="off")
def add_revenue_band(df: pd.DataFrame) -> pd.DataFrame:
    """Bin revenue into Low / Mid / High — adds a column."""
    df["revenue_band"] = pd.cut(
        df["revenue"],
        bins=[0, 100, 300, 500],
        labels=["Low", "Mid", "High"],
    )
    return df


@watch(track_memory="off")
def drop_temp_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Remove columns not needed downstream — removes a column."""
    return df.drop(columns=["created_at"])


# -----------------------------------------------------------------------------
# Run inside a session so we get a full summary table at the end
# -----------------------------------------------------------------------------

with session("basic e-commerce pipeline") as s:
    df = drop_nulls(raw)
    df = filter_active(df)
    df = add_revenue_band(df)
    df = drop_temp_columns(df)

print(f"\nOutput: {len(df):,} rows")
print(f"Columns: {list(df.columns)}")
