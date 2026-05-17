# -----------------------------------------------------------------------------
# watcher/core.py
#
# The @watch decorator and pipeline session engine.
# Instruments DataFrame-transforming functions to surface row changes,
# schema drift, memory usage, and join explosion — automatically.
#
# License : MIT
# Docs    : https://github.com/Abineshabee/watcher
# -----------------------------------------------------------------------------

from __future__ import annotations

import contextvars
import functools
import os
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    Optional,
    Protocol,
    TypeVar,
    Union,
    runtime_checkable,
)

from watcher.exceptions import ThresholdExceeded, WatcherWarning
from watcher.handlers import HandlerBase, TerminalHandler, _get_handlers
from watcher.stats import StepStats, compute_stats, _PandasStatsBackend

# -----------------------------------------------------------------------------
# psutil — optional but strongly recommended for RSS memory tracking.
#
# tracemalloc only tracks Python-heap allocations and misses everything
# NumPy, pandas, Arrow, and Polars allocate in C.  psutil.Process.rss
# captures the full resident set size of the process, which is what
# users actually care about when a merge blows up memory.
#
# We import lazily so the library is installable without psutil, but we
# warn loudly at runtime when track_memory != "off" and psutil is absent.
# -----------------------------------------------------------------------------

try:
    import psutil as _psutil

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


# -----------------------------------------------------------------------------
# MemoryMode — controls how (and whether) memory is measured
# -----------------------------------------------------------------------------


class MemoryMode(str, Enum):
    """
    Controls the memory measurement strategy used by ``@watch``.

    Attributes
    ----------
    RSS : str
        Measure the process Resident Set Size (RSS) delta via
        ``psutil.Process().memory_info().rss``.  This captures NumPy,
        pandas, Arrow, and Polars allocations that live outside the
        Python heap.  **Recommended default.**
    PEAK : str
        Use ``tracemalloc`` to record peak Python-heap allocations
        during the call.  Misses C-level allocations but has zero
        external dependencies.  Useful for pure-Python transforms.
    OFF : str
        Disable memory tracking entirely.  Zero overhead.  Use in
        high-frequency or production pipelines where the measurement
        cost is unacceptable.

    Notes
    -----
    ``RSS`` requires ``psutil`` (``pip install psutil``).  If psutil is
    not installed and ``MemoryMode.RSS`` is requested, watcher falls
    back to ``PEAK`` and emits a one-time ``WatcherWarning``.
    """

    RSS = "rss"
    PEAK = "peak"
    OFF = "off"


# Sentinel so ``track_memory=True`` still works as a convenience alias
_BOOL_TO_MODE: dict[bool, MemoryMode] = {
    True: MemoryMode.RSS,
    False: MemoryMode.OFF,
}

# One-time warning flag so we don't spam on every call
_PSUTIL_WARNING_EMITTED = False

# -----------------------------------------------------------------------------
# DataFrame protocol — backend-agnostic foundation
# -----------------------------------------------------------------------------


@runtime_checkable
class DataFrameLike(Protocol):
    """
    Structural protocol that any tabular DataFrame must satisfy.

    Watcher never imports a concrete DataFrame type at the module level.
    Backends implement this protocol so the core engine stays decoupled
    from pandas, Polars, DuckDB, or any future engine.

    Anything with ``.shape`` and ``.columns`` satisfies the protocol at
    runtime.  Richer validation (null counts, dtypes, memory) is
    delegated to a :class:`BackendAdapter` looked up via
    :class:`BackendRegistry`.
    """

    @property
    def shape(self) -> tuple[int, ...]:
        """``(n_rows, n_cols)`` or equivalent."""
        ...

    @property
    def columns(self) -> Any:
        """Column labels — exact type varies by backend."""
        ...


# -----------------------------------------------------------------------------
# BackendRegistry — maps DataFrame types to their adapter
#
# The registry is the authoritative place to detect which backend an
# object belongs to.  compute_stats() delegates here instead of doing
# isinstance(df, pd.DataFrame) inline, keeping stats.py backend-agnostic.
# -----------------------------------------------------------------------------


