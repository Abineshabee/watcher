# watcher — Technical Reference

> Version: current · License: MIT · Python: 3.10–3.13

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Installation](#installation)
3. [Core Module — `watcher.core`](#core-module)
   - [The `@watch` Decorator](#the-watch-decorator)
   - [`StepResult`](#stepresult)
   - [`WatcherSession` and `session()`](#watchersession-and-session)
   - [`MemoryMode`](#memorymode)
   - [`DataFrameLike` Protocol](#dataframelike-protocol)
   - [`BackendRegistry` and `BackendAdapter`](#backendregistry-and-backendadapter)
4. [Stats Engine — `watcher.stats`](#stats-engine)
   - [`StepStats`](#stepstats)
   - [`ColumnDiff`](#columndiff)
   - [`DtypeChange`](#dtypechange)
   - [`NullDelta`](#nulldelta)
   - [`JoinExplosionDetail`](#joinexplosiondetail)
   - [`compute_stats()`](#compute_stats)
   - [Join Explosion Detection — How It Works](#join-explosion-detection--how-it-works)
   - [Large-DataFrame Sampling](#large-dataframe-sampling)
5. [Handler Layer — `watcher.handlers`](#handler-layer)
   - [`HandlerBase`](#handlerbase)
   - [`TerminalHandler`](#terminalhandler)
   - [`register_handler()` and `deregister_handler()`](#register_handler-and-deregister_handler)
   - [Writing a Custom Handler](#writing-a-custom-handler)
6. [Reporter — `watcher.reporter`](#reporter)
   - [`Reporter`](#reporter-class)
   - [Rich vs. Plain-text Output](#rich-vs-plain-text-output)
7. [Exceptions — `watcher.exceptions`](#exceptions)
   - [Exception Hierarchy](#exception-hierarchy)
   - [`ThresholdExceeded`](#thresholdexceeded)
   - [`BackendError`](#backenderror)
   - [`ConfigurationError`](#configurationerror)
   - [`WatcherWarning`](#watcherwarning)
8. [Threshold Guards In Depth](#threshold-guards-in-depth)
9. [Memory Tracking In Depth](#memory-tracking-in-depth)
10. [Session Grouping In Depth](#session-grouping-in-depth)
11. [Extending watcher — Adding a Backend](#extending-watcher--adding-a-backend)
12. [Thread and Async Safety](#thread-and-async-safety)
13. [Performance Characteristics](#performance-characteristics)
14. [Public API Surface at a Glance](#public-api-surface-at-a-glance)

---

## Architecture Overview

watcher is structured as four decoupled layers. Each layer has a single responsibility and communicates with the others through narrow, well-defined interfaces.

```
  Your pipeline functions
         │
         ▼
  ┌─────────────────────────────────────────────────────┐
  │  core.py  — @watch decorator + WatcherSession       │
  │  Orchestrates execution, timing, and event dispatch │
  └──────┬──────────────────────┬───────────────────────┘
         │                      │
         ▼                      ▼
  ┌─────────────┐      ┌────────────────────┐
  │  stats.py   │      │   handlers.py      │
  │  Stats      │      │   Event dispatch   │
  │  engine:    │      │   to registered    │
  │  nulls,     │      │   listeners        │
  │  dtypes,    │      └────────┬───────────┘
  │  schema,    │               │
  │  joins      │               ▼
  └─────────────┘      ┌────────────────────┐
                       │   reporter.py      │
                       │   Terminal render  │
                       │   via Rich or      │
                       │   plain-text       │
                       └────────────────────┘
```

**Data flow for one decorated call:**

1. `@watch` captures the input DataFrame's row count, columns, and nulls.
2. The function executes inside a memory-measurement context.
3. `compute_stats()` compares the before/after DataFrames — producing nulls, dtype changes, schema drift, and join-explosion signals.
4. A `StepResult` is constructed from all gathered data.
5. Threshold guards fire (warn or raise) if configured.
6. The `StepResult` is dispatched to every registered handler.
7. If a `session()` context is active, the result is also accumulated there.

---

## Installation

```bash
# Core only (pandas required separately)
pip install watcher

# + coloured terminal output
pip install "watcher[rich]"

# + Rich + psutil (full RSS memory tracking)
pip install "watcher[full]"
```

**Dependency matrix:**

| Feature | Requires |
|---|---|
| Core row tracking, schema drift, nulls | `pandas` |
| Coloured terminal tables | `rich` |
| RSS memory tracking (`track_memory="rss"`) | `psutil` |
| Python-heap peak tracking | `tracemalloc` (stdlib) |

When `rich` is absent, watcher falls back to plain-text output automatically. When `psutil` is absent and `track_memory="rss"` is requested, watcher emits a one-time `WatcherWarning` and falls back to `"peak"` mode.

---

## Core Module

**Source:** `watcher/core.py`

The core module contains the `@watch` decorator, the `WatcherSession` session accumulator, `StepResult`, and all internal helpers for memory measurement, threshold enforcement, and backend validation.

### The `@watch` Decorator

```python
@watch(
    func         = None,
    *,
    warn_on_gain : float | None = None,
    warn_on_loss : float | None = None,
    raise_on_gain: float | None = None,
    raise_on_loss: float | None = None,
    label        : str | None = None,
    track_memory : bool | str | MemoryMode = MemoryMode.RSS,
    verbose      : bool = True,
)
```

The decorator instruments any function whose first argument is a DataFrame and whose return value is a DataFrame. It can be used bare or with arguments:

```python
# Bare — no parentheses
@watch
def clean(df):
    return df.dropna()

# With arguments — parentheses required
@watch(warn_on_loss=0.05, track_memory="rss")
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `warn_on_gain` | `float \| None` | `None` | Fractional row-gain threshold for a soft `WatcherWarning`. E.g. `0.10` = warn if > 10% rows gained. |
| `warn_on_loss` | `float \| None` | `None` | Fractional row-loss threshold for a soft `WatcherWarning`. E.g. `0.05` = warn if > 5% rows lost. |
| `raise_on_gain` | `float \| None` | `None` | Fractional gain threshold for a hard `ThresholdExceeded` exception. |
| `raise_on_loss` | `float \| None` | `None` | Fractional loss threshold for a hard `ThresholdExceeded` exception. |
| `label` | `str \| None` | `None` | Overrides the function name shown in all reports. Useful for generic helpers used across multiple steps. |
| `track_memory` | `bool \| str \| MemoryMode` | `MemoryMode.RSS` | Memory measurement strategy. Accepts `"rss"`, `"peak"`, `"off"`, or boolean aliases. |
| `verbose` | `bool` | `True` | When `False`, the step is silently tracked (accumulated into an active session and dispatched to handlers) but produces no terminal output. |

**What is measured on every call:**

- Row count before and after, signed diff, percentage change, and wall-clock time.
- Columns added or removed (schema drift).
- Columns whose dtype changed (widening or narrowing).
- Per-column null count deltas, sorted by magnitude.
- Join explosion: whether rows were gained due to a fan-out from a non-unique join key, and which key values caused it.
- Memory delta in MB, according to the selected `MemoryMode`.

**Return value:** The decorated function's return value is passed through unchanged. The wrapper is transparent to the rest of the pipeline.

**Raises:**

- `TypeError` — if the first argument or return value does not satisfy `DataFrameLike`.
- `ThresholdExceeded` — if a `raise_on_*` threshold is breached.

---

### `StepResult`

```python
@dataclass
class StepResult:
    func_name       : str
    rows_in         : int
    rows_out        : int
    elapsed_s       : float
    memory_delta_mb : float
    memory_mode     : MemoryMode
    stats           : StepStats
    warned          : bool = False
```

`StepResult` is an immutable snapshot of one decorated function's execution. It is the primary data object passed to every handler's `on_step()` method and stored in `WatcherSession.steps`.

**Computed properties (read-only):**

| Property | Type | Description |
|---|---|---|
| `row_diff` | `int` | `rows_out - rows_in`. Positive = gained, negative = lost. |
| `row_diff_pct` | `float` | `row_diff / rows_in`. Returns `0.0` when `rows_in == 0`. |
| `gained_rows` | `bool` | `True` when `row_diff > 0`. |
| `lost_rows` | `bool` | `True` when `row_diff < 0`. |
| `is_join_explosion` | `bool` | `True` when a join fan-out is suspected. Uses `stats.duplicate_key_detected` as the primary signal, falling back to a 50% row-gain heuristic. |

**`is_join_explosion` heuristic in detail:**

The property uses two independent signals to avoid false positives from legitimate row-expansion operations like `df.explode()` or time-series resampling:

1. `stats.duplicate_key_detected` — set by the stats engine when a candidate join-key column shows increased value repetition in the output. This is the primary signal.
2. If the stats engine did not flag a specific key but row gain exceeds 50% (`_JOIN_EXPLOSION_GAIN_THRESHOLD`), the explosion flag is still set as a fallback.

Combining both signals means a `df.explode()` that gains 200% rows without duplicating key columns will not be misreported as a join explosion.

---

### `WatcherSession` and `session()`

```python
@contextmanager
def session(name: str) -> Iterator[WatcherSession]:
    ...
```

Groups multiple `@watch` steps into a named pipeline run. When the `with` block closes, all registered handlers receive `on_session_end` with the accumulated session object, which prints a summary table.

```python
with session("nightly ETL") as s:
    df = clean(df)
    df = merge_orders(df)
    df = filter_active(df)

result = s.summary()
```

**`WatcherSession` attributes:**

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | The label passed to `session()`. |
| `steps` | `list[StepResult]` | All steps recorded while this session was active. |

**`WatcherSession.summary()` return value:**

```python
{
    "name": str,                    # session name
    "total_steps": int,             # number of decorated calls
    "total_rows_in": int,           # rows_in of the first step
    "total_rows_out": int,          # rows_out of the last step
    "total_rows_lost": int,         # sum of all negative row_diffs
    "total_rows_gained": int,       # sum of all positive row_diffs
    "total_elapsed_s": float,       # sum of all elapsed_s (4 decimal places)
    "total_memory_delta_mb": float, # sum of all memory_delta_mb
    "steps": [                      # per-step records
        {
            "func": str,
            "rows_in": int,
            "rows_out": int,
            "diff": int,
            "diff_pct": float,       # percentage points, e.g. -29.3
            "elapsed_s": float,
            "memory_delta_mb": float,
            "memory_mode": str,      # "rss", "peak", or "off"
            "join_explosion": bool,
            "warned": bool,
        },
        ...
    ]
}
```

**Thread and async safety:** The active session is stored in a `contextvars.ContextVar`, so each thread and each asyncio `Task` maintains its own session independently. Concurrent pipelines do not interfere.

---

### `MemoryMode`

```python
class MemoryMode(str, Enum):
    RSS  = "rss"
    PEAK = "peak"
    OFF  = "off"
```

Controls how memory is measured during a decorated call.

| Mode | Mechanism | Captures | Dependency |
|---|---|---|---|
| `RSS` | `psutil.Process().memory_info().rss` delta | NumPy, pandas, Arrow, Polars C-heap allocations | `psutil` |
| `PEAK` | `tracemalloc` Python-heap peak | Python-object allocations only | stdlib |
| `OFF` | Disabled | Nothing — zero overhead | None |

**Boolean aliases** are accepted for backwards compatibility and convenience:

```python
@watch(track_memory=True)   # → MemoryMode.RSS
@watch(track_memory=False)  # → MemoryMode.OFF
```

**Fallback behaviour:** When `RSS` is requested but `psutil` is not installed, watcher automatically falls back to `PEAK` and emits a single `WatcherWarning` (not repeated on subsequent calls).

---

### `DataFrameLike` Protocol

```python
@runtime_checkable
class DataFrameLike(Protocol):
    @property
    def shape(self) -> tuple[int, ...]: ...

    @property
    def columns(self) -> Any: ...
```

A structural protocol that any supported DataFrame must satisfy. The core engine never imports a concrete DataFrame type directly — it uses this protocol to stay backend-agnostic.

Any object with `.shape` and `.columns` satisfies the protocol at the `isinstance()` check level. Richer runtime introspection (null counts, dtypes, memory) is handled by registered `BackendAdapter` implementations.

If a decorated function receives or returns an object that does not satisfy `DataFrameLike`, watcher raises a `TypeError` with a human-readable message listing supported and planned backends.

---

### `BackendRegistry` and `BackendAdapter`

```python
class BackendRegistry:
    @classmethod
    def register(cls, adapter: type[BackendAdapter]) -> None: ...

    @classmethod
    def detect(cls, obj: Any) -> type[BackendAdapter] | None: ...
```

`BackendRegistry` maps DataFrame objects to the adapter that knows how to introspect them. Adapters are checked in registration order; the first adapter whose `accepts()` method returns `True` wins.

```python
class BackendAdapter(Protocol):
    @staticmethod
    def accepts(obj: Any) -> bool: ...

    @staticmethod
    def row_count(obj: Any) -> int: ...

    @staticmethod
    def column_names(obj: Any) -> list[str]: ...
```

This is a `Protocol` — you implement the methods rather than subclassing. The pandas adapter (`_PandasStatsBackend`) is registered automatically when the library is imported:

```python
# At the bottom of core.py:
BackendRegistry.register(_PandasStatsBackend)
```

When Polars and DuckDB backends ship, their modules will call `BackendRegistry.register()` at import time. Nothing in `core.py` will change.

---

## Stats Engine

**Source:** `watcher/stats.py`

The stats engine is the analytical core of watcher. It compares before/after DataFrames and extracts everything the reporter needs to display: null changes, dtype shifts, schema drift, and join-explosion diagnostics.

**Design principles:**

- No pandas import at module level — pandas is imported inside methods, so the module is importable in Polars-only environments.
- Sampling is applied first for large DataFrames to keep overhead predictable.
- All public types are plain frozen dataclasses — no pandas objects leak into the public API.
- Stats computation failures are caught and return an empty sentinel (`_EMPTY_STATS`) with a `WatcherWarning` rather than crashing the pipeline.

---

### `StepStats`

```python
@dataclass
class StepStats:
    column_diff            : ColumnDiff
    dtype_changes          : list[DtypeChange]
    null_deltas            : list[NullDelta]
    duplicate_key_detected : bool
    join_explosion         : JoinExplosionDetail
    sampled                : bool = False
    sample_size            : int  = 0
    backend                : str  = "unknown"
```

The top-level container for all statistical output. Stored on every `StepResult.stats`. The `duplicate_key_detected` flag is a shortcut that `StepResult.is_join_explosion` reads without needing to inspect the full `join_explosion` object.

---

### `ColumnDiff`

```python
@dataclass(frozen=True)
class ColumnDiff:
    added  : list[str]   # columns present in output but not input
    removed: list[str]   # columns present in input but not output

    @property
    def has_drift(self) -> bool: ...  # True when added or removed is non-empty
```

Schema drift summary. The reporter shows added columns in magenta and removed columns in red. Dtype changes are also reported in this block for visual grouping.

---

### `DtypeChange`

```python
@dataclass(frozen=True)
class DtypeChange:
    column     : str
    before     : str   # e.g. "int64"
    after      : str   # e.g. "object"
    is_widening: bool  # True for safe promotions, False for narrowing/coercions
```

A dtype change on a single column. `is_widening` is determined by a lookup table of known-safe promotions:

```
int8  → int16, int32, int64
int16 → int32, int64
int32 → int64
float32 → float64
uint8  → uint16, uint32, uint64
uint16 → uint32, uint64
uint32 → uint64
```

Any transition not in this table — including `float64 → object`, `int64 → str`, `datetime64 → object` — is classified as narrowing (`is_widening=False`) and displayed in yellow as a potential data quality concern.

---

### `NullDelta`

```python
@dataclass(frozen=True)
class NullDelta:
    column: str
    before: int   # null count before the step
    after : int   # null count after the step
    delta : int   # signed difference (positive = more nulls introduced)
```

Null count change on a single column. The stats engine only includes columns where `delta != 0`, and sorts results by `abs(delta)` descending so the worst offenders appear first. The reporter shows the top 5.

---

### `JoinExplosionDetail`

```python
@dataclass(frozen=True)
class JoinExplosionDetail:
    duplicate_key_detected: bool
    offending_columns     : list[str]
    top_offenders         : dict[str, list[tuple[Any, int]]]
    duplication_ratio     : float
```

Full diagnostic when a join fan-out is detected.

| Attribute | Description |
|---|---|
| `duplicate_key_detected` | `True` when at least one likely join key shows increased value repetition. |
| `offending_columns` | Column names where duplication was detected, sorted by worst first. |
| `top_offenders` | For each offending column: the top 5 values and their repeat counts. E.g. `{"customer_id": [(9182, 184), (3310, 97)]}`. |
| `duplication_ratio` | `(rows_out - rows_in) / rows_in` — raw fan-out fraction. |

---

### `compute_stats()`

```python
def compute_stats(before: Any, after: Any) -> StepStats:
```

The single public entry point of the stats engine. Called by `@watch` after every decorated function with the before/after DataFrames.

**Internally, it:**

1. Detects the backend via `BackendRegistry`.
2. Delegates to the appropriate `_StatsBackend` implementation.
3. Catches any exception from the backend, emits a `WatcherWarning`, and returns `_EMPTY_STATS`.

The pandas backend (`_PandasStatsBackend`) executes these four sub-steps:

1. **Schema diff** — identifies added and removed columns using set operations on column names.
2. **Dtype changes** — compares `dtype` strings for columns present in both DataFrames.
3. **Null deltas** — computes `isna().sum()` on shared columns before and after.
4. **Join explosion** — only runs when `rows_out > rows_in`, to avoid unnecessary computation.

---

### Join Explosion Detection — How It Works

The join explosion detector is the most sophisticated part of the stats engine. Its goal is to tell users not just that rows increased, but *why* and *which specific values* caused it.

**Step 1 — Identify candidate key columns**

A column is considered a candidate join key if:
- Its name ends with a known suffix: `_id`, `_key`, `_code`, `id`, or `key` (case-insensitive), **or**
- Its dtype is integer (likely a surrogate key).

**Step 2 — Compare value-count distributions**

For each candidate column, the maximum repeat count is computed before and after the step using `value_counts().max()`. If the after max is greater than the before max and greater than 1, the column is flagged as offending.

**Step 3 — Collect top offenders**

For each offending column, the top 5 values by repeat count in the output are recorded via `value_counts().head(5)`.

**Step 4 — Sort by severity**

Offending columns are sorted by their top repeat count, worst first, so the primary cause always appears at the top of the reporter table.

**Why this heuristic is reliable:**

- It checks whether duplication *increased* (not just exists), avoiding false positives on columns that were already duplicated before the step.
- It works at the value level, not just the count level — so it can identify `customer_id=9182` as the specific culprit rather than just saying "there are duplicates."
- Columns that cannot be value-counted (unhashable types) are skipped silently without crashing.

---

### Large-DataFrame Sampling

For DataFrames above 50,000 rows (`_DEFAULT_SAMPLE_SIZE`), the stats engine computes null counts and dtype changes on a random sample rather than the full DataFrame. This keeps column-stat overhead predictable in production pipelines.

Join explosion detection always uses the full DataFrame (not the sample) because it needs exact value counts to produce accurate top-offender data.

When sampling is active, `StepStats.sampled` is `True` and `StepStats.sample_size` reflects the number of rows used. The terminal reporter notes this as a footnote so users know the column stats are approximate.

---

## Handler Layer

**Source:** `watcher/handlers.py`

The handler layer is the event-dispatch system. `core.py` never prints anything directly — it fires events on every registered handler after each step and at session boundaries. This decouples the engine from any one output target.

---

### `HandlerBase`

```python
class HandlerBase:
    def on_session_start(self, session: WatcherSession) -> None: ...
    def on_step(self, step: StepResult) -> None: ...
    def on_session_end(self, session: WatcherSession) -> None: ...
```

The base class every handler must implement. All three methods have no-op defaults, so a handler only needs to override the events it cares about.

| Event | When fired |
|---|---|
| `on_session_start` | When a `session()` context manager opens. |
| `on_step` | After every decorated function call (only when `verbose=True`). |
| `on_session_end` | When a `session()` context manager closes. |

---

### `TerminalHandler`

```python
class TerminalHandler(HandlerBase):
    def on_session_start(self, session): ...   # prints session header rule
    def on_step(self, step): ...               # prints step line + details
    def on_session_end(self, session): ...     # prints summary table panel
```

The default handler, installed automatically at library import. It delegates to `reporter.Reporter` for all rendering. Replacing it is straightforward:

```python
from watcher.handlers import _get_handlers, TerminalHandler

# Remove the default terminal handler
handlers = _get_handlers()
handlers.clear()

# Install your own instead
register_handler(MyCustomHandler())
```

---

### `register_handler()` and `deregister_handler()`

```python
def register_handler(handler: HandlerBase) -> None: ...
def deregister_handler(handler: HandlerBase) -> None: ...
```

Manage the process-global handler list. `register_handler` is idempotent — registering the same instance twice has no effect. `deregister_handler` is a no-op if the handler was never registered.

Multiple handlers can be registered simultaneously. All receive events in registration order.

---

### Writing a Custom Handler

A handler receives a `StepResult` after every decorated call. Here is a complete example that writes structured JSON logs:

```python
import json
from watcher import register_handler, deregister_handler
from watcher.handlers import HandlerBase
from watcher.core import StepResult, WatcherSession

class JSONLogHandler(HandlerBase):
    def __init__(self):
        self.log = []

    def on_step(self, step: StepResult) -> None:
        self.log.append({
            "step"    : step.func_name,
            "rows_in" : step.rows_in,
            "rows_out": step.rows_out,
            "diff"    : step.row_diff,
            "diff_pct": round(step.row_diff_pct * 100, 2),
            "ms"      : round(step.elapsed_s * 1000, 2),
            "mem_mb"  : step.memory_delta_mb,
            "exploded": step.is_join_explosion,
            "warned"  : step.warned,
        })

    def on_session_end(self, session: WatcherSession) -> None:
        print(json.dumps({"session": session.name, "steps": self.log}, indent=2))

handler = JSONLogHandler()
register_handler(handler)

# ... run your pipeline ...

deregister_handler(handler)
```

**Other use-cases for custom handlers:**

- Sending Slack/Teams alerts when `step.is_join_explosion` or `step.warned` is True.
- Emitting Prometheus metrics for row counts and timing.
- Writing machine-readable CI artefacts (JSON, NDJSON) for downstream assertion checks.
- Suppressing terminal output entirely while retaining session accumulation.

---

## Reporter

**Source:** `watcher/reporter.py`

The reporter is the rendering layer — it formats `StepResult` and `WatcherSession` objects into terminal output. It is invoked by `TerminalHandler` and has no dependency on `core.py`, making it independently replaceable.

---

### `Reporter` Class

```python
class Reporter:
    def __init__(
        self,
        width          : int  = 100,
        show_memory    : bool = True,
        show_null_deltas: bool = True,
        show_schema_drift: bool = True,
        show_join_detail: bool = True,
    ): ...

    def print_step(self, step: StepResult) -> None: ...
    def print_session_header(self, name: str) -> None: ...
    def print_session_footer(self, session: WatcherSession) -> None: ...
```

| Parameter | Default | Description |
|---|---|---|
| `width` | `100` | Maximum terminal width for Rich output. |
| `show_memory` | `True` | Include the memory-delta column in step output. |
| `show_null_deltas` | `True` | Show per-column null count changes. |
| `show_schema_drift` | `True` | Show added/removed columns and dtype changes. |
| `show_join_detail` | `True` | Show the join-explosion offender table. |

**Visual symbols used in step output:**

| Symbol | Meaning |
|---|---|
| `▲` | Rows gained |
| `▼` | Rows lost |
| `●` | No row change |
| `⚠` | Threshold warning triggered |
| `💥` | Join explosion detected |

---

### Rich vs. Plain-text Output

The reporter checks for Rich at import time. When Rich is available, it uses `Console`, `Panel`, `Table`, and `Text` for coloured, aligned output. When Rich is absent, a `_PlainConsole` shim strips Rich markup tags and writes to `stdout` with plain text and Unicode box-drawing characters.

**Rich colour scheme:**

| Element | Colour |
|---|---|
| Row gain | green |
| Row loss | red |
| No change | dim |
| Threshold warning | yellow |
| Join explosion | bold red |
| Session header | bold cyan |
| Schema drift (added) | magenta |
| Schema drift (removed) | red |

The join explosion detail is rendered as a Rich `Table` showing up to 3 offending columns and 3 top values per column. The session footer is rendered as a Rich `Panel` with a per-step table and a total row in the subtitle.

---

## Exceptions

**Source:** `watcher/exceptions.py`

---

### Exception Hierarchy

```
WatcherError (Exception)
├── ThresholdExceeded
├── BackendError
└── ConfigurationError

WatcherWarning (UserWarning)
```

Catching `WatcherError` handles every hard failure the library can raise. Catching `WatcherWarning` via `warnings.filterwarnings` handles all soft threshold breaches.

---

### `ThresholdExceeded`

```python
class ThresholdExceeded(WatcherError):
    message: str
```

Raised when a `raise_on_gain` or `raise_on_loss` threshold is breached. The `message` attribute contains a structured description:

```
[watcher] 'massive_drop' breached raise_on_loss=10.0%.
  Rows : 1,000 → 50  (-950 rows, -95.0%)
  Memory: +0.0 MB (rss)
```

When a join explosion contributed to a gain threshold breach, the message includes an additional hint:

```
  Likely cause: non-unique join key causing fan-out.
  Run stats.join_explosion_detail for offending columns and duplication counts.
```

**Catching in CI:**

```python
from watcher.exceptions import ThresholdExceeded

try:
    result = pipeline(df)
except ThresholdExceeded as exc:
    logger.error("Data contract violated: %s", exc)
    raise SystemExit(1)
```

---

### `BackendError`

```python
class BackendError(WatcherError):
    message      : str
    backend_name : str  = "unknown"
    original     : Exception | None = None
```

Raised when a backend adapter fails during runtime introspection. The `__str__` output includes the backend name and the underlying exception:

```
[watcher/pandas] null_counts() failed on step 'clean'
  Caused by: AttributeError: 'NoneType' object has no attribute 'isna'
```

---

### `ConfigurationError`

```python
class ConfigurationError(WatcherError): ...
```

Raised at decoration time for invalid arguments — for example, a `track_memory` string that is not a valid `MemoryMode`, or a threshold value outside `[0.0, 1.0]`.

---

### `WatcherWarning`

```python
class WatcherWarning(UserWarning): ...
```

Issued via `warnings.warn()` (not raised) for soft threshold breaches (`warn_on_gain`, `warn_on_loss`) and for the psutil fallback. The pipeline continues executing after a `WatcherWarning`.

**Control warnings in testing:**

```python
import warnings

# Treat all watcher warnings as errors (useful in CI)
warnings.filterwarnings("error", category=WatcherWarning)

# Suppress watcher warnings entirely
warnings.filterwarnings("ignore", category=WatcherWarning)
```

---

## Threshold Guards In Depth

Threshold guards turn `@watch` into a data contract enforcer. They work at the step level — each decorated function can have its own independent thresholds.

**Evaluation order:**

Within a single step, `raise_on_*` is always checked before `warn_on_*` so the harder constraint takes precedence. Both gain and loss conditions are checked independently — both can fire in the same call if the step both gains and then loses rows (though this is rare with standard DataFrame operations).

**Threshold values** are fractions of the input row count (`0.0`–`1.0`):

```python
@watch(
    warn_on_loss=0.05,    # warn  if > 5%  rows lost
    raise_on_loss=0.20,   # raise if > 20% rows lost
    warn_on_gain=0.10,    # warn  if > 10% rows gained
    raise_on_gain=1.00,   # raise if rows more than double (> 100% gain)
)
def merge_orders(df):
    return df.merge(orders, on="customer_id", how="left")
```

**CI pipeline pattern:**

```python
from watcher.exceptions import ThresholdExceeded, WatcherWarning
import warnings

# Escalate soft warnings to test failures
warnings.filterwarnings("error", category=WatcherWarning)

def run_nightly(df):
    with session("nightly") as s:
        df = clean(df)
        df = merge_orders(df)
        df = filter_active(df)
    
    summary = s.summary()
    assert summary["total_rows_out"] > 500_000, \
        f"Too few rows: {summary['total_rows_out']:,}"
    return df
```

---

## Memory Tracking In Depth

watcher supports three memory measurement strategies with different trade-offs.

**RSS mode (recommended):**

```python
@watch(track_memory="rss")
def big_merge(df):
    return df.merge(large_table, on="id", how="left")
```

Measures `psutil.Process(os.getpid()).memory_info().rss` before and after the call. RSS (Resident Set Size) captures all physical memory used by the process, including NumPy arrays, pandas internals, and any Arrow or Polars buffers allocated in C. This is the most accurate measure for real-world pandas workloads.

**Peak mode (no psutil dependency):**

```python
@watch(track_memory="peak")
def pure_python_transform(df):
    return df.assign(new_col=df["x"].apply(some_fn))
```

Uses `tracemalloc` to record the Python-heap peak allocation during the call. This only captures Python-managed objects and misses C-level allocations, so it tends to undercount for NumPy-heavy operations. Useful in environments where psutil cannot be installed.

**Off mode (zero overhead):**

```python
@watch(track_memory="off")
def high_frequency_transform(df):
    return df.rename(columns={"old": "new"})
```

Disables all memory measurement. The `memory_delta_mb` field in `StepResult` will always be `0.0`. Use this in production hot paths where the measurement cost (two RSS reads per call) is unacceptable.

**Memory delta interpretation:**

The reported delta is `RSS_after - RSS_before`. It can be negative if the garbage collector reclaimed memory during the function's execution. A large positive delta on a step that should not allocate much memory is a useful signal that something unexpected is happening inside the function.

---

## Session Grouping In Depth

Sessions group related steps into a single named pipeline run and produce a summary table at the end. They are optional — `@watch` works perfectly without a session context.

**What sessions enable that standalone `@watch` does not:**

- A summary table showing all steps in one view with totals.
- A machine-readable `summary()` dict for programmatic assertions.
- `verbose=False` steps that produce no per-step output but still appear in the session summary.

**Nested sessions are not supported.** Opening a `session()` inside another `session()` block will silently replace the inner context, and the inner steps will be accumulated into the outer session. This behaviour may become a `ConfigurationError` in a future version.

**`verbose=False` pattern for clean CI output:**

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

# No per-step output printed above.
# The session summary table prints on __exit__ and shows both steps.

result = s.summary()
assert result["total_rows_out"] > 0
```

---

## Extending watcher — Adding a Backend

The backend system is designed for adding new DataFrame engines (Polars, DuckDB, cuDF, Modin) without modifying core library code.

**Step 1 — Implement `BackendAdapter`:**

```python
# watcher/backends/polars.py
from watcher.core import BackendAdapter, BackendRegistry

class PolarsBackendAdapter:
    @staticmethod
    def accepts(obj: Any) -> bool:
        try:
            import polars as pl
            return isinstance(obj, pl.DataFrame)
        except ImportError:
            return False

    @staticmethod
    def row_count(obj: Any) -> int:
        return obj.height

    @staticmethod
    def column_names(obj: Any) -> list[str]:
        return obj.columns
```

**Step 2 — Implement `_StatsBackend`** for column-level stats:

```python
class _PolarsStatsBackend:
    @staticmethod
    def accepts(obj: Any) -> bool:
        # same as adapter
        ...

    @staticmethod
    def compute(before: Any, after: Any, sample_size: int) -> StepStats:
        # compute column_diff, dtype_changes, null_deltas, join_explosion
        # using Polars APIs instead of pandas
        ...
```

**Step 3 — Register at import time:**

```python
BackendRegistry.register(PolarsBackendAdapter)
_BACKENDS.append(_PolarsStatsBackend)
```

Once registered, `@watch` will work transparently on Polars DataFrames with no changes to user code.

---

## Thread and Async Safety

watcher uses `contextvars.ContextVar` to store the active session, which gives thread safety and asyncio task isolation for free.

Each OS thread and each asyncio `Task` that calls `session()` gets its own slot in the context variable. Two concurrent pipelines running in different threads will not interfere with each other's session accumulation.

The global handler list (`_handlers`) is a plain Python list. It is written only by `register_handler` and `deregister_handler`, which are typically called at module level (not in concurrent code). If you need to mutate the handler list from multiple threads, add external locking.

---

## Performance Characteristics

| Operation | Typical overhead |
|---|---|
| Row count (`.shape[0]`) | < 1 µs — pandas attribute, no scan |
| Column diff (set operations) | < 50 µs for typical column counts |
| Null deltas (`isna().sum()`) on 50k-row sample | 1–5 ms |
| Null deltas on 1M-row full scan | 20–80 ms (skipped by sampling) |
| Join explosion detection | 5–30 ms depending on key cardinality |
| RSS memory read (`psutil`) | < 1 ms (two reads per call) |
| `tracemalloc` peak | 2–10 ms setup/teardown overhead |

The 50,000-row sampling threshold means that for large DataFrames, column stat overhead is bounded at roughly the same cost as scanning 50k rows regardless of actual DataFrame size. The trade-off is that null deltas and dtype changes on large DataFrames are approximate.

For high-frequency operations where even the sampling overhead is too much, use `track_memory="off"` and consider wrapping multiple micro-steps in a single `@watch`-decorated function that calls them internally.

---

## Public API Surface at a Glance

### Functions and context managers

| Symbol | Module | Description |
|---|---|---|
| `watch` | `watcher` | Decorator — instruments a DataFrame-transforming function. |
| `session(name)` | `watcher` | Context manager — groups steps into a named pipeline run. |
| `register_handler(h)` | `watcher` | Add a handler to the global event list. |
| `deregister_handler(h)` | `watcher` | Remove a handler from the global event list. |

### Classes

| Symbol | Module | Description |
|---|---|---|
| `StepResult` | `watcher` | Snapshot of one decorated function call. |
| `WatcherSession` | `watcher` | Accumulates steps within a `session()` block. |
| `MemoryMode` | `watcher` | Enum — `RSS`, `PEAK`, `OFF`. |
| `DataFrameLike` | `watcher` | Protocol — structural type check for DataFrame arguments. |
| `BackendRegistry` | `watcher` | Maps DataFrame objects to their backend adapter. |
| `BackendAdapter` | `watcher` | Protocol — interface for custom backend implementations. |
| `HandlerBase` | `watcher` | Base class for custom event handlers. |
| `TerminalHandler` | `watcher` | Default handler — renders to terminal via Rich. |
| `Reporter` | `watcher.reporter` | Formats step and session output. |
| `StepStats` | `watcher.stats` | Full statistical snapshot for one step. |
| `ColumnDiff` | `watcher.stats` | Added/removed columns between steps. |
| `DtypeChange` | `watcher.stats` | Dtype change on a single column. |
| `NullDelta` | `watcher.stats` | Null count change on a single column. |
| `JoinExplosionDetail` | `watcher.stats` | Diagnostic detail for a join fan-out. |

### Exceptions

| Symbol | Module | Kind | When raised |
|---|---|---|---|
| `WatcherError` | `watcher` | Exception | Root — catch for all hard failures. |
| `ThresholdExceeded` | `watcher` | Exception | `raise_on_*` threshold breached. |
| `BackendError` | `watcher` | Exception | Backend adapter failed at runtime. |
| `ConfigurationError` | `watcher` | Exception | Invalid decorator arguments at decoration time. |
| `WatcherWarning` | `watcher` | Warning | `warn_on_*` threshold breached, or psutil fallback. |
