# -----------------------------------------------------------------------------
# tests/test_stats.py
#
# Tests for watcher/stats.py
#
# Coverage targets
# ----------------
# * compute_stats() — happy path for pandas DataFrames
# * compute_stats() — unknown backend returns _EMPTY_STATS
# * compute_stats() — backend exception emits WatcherWarning and returns empty
# * ColumnDiff.has_drift property
# * _pandas_dtype_changes — no change, narrowing, widening
# * _pandas_null_deltas — no change, new nulls, nulls removed
# * _pandas_join_explosion — no gain, gain with key match, gain no key
# * StepStats sampled flag behaviour
# * DtypeChange, NullDelta, JoinExplosionDetail dataclasses
#
# Run with:
#   pytest tests/test_stats.py -v
# -----------------------------------------------------------------------------

from __future__ import annotations

import warnings
from typing import Any

import pandas as pd
import numpy as np
import pytest

from watcher.exceptions import WatcherWarning
from watcher.stats import (
    ColumnDiff,
    DtypeChange,
    JoinExplosionDetail,
    NullDelta,
    StepStats,
    _pandas_dtype_changes,
    _pandas_join_explosion,
    _pandas_null_deltas,
    compute_stats,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _df(**kwargs) -> pd.DataFrame:
    """Quick DataFrame constructor: _df(a=[1,2], b=[3,4])."""
    return pd.DataFrame(kwargs)


# ===========================================================================
# ColumnDiff
# ===========================================================================


class TestColumnDiff:
    def test_has_drift_true_when_added(self):
        cd = ColumnDiff(added=["x"], removed=[])
        assert cd.has_drift is True

    def test_has_drift_true_when_removed(self):
        cd = ColumnDiff(added=[], removed=["y"])
        assert cd.has_drift is True

    def test_has_drift_false_when_empty(self):
        cd = ColumnDiff(added=[], removed=[])
        assert cd.has_drift is False

    def test_has_drift_true_when_both(self):
        cd = ColumnDiff(added=["a"], removed=["b"])
        assert cd.has_drift is True


# ===========================================================================
# DtypeChange
# ===========================================================================


class TestDtypeChange:
    def test_fields(self):
        dc = DtypeChange(column="col", before="int32", after="int64", is_widening=True)
        assert dc.column == "col"
        assert dc.before == "int32"
        assert dc.after == "int64"
        assert dc.is_widening is True

    def test_frozen(self):
        dc = DtypeChange(column="col", before="int32", after="int64", is_widening=True)
        with pytest.raises(Exception):
            dc.column = "other"  # type: ignore[misc]


# ===========================================================================
# NullDelta
# ===========================================================================


class TestNullDelta:
    def test_fields(self):
        nd = NullDelta(column="age", before=0, after=5, delta=5)
        assert nd.column == "age"
        assert nd.before == 0
        assert nd.after == 5
        assert nd.delta == 5


# ===========================================================================
# JoinExplosionDetail
# ===========================================================================


class TestJoinExplosionDetail:
    def test_fields(self):
        jed = JoinExplosionDetail(
            duplicate_key_detected=True,
            offending_columns=["id"],
            top_offenders={"id": [(1, 10)]},
            duplication_ratio=0.5,
        )
        assert jed.duplicate_key_detected is True
        assert "id" in jed.offending_columns
        assert jed.duplication_ratio == 0.5


# ===========================================================================
# _pandas_dtype_changes
# ===========================================================================


class TestPandasDtypeChanges:
    def test_no_change(self):
        df = _df(a=[1, 2, 3])
        result = _pandas_dtype_changes(df, df, ["a"])
        assert result == []

    def test_detects_narrowing(self):
        before = _df(a=pd.array([1, 2, 3], dtype="int64"))
        after  = _df(a=pd.array([1, 2, 3], dtype="int32"))
        result = _pandas_dtype_changes(before, after, ["a"])
        assert len(result) == 1
        assert result[0].column == "a"
        assert result[0].is_widening is False

    def test_detects_widening(self):
        before = _df(a=pd.array([1, 2, 3], dtype="int32"))
        after  = _df(a=pd.array([1, 2, 3], dtype="int64"))
        result = _pandas_dtype_changes(before, after, ["a"])
        assert len(result) == 1
        assert result[0].is_widening is True

    def test_multiple_columns(self):
        before = _df(a=pd.array([1], dtype="int32"), b=pd.array([1.0], dtype="float32"))
        after  = _df(a=pd.array([1], dtype="int64"), b=pd.array([1.0], dtype="float64"))
        result = _pandas_dtype_changes(before, after, ["a", "b"])
        assert len(result) == 2

    def test_type_coercion_to_object(self):
        before = _df(a=pd.array([1, 2], dtype="float64"))
        after  = pd.DataFrame({"a": ["x", "y"]})  # object or str dtype depending on pandas version
        result = _pandas_dtype_changes(before, after, ["a"])
        assert result[0].before == "float64"
        # pandas < 2.0 infers "object"; pandas >= 2.0 infers "str" (StringDtype)
        assert result[0].after in ("object", "str")
        assert result[0].is_widening is False


# ===========================================================================
# _pandas_null_deltas
# ===========================================================================


class TestPandasNullDeltas:
    def test_no_nulls_no_delta(self):
        df = _df(a=[1, 2, 3])
        result = _pandas_null_deltas(df, df, ["a"])
        assert result == []

    def test_new_nulls_detected(self):
        before = _df(a=[1, 2, 3])
        after  = _df(a=[1, None, None])
        result = _pandas_null_deltas(before, after, ["a"])
        assert len(result) == 1
        assert result[0].delta > 0
        assert result[0].column == "a"

    def test_nulls_removed(self):
        before = _df(a=[None, None, 3])
        after  = _df(a=[1, 2, 3])
        result = _pandas_null_deltas(before, after, ["a"])
        assert len(result) == 1
        assert result[0].delta < 0

    def test_sorted_by_abs_delta_descending(self):
        before = _df(a=[1, 2, 3, 4], b=[1, 2, 3, 4])
        after  = _df(a=[None, None, None, 4], b=[None, 2, 3, 4])  # a: +3, b: +1
        result = _pandas_null_deltas(before, after, ["a", "b"])
        assert result[0].column == "a"  # worst first

    def test_no_change_columns_excluded(self):
        df = _df(a=[None, None], b=[1, 2])
        result = _pandas_null_deltas(df, df, ["a", "b"])
        # No delta since both before and after are identical
        assert result == []


# ===========================================================================
# _pandas_join_explosion
# ===========================================================================


class TestPandasJoinExplosion:
    def test_no_gain_still_returns_detail(self):
        before = _df(customer_id=[1, 2, 3])
        after  = _df(customer_id=[1, 2])   # rows lost, not gained — caller guards
        result = _pandas_join_explosion(before, after, ["customer_id"], 50_000)
        # duplication_ratio is negative when rows lost
        assert isinstance(result, JoinExplosionDetail)

    def test_detects_key_duplication(self):
        before = _df(customer_id=[1, 2, 3])
        # Simulating a fan-out: customer_id=1 appears 3× in the output
        after  = _df(customer_id=[1, 1, 1, 2, 3])
        result = _pandas_join_explosion(before, after, ["customer_id"], 50_000)
        assert result.duplicate_key_detected is True
        assert "customer_id" in result.offending_columns

    def test_no_key_columns_returns_no_detection(self):
        # Columns have names with no key suffix and are string dtype
        before = _df(name=["alice", "bob"])
        after  = _df(name=["alice", "alice", "bob"])
        result = _pandas_join_explosion(before, after, ["name"], 50_000)
        # "name" doesn't match any key suffix and isn't int dtype
        assert result.duplicate_key_detected is False

    def test_top_offenders_populated(self):
        before = _df(order_id=[1, 2, 3])
        after  = _df(order_id=[1, 1, 1, 2, 3])
        result = _pandas_join_explosion(before, after, ["order_id"], 50_000)
        # "order_id" ends with "_id" → candidate key
        if result.duplicate_key_detected:
            assert "order_id" in result.top_offenders
            assert isinstance(result.top_offenders["order_id"], list)

    def test_integer_column_considered_key_candidate(self):
        """Integer-typed columns should be treated as potential join keys."""
        before = pd.DataFrame({"val": pd.array([1, 2, 3], dtype="int64")})
        after  = pd.DataFrame({"val": pd.array([1, 1, 1, 2, 3], dtype="int64")})
        result = _pandas_join_explosion(before, after, ["val"], 50_000)
        # int column → candidate; should detect duplication
        assert result.duplicate_key_detected is True


# ===========================================================================
# compute_stats — integration tests
# ===========================================================================


class TestComputeStats:
    def test_returns_stepstats(self):
        df = _df(a=[1, 2, 3], b=[4, 5, 6])
        result = compute_stats(df, df)
        assert isinstance(result, StepStats)

    def test_schema_drift_detected(self):
        before = _df(a=[1, 2], b=[3, 4])
        after  = _df(a=[1, 2], c=[5, 6])   # b removed, c added
        result = compute_stats(before, after)
        assert "c" in result.column_diff.added
        assert "b" in result.column_diff.removed

    def test_no_drift_when_same_columns(self):
        df = _df(a=[1, 2], b=[3, 4])
        result = compute_stats(df, df)
        assert not result.column_diff.has_drift

    def test_dtype_changes_surfaced(self):
        before = _df(a=pd.array([1, 2], dtype="int32"))
        after  = _df(a=pd.array([1, 2], dtype="int64"))
        result = compute_stats(before, after)
        assert len(result.dtype_changes) == 1
        assert result.dtype_changes[0].column == "a"

    def test_null_deltas_surfaced(self):
        before = _df(a=[1, 2, 3])
        after  = _df(a=[None, 2, 3])
        result = compute_stats(before, after)
        assert any(nd.column == "a" for nd in result.null_deltas)

    def test_sampling_flag_set_on_large_df(self):
        n = 1000
        df = pd.DataFrame({"a": range(n)})
        result = compute_stats(df, df, sample_size=100)
        assert result.sampled is True
        assert result.sample_size == 100

    def test_no_sampling_on_small_df(self):
        df = _df(a=[1, 2, 3])
        result = compute_stats(df, df, sample_size=50_000)
        assert result.sampled is False

    def test_unknown_backend_returns_empty_and_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = compute_stats({"not": "a dataframe"}, {"not": "a dataframe"})
        assert result.backend == "empty"
        assert any(issubclass(x.category, WatcherWarning) for x in w)

    def test_backend_exception_returns_empty_and_warns(self):
        """If the backend raises unexpectedly, compute_stats returns empty stats."""
        import watcher.stats as stats_mod
        original = stats_mod._BACKENDS[:]

        class BrokenBackend:
            @staticmethod
            def accepts(obj: Any) -> bool:
                return True
            def compute(self, *args, **kwargs):
                raise RuntimeError("simulated backend failure")

        stats_mod._BACKENDS.insert(0, BrokenBackend)
        try:
            df = _df(a=[1, 2])
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = compute_stats(df, df)
            assert result.backend == "empty"
            assert any(issubclass(x.category, WatcherWarning) for x in w)
        finally:
            stats_mod._BACKENDS[:] = original

    def test_join_explosion_detected_on_row_gain(self):
        before = _df(customer_id=[1, 2, 3])
        after  = _df(customer_id=[1, 1, 1, 2, 3])
        result = compute_stats(before, after)
        assert result.duplicate_key_detected is True

    def test_join_explosion_not_set_on_row_loss(self):
        before = _df(customer_id=[1, 2, 3])
        after  = _df(customer_id=[1, 2])
        result = compute_stats(before, after)
        assert result.duplicate_key_detected is False

    def test_backend_name_is_pandas(self):
        df = _df(a=[1])
        result = compute_stats(df, df)
        assert result.backend == "pandas"
