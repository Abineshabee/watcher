
<p align="center">
  <img src="assets/logo/watcher_logo_text_right.svg" width="500">
</p>

> **The silent data watcher.** Decorates your pipeline functions and tells you exactly what happened to your data — row counts, schema drift, null changes, memory usage, join explosions — automatically, with zero config.

[![PyPI](https://img.shields.io/pypi/v/dfwatcher?cache=0)](https://pypi.org/project/dfwatcher/)
[![Python](https://img.shields.io/pypi/pyversions/dfwatcher)](https://pypi.org/project/dfwatcher/)
[![CI](https://github.com/Abineshabee/watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/Abineshabee/watcher/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.20286838-blue)](https://doi.org/10.5281/zenodo.20286838)
[![GitHub release](https://img.shields.io/github/v/release/Abineshabee/watcher)](https://github.com/Abineshabee/watcher/releases)
---

## The problem

You run a data pipeline. The output looks wrong. Your only clue:

```
Input:  1,000,000 rows
Output:   263,979 rows
```

Which step dropped the rows? Was it a filter, a null drop, or a bad join? You have no idea without adding print statements everywhere and re-running the whole thing.

**watcher answers that — automatically.**

---

## Install

```bash
pip install dfwatcher                 # core only (pandas)
pip install "dfwatcher[rich]"         # + coloured terminal output
pip install "dfwatcher[full]"         # + Rich + psutil memory tracking
```

---

## Quickstart

```python
import pandas as pd
from watcher import watch, session

raw = pd.DataFrame({
    "customer_id": [1, 2, 3, 4],
    "status": ["active", "inactive", "active", None]
})

orders = pd.DataFrame({
    "customer_id": [1, 3],
    "amount": [250.0, 150.0]
})

@watch
def clean(df):
    return df.dropna()

@watch
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")

@watch
def filter_active(df):
    return df[df["status"] == "active"]

# 3. Run the session to see the watcher summary!
if __name__ == "__main__":
    with session("nightly ETL") as s:
        df = clean(raw)
        df = merge_orders(df)
        df = filter_active(df)

#=====================================
# For more Examples    : exammples/
# For Syntax and Usage : docs/usage.md
# ====================================
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/quick_start_output.png" width="800">
</p>

---

## Documentation

- [Usage Guide](docs/usage.md)
- [API Reference](docs/index.md)
- [Examples](examples/)

---

For advanced pipeline patterns and debugging workflows, see the full documentation.
## Features

### Row tracking

Every decorated function shows rows before → after, the signed diff, percentage change, and elapsed time. Nothing is hidden, nothing needs configuring.

```python
import pandas as pd
from watcher import watch, session


data = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "status": ["active", "inactive", "active", None, "active"],
    "amount": [100, 200, 300, 400, 500]
})


@watch
def step_1_drop_nulls(df):
    return df.dropna()


@watch
def step_2_filter_active(df):
    return df[df["status"] == "active"]


@watch
def step_3_double_amount(df):
    df = df.copy()
    df["amount"] = df["amount"] * 2
    return df


with session("row tracking demo"):
    df = step_1_drop_nulls(data)
    df = step_2_filter_active(df)
    df = step_3_double_amount(df)
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/row_tracking.png" width="800">
</p>

---

### Null-count deltas

Per-column null counts are compared before and after each step. The worst offenders are shown first.

```python
import pandas as pd
from watcher import watch, session


data = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "status": ["active", None, "active", None, "inactive"],
    "amount": [100, None, 300, 400, None]
})


@watch
def step_1_fill_nulls(df):
    return df.fillna({
        "status": "unknown",
        "amount": 0
    })


@watch
def step_2_filter_active(df):
    return df[df["status"] == "active"]


@watch
def step_3_drop_missing_amount(df):
    return df[df["amount"] > 0]


with session("null delta tracking demo"):
    df = step_1_fill_nulls(data)
    df = step_2_filter_active(df)
    df = step_3_drop_missing_amount(df)
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/null_count_delta.png" width="800">
</p>

---

### Schema drift

Columns added or removed between steps are detected and reported immediately.

```python
import pandas as pd
from watcher import watch, session


data = pd.DataFrame({
    "customer_id": [1, 2, 3, 4],
    "status": ["active", "inactive", "active", "active"],
    "amount": [100, 200, 300, 400]
})


orders = pd.DataFrame({
    "customer_id": [1, 2, 3, 4],
    "order_value": [50, 60, 70, 80]
})


@watch
def step_1_add_feature(df):
    df = df.copy()
    df["amount_with_tax"] = df["amount"] * 1.18
    return df


@watch
def step_2_merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")


@watch
def step_3_drop_columns(df):
    return df.drop(columns=["status"])


with session("schema drift demo"):
    df = step_1_add_feature(data)
    df = step_2_merge_orders(df)
    df = step_3_drop_columns(df)
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/schema_drift_tracking.png" width="800">
</p>

---

### Dtype change detection

If a step changes a column's dtype — widening (`int32` → `int64`) or narrowing (`float64` → `object`) — watcher flags it.

```python
import pandas as pd
from watcher import watch, session


data = pd.DataFrame({
    "customer_id": [1, 2, 3, 4],
    "age": [25, 30, 35, 40],
    "salary": [50000.0, 60000.0, 70000.0, 80000.0]
})


@watch
def step_1_int_to_float(df):
    df = df.copy()
    df["age"] = df["age"].astype(float)
    return df


@watch
def step_2_float_to_object(df):
    df = df.copy()
    df["salary"] = df["salary"].astype(str)
    return df


@watch
def step_3_mixed_transform(df):
    df = df.copy()
    df["customer_id"] = df["customer_id"].astype("object")
    return df


with session("dtype change detection demo"):
    df = step_1_int_to_float(data)
    df = step_2_float_to_object(df)
    df = step_3_mixed_transform(df)
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/dtype_change_detection.png" width="800">
</p>

---

### Join explosion detection

When a merge fans out unexpectedly, watcher tells you which key column caused it, which values are duplicated, and how many times — not just that rows were gained.

```python
import pandas as pd
from watcher import watch, session

# small dataset that becomes a BIG problem silently
users = pd.DataFrame({
    "customer_id": [1, 2, 3, 4, 5],
    "status": ["active", "active", "inactive", None, "active"],
})

orders = pd.DataFrame({
    "customer_id": [1, 1, 2, 2, 2, 3, 3, 3],
    "amount": [10, 20, 30, 40, 50, 60, 70, 80],
})

@watch
def clean(df):
    return df.dropna()

@watch
def join(df):
    return df.merge(orders, on="customer_id", how="left")

@watch
def final(df):
    return df.groupby("customer_id").sum().reset_index()


with session("silent data explosion detector") as s:
    df = clean(users)
    df = join(df)
    df = final(df)
```

**Output — automatically, no extra code:**

<p align="center">
  <img src="assets/screenshorts/join_explosion.png" width="800">
</p>

---

### Threshold guards

Turn watcher into a data contract enforcer. Set soft warnings or hard stops on row gain or loss.

```python
@watch(
    warn_on_loss=0.05,    # ⚠  warn  if > 5 %  rows lost
    raise_on_loss=0.20,   # ✗  raise if > 20 % rows lost
    warn_on_gain=0.10,    # ⚠  warn  if > 10 % rows gained
    raise_on_gain=1.00,   # ✗  raise if rows more than double
)
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")
```

Catching exceptions in CI:

```python
from watcher.exceptions import ThresholdExceeded, WatcherWarning

try:
    result = pipeline(df)
except ThresholdExceeded as exc:
    logger.error("Data contract violated: %s", exc)
    raise
```

---

### Memory tracking

```python
@watch(track_memory="rss")    # process RSS via psutil  — captures NumPy/pandas C allocations
@watch(track_memory="peak")   # Python-heap peak via tracemalloc — no psutil needed
@watch(track_memory="off")    # disabled — zero overhead for production pipelines
@watch(track_memory=True)     # alias for "rss"
@watch(track_memory=False)    # alias for "off"
```

Example output with RSS tracking on a 1M-row allocation:

```
big_allocation()  1,000,000 → 1,000,000  ● +0 rows  56.2 ms  mem +38.5 MB (rss)
  columns added : +col1, +col2, +col3, +col4, +col5
```

---

### Session grouping

Group multiple steps into one named pipeline run. Get a full summary table and a machine-readable dict for CI assertions.

```python
with session("user churn model — daily run") as s:
    df = clean(df)
    df = merge(df)
    df = score(df)

summary = s.summary()
assert summary["total_rows_out"] > 500_000, "Too many rows dropped!"
print(summary["total_elapsed_s"])
```

`summary()` returns:

```python
{
    "name": "user churn model — daily run",
    "steps": [
        {"func": "clean",  "rows_in": 1000000, "rows_out": 964203, "diff": -35797, ...},
        {"func": "merge",  ...},
        {"func": "score",  ...},
    ],
    "total_rows_in":        1000000,
    "total_rows_out":        631822,
    "total_elapsed_s":        0.072,
    "total_memory_delta_mb":  +38.5,
}
```

---

### Custom handlers

Swap or extend the output layer without touching your pipeline code. Every step fires `on_step()` on all registered handlers.

```python
from watcher import register_handler, deregister_handler
from watcher.handlers import HandlerBase
from watcher.core import StepResult
import json

class JSONLogHandler(HandlerBase):
    def __init__(self):
        self.log = []

    def on_step(self, step: StepResult):
        self.log.append({
            "step":     step.func_name,
            "rows_in":  step.rows_in,
            "rows_out": step.rows_out,
            "diff":     step.row_diff,
            "ms":       round(step.elapsed_s * 1000, 2),
        })

handler = JSONLogHandler()
register_handler(handler)

# ... run your pipeline ...

deregister_handler(handler)
print(json.dumps(handler.log, indent=2))
```

---

## API reference

### `@watch`

```python
@watch(
    label:         str   | None = None,          # custom step name shown in output
    warn_on_loss:  float | None = None,          # soft warning threshold (0.0–1.0)
    raise_on_loss: float | None = None,          # hard stop threshold   (0.0–1.0)
    warn_on_gain:  float | None = None,          # soft warning on row gain
    raise_on_gain: float | None = None,          # hard stop on row gain
    track_memory:  bool | str | MemoryMode = "rss",
    verbose:       bool = True,                  # False = silent, step still tracked in session
)
```

Can be used bare (`@watch`) or with arguments (`@watch(warn_on_loss=0.05)`).

---

### `session(name)`

Context manager. Groups `@watch` steps into one named pipeline run and prints a summary table on exit. Access `.summary()` on the session object for machine-readable results.

---

### `MemoryMode`

| Value | Meaning |
|---|---|
| `"rss"` / `True` | Process RSS via psutil — captures NumPy, pandas, Arrow C allocations |
| `"peak"` | Python-heap peak via tracemalloc — no extra dependencies |
| `"off"` / `False` | Disabled — zero overhead |

---

### `StepResult` attributes

| Attribute | Type | Description |
|---|---|---|
| `func_name` | `str` | Decorated function name (or `label`) |
| `rows_in` | `int` | Row count before the step |
| `rows_out` | `int` | Row count after the step |
| `row_diff` | `int` | Signed difference (`rows_out - rows_in`) |
| `row_diff_pct` | `float` | Fractional change relative to input |
| `lost_rows` | `bool` | True when rows were dropped |
| `gained_rows` | `bool` | True when rows were added |
| `is_join_explosion` | `bool` | True when a fan-out was detected |
| `elapsed_s` | `float` | Wall-clock time in seconds |
| `memory_delta_mb` | `float` | Memory change in MB |
| `memory_mode` | `MemoryMode` | Which memory strategy was used |
| `warned` | `bool` | True when a `warn_on_*` threshold fired |
| `stats` | `StepStats` | Full column-level stats (nulls, dtypes, schema drift) |

---

### Exceptions

| Exception | When |
|---|---|
| `ThresholdExceeded` | A `raise_on_*` threshold is breached — hard stop |
| `WatcherWarning` | A `warn_on_*` threshold is breached — soft, pipeline continues |
| `ConfigurationError` | Invalid decorator arguments at decoration time |
| `BackendError` | A backend adapter failed at runtime |

All exceptions inherit from `WatcherError` so you can catch the entire family with one clause.

---

### `HandlerBase`

| Method | Called when |
|---|---|
| `on_session_start(session)` | A `session()` block opens |
| `on_step(step)` | A decorated function completes |
| `on_session_end(session)` | A `session()` block closes |

---

## Examples

```bash
python examples/basic_pipeline.py     # 1M-row e-commerce ETL with session summary
python examples/threshold_demo.py     # all four threshold modes demonstrated
```

---

## Development

```bash
git clone https://github.com/Abineshabee/watcher
cd watcher
pip install -e ".[dev]"
pytest tests/ -v --cov=watcher
```

CI runs on Python 3.10–3.13 across Ubuntu, Windows, and macOS on every push.

---

## Roadmap

- Polars backend
- DuckDB backend
- Notebook / HTML renderer
- JSON handler for structured logging pipelines
- `watcher.config` — global defaults without decorator arguments

---

## License

MIT — see [LICENSE](LICENSE).