class BackendRegistry:
    """
    Maps DataFrame-like objects to their :class:`BackendAdapter`.

    Backends register themselves at import time via
    :meth:`register`.  The registry is checked in registration order;
    the first matching adapter wins.

    This is intentionally simple now.  When Polars support ships,
    ``watcher/backends/polars.py`` calls ``BackendRegistry.register``
    and nothing in ``core.py`` changes.

    Example
    -------
    >>> from watcher.backends.pandas import PandasBackend
    >>> BackendRegistry.register(PandasBackend)
    """

    _adapters: list[type[BackendAdapter]] = []

    @classmethod
    def register(cls, adapter: type[BackendAdapter]) -> None:
        """
        Register a backend adapter class.

        Parameters
        ----------
        adapter : type[BackendAdapter]
            The adapter class to register.  Must implement
            :class:`BackendAdapter`.
        """
        if adapter not in cls._adapters:
            cls._adapters.append(adapter)

    @classmethod
    def detect(cls, obj: Any) -> type[BackendAdapter] | None:
        """
        Return the first registered adapter that claims *obj*, or ``None``.

        Parameters
        ----------
        obj : Any
            The object to inspect — typically a DataFrame returned by a
            decorated function.

        Returns
        -------
        type[BackendAdapter] or None
            The matching adapter class, or ``None`` if no adapter
            recognises *obj*.
        """
        for adapter in cls._adapters:
            if adapter.accepts(obj):
                return adapter
        return None


class BackendAdapter(Protocol):
    """
    Interface that every backend adapter must implement.

    Adapters translate backend-specific introspection calls (null counts,
    dtypes, memory estimates) into the neutral :class:`StepStats` format
    that the rest of watcher consumes.

    This is a :class:`~typing.Protocol` — you don't subclass it, you
    just implement the methods.
    """

    @staticmethod
    def accepts(obj: Any) -> bool:
        """Return ``True`` if this adapter handles *obj*."""
        ...

    @staticmethod
    def row_count(obj: Any) -> int:
        """Return the number of rows in *obj*."""
        ...

    @staticmethod
    def column_names(obj: Any) -> list[str]:
        """Return column names as a plain list of strings."""
        ...


# -----------------------------------------------------------------------------
# StepResult — one record per decorated function call
# -----------------------------------------------------------------------------


@dataclass
class StepResult:
    """
    Immutable snapshot of a single pipeline step's execution.

    Produced by ``@watch`` after each function call and accumulated by
    an active :class:`WatcherSession` when one is open.

    Attributes
    ----------
    func_name : str
        Name of the decorated function (or ``label`` override).
    rows_in : int
        Row count of the DataFrame passed into the function.
    rows_out : int
        Row count of the DataFrame returned by the function.
    elapsed_s : float
        Wall-clock execution time in seconds.
    memory_delta_mb : float
        Change in process RSS (or Python-heap peak, depending on
        :class:`MemoryMode`) during the call, in megabytes.
        Positive = more memory consumed; negative = released.
    memory_mode : MemoryMode
        Which measurement strategy produced ``memory_delta_mb``.
        Stored so the reporter can label the column correctly.
    stats : StepStats
        Column-level statistics: null counts, dtype changes, schema
        drift, and join-explosion signals.
    warned : bool
        ``True`` if any threshold warning was emitted for this step.
    """

    func_name: str
    rows_in: int
    rows_out: int
    elapsed_s: float
    memory_delta_mb: float
    memory_mode: MemoryMode
    stats: StepStats
    warned: bool = False

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def row_diff(self) -> int:
        """Signed row difference (positive = gained, negative = lost)."""
        return self.rows_out - self.rows_in

    @property
    def row_diff_pct(self) -> float:
        """
        Row change as a fraction of input size.

        Returns ``0.0`` when ``rows_in`` is zero to avoid
        ``ZeroDivisionError``.
        """
        if self.rows_in == 0:
            return 0.0
        return self.row_diff / self.rows_in

    @property
    def gained_rows(self) -> bool:
        """``True`` when the step produced *more* rows than it received."""
        return self.row_diff > 0

    @property
    def lost_rows(self) -> bool:
        """``True`` when the step produced *fewer* rows than it received."""
        return self.row_diff < 0

    @property
    def is_join_explosion(self) -> bool:
        """
        Heuristic: likely fan-out from a non-unique join key.

        We use two signals instead of a single > 100 % threshold:

        1. The step gained rows (``row_diff > 0``).
        2. The stats engine flagged duplicate join keys
           (``stats.duplicate_key_detected``), OR the gain exceeds
           ``JOIN_EXPLOSION_GAIN_THRESHOLD`` (default 50 %).

        This avoids false positives from legitimate operations like
        ``df.explode()``, time-series resampling, or feature expansion
        that also produce row gains, while still catching a disastrous
        +35 % gain caused by a bad merge on a 50 M-row table.

        The definitive diagnosis lives in
        ``stats.join_explosion_detail``.
        """
        if not self.gained_rows:
            return False
        # Duplicate key signal from stats engine takes priority
        if self.stats.duplicate_key_detected:
            return True
        # Fall back to a 50 % heuristic — still catches most explosions
        # without firing on deliberate row-expansion operations
        return self.row_diff_pct > _JOIN_EXPLOSION_GAIN_THRESHOLD


