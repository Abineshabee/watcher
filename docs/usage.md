# watcher — Usage Guide

---

## Install

```bash
pip install dfwatcher                 # pandas only
pip install "dfwatcher[rich]"         # + coloured terminal output
pip install "dfwatcher[full]"         # + Rich + psutil (recommended)
```

---

## `@watch` — the decorator

### Bare form (no arguments)

```python
from watcher import watch

@watch
def clean(df):
    return df.dropna()
```

Output on every call:
```
clean()  1,000,000 → 921,330  ▼ -78,670 rows (-7.9%)  68.5 ms  mem +0.0 MB (rss)
  nulls -2,477  status  (2,477 → 0)
  nulls -1,448  revenue  (1,448 → 0)
```

### With arguments

Parentheses are required when passing any argument:

```python
@watch(warn_on_loss=0.05, track_memory="rss", verbose=True)
def clean(df):
    return df.dropna()
```

### Full signature

```python
@watch(
    warn_on_gain  = None,      # float | None — warn if row gain  > this fraction
    warn_on_loss  = None,      # float | None — warn if row loss  > this fraction
    raise_on_gain = None,      # float | None — raise if row gain > this fraction
    raise_on_loss = None,      # float | None — raise if row loss > this fraction
    label         = None,      # str   | None — override the function name in output
    track_memory  = "rss",     # "rss" | "peak" | "off" | True | False
    verbose       = True,      # bool  — False = silent but still tracked in session
)
```

All threshold values are **fractions of input row count** in the range `0.0–1.0`.

```python
warn_on_loss=0.05    # warn  if more than  5% of rows are lost
raise_on_loss=0.20   # raise if more than 20% of rows are lost
warn_on_gain=0.10    # warn  if more than 10% of rows are gained
raise_on_gain=1.00   # raise if rows more than double (> 100% gain)
```

### `label` — rename a step in output

Useful when the same helper function is reused across many pipeline steps:

```python
@watch(label="remove nulls — revenue column")
def drop_bad_rows(df):
    return df.dropna(subset=["revenue"])
```

Output shows your label, not the function name:
```
remove nulls — revenue column()  10,000 → 9,100  ▼ -900 rows (-9.0%)  ...
```

---

## What `@watch` measures automatically

Every decorated call captures and reports all of the following with zero configuration.

### Row counts

```
drop_nulls()  1,000,000 → 921,330  ▼ -78,670 rows (-7.9%)  68.5 ms  mem +0.0 MB (rss)
```

Symbols: `▲` rows gained · `▼` rows lost · `●` no change

### Null deltas

Per-column null counts before and after, sorted by magnitude (worst first):

```
drop_nulls()  1,000,000 → 921,330  ▼ -78,670 rows (-7.9%)
  nulls -2,477  status   (2,477 → 0)
  nulls -1,448  revenue  (1,448 → 0)
```

### Schema drift

Columns added or removed between input and output:

```python
@watch
def add_revenue_band(df):
    df = df.copy()
    df["revenue_band"] = pd.cut(df["revenue"], bins=3, labels=["low","mid","high"])
    return df

@watch
def drop_temp(df):
    return df.drop(columns=["created_at"])
```

```
add_revenue_band()  582,246 → 582,246  ● +0 rows
  columns added   : +revenue_band

drop_temp()  582,246 → 582,246  ● +0 rows
  columns removed : -created_at
```

### Dtype change detection

When a column's dtype changes between steps:

```python
@watch
def coerce_ids(df):
    df = df.copy()
    df["customer_id"] = df["customer_id"].astype(str)
    return df
```

```
coerce_ids()  10,000 → 10,000  ● +0 rows
  dtype change : customer_id  int64 → object
```

Safe widenings (e.g. `int32 → int64`, `float32 → float64`) are detected separately from narrowing coercions (e.g. `float64 → object`) which may lose information.

### Join explosion detection

