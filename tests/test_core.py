# -----------------------------------------------------------------------------
# tests/test_core.py
#
# Tests for watcher/core.py
#
# Coverage targets
# ----------------
# * @watch bare decorator and with-arguments decorator
# * StepResult derived properties (row_diff, row_diff_pct, gained_rows,
#   lost_rows, is_join_explosion)
# * WatcherSession.record() and .summary()
# * session() context manager — recording, token reset, handler dispatch
# * MemoryMode resolution (_resolve_memory_mode)
# * _measure_memory for all three modes
# * _ensure_psutil_available fallback behaviour
# * _assert_dataframe_like — valid and invalid inputs
# * _check_thresholds — warn and raise paths for gain and loss
# * BackendRegistry.register / detect
# * DataFrameLike structural protocol check
#
# Run with:
#   pip install pytest pandas psutil rich watcher
#   pytest tests/test_core.py -v
# -----------------------------------------------------------------------------

from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from watcher.core import (
    BackendRegistry,
    DataFrameLike,
    MemoryMode,
    StepResult,
    WatcherSession,
    _assert_dataframe_like,
    _check_thresholds,
    _ensure_psutil_available,
    _measure_memory,
    _resolve_memory_mode,
    session,
    watch,
)
from watcher.exceptions import ThresholdExceeded, WatcherWarning
from watcher.stats import StepStats, ColumnDiff, JoinExplosionDetail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_stats() -> StepStats:
    """Return a minimal StepStats with no changes detected."""
    return StepStats(
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
    )


def _make_step(
    rows_in: int = 1000,
    rows_out: int = 900,
    memory_delta_mb: float = 0.0,
    memory_mode: MemoryMode = MemoryMode.OFF,
    duplicate_key: bool = False,
) -> StepResult:
    """Construct a StepResult for threshold / property tests."""
    stats = StepStats(
        column_diff=ColumnDiff(added=[], removed=[]),
        dtype_changes=[],
        null_deltas=[],
        duplicate_key_detected=duplicate_key,
        join_explosion=JoinExplosionDetail(
            duplicate_key_detected=duplicate_key,
            offending_columns=["customer_id"] if duplicate_key else [],
            top_offenders={"customer_id": [(42, 50)]} if duplicate_key else {},
            duplication_ratio=(rows_out - rows_in) / max(rows_in, 1),
        ),
    )
    return StepResult(
        func_name="test_step",
        rows_in=rows_in,
        rows_out=rows_out,
        elapsed_s=0.01,
        memory_delta_mb=memory_delta_mb,
        memory_mode=memory_mode,
        stats=stats,
    )


def _simple_df(n: int = 100) -> pd.DataFrame:
    return pd.DataFrame({"id": range(n), "value": range(n)})


# ===========================================================================
# StepResult — derived properties
# ===========================================================================


class TestStepResultProperties:
    def test_row_diff_positive(self):
        step = _make_step(rows_in=100, rows_out=150)
        assert step.row_diff == 50

    def test_row_diff_negative(self):
        step = _make_step(rows_in=100, rows_out=80)
        assert step.row_diff == -20

    def test_row_diff_zero(self):
        step = _make_step(rows_in=100, rows_out=100)
        assert step.row_diff == 0

    def test_row_diff_pct_normal(self):
        step = _make_step(rows_in=1000, rows_out=1100)
        assert abs(step.row_diff_pct - 0.10) < 1e-9

    def test_row_diff_pct_zero_input(self):
        # Should return 0.0, not raise ZeroDivisionError
        step = _make_step(rows_in=0, rows_out=0)
        assert step.row_diff_pct == 0.0

    def test_gained_rows_true(self):
        assert _make_step(rows_in=100, rows_out=110).gained_rows is True

    def test_gained_rows_false(self):
        assert _make_step(rows_in=100, rows_out=90).gained_rows is False

    def test_lost_rows_true(self):
        assert _make_step(rows_in=100, rows_out=80).lost_rows is True

    def test_lost_rows_false(self):
        assert _make_step(rows_in=100, rows_out=120).lost_rows is False

    # ---- is_join_explosion ------------------------------------------------

    def test_join_explosion_no_gain(self):
        step = _make_step(rows_in=100, rows_out=80, duplicate_key=True)
        assert step.is_join_explosion is False

    def test_join_explosion_duplicate_key_signal(self):
        # Gain is only 10 % (below 50 % heuristic) but key dupe flagged
        step = _make_step(rows_in=100, rows_out=110, duplicate_key=True)
        assert step.is_join_explosion is True

    def test_join_explosion_heuristic_only(self):
        # Gain > 50 % and no duplicate_key_detected — heuristic fires
        step = _make_step(rows_in=100, rows_out=160, duplicate_key=False)
        assert step.is_join_explosion is True

    def test_no_join_explosion_small_gain(self):
        step = _make_step(rows_in=100, rows_out=105, duplicate_key=False)
        assert step.is_join_explosion is False