# Tunable — can be overridden at the session or global level in a future
# config layer without touching this dataclass.
_JOIN_EXPLOSION_GAIN_THRESHOLD: float = 0.50  # 50 % row gain


# -----------------------------------------------------------------------------
# WatcherSession — groups multiple @watch calls into one report
# -----------------------------------------------------------------------------


@dataclass
class WatcherSession:
    """
    Collects :class:`StepResult` objects across multiple decorated calls.

    Use :func:`session` as a context manager.  On exit, all registered
    handlers receive ``on_session_end`` and print or log their reports.

    Thread- and async-safety are provided by storing the active session
    in a ``contextvars.ContextVar``, so concurrent pipelines each see
    their own session.

    Attributes
    ----------
    name : str
        Human-readable label for this pipeline run.
    steps : list[StepResult]
        Ordered results, one per decorated step executed while this
        session was active.

    Example
    -------
    >>> with watcher.session("nightly ETL") as s:
    ...     df = clean(df)
    ...     df = merge_orders(df)
    ...     df = filter_active(df)
    ...
    ... print(s.summary())
    """

    name: str
    steps: List[StepResult] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Internal API
    # ------------------------------------------------------------------

    def record(self, result: StepResult) -> None:
        """
        Append a :class:`StepResult` to this session.

        Called automatically by ``@watch``.  Do not call directly.

        Parameters
        ----------
        result : StepResult
            The result produced by a single decorated function call.
        """
        self.steps.append(result)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """
        Return a machine-readable summary of the entire session.

        Useful for logging, CI assertions, or custom renderers.

        Returns
        -------
        dict
            Keys: ``name``, ``total_steps``, ``total_rows_in``,
            ``total_rows_out``, ``total_rows_lost``,
            ``total_rows_gained``, ``total_elapsed_s``,
            ``total_memory_delta_mb``, ``steps``.
        """
        return {
            "name": self.name,
            "total_steps": len(self.steps),
            "total_rows_in": self.steps[0].rows_in if self.steps else 0,
            "total_rows_out": self.steps[-1].rows_out if self.steps else 0,
            "total_rows_lost": sum(s.row_diff for s in self.steps if s.lost_rows),
            "total_rows_gained": sum(s.row_diff for s in self.steps if s.gained_rows),
            "total_elapsed_s": round(sum(s.elapsed_s for s in self.steps), 4),
            "total_memory_delta_mb": round(
                sum(s.memory_delta_mb for s in self.steps), 2
            ),
            "steps": [
                {
                    "func": s.func_name,
                    "rows_in": s.rows_in,
                    "rows_out": s.rows_out,
                    "diff": s.row_diff,
                    "diff_pct": round(s.row_diff_pct * 100, 2),
                    "elapsed_s": round(s.elapsed_s, 4),
                    "memory_delta_mb": round(s.memory_delta_mb, 2),
                    "memory_mode": s.memory_mode.value,
                    "join_explosion": s.is_join_explosion,
                    "warned": s.warned,
                }
                for s in self.steps
            ],
        }