When a merge fans out rows unexpectedly, watcher identifies which key column caused it and which values are duplicated:

```python
@watch
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")
```

```
merge_orders()  10,000 → 20,000  ▲ +10,000 rows (+100.0%) ⚠ 💥 join explosion
  columns added : +tier
  join explosion · duplication ratio 100.0%
  key column     top value    repeat count
  customer_id    9182                  184
  customer_id    3310                   97
```

The explosion detector looks at columns ending with `_id`, `_key`, `_code`, or that are integer-typed. It compares the max repeat count before vs after — if it grew and is greater than 1, that column is flagged as the culprit.

---

## Threshold guards

### Soft warning — `warn_on_gain` / `warn_on_loss`

The pipeline continues. A `WatcherWarning` is emitted via Python's `warnings` module:

```python
import warnings
from watcher.exceptions import WatcherWarning

@watch(warn_on_loss=0.05)
def filter_active(df):
    return df[df["status"] == "active"]

# Catch the warning in code
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    result = filter_active(df)
    if w:
        print(w[0].category.__name__)   # WatcherWarning
        print(w[0].message)
```

### Hard stop — `raise_on_gain` / `raise_on_loss`

The pipeline stops immediately. A `ThresholdExceeded` exception is raised:

```python
from watcher.exceptions import ThresholdExceeded

@watch(raise_on_loss=0.20)
def drop_nulls(df):
    return df.dropna()

try:
    result = drop_nulls(df)
except ThresholdExceeded as e:
    print(e)
    # [watcher] 'drop_nulls' breached raise_on_loss=20.0%.
    #   Rows : 100 → 50  (-50 rows, -50.0%)
    #   Memory: +0.0 MB (rss)
```

### All four thresholds together

```python
@watch(
    warn_on_loss=0.05,
    raise_on_loss=0.20,
    warn_on_gain=0.10,
    raise_on_gain=1.00,
)
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")
```

Evaluation order within a single call: `raise_on_*` is always checked before `warn_on_*` so the harder constraint takes precedence.

### Zero-tolerance threshold

Setting `raise_on_loss=0.0` makes any row loss a hard failure:

```python
@watch(raise_on_loss=0.0)
def strict_step(df):
    return df[df["val"] > 1]   # drops even 1 row → raises immediately
```

### CI pipeline pattern

```python
import warnings
from watcher.exceptions import ThresholdExceeded, WatcherWarning

# Escalate soft warnings to hard errors in CI
warnings.filterwarnings("error", category=WatcherWarning)

try:
    result = pipeline(df)
except ThresholdExceeded as e:
    print(f"Data contract violated: {e}")
    raise SystemExit(1)
```

---

## Memory tracking

### `track_memory` values

| Value | Mechanism | Captures | Requires |
|---|---|---|---|
| `"rss"` / `True` | `psutil` process RSS delta | NumPy, pandas, Arrow C allocations | `psutil` |
| `"peak"` | `tracemalloc` Python-heap peak | Python objects only | stdlib |
| `"off"` / `False` | Disabled | Nothing — zero overhead | — |

Default is `"rss"`. If `psutil` is not installed and `"rss"` is requested, watcher falls back to `"peak"` and emits a one-time `WatcherWarning`.

### Examples

```python
@watch(track_memory="rss")    # recommended — captures everything
def big_merge(df):
    return df.merge(large_table, on="id", how="left")

@watch(track_memory="peak")   # no psutil needed
def pure_python_step(df):
    return df.assign(score=df["x"].apply(some_fn))

@watch(track_memory="off")    # zero overhead for hot paths
def fast_rename(df):
    return df.rename(columns={"old": "new"})

# Boolean aliases also work
@watch(track_memory=True)     # same as "rss"
@watch(track_memory=False)    # same as "off"
```

### Reading the memory output

```
big_merge()  1,000,000 → 1,000,000  ● +0 rows  56.2 ms  mem +38.5 MB (rss)
```

