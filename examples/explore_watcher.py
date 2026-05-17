# =============================================================================
# explore_watcher.py
#
# Full feature exploration of the watcher library.
# Covers every public API, class, and behaviour.
#
# Run:
#   python explore_watcher.py
# =============================================================================

import warnings
import numpy as np
import pandas as pd

# -----------------------------------------------------------------------------
# 0. Imports — everything watcher exposes publicly
# -----------------------------------------------------------------------------
from watcher import (
    watch,
    session,
    MemoryMode,
    StepResult,
    BackendRegistry,
    WatcherError,
    WatcherWarning,
    ThresholdExceeded,
    BackendError,
    ConfigurationError,
    HandlerBase,
    register_handler,
    deregister_handler,
)

rng = np.random.default_rng(42)

DIVIDER = "\n" + "=" * 70 + "\n"

# =============================================================================
# SECTION 1 — Basic @watch decorator (bare, no args)
# =============================================================================
print(DIVIDER)
print("SECTION 1 — Basic @watch (no arguments)")
print(DIVIDER)

df_base = pd.DataFrame(
    {
        "id": np.arange(1000),
        "value": rng.uniform(0, 100, 1000).round(2),
        "status": rng.choice(["ok", "bad", None], 1000, p=[0.7, 0.2, 0.1]),
    }
)


@watch
def drop_bad(df):
    return df[df["status"] == "ok"].copy()


result = drop_bad(df_base)
print(f"Output rows: {len(result)}")


# =============================================================================
# SECTION 2 — @watch with label override
# =============================================================================
print(DIVIDER)
print("SECTION 2 — @watch(label=...) custom step name")
print(DIVIDER)


@watch(label="remove_invalid_rows")
def filter_step(df):
    return df.dropna(subset=["status"])


result = filter_step(df_base)
print(f"Output rows: {len(result)}")


# =============================================================================
# SECTION 3 — MemoryMode variants
# =============================================================================
print(DIVIDER)
print("SECTION 3 — MemoryMode: OFF / PEAK / RSS")
print(DIVIDER)


@watch(track_memory=MemoryMode.OFF)
def step_mem_off(df):
    return df.copy()


@watch(track_memory=MemoryMode.PEAK)
def step_mem_peak(df):
    return df.copy()


@watch(track_memory=MemoryMode.RSS)
def step_mem_rss(df):
    return df.copy()


@watch(track_memory=False)  # convenience alias for OFF
def step_mem_bool_false(df):
    return df.copy()


@watch(track_memory=True)  # convenience alias for RSS
def step_mem_bool_true(df):
    return df.copy()


for fn in [
    step_mem_off,
    step_mem_peak,
    step_mem_rss,
    step_mem_bool_false,
    step_mem_bool_true,
]:
    fn(df_base)


# =============================================================================
# SECTION 4 — verbose=False (silent step, no output)
# =============================================================================
print(DIVIDER)
print("SECTION 4 — verbose=False  (nothing printed, step still tracked)")
print(DIVIDER)


@watch(verbose=False)
def silent_step(df):
    return df[df["value"] > 50].copy()


result = silent_step(df_base)
print(f"Silent step ran fine, output rows: {len(result)}")


# =============================================================================
# SECTION 5 — Schema drift: columns added & removed
# =============================================================================
print(DIVIDER)
print("SECTION 5 — Schema drift: columns added and removed")
print(DIVIDER)


@watch(track_memory="off")
def add_columns(df):
    df = df.copy()
    df["score"] = df["value"] * 2
    df["is_valid"] = df["status"] == "ok"
    return df


@watch(track_memory="off")
def remove_columns(df):
    return df.drop(columns=["score", "status"])


r1 = add_columns(df_base)
r2 = remove_columns(r1)


# =============================================================================
# SECTION 6 — Dtype change detection
# =============================================================================
print(DIVIDER)
print("SECTION 6 — Dtype change: int64 → object (narrowing)")
print(DIVIDER)


@watch(track_memory="off")
def coerce_dtype(df):
    df = df.copy()
    df["id"] = df["id"].astype(str)  # int64 → object (narrowing)
    return df


coerce_dtype(df_base)


# =============================================================================
# SECTION 7 — Null delta tracking
# =============================================================================
print(DIVIDER)
print("SECTION 7 — Null delta: introducing and removing nulls")
print(DIVIDER)


@watch(track_memory="off")
def introduce_nulls(df):
    df = df.copy()
    mask = rng.random(len(df)) < 0.15
    df.loc[mask, "value"] = np.nan
    return df


@watch(track_memory="off")
def fill_nulls(df):
    return df.fillna({"value": 0.0})


r1 = introduce_nulls(df_base)
fill_nulls(r1)


