# -----------------------------------------------------------------------------
# watcher/stats.py
#
# Row and column statistics engine.
#
# compute_stats() is the single entry point called by core.py after every
# decorated function.  It compares the before/after DataFrames and returns
# a StepStats dataclass that captures everything the reporter and session
# summary need — null counts, dtype changes, schema drift, value ranges,
# and join-explosion signals.
#
# Design constraints
# ------------------
# * No direct pandas import at module level — the PandasStatsBackend
#   imports pandas inside its methods so the module stays importable in
#   environments that only have Polars.
# * Sampling is applied first for large DataFrames to keep overhead
#   predictable in production pipelines.
# * All public types are plain dataclasses — no pandas objects leak out.
#
# License : MIT
# Docs    : https://github.com/Abineshabee/watcher
# -----------------------------------------------------------------------------

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from watcher.exceptions import WatcherWarning

# Rows above this threshold trigger sampling before computing column stats.
# Override globally via watcher.config.stats_sample_size.
_DEFAULT_SAMPLE_SIZE: int = 50_000

__all__ = [
    "StepStats",
    "ColumnDiff",
    "DtypeChange",
    "JoinExplosionDetail",
    "compute_stats",
]


# -----------------------------------------------------------------------------
# Leaf dataclasses — one per concept, composed into StepStats
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DtypeChange:
    """
    Records a dtype change on a single column between pipeline steps.

    Attributes
    ----------
    column : str
        The column that changed dtype.
    before : str
        String representation of the dtype before the step.
    after : str
        String representation of the dtype after the step.
    is_widening : bool
        ``True`` when the change is a safe widening (e.g. ``int32``
        to ``int64``).  ``False`` for narrowing or type coercions that
        may lose information (e.g. ``float64`` to ``object``).

    Example
    -------
    >>> DtypeChange(column="customer_id", before="int64", after="object",
    ...             is_widening=False)
    """

    column: str
    before: str
    after: str
    is_widening: bool


@dataclass(frozen=True)
class ColumnDiff:
    """
    Schema drift summary — columns added and removed between steps.

    Attributes
    ----------
    added : list[str]
        Columns present in the output that were absent in the input.
    removed : list[str]
        Columns present in the input that are absent in the output.

    Example
    -------
    >>> ColumnDiff(added=["churn_score", "revenue_usd"],
    ...            removed=["raw_json", "tmp_flag"])
    """

    added: List[str]
    removed: List[str]

    @property
    def has_drift(self) -> bool:
        """``True`` when any columns were added or removed."""
        return bool(self.added or self.removed)


@dataclass(frozen=True)
class NullDelta:
    """
    Change in null count for a single column.

    Attributes
    ----------
    column : str
        Column name.
    before : int
        Null count before the step.
    after : int
        Null count after the step.
    delta : int
        Signed difference (positive = more nulls introduced).
    """

    column: str
    before: int
    after: int
    delta: int


@dataclass(frozen=True)
class JoinExplosionDetail:
    """
    Diagnostic detail for a suspected join fan-out.

    Populated by :func:`compute_stats` when the stats engine detects
    duplicate values in likely join-key columns.  The reporter surfaces
    this so users see *why* rows were gained, not just *that* they were.

    Attributes
    ----------
    duplicate_key_detected : bool
        ``True`` when at least one likely join key has duplicate values
        in the output that were absent in the input.
    offending_columns : list[str]
        Column names where duplication was detected, ordered by
        duplication ratio descending.
    top_offenders : dict[str, list[tuple[Any, int]]]
        For each offending column, the top-5 values and their repeat
        counts.  E.g. ``{"customer_id": [(9182, 184), (3310, 97)]}``.
    duplication_ratio : float
        ``(rows_out - rows_in) / rows_in`` — the raw fan-out fraction.
        ``0.0`` when no explosion was detected.

    Example
    -------
    ::

        JoinExplosionDetail(
            duplicate_key_detected=True,
            offending_columns=["customer_id"],
            top_offenders={"customer_id": [(9182, 184), (3310, 97)]},
            duplication_ratio=0.76,
        )
    """

    duplicate_key_detected: bool
    offending_columns: List[str]
    top_offenders: Dict[str, List[Tuple[Any, int]]]
    duplication_ratio: float


# -----------------------------------------------------------------------------
# StepStats — the full stat payload returned by compute_stats()
# -----------------------------------------------------------------------------