The delta is `RSS_after − RSS_before`. It can be negative if the garbage collector ran during the function. A large positive delta on a step that shouldn't allocate is a useful signal that something unexpected is happening.

---

## `session()` — group steps into a pipeline run

### Basic usage

```python
from watcher import watch, session

with session("nightly ETL") as s:
    df = clean(df)
    df = merge_orders(df)
    df = filter_active(df)
```

On exit, the summary table prints automatically:

```
╭──────────────── watcher · nightly ETL · summary ───────────────────╮
│  step            rows in    rows out      Δ rows   time (ms)       │
│  clean         1,000,000     964,203     -35,797       12.3        │
│  merge_orders    964,203   1,069,104    +104,901       41.1        │
│  filter_active 1,069,104     631,822    -437,282       18.7        │
│                                                                    │
│  total  1,000,000 → 631,822  (-368,178 rows)  72.1 ms              │
╰────────────────────────────────────────────────────────────────────╯
```

### `s.summary()` — machine-readable dict

Call `s.summary()` after the `with` block for programmatic access:

```python
with session("my pipeline") as s:
    df = clean(df)
    df = filter_active(df)

result = s.summary()
```

The dict has this structure:

```python
{
    "name":                 "my pipeline",
    "total_steps":          2,
    "total_rows_in":        1_000_000,
    "total_rows_out":       631_822,
    "total_rows_lost":      -368_178,    # sum of all negative diffs
    "total_rows_gained":    0,           # sum of all positive diffs
    "total_elapsed_s":      0.0721,
    "total_memory_delta_mb": 38.5,
    "steps": [
        {
            "func":             "clean",
            "rows_in":          1_000_000,
            "rows_out":         964_203,
            "diff":             -35_797,
            "diff_pct":         -3.58,    # percentage points
            "elapsed_s":        0.0123,
            "memory_delta_mb":  12.3,
            "memory_mode":      "rss",
            "join_explosion":   False,
            "warned":           False,
        },
        # ... one dict per step
    ]
}
```

### Assertions in CI

```python
with session("nightly") as s:
    df = clean(df)
    df = score(df)

summary = s.summary()
assert summary["total_rows_out"] > 500_000, \
    f"Too many rows dropped: {summary['total_rows_out']:,}"
assert summary["total_elapsed_s"] < 60.0, "Pipeline too slow"
```

### `verbose=False` — silent steps, still tracked

Steps decorated with `verbose=False` produce no per-step terminal output but still appear in the session summary:

```python
@watch(verbose=False)
def clean(df):
    return df.dropna()

@watch(verbose=False)
def enrich(df):
    df = df.copy()
    df["score"] = compute_score(df)
    return df

with session("silent pipeline") as s:
    df = clean(raw)
    df = enrich(df)

# No per-step output above.
# Session summary prints on exit and shows both steps.
summary = s.summary()
assert len(summary["steps"]) == 2
assert summary["steps"][0]["rows_out"] == expected
```

---

## Custom handlers

Every decorated function call fires `on_step()` on all registered handlers. The default `TerminalHandler` prints to the terminal. You can add your own alongside it or replace it entirely.

### Writing a custom handler

Subclass `HandlerBase` and override only the events you need:

```python
from watcher import register_handler, deregister_handler
from watcher.handlers import HandlerBase
from watcher.core import StepResult, WatcherSession
import json

class JSONLogHandler(HandlerBase):
    def __init__(self):
        self.log = []

    def on_step(self, step: StepResult) -> None:
        self.log.append({
            "step":        step.func_name,
            "rows_in":     step.rows_in,
            "rows_out":    step.rows_out,
            "diff":        step.row_diff,
            "diff_pct":    round(step.row_diff_pct * 100, 2),
            "ms":          round(step.elapsed_s * 1000, 2),
            "mem_mb":      step.memory_delta_mb,
            "exploded":    step.is_join_explosion,
            "warned":      step.warned,
        })

    def on_session_end(self, session: WatcherSession) -> None:
        print(json.dumps({"session": session.name, "steps": self.log}, indent=2))

handler = JSONLogHandler()
register_handler(handler)

# ... run your pipeline ...

deregister_handler(handler)
```