# =============================================================================
# SECTION 8 — warn_on_loss threshold
# =============================================================================
print(DIVIDER)
print("SECTION 8 — warn_on_loss=0.05  (soft warning)")
print(DIVIDER)


@watch(warn_on_loss=0.05, track_memory="off")
def lossy_filter(df):
    return df[df["value"] > 50].copy()  # drops ~50%


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    lossy_filter(df_base)

print(
    f"WatcherWarning fired: {any(issubclass(w.category, WatcherWarning) for w in caught)}"
)


# =============================================================================
# SECTION 9 — raise_on_loss threshold
# =============================================================================
print(DIVIDER)
print("SECTION 9 — raise_on_loss=0.02  (hard stop)")
print(DIVIDER)


@watch(raise_on_loss=0.02, track_memory="off")
def strict_filter(df):
    return df[df["value"] > 50].copy()


try:
    strict_filter(df_base)
except ThresholdExceeded as exc:
    print(f"ThresholdExceeded caught: {str(exc)[:120]}")


# =============================================================================
# SECTION 10 — warn_on_gain + join explosion detection
# =============================================================================
print(DIVIDER)
print("SECTION 10 — warn_on_gain=0.10  (join fan-out)")
print(DIVIDER)

ref = pd.DataFrame(
    {
        "id": np.repeat(np.arange(100), 3),  # every id appears 3x
        "tag": rng.choice(["a", "b", "c"], 300),
    }
)
df_small = pd.DataFrame(
    {
        "id": np.arange(100),
        "value": rng.uniform(0, 100, 100).round(2),
    }
)


@watch(warn_on_gain=0.10, track_memory="off")
def fanout_merge(df):
    return df.merge(ref, on="id", how="left")


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    fanout_merge(df_small)

print(
    f"WatcherWarning fired: {any(issubclass(w.category, WatcherWarning) for w in caught)}"
)


# =============================================================================
# SECTION 11 — raise_on_gain threshold
# =============================================================================
print(DIVIDER)
print("SECTION 11 — raise_on_gain=0.05  (hard stop on row gain)")
print(DIVIDER)


@watch(raise_on_gain=0.05, track_memory="off")
def strict_merge(df):
    return df.merge(ref, on="id", how="left")


try:
    strict_merge(df_small)
except ThresholdExceeded as exc:
    print(f"ThresholdExceeded caught: {str(exc)[:120]}")


# =============================================================================
# SECTION 12 — Two-tier guard (warn + raise on same decorator)
# =============================================================================
print(DIVIDER)
print("SECTION 12 — Two-tier: warn_on_loss=0.05, raise_on_loss=0.90")
print(DIVIDER)


@watch(warn_on_loss=0.05, raise_on_loss=0.90, track_memory="off")
def two_tier(df):
    return df[df["value"] > 50].copy()  # drops ~50%, warn fires, raise doesn't


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = two_tier(df_base)

warn_count = sum(1 for w in caught if issubclass(w.category, WatcherWarning))
print(f"Warnings: {warn_count}  |  Output rows: {len(result)}")


# =============================================================================
# SECTION 13 — session() context manager + summary table
# =============================================================================
print(DIVIDER)
print("SECTION 13 — session() context manager")
print(DIVIDER)


@watch(track_memory="off")
def s_clean(df):
    return df.dropna()


@watch(track_memory="off")
def s_filter(df):
    return df[df["value"] > 20].copy()


@watch(track_memory="off")
def s_enrich(df):
    df = df.copy()
    df["bucket"] = pd.cut(df["value"], bins=3, labels=["low", "mid", "high"])
    return df


with session("explore session") as s:
    df = s_clean(df_base)
    df = s_filter(df)
    df = s_enrich(df)

# Inspect session object directly
summary = s.summary()
print(f"\nSession name   : {summary['name']}")
print(f"Steps recorded : {len(summary['steps'])}")
print(f"Total rows in  : {summary['total_rows_in']:,}")
print(f"Total rows out : {summary['total_rows_out']:,}")
print(f"Total time     : {summary['total_elapsed_s'] * 1000:.1f} ms")
print(f"Total memory   : {summary['total_memory_delta_mb']:+.1f} MB")


# =============================================================================
# SECTION 14 — Nested / sequential sessions
# =============================================================================
print(DIVIDER)
print("SECTION 14 — Two sequential sessions")
print(DIVIDER)

with session("session A") as sa:
    s_clean(df_base)

with session("session B") as sb:
    s_filter(df_base)

print(f"Session A steps: {len(sa.summary()['steps'])}")
print(f"Session B steps: {len(sb.summary()['steps'])}")


# =============================================================================
# SECTION 15 — StepResult attributes
# =============================================================================
print(DIVIDER)
print("SECTION 15 — StepResult attributes inspection")
print(DIVIDER)

captured_step: StepResult = None