# -----------------------------------------------------------------------------
# Context-local session registry
#
# ContextVar gives us thread-safety and asyncio-task isolation for free.
# Each thread / asyncio Task that calls session() gets its own slot.
# -----------------------------------------------------------------------------

_SESSION_VAR: contextvars.ContextVar[Optional[WatcherSession]] = (
    contextvars.ContextVar("watcher_active_session", default=None)
)


def _get_active_session() -> Optional[WatcherSession]:
    """
    Return the active :class:`WatcherSession` for this context, or ``None``.

    Internal helper used by ``@watch`` to decide whether to accumulate
    results into a session or fire them directly to handlers.
    """
    return _SESSION_VAR.get()


# -----------------------------------------------------------------------------
# session() — public context manager
# -----------------------------------------------------------------------------


@contextmanager
def session(name: str = "pipeline") -> Iterator[WatcherSession]:
    """
    Context manager that groups multiple ``@watch`` steps into one report.

    Opens a :class:`WatcherSession`, makes it active for the duration of
    the ``with`` block, then calls ``on_session_end`` on every registered
    handler when the block exits.

    Safe for threads and asyncio tasks — the session is stored in a
    ``ContextVar`` so concurrent pipelines do not interfere.

    Parameters
    ----------
    name : str, optional
        Label for this pipeline run.  Defaults to ``"pipeline"``.

    Yields
    ------
    WatcherSession
        The active session.  Call ``.summary()`` after the block for a
        machine-readable dict.

    Example
    -------
    >>> with watcher.session("user churn model — daily run") as s:
    ...     df = clean(df)
    ...     df = merge_orders(df)
    ...     df = filter_active(df)
    """
    sess = WatcherSession(name=name)
    token = _SESSION_VAR.set(sess)

    for handler in _get_handlers():
        handler.on_session_start(sess)

    try:
        yield sess
    finally:
        _SESSION_VAR.reset(token)
        for handler in _get_handlers():
            handler.on_session_end(sess)


# -----------------------------------------------------------------------------
# Memory measurement helpers
# -----------------------------------------------------------------------------


def _resolve_memory_mode(track_memory: Union[bool, str, MemoryMode]) -> MemoryMode:
    """
    Normalise the ``track_memory`` argument into a :class:`MemoryMode`.

    Accepts ``bool`` (legacy), ``str`` (``"rss"``, ``"peak"``, ``"off"``),
    or a :class:`MemoryMode` enum value directly.

    Parameters
    ----------
    track_memory : bool, str, or MemoryMode
        The value passed to ``@watch(track_memory=...)``.

    Returns
    -------
    MemoryMode
        The resolved mode.

    Raises
    ------
    ValueError
        If a string value is not a valid :class:`MemoryMode`.
    """
    if isinstance(track_memory, bool):
        return _BOOL_TO_MODE[track_memory]
    if isinstance(track_memory, MemoryMode):
        return track_memory
    try:
        return MemoryMode(track_memory.lower())
    except ValueError:
        valid = [m.value for m in MemoryMode]
        raise ValueError(
            f"[watcher] Invalid track_memory value {track_memory!r}. "
            f"Expected one of {valid} or a bool."
        )


def _rss_mb() -> float:
    """
    Return current process RSS in megabytes via psutil.

    This captures NumPy, pandas, Arrow, and Polars memory that lives
    outside the Python heap — which ``tracemalloc`` cannot see.

    Returns
    -------
    float
        RSS in megabytes.
    """
    return _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def _measure_memory(
    mode: MemoryMode,
    fn: Callable[[], Any],
) -> tuple[Any, float]:
    """
    Execute *fn* and measure memory consumption according to *mode*.

    Keeps all measurement logic in one place so the wrapper stays clean.

    Parameters
    ----------
    mode : MemoryMode
        Which strategy to use.
    fn : callable
        Zero-argument callable that executes the decorated function
        (already has ``df``, ``*args``, ``**kwargs`` in its closure).

    Returns
    -------
    result : Any
        The return value of ``fn()``.
    memory_delta_mb : float
        Memory change in megabytes.  Always ``0.0`` for ``OFF`` mode.
    """
    if mode == MemoryMode.OFF:
        return fn(), 0.0

    if mode == MemoryMode.RSS:
        before = _rss_mb()
        result = fn()
        after = _rss_mb()
        return result, round(after - before, 2)

    # MemoryMode.PEAK — tracemalloc Python-heap peak
    import tracemalloc

    tracemalloc.start()
    try:
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, round(peak / 1024 / 1024, 2)