### Three handler events

```python
class HandlerBase:
    def on_session_start(self, session: WatcherSession) -> None: ...  # session() opens
    def on_step(self, step: StepResult) -> None: ...                  # after each @watch call
    def on_session_end(self, session: WatcherSession) -> None: ...    # session() closes
```

All three default to no-ops. Override only what you need.

### Replacing the default terminal handler

```python
from watcher.handlers import _get_handlers

# Remove the built-in terminal output
_get_handlers().clear()

# Install only your handler
register_handler(MyCustomHandler())
```

### Common handler use-cases

```python
# Slack/Teams alert on join explosion
class AlertHandler(HandlerBase):
    def on_step(self, step: StepResult) -> None:
        if step.is_join_explosion:
            send_slack_alert(f"💥 Join explosion in {step.func_name}!")
        if step.warned:
            send_slack_alert(f"⚠ Threshold warning in {step.func_name}")

# Prometheus metrics
class MetricsHandler(HandlerBase):
    def on_step(self, step: StepResult) -> None:
        row_counter.labels(step=step.func_name).inc(step.rows_out)
        latency_histogram.labels(step=step.func_name).observe(step.elapsed_s)
```

---

## `StepResult` — all attributes

Every `on_step(step)` call gives you a `StepResult`. All attributes and properties:

```python
step.func_name          # str   — function name or label
step.rows_in            # int   — row count before the step
step.rows_out           # int   — row count after the step
step.row_diff           # int   — rows_out - rows_in (signed)
step.row_diff_pct       # float — row_diff / rows_in (0.0 when rows_in == 0)
step.gained_rows        # bool  — True when row_diff > 0
step.lost_rows          # bool  — True when row_diff < 0
step.is_join_explosion  # bool  — True when a join fan-out is suspected
step.elapsed_s          # float — wall-clock time in seconds
step.memory_delta_mb    # float — memory change in MB
step.memory_mode        # MemoryMode — "rss", "peak", or "off"
step.warned             # bool  — True when a warn_on_* threshold fired
step.stats              # StepStats — full column-level stats (see below)
```

---

## `StepResult.stats` — column-level detail

`step.stats` is a `StepStats` object with the full diagnostic payload:

```python
step.stats.column_diff              # ColumnDiff — schema drift
step.stats.column_diff.added        # list[str] — columns added
step.stats.column_diff.removed      # list[str] — columns removed
step.stats.column_diff.has_drift    # bool — True when added or removed is non-empty

step.stats.dtype_changes            # list[DtypeChange]
step.stats.null_deltas              # list[NullDelta]
step.stats.duplicate_key_detected   # bool — join explosion shortcut flag
step.stats.join_explosion           # JoinExplosionDetail
step.stats.sampled                  # bool — True when stats used a sample
step.stats.sample_size              # int  — sample size (0 if not sampled)
step.stats.backend                  # str  — "pandas" (more coming)
```

### `DtypeChange`

```python
for change in step.stats.dtype_changes:
    change.column       # str  — column name
    change.before       # str  — e.g. "int64"
    change.after        # str  — e.g. "object"
    change.is_widening  # bool — True for safe promotions (int32→int64)
                        #        False for narrowing (float64→object)
```

### `NullDelta`

```python
for nd in step.stats.null_deltas:
    nd.column   # str — column name
    nd.before   # int — null count before
    nd.after    # int — null count after
    nd.delta    # int — signed difference (negative = nulls removed)
```

### `JoinExplosionDetail`