class CapturingHandler(HandlerBase):
    def on_step(self, step: StepResult):
        global captured_step
        captured_step = step


handler = CapturingHandler()
register_handler(handler)


@watch(track_memory="off")
def inspectable(df):
    return df[df["value"] > 30].copy()


inspectable(df_base)
deregister_handler(handler)

if captured_step:
    s = captured_step
    print(f"func_name       : {s.func_name}")
    print(f"rows_in         : {s.rows_in:,}")
    print(f"rows_out        : {s.rows_out:,}")
    print(f"row_diff        : {s.row_diff:,}")
    print(f"row_diff_pct    : {s.row_diff_pct:.2%}")
    print(f"lost_rows       : {s.lost_rows}")
    print(f"gained_rows     : {s.gained_rows}")
    print(f"is_join_explosion:{s.is_join_explosion}")
    print(f"elapsed_s       : {s.elapsed_s * 1000:.2f} ms")
    print(f"memory_delta_mb : {s.memory_delta_mb:+.3f}")
    print(f"memory_mode     : {s.memory_mode}")
    print(f"warned          : {s.warned}")
    print(f"stats.backend   : {s.stats.backend}")
    print(f"stats.sampled   : {s.stats.sampled}")


# =============================================================================
# SECTION 16 — Custom handler (register / deregister)
# =============================================================================
print(DIVIDER)
print("SECTION 16 — Custom HandlerBase: JSON logger")
print(DIVIDER)

import json


class JSONLogHandler(HandlerBase):
    def __init__(self):
        self.log = []

    def on_step(self, step: StepResult):
        self.log.append(
            {
                "step": step.func_name,
                "rows_in": step.rows_in,
                "rows_out": step.rows_out,
                "diff": step.row_diff,
                "ms": round(step.elapsed_s * 1000, 2),
            }
        )

    def on_session_start(self, session):
        print(f"  [JSONLogHandler] session started: {session.name}")

    def on_session_end(self, session):
        print(f"  [JSONLogHandler] session ended, steps: {len(self.log)}")


json_handler = JSONLogHandler()
register_handler(json_handler)


@watch(track_memory="off")
def logged_step(df):
    return df[df["value"] > 60].copy()


with session("logged session"):
    logged_step(df_base)

deregister_handler(json_handler)

print("JSON log captured:")
print(json.dumps(json_handler.log, indent=2))


# =============================================================================
# SECTION 17 — BackendRegistry inspection
# =============================================================================
print(DIVIDER)
print("SECTION 17 — BackendRegistry")
print(DIVIDER)

adapter = BackendRegistry.detect(df_base)
print(f"Detected adapter for pandas DataFrame: {adapter}")

non_df = {"not": "a dataframe"}
adapter_none = BackendRegistry.detect(non_df)
print(f"Detected adapter for dict: {adapter_none}")


# =============================================================================
# SECTION 18 — Exception hierarchy
# =============================================================================
print(DIVIDER)
print("SECTION 18 — Exception hierarchy")
print(DIVIDER)

# ThresholdExceeded is a WatcherError
exc = ThresholdExceeded("test message")
print(f"ThresholdExceeded is WatcherError: {isinstance(exc, WatcherError)}")
print(f"str(exc): {exc}")

# BackendError with full detail
be = BackendError(
    "null_counts failed", backend_name="pandas", original=ValueError("oops")
)
print(f"\nBackendError:\n{be}")

# ConfigurationError
ce = ConfigurationError("raise_on_loss must be in [0.0, 1.0]")
print(f"\nConfigurationError: {ce}")


# =============================================================================
# SECTION 19 — Large DataFrame (sampling behaviour)
# =============================================================================
print(DIVIDER)
print("SECTION 19 — Large DataFrame: sampling kicks in (>50,000 rows)")
print(DIVIDER)

df_large = pd.DataFrame(
    {
        "id": np.arange(200_000),
        "value": rng.uniform(0, 1000, 200_000).round(2),
        "cat": rng.choice(["A", "B", "C"], 200_000),
    }
)


@watch(track_memory="off")
def large_filter(df):
    return df[df["value"] > 500].copy()


large_filter(df_large)
print("(sampling footnote should appear above — stats on 50,000 row sample)")


# =============================================================================
# SECTION 20 — TypeError on non-DataFrame input
# =============================================================================
print(DIVIDER)
print("SECTION 20 — TypeError when non-DataFrame passed to @watch function")
print(DIVIDER)


@watch(track_memory="off")
def expects_df(df):
    return df.copy()


try:
    expects_df([1, 2, 3])
except TypeError as exc:
    print(f"TypeError caught (expected):\n  {str(exc)[:200]}")


# =============================================================================
# DONE
# =============================================================================
print(DIVIDER)
print("All sections complete. Every watcher feature exercised.")
print(DIVIDER)