# ===========================================================================
# WatcherSession
# ===========================================================================


class TestWatcherSession:
    def test_record_appends(self):
        sess = WatcherSession(name="test")
        step = _make_step()
        sess.record(step)
        assert len(sess.steps) == 1
        assert sess.steps[0] is step

    def test_summary_empty(self):
        sess = WatcherSession(name="empty")
        s = sess.summary()
        assert s["total_steps"] == 0
        assert s["total_rows_in"] == 0
        assert s["total_rows_out"] == 0

    def test_summary_single_step(self):
        sess = WatcherSession(name="one")
        sess.record(_make_step(rows_in=1000, rows_out=900))
        s = sess.summary()
        assert s["total_steps"] == 1
        assert s["total_rows_in"] == 1000
        assert s["total_rows_out"] == 900
        assert s["total_rows_lost"] == -100
        assert s["total_rows_gained"] == 0

    def test_summary_multiple_steps(self):
        sess = WatcherSession(name="multi")
        sess.record(_make_step(rows_in=1000, rows_out=900))   # lost 100
        sess.record(_make_step(rows_in=900,  rows_out=950))   # gained 50
        s = sess.summary()
        assert s["total_steps"] == 2
        assert s["total_rows_in"]  == 1000
        assert s["total_rows_out"] == 950
        assert s["total_rows_lost"]   == -100
        assert s["total_rows_gained"] == 50

    def test_summary_steps_list_structure(self):
        sess = WatcherSession(name="struct")
        sess.record(_make_step())
        step_dict = sess.summary()["steps"][0]
        for key in ("func", "rows_in", "rows_out", "diff", "diff_pct",
                    "elapsed_s", "memory_delta_mb", "join_explosion", "warned"):
            assert key in step_dict, f"missing key: {key}"


# ===========================================================================
# session() context manager
# ===========================================================================


class TestSessionContextManager:
    def test_session_collects_steps(self):
        df = _simple_df(100)

        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def drop_half(df):
            return df.head(50)

        with session("test run") as s:
            drop_half(df)

        assert len(s.steps) == 1
        assert s.steps[0].rows_out == 50

    def test_session_name_propagates(self):
        with session("my pipeline") as s:
            pass
        assert s.name == "my pipeline"

    def test_session_token_reset_after_exit(self):
        """After the context manager exits, there should be no active session."""
        from watcher.core import _get_active_session
        with session("temp"):
            pass
        assert _get_active_session() is None

    def test_nested_sessions_isolated(self):
        """Each context gets its own session — steps don't bleed across."""
        df = _simple_df(100)

        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def identity(df):
            return df

        with session("outer") as outer:
            identity(df)
            with session("inner") as inner:
                identity(df)

        assert len(outer.steps) == 1
        assert len(inner.steps) == 1


# ===========================================================================
# @watch decorator
# ===========================================================================


class TestWatchDecorator:
    def test_bare_decorator_passthrough(self):
        """@watch with no args should not alter the DataFrame."""
        df = _simple_df(50)

        @watch
        def passthrough(df):
            return df

        result = passthrough(df)
        assert len(result) == 50

    def test_with_args_decorator(self):
        df = _simple_df(100)

        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def head50(df):
            return df.head(50)

        result = head50(df)
        assert len(result) == 50

    def test_label_override(self):
        """The label= argument should rename the step in the report."""
        df = _simple_df(10)

        @watch(label="custom_label", track_memory=MemoryMode.OFF, verbose=False)
        def fn(df):
            return df

        with session("label test") as s:
            fn(df)

        assert s.steps[0].func_name == "custom_label"

    def test_verbose_false_suppresses_handler(self):
        """verbose=False: no handler output, but session still records."""
        df = _simple_df(20)

        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def fn(df):
            return df

        with session("quiet") as s:
            fn(df)

        assert len(s.steps) == 1

    def test_invalid_input_raises_type_error(self):
        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def fn(df):
            return df

        with pytest.raises(TypeError, match="DataFrame-like"):
            fn([1, 2, 3])

    def test_invalid_output_raises_type_error(self):
        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def fn(df):
            return [1, 2, 3]  # wrong return type

        with pytest.raises(TypeError, match="DataFrame-like"):
            fn(_simple_df(10))

    def test_functools_wraps_preserves_name(self):
        @watch(track_memory=MemoryMode.OFF, verbose=False)
        def my_transform(df):
            """docstring"""
            return df

        assert my_transform.__name__ == "my_transform"
        assert my_transform.__doc__ == "docstring"