@dataclass
class StepStats:
    """
    Complete statistical snapshot comparing a before/after DataFrame pair.

    Returned by :func:`compute_stats` and stored on every
    :class:`~watcher.core.StepResult`.

    Attributes
    ----------
    column_diff : ColumnDiff
        Schema drift — columns added and removed.
    dtype_changes : list[DtypeChange]
        Columns whose dtype changed between steps.
    null_deltas : list[NullDelta]
        Per-column null count changes (only columns present in both
        DataFrames are included).
    duplicate_key_detected : bool
        Shortcut flag used by :attr:`~watcher.core.StepResult.is_join_explosion`.
        ``True`` when :attr:`join_explosion` found at least one likely
        fan-out key.
    join_explosion : JoinExplosionDetail
        Full diagnosis when a join explosion is suspected.
    sampled : bool
        ``True`` when column stats were computed on a sample rather than
        the full DataFrame.  Reported by the terminal handler as a
        footnote.
    sample_size : int
        Number of rows used for sampling.  ``0`` when ``sampled=False``.
    backend : str
        Name of the backend that computed these stats (e.g. ``"pandas"``).
    """

    column_diff: ColumnDiff
    dtype_changes: List[DtypeChange]
    null_deltas: List[NullDelta]
    duplicate_key_detected: bool
    join_explosion: JoinExplosionDetail
    sampled: bool = False
    sample_size: int = 0
    backend: str = "unknown"


# -----------------------------------------------------------------------------
# Empty sentinel — returned when stats computation fails non-fatally
# -----------------------------------------------------------------------------

_EMPTY_STATS = StepStats(
    column_diff=ColumnDiff(added=[], removed=[]),
    dtype_changes=[],
    null_deltas=[],
    duplicate_key_detected=False,
    join_explosion=JoinExplosionDetail(
        duplicate_key_detected=False,
        offending_columns=[],
        top_offenders={},
        duplication_ratio=0.0,
    ),
    backend="empty",
)


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