def _ensure_psutil_available(mode: MemoryMode) -> MemoryMode:
    """
    Verify psutil is installed when RSS mode is requested.

    If psutil is missing, falls back to PEAK and emits a one-time
    ``WatcherWarning`` so users know their numbers are Python-heap only.

    Parameters
    ----------
    mode : MemoryMode
        The resolved mode before the check.

    Returns
    -------
    MemoryMode
        ``mode`` unchanged, or ``MemoryMode.PEAK`` if psutil is absent.
    """
    global _PSUTIL_WARNING_EMITTED

    if mode == MemoryMode.RSS and not _PSUTIL_AVAILABLE:
        if not _PSUTIL_WARNING_EMITTED:
            warnings.warn(
                "[watcher] track_memory='rss' requires psutil "
                "(pip install psutil).  Falling back to 'peak' "
                "(Python-heap only — NumPy/pandas/Arrow allocations "
                "will NOT be captured).",
                WatcherWarning,
                stacklevel=5,
            )
            _PSUTIL_WARNING_EMITTED = True
        return MemoryMode.PEAK

    return mode


# -----------------------------------------------------------------------------
# @watch — the public decorator
# -----------------------------------------------------------------------------


def watch(
    func: Optional[F] = None,
    *,
    warn_on_gain: ThresholdValue = None,
    warn_on_loss: ThresholdValue = None,
    raise_on_gain: ThresholdValue = None,
    raise_on_loss: ThresholdValue = None,
    label: Optional[str] = None,
    track_memory: Union[bool, str, MemoryMode] = MemoryMode.RSS,
    verbose: bool = True,
) -> Union[F, Callable[[F], F]]:
    """
    Decorator that instruments a DataFrame-transforming function.

    On every call, captures and reports:

    * **Row counts** — before and after, with signed diff and percentage.
    * **Schema drift** — columns added or removed between steps.
    * **Join explosion** — heuristic + key-level diagnosis when a merge
      fans out (uses duplicate-key signal from the stats engine, not
      just a raw row-count threshold).
    * **Memory delta** — real process RSS via psutil (captures NumPy,
      pandas, Arrow allocations), or Python-heap peak via tracemalloc,
      or disabled entirely.  See :class:`MemoryMode`.
    * **Column statistics** — null counts, dtype changes, value ranges.

    Output is emitted by registered *handlers* (terminal, notebook, JSON
    logger) rather than being printed directly from this function.  This
    decouples the engine from any one rendering target.

    Can be used with or without arguments::

        @watch
        def clean(df): ...

        @watch(warn_on_loss=0.05, track_memory="rss")
        def merge_orders(df): ...

    Parameters
    ----------
    func : callable, optional
        Supplied automatically when the decorator is used without
        parentheses (``@watch``).
    warn_on_gain : float, optional
        Emit a :class:`~watcher.exceptions.WatcherWarning` when rows
        gained exceed this fraction of input.  E.g. ``0.10`` = > 10 %.
    warn_on_loss : float, optional
        Emit a :class:`~watcher.exceptions.WatcherWarning` when rows
        lost exceed this fraction of input.  E.g. ``0.05`` = > 5 %.
    raise_on_gain : float, optional
        Raise :class:`~watcher.exceptions.ThresholdExceeded` (hard
        failure) when the gain threshold is breached.
    raise_on_loss : float, optional
        Raise :class:`~watcher.exceptions.ThresholdExceeded` when the
        loss threshold is breached.
    label : str, optional
        Override the function name shown in reports.
    track_memory : bool | str | MemoryMode, optional
        Memory measurement strategy.

        * ``"rss"`` / ``True`` — process RSS delta via psutil.
          Captures C-level allocations.  **Default.**
        * ``"peak"`` — Python-heap peak via ``tracemalloc``.
          No psutil dependency, but misses NumPy/pandas/Arrow memory.
        * ``"off"`` / ``False`` — disabled.  Zero overhead.
    verbose : bool, optional
        Forward step events to registered handlers (terminal output by
        default).  Set ``False`` for silent / machine-readable mode.
        Defaults to ``True``.

    Returns
    -------
    callable
        The wrapped function with an identical signature to the original.

    Raises
    ------
    TypeError
        If the first argument or return value is not
        :class:`DataFrameLike`.
    ThresholdExceeded
        If a ``raise_on_*`` threshold is breached.

    Example
    -------
    >>> @watch(warn_on_loss=0.05, raise_on_gain=1.0, track_memory="rss")
    ... def merge_orders(df: pd.DataFrame) -> pd.DataFrame:
    ...     return df.merge(orders, on="customer_id", how="left")
    """

    def decorator(fn: F) -> F:
        step_label = label or fn.__name__

        # Resolve and validate memory mode once at decoration time,
        # not on every call.  psutil availability is checked at first
        # call (lazy) so import-time warnings don't appear for OFF mode.
        resolved_mode = _resolve_memory_mode(track_memory)

        @functools.wraps(fn)
        def wrapper(df: Any, *args: Any, **kwargs: Any) -> Any:
            """
            Capture before-state, execute ``fn``, capture after-state,
            run diagnostics, dispatch events to handlers.
            """
            _assert_dataframe_like(df, step_label, position="input")

            rows_before = df.shape[0]

            # Validate and possibly downgrade memory mode (psutil check)
            effective_mode = _ensure_psutil_available(resolved_mode)

            # Build the zero-arg closure that measurement helpers call
            t_start = time.perf_counter()
            result, memory_delta_mb = _measure_memory(
                mode=effective_mode,
                fn=lambda: fn(df, *args, **kwargs),
            )
            elapsed = time.perf_counter() - t_start

            _assert_dataframe_like(result, step_label, position="output")

            rows_after = result.shape[0]

            # ---- Column-level stats + schema drift ----------------------
            stats = compute_stats(before=df, after=result)

            step = StepResult(
                func_name=step_label,
                rows_in=rows_before,
                rows_out=rows_after,
                elapsed_s=elapsed,
                memory_delta_mb=memory_delta_mb,
                memory_mode=effective_mode,
                stats=stats,
            )

            # ---- Threshold enforcement ----------------------------------
            warned = _check_thresholds(
                step=step,
                warn_on_gain=warn_on_gain,
                warn_on_loss=warn_on_loss,
                raise_on_gain=raise_on_gain,
                raise_on_loss=raise_on_loss,
            )
            step.warned = warned

            # ---- Dispatch to handlers (event-driven, not direct print) --
            if verbose:
                for handler in _get_handlers():
                    handler.on_step(step)

            active = _get_active_session()
            if active is not None:
                active.record(step)

            return result

        return wrapper  # type: ignore[return-value]

    # Support bare @watch (no parentheses)
    if func is not None:
        return decorator(func)
    return decorator