# ===========================================================================
# Threshold enforcement
# ===========================================================================


class TestThresholds:
    # ---- warn_on_loss -------------------------------------------------------

    def test_warn_on_loss_triggers(self):
        step = _make_step(rows_in=1000, rows_out=800)  # 20 % loss
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _check_thresholds(
                step=step,
                warn_on_gain=None,
                warn_on_loss=0.10,
                raise_on_gain=None,
                raise_on_loss=None,
            )
        assert any(issubclass(x.category, WatcherWarning) for x in w)

    def test_warn_on_loss_not_triggered_below_threshold(self):
        step = _make_step(rows_in=1000, rows_out=960)  # 4 % loss
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _check_thresholds(
                step=step,
                warn_on_gain=None,
                warn_on_loss=0.10,
                raise_on_gain=None,
                raise_on_loss=None,
            )
        assert not any(issubclass(x.category, WatcherWarning) for x in w)

    # ---- raise_on_loss ------------------------------------------------------

    def test_raise_on_loss_fires(self):
        step = _make_step(rows_in=1000, rows_out=700)  # 30 % loss
        with pytest.raises(ThresholdExceeded):
            _check_thresholds(
                step=step,
                warn_on_gain=None,
                warn_on_loss=None,
                raise_on_gain=None,
                raise_on_loss=0.10,
            )

    def test_raise_on_loss_not_fired_below_threshold(self):
        step = _make_step(rows_in=1000, rows_out=980)  # 2 % loss
        # Should not raise
        _check_thresholds(
            step=step,
            warn_on_gain=None,
            warn_on_loss=None,
            raise_on_gain=None,
            raise_on_loss=0.10,
        )

    # ---- warn_on_gain -------------------------------------------------------

    def test_warn_on_gain_triggers(self):
        step = _make_step(rows_in=1000, rows_out=1300)  # 30 % gain
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _check_thresholds(
                step=step,
                warn_on_gain=0.10,
                warn_on_loss=None,
                raise_on_gain=None,
                raise_on_loss=None,
            )
        assert any(issubclass(x.category, WatcherWarning) for x in w)

    # ---- raise_on_gain ------------------------------------------------------

    def test_raise_on_gain_fires(self):
        step = _make_step(rows_in=1000, rows_out=2500)  # 150 % gain
        with pytest.raises(ThresholdExceeded):
            _check_thresholds(
                step=step,
                warn_on_gain=None,
                warn_on_loss=None,
                raise_on_gain=0.50,
                raise_on_loss=None,
            )

    # ---- decorator integration ----------------------------------------------

    def test_raise_on_loss_via_decorator(self):
        df = _simple_df(100)

        @watch(raise_on_loss=0.05, track_memory=MemoryMode.OFF, verbose=False)
        def heavy_filter(df):
            return df.head(50)  # drops 50 %

        with pytest.raises(ThresholdExceeded):
            heavy_filter(df)

    def test_warn_on_gain_via_decorator(self):
        df = _simple_df(100)

        @watch(warn_on_gain=0.05, track_memory=MemoryMode.OFF, verbose=False)
        def explode(df):
            return pd.concat([df, df], ignore_index=True)  # doubles rows

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            explode(df)

        assert any(issubclass(x.category, WatcherWarning) for x in w)

    def test_returns_true_when_triggered(self):
        step = _make_step(rows_in=1000, rows_out=700)
        result = _check_thresholds(
            step=step,
            warn_on_gain=None,
            warn_on_loss=0.10,
            raise_on_gain=None,
            raise_on_loss=None,
        )
        assert result is True

    def test_returns_false_when_not_triggered(self):
        step = _make_step(rows_in=1000, rows_out=999)
        result = _check_thresholds(
            step=step,
            warn_on_gain=None,
            warn_on_loss=0.50,
            raise_on_gain=None,
            raise_on_loss=None,
        )
        assert result is False


# ===========================================================================
# MemoryMode resolution
# ===========================================================================


class TestResolveMemoryMode:
    def test_bool_true_maps_to_rss(self):
        assert _resolve_memory_mode(True) == MemoryMode.RSS

    def test_bool_false_maps_to_off(self):
        assert _resolve_memory_mode(False) == MemoryMode.OFF

    def test_string_rss(self):
        assert _resolve_memory_mode("rss") == MemoryMode.RSS

    def test_string_peak(self):
        assert _resolve_memory_mode("peak") == MemoryMode.PEAK

    def test_string_off(self):
        assert _resolve_memory_mode("off") == MemoryMode.OFF

    def test_string_case_insensitive(self):
        assert _resolve_memory_mode("RSS") == MemoryMode.RSS
        assert _resolve_memory_mode("Peak") == MemoryMode.PEAK

    def test_enum_passthrough(self):
        assert _resolve_memory_mode(MemoryMode.OFF) == MemoryMode.OFF

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Invalid track_memory"):
            _resolve_memory_mode("turbo")


