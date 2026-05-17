# -----------------------------------------------------------------------------
# tests/test_reporter.py
#
# Tests for watcher/reporter.py
#
# Coverage targets
# ----------------
# * Reporter instantiation and default arguments
# * Reporter.print_session_header — Rich and plain paths
# * Reporter.print_session_footer — Rich and plain paths
# * Reporter.print_step — all optional sections (schema drift, null deltas,
#   join detail, sampling footnote)
# * _row_direction — gain / loss / stable
# * _fmt_diff — positive, negative, zero
# * _PlainConsole.print — Rich markup stripping
# * _PlainConsole.rule — with and without title
# * Graceful degradation when Rich is absent
#
# Run with:
#   pytest tests/test_reporter.py -v
# -----------------------------------------------------------------------------

from __future__ import annotations

from io import StringIO
from typing import List
from unittest.mock import patch

import pytest

try:
    from rich.console import Console
except ImportError:
    Console = None  # type: ignore[assignment,misc]

from watcher.reporter import (
    Reporter,
    _PlainConsole,
    _fmt_diff,
    _row_direction,
)
from watcher.core import MemoryMode, StepResult, WatcherSession
from watcher.stats import (
    ColumnDiff,
    DtypeChange,
    JoinExplosionDetail,
    NullDelta,
    StepStats,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stats(
    added: List[str] | None = None,
    removed: List[str] | None = None,
    dtype_changes: List[DtypeChange] | None = None,
    null_deltas: List[NullDelta] | None = None,
    duplicate_key: bool = False,
    duplication_ratio: float = 0.0,
    sampled: bool = False,
    sample_size: int = 0,
) -> StepStats:
    return StepStats(
        column_diff=ColumnDiff(added=added or [], removed=removed or []),
        dtype_changes=dtype_changes or [],
        null_deltas=null_deltas or [],
        duplicate_key_detected=duplicate_key,
        join_explosion=JoinExplosionDetail(
            duplicate_key_detected=duplicate_key,
            offending_columns=["customer_id"] if duplicate_key else [],
            top_offenders={"customer_id": [(42, 50)]} if duplicate_key else {},
            duplication_ratio=duplication_ratio,
        ),
        sampled=sampled,
        sample_size=sample_size,
    )


def _make_step(
    func_name: str = "clean",
    rows_in: int = 1000,
    rows_out: int = 900,
    elapsed_s: float = 0.05,
    memory_delta_mb: float = 1.5,
    memory_mode: MemoryMode = MemoryMode.OFF,
    stats: StepStats | None = None,
    warned: bool = False,
) -> StepResult:
    return StepResult(
        func_name=func_name,
        rows_in=rows_in,
        rows_out=rows_out,
        elapsed_s=elapsed_s,
        memory_delta_mb=memory_delta_mb,
        memory_mode=memory_mode,
        stats=stats or _make_stats(),
        warned=warned,
    )


def _make_session(steps: List[StepResult] | None = None) -> WatcherSession:
    sess = WatcherSession(name="test pipeline")
    for step in steps or [_make_step()]:
        sess.record(step)
    return sess


# ---------------------------------------------------------------------------
# Capture plain-text output from Reporter by redirecting stdout
# ---------------------------------------------------------------------------


def _capture(fn):
    """Call fn(), capture everything written to stdout, return as str."""
    buf = StringIO()
    with patch("watcher.reporter._console", None):
        # Patch out Rich so we always get plain text in tests
        import watcher.reporter as reporter_mod

        original = reporter_mod._RICH_AVAILABLE
        reporter_mod._RICH_AVAILABLE = False
        try:
            with patch("sys.stdout", buf):
                fn()
        finally:
            reporter_mod._RICH_AVAILABLE = original
    return buf.getvalue()


# ===========================================================================
# _PlainConsole
# ===========================================================================


class TestPlainConsole:
    def test_print_strips_rich_markup(self):
        buf = StringIO()
        console = _PlainConsole()
        with patch("sys.stdout", buf):
            console.print("[bold red]hello[/bold red]")
        assert "hello" in buf.getvalue()
        assert "[" not in buf.getvalue()

    def test_print_handles_multiple_args(self):
        buf = StringIO()
        console = _PlainConsole()
        with patch("sys.stdout", buf):
            console.print("a", "b", "c")
        assert "a b c" in buf.getvalue()

    def test_rule_with_title(self):
        buf = StringIO()
        console = _PlainConsole()
        with patch("sys.stdout", buf):
            console.rule("My Title")
        output = buf.getvalue()
        assert "My Title" in output
        assert "─" in output

    def test_rule_without_title(self):
        buf = StringIO()
        console = _PlainConsole()
        with patch("sys.stdout", buf):
            console.rule()
        assert "─" in buf.getvalue()


# ===========================================================================
# _row_direction
# ===========================================================================


class TestRowDirection:
    def test_loss(self):
        step = _make_step(rows_in=100, rows_out=80)
        sym, color = _row_direction(step)
        assert "▼" in sym
        assert color == "red"

    def test_gain(self):
        step = _make_step(rows_in=100, rows_out=120)
        sym, color = _row_direction(step)
        assert "▲" in sym
        assert color == "green"

    def test_stable(self):
        step = _make_step(rows_in=100, rows_out=100)
        sym, color = _row_direction(step)
        assert "●" in sym
        assert "dim" in color


# ===========================================================================
# _fmt_diff
# ===========================================================================


class TestFmtDiff:
    def test_positive_diff(self):
        s = _fmt_diff(500, 0.10)
        assert "+500" in s
        assert "10.0%" in s

    def test_negative_diff(self):
        s = _fmt_diff(-200, -0.20)
        assert "-200" in s

    def test_zero_diff(self):
        s = _fmt_diff(0, 0.0)
        assert "0" in s
        assert "0.0%" in s


# ===========================================================================
# Reporter — instantiation
# ===========================================================================


class TestReporterInit:
    def test_defaults(self):
        r = Reporter()
        assert r.width == 100
        assert r.show_memory is True
        assert r.show_null_deltas is True
        assert r.show_schema_drift is True
        assert r.show_join_detail is True

    def test_custom_args(self):
        r = Reporter(width=60, show_memory=False, show_null_deltas=False)
        assert r.width == 60
        assert r.show_memory is False
        assert r.show_null_deltas is False


# ===========================================================================
# Reporter.print_session_header
# ===========================================================================


class TestPrintSessionHeader:
    def test_plain_contains_session_name(self):
        r = Reporter()
        output = _capture(lambda: r.print_session_header("nightly ETL"))
        assert "nightly ETL" in output

    def test_rich_path_called_when_rich_available(self):
        """When Rich is available, the rule should render without error."""
        import watcher.reporter as reporter_mod

        if not reporter_mod._RICH_AVAILABLE:
            pytest.skip("Rich not installed")

        r = Reporter()
        # We just verify it runs without exception; visual output is Rich's concern
        with patch("watcher.reporter._console", None):
            r.print_session_header("ETL test")


# ===========================================================================
# Reporter.print_step
# ===========================================================================


class TestPrintStep:
    def test_plain_step_contains_func_name(self):
        r = Reporter()
        step = _make_step(func_name="my_transform")
        output = _capture(lambda: r.print_step(step))
        assert "my_transform" in output

    def test_plain_step_contains_row_counts(self):
        r = Reporter()
        step = _make_step(rows_in=1000, rows_out=800)
        output = _capture(lambda: r.print_step(step))
        assert "1,000" in output
        assert "800" in output

    def test_schema_drift_added_shown(self):
        r = Reporter()
        step = _make_step(stats=_make_stats(added=["churn_score"]))
        output = _capture(lambda: r.print_step(step))
        assert "churn_score" in output

    def test_schema_drift_removed_shown(self):
        r = Reporter()
        step = _make_step(stats=_make_stats(removed=["raw_json"]))
        output = _capture(lambda: r.print_step(step))
        assert "raw_json" in output

    def test_schema_drift_hidden_when_show_false(self):
        r = Reporter(show_schema_drift=False)
        step = _make_step(stats=_make_stats(added=["churn_score"]))
        output = _capture(lambda: r.print_step(step))
        assert "churn_score" not in output

    def test_null_deltas_shown(self):
        r = Reporter()
        nd = NullDelta(column="age", before=0, after=5, delta=5)
        step = _make_step(stats=_make_stats(null_deltas=[nd]))
        output = _capture(lambda: r.print_step(step))
        assert "age" in output

    def test_null_deltas_hidden_when_show_false(self):
        r = Reporter(show_null_deltas=False)
        nd = NullDelta(column="age", before=0, after=5, delta=5)
        step = _make_step(stats=_make_stats(null_deltas=[nd]))
        output = _capture(lambda: r.print_step(step))
        assert "age" not in output

    def test_join_explosion_shown(self):
        r = Reporter()
        step = _make_step(
            rows_in=100,
            rows_out=160,  # > 50 % gain → heuristic fires even without key
            stats=_make_stats(
                duplicate_key=True,
                duplication_ratio=0.6,
            ),
        )
        output = _capture(lambda: r.print_step(step))
        # customer_id is in the top_offenders for duplicate_key=True
        assert "customer_id" in output or "join explosion" in output.lower()

    def test_join_detail_hidden_when_show_false(self):
        r = Reporter(show_join_detail=False)
        step = _make_step(
            rows_in=100,
            rows_out=160,
            stats=_make_stats(duplicate_key=True, duplication_ratio=0.6),
        )
        output = _capture(lambda: r.print_step(step))
        assert "customer_id" not in output

    def test_sampling_footnote_shown(self):
        import watcher.reporter as reporter_mod

        if not reporter_mod._RICH_AVAILABLE:
            pytest.skip("Rich not installed — sampling footnote is Rich-only")

        r = Reporter()
        step = _make_step(stats=_make_stats(sampled=True, sample_size=50_000))

        buf = StringIO()
        real_console = reporter_mod.Console(file=buf, highlight=False)
        with patch("watcher.reporter._console", real_console):
            r.print_step(step)

        output = buf.getvalue()
        assert "sample" in output.lower() or "50,000" in output

    def test_memory_shown_by_default(self):
        r = Reporter()
        step = _make_step(memory_delta_mb=3.14, memory_mode=MemoryMode.OFF)
        output = _capture(lambda: r.print_step(step))
        # The plain renderer includes "mem" when show_memory=True
        assert "mem" in output.lower() or "3.1" in output

    def test_memory_hidden_when_show_false(self):
        r = Reporter(show_memory=False)
        step = _make_step(memory_delta_mb=3.14, memory_mode=MemoryMode.OFF)
        output = _capture(lambda: r.print_step(step))
        assert "3.1" not in output


# ===========================================================================
# Reporter.print_session_footer
# ===========================================================================


class TestPrintSessionFooter:
    def test_plain_footer_contains_step_names(self):
        r = Reporter()
        sess = _make_session(
            [
                _make_step(func_name="clean"),
                _make_step(func_name="merge", rows_in=900, rows_out=950),
            ]
        )
        output = _capture(lambda: r.print_session_footer(sess))
        assert "clean" in output
        assert "merge" in output

    def test_plain_footer_contains_total(self):
        r = Reporter()
        sess = _make_session()
        output = _capture(lambda: r.print_session_footer(sess))
        assert "TOTAL" in output.upper()

    def test_plain_footer_contains_session_name(self):
        r = Reporter()
        sess = _make_session()
        output = _capture(lambda: r.print_session_footer(sess))
        assert "test pipeline" in output

    def test_rich_session_footer_runs_without_error(self):
        import watcher.reporter as reporter_mod

        if not reporter_mod._RICH_AVAILABLE:
            pytest.skip("Rich not installed")

        r = Reporter()
        sess = _make_session()
        # Just verify no exception — Rich output goes to its own console
        with patch("watcher.reporter._console", None):
            r.print_session_footer(sess)


# ===========================================================================
# Graceful degradation — Rich absent
# ===========================================================================


class TestRichDegradation:
    def test_print_step_works_without_rich(self):
        import watcher.reporter as reporter_mod

        original = reporter_mod._RICH_AVAILABLE
        try:
            reporter_mod._RICH_AVAILABLE = False
            reporter_mod._console = None  # force recreation
            r = Reporter()
            buf = StringIO()
            with patch("sys.stdout", buf):
                r.print_step(_make_step())
            assert "clean" in buf.getvalue()
        finally:
            reporter_mod._RICH_AVAILABLE = original
            reporter_mod._console = None

    def test_session_header_works_without_rich(self):
        import watcher.reporter as reporter_mod

        original = reporter_mod._RICH_AVAILABLE
        try:
            reporter_mod._RICH_AVAILABLE = False
            reporter_mod._console = None
            r = Reporter()
            buf = StringIO()
            with patch("sys.stdout", buf):
                r.print_session_header("test")
            assert "test" in buf.getvalue()
        finally:
            reporter_mod._RICH_AVAILABLE = original
            reporter_mod._console = None
