# -----------------------------------------------------------------------------
# examples/threshold_demo.py
#
# Demonstrates all four threshold arguments:
#   warn_on_loss, raise_on_loss, warn_on_gain, raise_on_gain
#
# Run:
#   python examples/threshold_demo.py
# -----------------------------------------------------------------------------

import warnings

import numpy as np
import pandas as pd

from watcher import watch
from watcher.exceptions import ThresholdExceeded, WatcherWarning

rng = np.random.default_rng(0)

# -----------------------------------------------------------------------------
# Base dataset
# -----------------------------------------------------------------------------
df_base = pd.DataFrame(
    {
        "order_id": np.arange(10_000),
        "customer_id": rng.integers(1, 2_000, size=10_000),
        "status": rng.choice(["active", "cancelled"], size=10_000, p=[0.7, 0.3]),
        "revenue": rng.uniform(10.0, 400.0, size=10_000).round(2),
    }
)

# Duplicate customer reference table — will cause join fan-out when merged
customer_ref = pd.DataFrame(
    {
        "customer_id": np.repeat(np.arange(1, 2_001), 2),  # every ID appears twice
        "tier": rng.choice(["gold", "silver", "bronze"], size=4_000),
    }
)

# -----------------------------------------------------------------------------
# 1. warn_on_loss — soft alert when > 5 % of rows are dropped
# -----------------------------------------------------------------------------
print("=" * 60)
print("Demo 1 — warn_on_loss=0.05")
print("=" * 60)


@watch(warn_on_loss=0.05, track_memory="off")
def filter_active_warn(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["status"] == "active"].copy()


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = filter_active_warn(df_base)

if caught:
    print(f"⚠  WatcherWarning fired — {len(caught)} warning(s) caught\n")
else:
    print("No warning (loss was within threshold)\n")

# -----------------------------------------------------------------------------
# 2. raise_on_loss — hard stop when > 2 % of rows are dropped
# -----------------------------------------------------------------------------
print("=" * 60)
print("Demo 2 — raise_on_loss=0.02  (pipeline will be stopped)")
print("=" * 60)


@watch(raise_on_loss=0.02, track_memory="off")
def aggressive_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drops 30 % of rows — well above the 2 % hard limit."""
    return df[df["status"] == "active"].copy()


try:
    aggressive_filter(df_base)
except ThresholdExceeded as exc:
    print(f"✗  ThresholdExceeded caught:\n{exc}\n")

# -----------------------------------------------------------------------------
# 3. warn_on_gain — soft alert when > 10 % rows are gained (join fan-out)
# -----------------------------------------------------------------------------
print("=" * 60)
print("Demo 3 — warn_on_gain=0.10  (join produces fan-out)")
print("=" * 60)


@watch(warn_on_gain=0.10, track_memory="off")
def merge_with_fanout(df: pd.DataFrame) -> pd.DataFrame:
    """Merging against a non-unique ref table doubles rows."""
    return df.merge(customer_ref, on="customer_id", how="left")


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    merged = merge_with_fanout(df_base)

if caught:
    print(f"⚠  WatcherWarning fired — {len(caught)} warning(s) caught\n")
else:
    print("No warning\n")

# -----------------------------------------------------------------------------
# 4. raise_on_gain — hard stop on unexpected row gain
# -----------------------------------------------------------------------------
print("=" * 60)
print("Demo 4 — raise_on_gain=0.05  (fan-out exceeds hard limit)")
print("=" * 60)


@watch(raise_on_gain=0.05, track_memory="off")
def merge_strict(df: pd.DataFrame) -> pd.DataFrame:
    return df.merge(customer_ref, on="customer_id", how="left")


try:
    merge_strict(df_base)
except ThresholdExceeded as exc:
    print(f"✗  ThresholdExceeded caught:\n{exc}\n")

# -----------------------------------------------------------------------------
# 5. Combining warn + raise on the same decorator
# -----------------------------------------------------------------------------
print("=" * 60)
print("Demo 5 — warn_on_loss=0.05 + raise_on_loss=0.40 (two-tier guard)")
print("=" * 60)


@watch(warn_on_loss=0.05, raise_on_loss=0.40, track_memory="off")
def two_tier_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drops ~30 % — triggers warn but not raise."""
    return df[df["status"] == "active"].copy()


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = two_tier_filter(df_base)

warn_count = sum(1 for w in caught if issubclass(w.category, WatcherWarning))
print(f"Warnings: {warn_count}  |  Output rows: {len(result):,}\n")

print("All threshold demos complete.")
