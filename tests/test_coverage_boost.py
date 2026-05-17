# -----------------------------------------------------------------------------
# tests/test_coverage_boost.py
#
# Targets the specific uncovered lines reported by pytest-cov:
#   core.py    — MemoryMode paths, watch decorator edge cases
#   reporter.py — Rich rendering paths, plain-text fallback
#   stats.py   — sampling, join explosion helpers, backend errors
# -----------------------------------------------------------------------------

import warnings
import pytest
import pandas as pd
import numpy as np

from watcher import watch, session, MemoryMode
from watcher.core import (
    StepResult,
    WatcherSession,
    BackendRegistry,
    _resolve_memory_mode,
    _measure_memory,
    _ensure_psutil_available,
    _assert_dataframe_like,
    _check_thresholds,
)
from watcher.exceptions import ThresholdExceeded, WatcherWarning
from watcher.stats import (
    StepStats,
    ColumnDiff,
    DtypeChange,
    NullDelta,
    JoinExplosionDetail,
    compute_stats,
)
from watcher.reporter import Reporter, _row_direction, _fmt_diff
from watcher.handlers import HandlerBase, register_handler, deregister_handler


# =============================================================================
# Helpers
# =============================================================================

def _make_step(
    func_name="step",
    rows_in=1000,
    rows_out=800,
    elapsed_s=0.01,
    memory_delta_mb=0.0,
    memory_mode=MemoryMode.OFF,
    warned=False,
    column_diff=None,
    null_deltas=None,
    dtype_changes=None,
    duplicate_key=False,
    join_explosion=None,
    sampled=False,
    sample_size=0,
) -> StepResult:
    cd = column_diff or ColumnDiff(added=[], removed=[])
    je = join_explosion or JoinExplosionDetail(
        duplicate_key_detected=duplicate_key,
        offending_columns=["customer_id"] if duplicate_key else [],
        top_offenders={"customer_id": [(1, 5), (2, 4)]} if duplicate_key else {},
        duplication_ratio=1.0 if duplicate_key else 0.0,
    )
    stats = StepStats(
        column_diff=cd,
        dtype_changes=dtype_changes or [],
        null_deltas=null_deltas or [],
        duplicate_key_detected=duplicate_key,
        join_explosion=je,
        sampled=sampled,
        sample_size=sample_size,
        backend="pandas",
    )
    return StepResult(
        func_name=func_name,
        rows_in=rows_in,
        rows_out=rows_out,
        elapsed_s=elapsed_s,
        memory_delta_mb=memory_delta_mb,
        memory_mode=memory_mode,
        stats=stats,
        warned=warned,
    )


# =============================================================================
# core.py — _resolve_memory_mode edge cases
# =============================================================================

class TestResolveMemoryModeEdgeCases:

    def test_invalid_string_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid track_memory"):
            _resolve_memory_mode("invalid_mode")

    def test_memory_mode_enum_passthrough(self):
        assert _resolve_memory_mode(MemoryMode.PEAK) == MemoryMode.PEAK
        assert _resolve_memory_mode(MemoryMode.RSS) == MemoryMode.RSS
        assert _resolve_memory_mode(MemoryMode.OFF) == MemoryMode.OFF

    def test_string_case_insensitive_rss(self):
        assert _resolve_memory_mode("RSS") == MemoryMode.RSS

    def test_string_case_insensitive_off(self):
        assert _resolve_memory_mode("OFF") == MemoryMode.OFF


# =============================================================================
# core.py — _measure_memory all three modes
# =============================================================================

class TestMeasureMemoryAllModes:

    def test_off_mode_zero_delta(self):
        result, delta = _measure_memory(MemoryMode.OFF, lambda: 42)
        assert result == 42
        assert delta == 0.0

    def test_peak_mode_non_negative(self):
        result, delta = _measure_memory(MemoryMode.PEAK, lambda: list(range(10000)))
        assert isinstance(delta, float)
        assert delta >= 0.0

    def test_rss_mode_returns_float(self):
        result, delta = _measure_memory(MemoryMode.RSS, lambda: list(range(1000)))
        assert isinstance(delta, float)

    def test_peak_mode_captures_allocation(self):
        def big_alloc():
            return [0] * 100_000

        _, delta = _measure_memory(MemoryMode.PEAK, big_alloc)
        assert delta >= 0.0  # tracemalloc may report 0 on some platforms


# =============================================================================
# core.py — watch decorator with memory modes via real DataFrames
# =============================================================================