# ===========================================================================
# Memory measurement
# ===========================================================================


class TestMeasureMemory:
    def test_off_mode_returns_zero_delta(self):
        result, delta = _measure_memory(MemoryMode.OFF, lambda: _simple_df(10))
        assert isinstance(result, pd.DataFrame)
        assert delta == 0.0

    def test_peak_mode_returns_non_negative_delta(self):
        result, delta = _measure_memory(MemoryMode.PEAK, lambda: _simple_df(500))
        assert isinstance(result, pd.DataFrame)
        assert delta >= 0.0

    def test_rss_mode_returns_float(self):
        pytest.importorskip("psutil")
        result, delta = _measure_memory(MemoryMode.RSS, lambda: _simple_df(500))
        assert isinstance(result, pd.DataFrame)
        assert isinstance(delta, float)


# ===========================================================================
# _ensure_psutil_available
# ===========================================================================


class TestEnsurePsutilAvailable:
    def test_returns_mode_unchanged_when_psutil_present(self):
        pytest.importorskip("psutil")
        mode = _ensure_psutil_available(MemoryMode.RSS)
        assert mode == MemoryMode.RSS

    def test_falls_back_to_peak_when_psutil_absent(self):
        import watcher.core as core_mod
        original = core_mod._PSUTIL_AVAILABLE
        original_warned = core_mod._PSUTIL_WARNING_EMITTED
        try:
            core_mod._PSUTIL_AVAILABLE = False
            core_mod._PSUTIL_WARNING_EMITTED = False
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                mode = _ensure_psutil_available(MemoryMode.RSS)
            assert mode == MemoryMode.PEAK
            assert any(issubclass(x.category, WatcherWarning) for x in w)
        finally:
            core_mod._PSUTIL_AVAILABLE = original
            core_mod._PSUTIL_WARNING_EMITTED = original_warned

    def test_off_mode_unchanged_regardless_of_psutil(self):
        import watcher.core as core_mod
        original = core_mod._PSUTIL_AVAILABLE
        try:
            core_mod._PSUTIL_AVAILABLE = False
            mode = _ensure_psutil_available(MemoryMode.OFF)
            assert mode == MemoryMode.OFF
        finally:
            core_mod._PSUTIL_AVAILABLE = original


# ===========================================================================
# _assert_dataframe_like
# ===========================================================================


class TestAssertDataFrameLike:
    def test_pandas_df_passes(self):
        # Must not raise
        _assert_dataframe_like(_simple_df(5), "fn", "input")

    def test_list_raises(self):
        with pytest.raises(TypeError, match="DataFrame-like"):
            _assert_dataframe_like([1, 2, 3], "fn", "input")

    def test_none_raises(self):
        with pytest.raises(TypeError, match="DataFrame-like"):
            _assert_dataframe_like(None, "fn", "output")

    def test_custom_protocol_object_passes(self):
        """Objects satisfying the DataFrameLike protocol should pass."""
        class FakeDF:
            @property
            def shape(self):
                return (10, 2)
            @property
            def columns(self):
                return ["a", "b"]

        # Should not raise — satisfies the structural protocol
        _assert_dataframe_like(FakeDF(), "fn", "input")


# ===========================================================================
# BackendRegistry
# ===========================================================================


class TestBackendRegistry:
    def test_detect_pandas_df(self):
        # BackendRegistry.detect() returns None until a concrete backend adapter
        # is registered (e.g. watcher/backends/pandas.py calls register()).
        # The pandas stats backend lives in stats.py — test via _detect_backend,
        # which is the actual code path used by core.py → compute_stats().
        from watcher.stats import _detect_backend
        df = _simple_df()
        backend = _detect_backend(df)
        assert backend is not None

    def test_detect_unknown_returns_none(self):
        assert BackendRegistry.detect({"a": 1}) is None

    def test_register_idempotent(self):
        from watcher.core import BackendAdapter
        # Registering the same adapter twice should not duplicate it
        initial_len = len(BackendRegistry._adapters)

        class DummyAdapter:
            @staticmethod
            def accepts(obj):
                return False
            @staticmethod
            def row_count(obj):
                return 0
            @staticmethod
            def column_names(obj):
                return []

        BackendRegistry.register(DummyAdapter)
        BackendRegistry.register(DummyAdapter)
        assert BackendRegistry._adapters.count(DummyAdapter) == 1

        # Cleanup
        BackendRegistry._adapters.remove(DummyAdapter)