```python
detail = step.stats.join_explosion
detail.duplicate_key_detected   # bool
detail.offending_columns        # list[str] — sorted worst-first
detail.top_offenders            # dict[str, list[tuple[value, count]]]
detail.duplication_ratio        # float — (rows_out - rows_in) / rows_in

# Example: inspect which values caused the explosion
for col in detail.offending_columns:
    for value, count in detail.top_offenders[col]:
        print(f"  {col}={value!r} appears {count} times")
```

---

## `MemoryMode` enum

```python
from watcher import MemoryMode

MemoryMode.RSS    # "rss"  — process RSS via psutil
MemoryMode.PEAK   # "peak" — Python-heap peak via tracemalloc
MemoryMode.OFF    # "off"  — disabled
```

Can be passed directly or as a string:

```python
@watch(track_memory=MemoryMode.RSS)
@watch(track_memory="rss")      # equivalent
@watch(track_memory=True)       # equivalent
```

---

## `BackendRegistry` — adding a new DataFrame backend

The engine never imports pandas directly. All DataFrame types are detected through registered adapters. The pandas adapter is registered automatically at import time.

To add support for a new DataFrame type (e.g. Polars):

```python
from watcher.core import BackendAdapter, BackendRegistry

class PolarsAdapter:
    @staticmethod
    def accepts(obj) -> bool:
        try:
            import polars as pl
            return isinstance(obj, pl.DataFrame)
        except ImportError:
            return False

    @staticmethod
    def row_count(obj) -> int:
        return obj.height

    @staticmethod
    def column_names(obj) -> list[str]:
        return obj.columns

BackendRegistry.register(PolarsAdapter)
```

After registration, `@watch` works transparently on that DataFrame type.

---

## Exceptions

All exceptions inherit from `WatcherError`, so you can catch the entire family with one clause:

```python
from watcher.exceptions import (
    WatcherError,        # root — catches everything below
    ThresholdExceeded,   # raise_on_* threshold breached
    BackendError,        # backend adapter failed at runtime
    ConfigurationError,  # invalid decorator arguments at decoration time
    WatcherWarning,      # warn_on_* threshold (UserWarning, not Exception)
)

try:
    result = pipeline(df)
except ThresholdExceeded as e:
    print(e.message)    # structured message with row counts and percentages
except WatcherError as e:
    print(f"watcher error: {e}")
```

### Control `WatcherWarning` in tests

```python
import warnings
from watcher.exceptions import WatcherWarning

# Escalate to errors in CI
warnings.filterwarnings("error", category=WatcherWarning)

# Suppress entirely
warnings.filterwarnings("ignore", category=WatcherWarning)
```

### `ConfigurationError` — raised at decoration time

```python
# These raise ConfigurationError immediately, before any data flows:
@watch(raise_on_loss=1.5)    # invalid — must be in [0.0, 1.0]
@watch(track_memory="xyz")   # invalid — not a valid MemoryMode
```

---

## Sampling behaviour

For DataFrames above 50,000 rows, null counts and dtype changes are computed on a random 50,000-row sample to keep overhead predictable. Join explosion detection always uses the full DataFrame for accurate counts.

When sampling is active, `step.stats.sampled` is `True` and `step.stats.sample_size` shows how many rows were used. The terminal output notes this as a footnote.

---

## Thread and async safety

The active session is stored in a `contextvars.ContextVar`. Each OS thread and each asyncio `Task` that calls `session()` gets its own independent slot — concurrent pipelines do not interfere with each other's session accumulation.

```python
import asyncio
from watcher import watch, session

@watch
async def async_step(df):
    ...

async def pipeline_a(df):
    with session("pipeline A") as s:
        df = await async_step(df)
    return s.summary()

async def pipeline_b(df):
    with session("pipeline B") as s:
        df = await async_step(df)
    return s.summary()

# Both pipelines run independently — sessions do not mix
asyncio.gather(pipeline_a(df1), pipeline_b(df2))
```