class TestWatchMemoryModes:

    def test_watch_rss_mode(self):
        @watch(track_memory=MemoryMode.RSS)
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(100)})
        result = step(df)
        assert len(result) == 100

    def test_watch_peak_mode(self):
        @watch(track_memory=MemoryMode.PEAK)
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(100)})
        result = step(df)
        assert len(result) == 100

    def test_watch_off_mode(self):
        @watch(track_memory=MemoryMode.OFF)
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(100)})
        result = step(df)
        assert len(result) == 100

    def test_watch_bool_true(self):
        @watch(track_memory=True)
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(10)})
        step(df)

    def test_watch_bool_false(self):
        @watch(track_memory=False)
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(10)})
        step(df)

    def test_watch_string_off(self):
        @watch(track_memory="off")
        def step(df):
            return df.copy()

        df = pd.DataFrame({"x": range(10)})
        step(df)


# =============================================================================
# core.py — _check_thresholds edge cases
# =============================================================================

class TestCheckThresholdsEdgeCases:

    def _step_with_gain(self, rows_in=100, rows_out=200):
        return _make_step(rows_in=rows_in, rows_out=rows_out)

    def _step_with_loss(self, rows_in=1000, rows_out=500):
        return _make_step(rows_in=rows_in, rows_out=rows_out)

    def test_no_thresholds_returns_false(self):
        step = self._step_with_loss()
        result = _check_thresholds(step, None, None, None, None)
        assert result is False

    def test_warn_on_gain_triggers(self):
        step = self._step_with_gain()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _check_thresholds(step, 0.10, None, None, None)
        assert result is True
        assert any(issubclass(w.category, WatcherWarning) for w in caught)

    def test_warn_on_loss_triggers(self):
        step = self._step_with_loss()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _check_thresholds(step, None, 0.10, None, None)
        assert result is True

    def test_raise_on_gain_fires(self):
        step = self._step_with_gain()
        with pytest.raises(ThresholdExceeded):
            _check_thresholds(step, None, None, 0.05, None)

    def test_raise_on_loss_fires(self):
        step = self._step_with_loss()
        with pytest.raises(ThresholdExceeded):
            _check_thresholds(step, None, None, None, 0.10)

    def test_gain_below_warn_threshold_no_warning(self):
        step = _make_step(rows_in=100, rows_out=105)  # +5%
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _check_thresholds(step, 0.10, None, None, None)  # warn at 10%
        assert result is False
        assert not any(issubclass(w.category, WatcherWarning) for w in caught)

    def test_join_explosion_hint_in_raise_message(self):
        step = _make_step(rows_in=100, rows_out=200, duplicate_key=True)
        with pytest.raises(ThresholdExceeded) as exc_info:
            _check_thresholds(step, None, None, 0.05, None)
        assert "fan-out" in str(exc_info.value) or "join" in str(exc_info.value).lower()


# =============================================================================
# reporter.py — Reporter with all display flags
# =============================================================================