# -----------------------------------------------------------------------------
# Input validation — internal helper
# -----------------------------------------------------------------------------


def _assert_dataframe_like(obj: Any, func_name: str, position: str) -> None:
    """
    Raise ``TypeError`` if *obj* does not satisfy :class:`DataFrameLike`.

    Uses ``BackendRegistry.detect`` first (richer check), then falls
    back to the structural protocol check (``shape`` + ``columns``).

    Parameters
    ----------
    obj : Any
        The object to inspect.
    func_name : str
        Decorated function name — used in the error message.
    position : str
        ``"input"`` or ``"output"`` — used in the error message.

    Raises
    ------
    TypeError
        When *obj* is not recognised as a DataFrame-like object.
    """
    # Prefer registry detection (stricter) over bare protocol check
    if BackendRegistry.detect(obj) is not None:
        return
    if isinstance(obj, DataFrameLike):
        return

    raise TypeError(
        f"[watcher] '{func_name}' {position} must be a DataFrame-like object, "
        f"got {type(obj).__name__!r}.\n"
        f"  Supported now : pandas DataFrame.\n"
        f"  Planned       : Polars DataFrame, DuckDB Relation.\n"
        f"  To add a backend: implement BackendAdapter and call "
        f"BackendRegistry.register(YourAdapter)."
    )


