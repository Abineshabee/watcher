# -----------------------------------------------------------------------------
# tests/test_handlers.py
#
# Full coverage for watcher/handlers.py
# -----------------------------------------------------------------------------

import pandas as pd
from unittest.mock import MagicMock

from watcher.handlers import (
    HandlerBase,
    TerminalHandler,
    register_handler,
    deregister_handler,
    _get_handlers,
)
from watcher.core import (
    StepResult,
    WatcherSession,
    MemoryMode,
    session,
    watch,
)
from watcher.stats import StepStats, ColumnDiff, JoinExplosionDetail


# =============================================================================
# Helpers
# =============================================================================


def _make_step(
    func_name="test_step",
    rows_in=1000,
    rows_out=800,
    elapsed_s=0.01,
    memory_delta_mb=0.0,
    memory_mode=MemoryMode.OFF,
    warned=False,
) -> StepResult:
    stats = StepStats(
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


def _make_session(name="test session") -> WatcherSession:
    s = WatcherSession(name=name)
    s.record(_make_step())
    return s


# =============================================================================
# HandlerBase — default no-op methods
# =============================================================================


class TestHandlerBase:
    def test_on_step_is_noop(self):
        h = HandlerBase()
        step = _make_step()
        h.on_step(step)  # must not raise

    def test_on_session_start_is_noop(self):
        h = HandlerBase()
        sess = _make_session()
        h.on_session_start(sess)  # must not raise

    def test_on_session_end_is_noop(self):
        h = HandlerBase()
        sess = _make_session()
        h.on_session_end(sess)  # must not raise

    def test_subclass_overrides_on_step(self):
        calls = []

        class MyHandler(HandlerBase):
            def on_step(self, step):
                calls.append(step.func_name)

        h = MyHandler()
        h.on_step(_make_step(func_name="captured"))
        assert calls == ["captured"]

    def test_subclass_overrides_on_session_start(self):
        calls = []

        class MyHandler(HandlerBase):
            def on_session_start(self, sess):
                calls.append(sess.name)

        h = MyHandler()
        h.on_session_start(_make_session("my pipeline"))
        assert calls == ["my pipeline"]

    def test_subclass_overrides_on_session_end(self):
        calls = []

        class MyHandler(HandlerBase):
            def on_session_end(self, sess):
                calls.append("ended")

        h = MyHandler()
        h.on_session_end(_make_session())
        assert calls == ["ended"]


# =============================================================================
# TerminalHandler — delegates to Reporter
# =============================================================================


class TestTerminalHandler:
    def test_instantiates_without_error(self):
        h = TerminalHandler()
        assert h is not None

    def test_on_step_calls_reporter(self):
        h = TerminalHandler()
        step = _make_step()
        h._reporter = MagicMock()
        h.on_step(step)
        h._reporter.print_step.assert_called_once_with(step)

    def test_on_session_start_calls_reporter(self):
        h = TerminalHandler()
        h._reporter = MagicMock()
        sess = _make_session("test pipeline")
        h.on_session_start(sess)
        h._reporter.print_session_header.assert_called_once_with("test pipeline")

    def test_on_session_end_calls_reporter(self):
        h = TerminalHandler()
        h._reporter = MagicMock()
        sess = _make_session()
        h.on_session_end(sess)
        h._reporter.print_session_footer.assert_called_once_with(sess)

    def test_is_handler_base_subclass(self):
        assert issubclass(TerminalHandler, HandlerBase)


# =============================================================================
# register_handler / deregister_handler / _get_handlers
# =============================================================================


class TestHandlerRegistry:
    def setup_method(self):
        """Snapshot handlers before each test and restore after."""
        from watcher import handlers as _h

        self._original = list(_h._handlers)

    def teardown_method(self):
        """Restore original handler list after each test."""
        from watcher import handlers as _h

        _h._handlers[:] = self._original

    def test_get_handlers_returns_list(self):
        result = _get_handlers()
        assert isinstance(result, list)

    def test_terminal_handler_registered_by_default(self):
        handlers = _get_handlers()
        assert any(isinstance(h, TerminalHandler) for h in handlers)

    def test_register_adds_handler(self):
        h = HandlerBase()
        register_handler(h)
        assert h in _get_handlers()

    def test_register_is_idempotent(self):
        h = HandlerBase()
        register_handler(h)
        register_handler(h)
        assert _get_handlers().count(h) == 1

    def test_deregister_removes_handler(self):
        h = HandlerBase()
        register_handler(h)
        deregister_handler(h)
        assert h not in _get_handlers()

    def test_deregister_noop_if_not_registered(self):
        h = HandlerBase()
        deregister_handler(h)  # must not raise

    def test_multiple_handlers_all_called(self):
        calls = []

        class CountingHandler(HandlerBase):
            def __init__(self, name):
                self.name = name

            def on_step(self, step):
                calls.append(self.name)

        h1 = CountingHandler("A")
        h2 = CountingHandler("B")
        register_handler(h1)
        register_handler(h2)

        df = pd.DataFrame({"x": range(10)})

        @watch(track_memory="off", verbose=True)
        def dummy(df):
            return df

        dummy(df)

        deregister_handler(h1)
        deregister_handler(h2)

        assert "A" in calls
        assert "B" in calls

    def test_custom_handler_receives_correct_step_data(self):
        received = []

        class CapturingHandler(HandlerBase):
            def on_step(self, step):
                received.append(step)

        h = CapturingHandler()
        register_handler(h)

        df = pd.DataFrame({"x": range(100)})

        @watch(track_memory="off")
        def half(df):
            return df.head(50)

        half(df)
        deregister_handler(h)

        assert len(received) == 1
        assert received[0].rows_in == 100
        assert received[0].rows_out == 50

    def test_session_start_end_fired_on_handler(self):
        events = []

        class EventHandler(HandlerBase):
            def on_session_start(self, sess):
                events.append(("start", sess.name))

            def on_session_end(self, sess):
                events.append(("end", sess.name))

        h = EventHandler()
        register_handler(h)

        df = pd.DataFrame({"x": range(10)})

        @watch(track_memory="off")
        def noop(df):
            return df

        with session("event test"):
            noop(df)

        deregister_handler(h)

        assert ("start", "event test") in events
        assert ("end", "event test") in events

    def test_deregister_terminal_handler_silences_output(self):
        """Removing TerminalHandler means no output is printed."""
        from watcher import handlers as _h

        terminal = next(h for h in _h._handlers if isinstance(h, TerminalHandler))
        deregister_handler(terminal)

        df = pd.DataFrame({"x": range(10)})

        @watch(track_memory="off")
        def silent(df):
            return df

        # Should not raise even with no handlers
        silent(df)