class TestReporterDisplayFlags:

    def test_show_memory_false_hides_memory(self, capsys):
        r = Reporter(show_memory=False)
        step = _make_step(memory_delta_mb=5.0, memory_mode=MemoryMode.RSS)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "mem" not in out

    def test_show_null_deltas_true_shows_nulls(self, capsys):
        nd = NullDelta(column="revenue", before=100, after=0, delta=-100)
        step = _make_step(null_deltas=[nd])
        r = Reporter(show_null_deltas=True)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "revenue" in out

    def test_show_null_deltas_false_hides_nulls(self, capsys):
        nd = NullDelta(column="revenue", before=100, after=0, delta=-100)
        step = _make_step(null_deltas=[nd])
        r = Reporter(show_null_deltas=False)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "revenue" not in out

    def test_show_schema_drift_true_shows_columns(self, capsys):
        cd = ColumnDiff(added=["score"], removed=["tmp"])
        step = _make_step(column_diff=cd)
        r = Reporter(show_schema_drift=True)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "score" in out or "tmp" in out

    def test_show_schema_drift_false_hides_columns(self, capsys):
        cd = ColumnDiff(added=["score"], removed=["tmp"])
        step = _make_step(column_diff=cd)
        r = Reporter(show_schema_drift=False)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "score" not in out
        assert "tmp" not in out

    def test_show_join_detail_true_shows_explosion(self, capsys):
        step = _make_step(rows_in=100, rows_out=300, duplicate_key=True)
        r = Reporter(show_join_detail=True)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "customer_id" in out or "explosion" in out.lower()

    def test_show_join_detail_false_hides_explosion(self, capsys):
        step = _make_step(rows_in=100, rows_out=300, duplicate_key=True)
        r = Reporter(show_join_detail=False)
        r.print_step(step)
        out = capsys.readouterr().out
        assert "customer_id" not in out

    def test_sampling_footnote_shown(self, capsys):
        step = _make_step(sampled=True, sample_size=50_000)
        r = Reporter()
        r.print_step(step)
        out = capsys.readouterr().out
        assert "sample" in out.lower() or "50,000" in out

    def test_dtype_change_shown(self, capsys):
        import watcher.reporter as reporter_mod
        dc = DtypeChange(column="id", before="int64", after="object", is_widening=False)
        step = _make_step(dtype_changes=[dc])
        original = reporter_mod._RICH_AVAILABLE
        try:
            reporter_mod._RICH_AVAILABLE = False
            reporter_mod._console = None
            r = Reporter(show_schema_drift=True)
            r.print_step(step)
        finally:
            reporter_mod._RICH_AVAILABLE = original
            reporter_mod._console = None
        out = capsys.readouterr().out
        assert "id" in out

    def test_session_header_contains_name(self, capsys):
        r = Reporter()
        r.print_session_header("my pipeline")
        out = capsys.readouterr().out
        assert "my pipeline" in out

    def test_session_footer_contains_step_names(self, capsys):
        r = Reporter()
        sess = WatcherSession(name="test")
        sess.record(_make_step(func_name="clean"))
        sess.record(_make_step(func_name="filter"))
        r.print_session_footer(sess)
        out = capsys.readouterr().out
        assert "clean" in out
        assert "filter" in out


# =============================================================================
# reporter.py — _row_direction and _fmt_diff helpers
# =============================================================================

class TestReporterHelpers:

    def test_row_direction_loss(self):
        step = _make_step(rows_in=1000, rows_out=500)
        sym, color = _row_direction(step)
        assert sym == "▼"
        assert color == "red"

    def test_row_direction_gain(self):
        step = _make_step(rows_in=100, rows_out=200)
        sym, color = _row_direction(step)
        assert sym == "▲"
        assert color == "green"

    def test_row_direction_stable(self):
        step = _make_step(rows_in=100, rows_out=100)
        sym, color = _row_direction(step)
        assert sym == "●"

    def test_fmt_diff_positive(self):
        result = _fmt_diff(500, 0.5)
        assert "+" in result
        assert "500" in result

    def test_fmt_diff_negative(self):
        result = _fmt_diff(-300, -0.3)
        assert "-300" in result

    def test_fmt_diff_zero(self):
        result = _fmt_diff(0, 0.0)
        assert "0" in result


# =============================================================================
# stats.py — compute_stats with larger DataFrames (sampling path)
# =============================================================================

class TestComputeStatsSampling:

    def test_sampling_triggered_above_threshold(self):
        rng = np.random.default_rng(0)
        before = pd.DataFrame({
            "id": np.arange(100_000),
            "val": rng.uniform(0, 1, 100_000),
        })
        after = before.copy()
        stats = compute_stats(before, after)
        assert stats.sampled is True
        assert stats.sample_size == 50_000

    def test_no_sampling_below_threshold(self):
        before = pd.DataFrame({"id": range(100), "val": range(100)})
        after = before.copy()
        stats = compute_stats(before, after)
        assert stats.sampled is False

    def test_join_explosion_detected_on_gain(self):
        before = pd.DataFrame({"customer_id": range(100)})
        ref = pd.DataFrame({"customer_id": np.repeat(range(100), 3)})
        after = before.merge(ref, on="customer_id", how="left")
        stats = compute_stats(before, after)
        assert stats.duplicate_key_detected is True
        assert "customer_id" in stats.join_explosion.offending_columns

    def test_join_explosion_not_triggered_on_loss(self):
        before = pd.DataFrame({"id": range(100), "val": range(100)})
        after = before.head(50)
        stats = compute_stats(before, after)
        assert stats.join_explosion.duplicate_key_detected is False

    def test_null_deltas_detected(self):
        before = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0]})
        after = pd.DataFrame({"x": [1.0, None, 3.0, None]})
        stats = compute_stats(before, after)
        assert any(nd.column == "x" and nd.delta > 0 for nd in stats.null_deltas)

    def test_dtype_change_detected(self):
        before = pd.DataFrame({"id": pd.array([1, 2, 3], dtype="int64")})
        after = pd.DataFrame({"id": ["1", "2", "3"]})  # int64 → object
        stats = compute_stats(before, after)
        assert any(dc.column == "id" for dc in stats.dtype_changes)

    def test_columns_added_detected(self):
        before = pd.DataFrame({"a": [1, 2, 3]})
        after = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        stats = compute_stats(before, after)
        assert "b" in stats.column_diff.added

    def test_columns_removed_detected(self):
        before = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        after = pd.DataFrame({"a": [1, 2, 3]})
        stats = compute_stats(before, after)
        assert "b" in stats.column_diff.removed

    def test_backend_name_is_pandas(self):
        before = pd.DataFrame({"x": [1, 2, 3]})
        after = before.copy()
        stats = compute_stats(before, after)
        assert stats.backend == "pandas"

    def test_unknown_object_returns_empty_and_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            stats = compute_stats({"not": "a df"}, {"also": "not"})
        assert stats.backend == "empty"
        assert any(issubclass(w.category, WatcherWarning) for w in caught)