def compute_stats(
    before: Any,
    after: Any,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> StepStats:
    """
    Compare two DataFrame-like objects and return a :class:`StepStats`.

    This is the single function called by ``core.py`` after every
    decorated step.  It detects the backend automatically and delegates
    to the appropriate stats implementation.

    Currently only pandas is supported.  Adding Polars means adding a
    ``_PolarsStatsBackend`` class and registering it in ``_BACKENDS``.

    Parameters
    ----------
    before : DataFrameLike
        The DataFrame *entering* the decorated function.
    after : DataFrameLike
        The DataFrame *leaving* the decorated function.
    sample_size : int, optional
        Maximum number of rows used for column-level statistics.
        For DataFrames smaller than this, the full data is used.
        Defaults to ``50_000``.  Set to ``0`` to disable sampling
        (expensive on large DataFrames but fully accurate).

    Returns
    -------
    StepStats
        Complete statistical comparison.  Never raises — on unexpected
        errors, a :class:`WatcherWarning` is emitted and an empty
        ``StepStats`` is returned so the pipeline is never interrupted
        by the observer.

    Notes
    -----
    The "observer must not affect the observed" principle is enforced
    here: any exception inside compute_stats is caught, warned about,
    and swallowed.  Your pipeline always continues.
    """
    backend = _detect_backend(before)

    if backend is None:
        warnings.warn(
            f"[watcher] compute_stats: no stats backend recognised "
            f"{type(before).__name__!r}.  Returning empty stats.",
            WatcherWarning,
            stacklevel=3,
        )
        return _EMPTY_STATS

    try:
        return backend.compute(before=before, after=after, sample_size=sample_size)
    except Exception as exc:  # pylint: disable=broad-except
        warnings.warn(
            f"[watcher] compute_stats failed ({type(exc).__name__}: {exc}).  "
            f"Returning empty stats.  Pipeline will continue.",
            WatcherWarning,
            stacklevel=3,
        )
        return _EMPTY_STATS


# -----------------------------------------------------------------------------
# Backend detection
# -----------------------------------------------------------------------------


def _detect_backend(obj: Any) -> Optional[_StatsBackend]:
    """
    Return the first registered stats backend that accepts *obj*.

    Parameters
    ----------
    obj : Any
        The DataFrame to inspect.

    Returns
    -------
    _StatsBackend or None
        Instantiated backend, or ``None`` if nothing matches.
    """
    for backend_cls in _BACKENDS:
        if backend_cls.accepts(obj):
            return backend_cls()
    return None


# -----------------------------------------------------------------------------
# Internal backend protocol
# -----------------------------------------------------------------------------


class _StatsBackend:
    """
    Internal interface that every stats backend must implement.

    Not part of the public API.  Users interact with backends via
    :func:`compute_stats` only.
    """

    @staticmethod
    def accepts(obj: Any) -> bool:
        """Return ``True`` if this backend handles *obj*."""
        raise NotImplementedError

    def compute(
        self,
        before: Any,
        after: Any,
        sample_size: int,
    ) -> StepStats:
        """
        Compute and return :class:`StepStats` for the before/after pair.

        Parameters
        ----------
        before : Any
            DataFrame entering the step.
        after : Any
            DataFrame leaving the step.
        sample_size : int
            Max rows for column-level computation.

        Returns
        -------
        StepStats
        """
        raise NotImplementedError


# -----------------------------------------------------------------------------
# Pandas backend
# -----------------------------------------------------------------------------


class _PandasStatsBackend(_StatsBackend):
    """
    Stats backend for pandas DataFrames.

    Imports pandas lazily so ``watcher`` is importable in environments
    that only have Polars or another engine installed.
    """

    @staticmethod
    def accepts(obj: Any) -> bool:
        """
        Return ``True`` for ``pd.DataFrame`` instances.

        Lazy import avoids making pandas a hard import-time dependency.
        """
        try:
            import pandas as pd  # noqa: PLC0415

            return isinstance(obj, pd.DataFrame)
        except ImportError:
            return False

    def compute(
        self,
        before: Any,
        after: Any,
        sample_size: int,
    ) -> StepStats:
        """
        Compute :class:`StepStats` for two pandas DataFrames.

        Steps
        -----
        1. Detect schema drift (added / removed columns).
        2. Detect dtype changes on shared columns.
        3. Compute null-count deltas on shared columns.
        4. Run join-explosion diagnosis if rows were gained.
        5. Apply sampling for large DataFrames before steps 2–4.

        Parameters
        ----------
        before : pd.DataFrame
            DataFrame entering the step.
        after : pd.DataFrame
            DataFrame leaving the step.
        sample_size : int
            Rows used for column stats; 0 = full scan.

        Returns
        -------
        StepStats
        """
        import pandas as pd  # noqa: PLC0415

        cols_before = set(before.columns)
        cols_after = set(after.columns)

        # ---- 1. Schema drift --------------------------------------------
        column_diff = ColumnDiff(
            added=sorted(cols_after - cols_before),
            removed=sorted(cols_before - cols_after),
        )

        # ---- 2 & 3. Stats on shared columns only ------------------------
        shared = sorted(cols_before & cols_after)

        # Apply sampling for large DataFrames
        sampled = False
        sample_used = 0

        if sample_size > 0 and (len(before) > sample_size or len(after) > sample_size):
            sampled = True
            sample_used = sample_size
            before_s: pd.DataFrame = before[shared].sample(
                n=min(sample_size, len(before)), random_state=42
            )
            after_s: pd.DataFrame = after[shared].sample(
                n=min(sample_size, len(after)), random_state=42
            )
        else:
            before_s = before[shared]
            after_s = after[shared]

        # ---- 2. Dtype changes -------------------------------------------
        dtype_changes = _pandas_dtype_changes(before_s, after_s, shared)

        # ---- 3. Null deltas ---------------------------------------------
        null_deltas = _pandas_null_deltas(before_s, after_s, shared)

        # ---- 4. Join explosion ------------------------------------------
        rows_gained = len(after) - len(before)
        if rows_gained > 0:
            explosion = _pandas_join_explosion(
                before=before,
                after=after,
                shared_cols=shared,
                sample_size=sample_size,
            )
        else:
            explosion = JoinExplosionDetail(
                duplicate_key_detected=False,
                offending_columns=[],
                top_offenders={},
                duplication_ratio=0.0,
            )

        return StepStats(
            column_diff=column_diff,
            dtype_changes=dtype_changes,
            null_deltas=null_deltas,
            duplicate_key_detected=explosion.duplicate_key_detected,
            join_explosion=explosion,
            sampled=sampled,
            sample_size=sample_used,
            backend="pandas",
        )


# -----------------------------------------------------------------------------
# Pandas helpers — internal, not exported
# -----------------------------------------------------------------------------

# Dtypes considered safe widenings (before → after direction only)
_WIDENING_PAIRS: set[tuple[str, str]] = {
    ("int8", "int16"),
    ("int8", "int32"),
    ("int8", "int64"),
    ("int16", "int32"),
    ("int16", "int64"),
    ("int32", "int64"),
    ("float32", "float64"),
    ("uint8", "uint16"),
    ("uint8", "uint32"),
    ("uint8", "uint64"),
    ("uint16", "uint32"),
    ("uint16", "uint64"),
    ("uint32", "uint64"),
}


def _pandas_dtype_changes(
    before: Any,
    after: Any,
    shared: List[str],
) -> List[DtypeChange]:
    """
    Return one :class:`DtypeChange` per column whose dtype shifted.

    Parameters
    ----------
    before : pd.DataFrame
        Sampled before-DataFrame restricted to shared columns.
    after : pd.DataFrame
        Sampled after-DataFrame restricted to shared columns.
    shared : list[str]
        Column names present in both DataFrames.

    Returns
    -------
    list[DtypeChange]
        Empty list when no dtype changes occurred.
    """
    changes: List[DtypeChange] = []
    for col in shared:
        b_dtype = str(before[col].dtype)
        a_dtype = str(after[col].dtype)
        if b_dtype != a_dtype:
            is_widening = (b_dtype, a_dtype) in _WIDENING_PAIRS
            changes.append(
                DtypeChange(
                    column=col,
                    before=b_dtype,
                    after=a_dtype,
                    is_widening=is_widening,
                )
            )
    return changes


def _pandas_null_deltas(
    before: Any,
    after: Any,
    shared: List[str],
) -> List[NullDelta]:
    """
    Return one :class:`NullDelta` per column with a changed null count.

    Columns with identical null counts are omitted to keep the report
    focused on meaningful changes.

    Parameters
    ----------
    before : pd.DataFrame
        Sampled before-DataFrame restricted to shared columns.
    after : pd.DataFrame
        Sampled after-DataFrame restricted to shared columns.
    shared : list[str]
        Column names present in both DataFrames.

    Returns
    -------
    list[NullDelta]
        Sorted by absolute delta descending — worst offenders first.
    """
    deltas: List[NullDelta] = []
    for col in shared:
        b_nulls = int(before[col].isna().sum())
        a_nulls = int(after[col].isna().sum())
        diff = a_nulls - b_nulls
        if diff != 0:
            deltas.append(
                NullDelta(column=col, before=b_nulls, after=a_nulls, delta=diff)
            )
    return sorted(deltas, key=lambda d: abs(d.delta), reverse=True)


# Columns likely to be join keys — heuristic based on common naming patterns
_KEY_SUFFIXES: tuple[str, ...] = ("_id", "_key", "_code", "id", "key")
_TOP_OFFENDERS_N: int = 5  # how many values to show per offending column


def _pandas_join_explosion(
    before: Any,
    after: Any,
    shared_cols: List[str],
    sample_size: int,
) -> JoinExplosionDetail:
    """
    Diagnose a potential join fan-out by inspecting key-column duplication.

    Strategy
    --------
    1. Identify candidate join-key columns — columns present in both
       DataFrames whose names match common key suffixes (``_id``,
       ``_key``, ``id``, etc.) or that are entirely integer-typed.
    2. For each candidate, compare the value-count distribution before
       and after the step.
    3. If any column's max repeat count increased significantly, flag it
       as an offending column and record the top-5 offenders.

    The goal is to tell users:
    ``"customer_id=9182 now appears 184 times — that's why you gained rows."``

    Parameters
    ----------
    before : pd.DataFrame
        Full before-DataFrame (not sampled — we need exact counts).
    after : pd.DataFrame
        Full after-DataFrame.
    shared_cols : list[str]
        Columns present in both DataFrames.
    sample_size : int
        Used to limit value_counts() on very wide tables.

    Returns
    -------
    JoinExplosionDetail
    """
    import pandas as pd  # noqa: PLC0415

    duplication_ratio = (len(after) - len(before)) / max(len(before), 1)

    # Identify candidate key columns
    key_candidates = [
        col
        for col in shared_cols
        if (
            any(col.lower().endswith(s) for s in _KEY_SUFFIXES)
            or pd.api.types.is_integer_dtype(before[col])
        )
    ]

    if not key_candidates:
        return JoinExplosionDetail(
            duplicate_key_detected=False,
            offending_columns=[],
            top_offenders={},
            duplication_ratio=duplication_ratio,
        )

    offending_columns: List[str] = []
    top_offenders: Dict[str, List[Tuple[Any, int]]] = {}

    for col in key_candidates:
        try:
            # Max repeat count before vs after
            before_max = int(before[col].value_counts().max()) if len(before) else 0
            after_max = int(after[col].value_counts().max()) if len(after) else 0

            # Fan-out signal: the max repeat count grew
            if after_max > before_max and after_max > 1:
                offending_columns.append(col)

                # Top offenders — values with highest repeat count
                top = after[col].value_counts().head(_TOP_OFFENDERS_N)
                top_offenders[col] = [(val, int(cnt)) for val, cnt in top.items()]
        except Exception:  # pylint: disable=broad-except
            # A column that looks like a key but can't be value-counted
            # (e.g. unhashable type) — skip silently
            continue

    # Sort offending columns by their top repeat count, worst first
    offending_columns.sort(
        key=lambda c: top_offenders[c][0][1] if c in top_offenders else 0,
        reverse=True,
    )

    return JoinExplosionDetail(
        duplicate_key_detected=bool(offending_columns),
        offending_columns=offending_columns,
        top_offenders=top_offenders,
        duplication_ratio=duplication_ratio,
    )


# -----------------------------------------------------------------------------
# Backend registry — add new backends here
# -----------------------------------------------------------------------------

_BACKENDS: List[type[_StatsBackend]] = [
    _PandasStatsBackend,
    # _PolarsStatsBackend,   # coming soon
    # _DuckDBStatsBackend,   # coming soon
]