# -----------------------------------------------------------------------------
# Threshold enforcement — internal helper
# -----------------------------------------------------------------------------


def _check_thresholds(
    step: StepResult,
    warn_on_gain: ThresholdValue,
    warn_on_loss: ThresholdValue,
    raise_on_gain: ThresholdValue,
    raise_on_loss: ThresholdValue,
) -> bool:
    """
    Evaluate row-change thresholds and emit warnings or raise exceptions.

    Checks gain and loss independently — both can fire in the same call.
    ``raise_on_*`` is always evaluated before ``warn_on_*`` so the
    harder constraint takes precedence.

    Parameters
    ----------
    step : StepResult
        The result of the current pipeline step.
    warn_on_gain : float or None
        Fractional gain threshold for :class:`~watcher.exceptions.WatcherWarning`.
    warn_on_loss : float or None
        Fractional loss threshold for :class:`~watcher.exceptions.WatcherWarning`.
    raise_on_gain : float or None
        Fractional gain threshold for :class:`~watcher.exceptions.ThresholdExceeded`.
    raise_on_loss : float or None
        Fractional loss threshold for :class:`~watcher.exceptions.ThresholdExceeded`.

    Returns
    -------
    bool
        ``True`` if any threshold condition was triggered.

    Raises
    ------
    ThresholdExceeded
        When a ``raise_on_*`` threshold is breached.
    """
    pct = abs(step.row_diff_pct)
    triggered = False

    # ---- Row gain -------------------------------------------------------
    if step.gained_rows:
        gain_detail = (
            f"  Rows : {step.rows_in:,} → {step.rows_out:,}  "
            f"(+{step.row_diff:,} rows, +{pct:.1%})\n"
            f"  Memory: {step.memory_delta_mb:+.1f} MB ({step.memory_mode.value})"
        )
        explosion_hint = (
            "\n  Likely cause: non-unique join key causing fan-out.\n"
            "  Run stats.join_explosion_detail for offending columns and "
            "duplication counts."
            if step.is_join_explosion
            else ""
        )

        if raise_on_gain is not None and pct > raise_on_gain:
            raise ThresholdExceeded(
                f"[watcher] '{step.func_name}' breached raise_on_gain="
                f"{raise_on_gain:.1%}.\n{gain_detail}{explosion_hint}"
            )
        if warn_on_gain is not None and pct > warn_on_gain:
            warnings.warn(
                f"[watcher] '{step.func_name}' exceeded warn_on_gain="
                f"{warn_on_gain:.1%}.\n{gain_detail}{explosion_hint}",
                WatcherWarning,
                stacklevel=4,
            )
            triggered = True

    # ---- Row loss -------------------------------------------------------
    if step.lost_rows:
        loss_detail = (
            f"  Rows : {step.rows_in:,} → {step.rows_out:,}  "
            f"(-{abs(step.row_diff):,} rows, -{pct:.1%})\n"
            f"  Memory: {step.memory_delta_mb:+.1f} MB ({step.memory_mode.value})"
        )

        if raise_on_loss is not None and pct > raise_on_loss:
            raise ThresholdExceeded(
                f"[watcher] '{step.func_name}' breached raise_on_loss="
                f"{raise_on_loss:.1%}.\n{loss_detail}"
            )
        if warn_on_loss is not None and pct > warn_on_loss:
            warnings.warn(
                f"[watcher] '{step.func_name}' exceeded warn_on_loss="
                f"{warn_on_loss:.1%}.\n{loss_detail}",
                WatcherWarning,
                stacklevel=4,
            )
            triggered = True

    return triggered


# -----------------------------------------------------------------------------
# Public API surface
# -----------------------------------------------------------------------------

__all__ = [
    "watch",
    "session",
    "DataFrameLike",
    "BackendRegistry",
    "BackendAdapter",
    "MemoryMode",
    "StepResult",
    "WatcherSession",
]

BackendRegistry.register(_PandasStatsBackend)