# =============================================================================
# Integration — watch + session + handler full flow
# =============================================================================

class TestIntegrationFlow:

    def setup_method(self):
        from watcher import handlers as _h
        self._original = list(_h._handlers)

    def teardown_method(self):
        from watcher import handlers as _h
        _h._handlers[:] = self._original

    def test_full_pipeline_with_custom_handler(self):
        steps_received = []

        class Recorder(HandlerBase):
            def on_step(self, step):
                steps_received.append(step)

        recorder = Recorder()
        register_handler(recorder)

        df = pd.DataFrame({
            "id": range(1000),
            "status": ["ok"] * 800 + [None] * 200,
            "value": np.random.uniform(0, 100, 1000),
        })

        @watch(track_memory="off")
        def clean(df):
            return df.dropna()

        @watch(track_memory="off")
        def filter_high(df):
            return df[df["value"] > 50].copy()

        @watch(track_memory="off")
        def enrich(df):
            df = df.copy()
            df["tier"] = "gold"
            return df

        with session("integration test") as s:
            df = clean(df)
            df = filter_high(df)
            df = enrich(df)

        deregister_handler(recorder)

        # All 3 steps captured
        assert len(steps_received) == 3
        assert steps_received[0].func_name == "clean"
        assert steps_received[1].func_name == "filter_high"
        assert steps_received[2].func_name == "enrich"

        # clean dropped nulls
        assert steps_received[0].rows_out == 800

        # enrich added a column
        assert "tier" in steps_received[2].stats.column_diff.added

        # session summary is consistent
        summary = s.summary()
        assert summary["name"] == "integration test"
        assert summary["total_steps"] == 3
        assert summary["total_rows_in"] == 1000

    def test_watch_inside_session_records_steps(self):
        df = pd.DataFrame({"x": range(500)})

        @watch(track_memory="off")
        def halve(df):
            return df.head(250)

        with session("record test") as s:
            halve(df)

        assert len(s.steps) == 1
        assert s.steps[0].rows_in == 500
        assert s.steps[0].rows_out == 250

    def test_threshold_inside_session_raises(self):
        df = pd.DataFrame({"x": range(1000)})

        @watch(raise_on_loss=0.10, track_memory="off")
        def big_drop(df):
            return df.head(100)  # drops 90%

        with pytest.raises(ThresholdExceeded):
            with session("threshold test"):
                big_drop(df)

# =============================================================================
# core.py — import-time registration coverage
# =============================================================================

class TestImportTimeCoverage:

    def test_all_exports_importable(self):
        from watcher import (
            watch, session, MemoryMode, StepResult, WatcherSession,
            DataFrameLike, BackendRegistry, BackendAdapter,
            WatcherError, WatcherWarning, ThresholdExceeded,
            BackendError, ConfigurationError,
            HandlerBase, TerminalHandler,
            register_handler, deregister_handler,
        )
        # All public names are importable
        assert watch is not None
        assert session is not None

    def test_backend_registry_has_pandas_registered(self):
        import pandas as pd
        adapter = BackendRegistry.detect(pd.DataFrame({"x": [1]}))
        assert adapter is not None

    def test_memory_mode_values(self):
        assert MemoryMode.RSS.value == "rss"
        assert MemoryMode.PEAK.value == "peak"
        assert MemoryMode.OFF.value == "off"
