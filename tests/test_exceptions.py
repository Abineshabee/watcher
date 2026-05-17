# -----------------------------------------------------------------------------
# tests/test_exceptions.py
#
# Full coverage for watcher/exceptions.py
# -----------------------------------------------------------------------------

import pytest
from watcher.exceptions import (
    WatcherError,
    WatcherWarning,
    ThresholdExceeded,
    BackendError,
    ConfigurationError,
)


# =============================================================================
# WatcherError — base exception
# =============================================================================

class TestWatcherError:

    def test_is_exception(self):
        assert issubclass(WatcherError, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(WatcherError):
            raise WatcherError("base error")

    def test_message_preserved(self):
        exc = WatcherError("something went wrong")
        assert "something went wrong" in str(exc)

    def test_subclasses_catchable_as_watcher_error(self):
        with pytest.raises(WatcherError):
            raise ThresholdExceeded("threshold breach")

    def test_backend_error_catchable_as_watcher_error(self):
        with pytest.raises(WatcherError):
            raise BackendError("backend failed")

    def test_configuration_error_catchable_as_watcher_error(self):
        with pytest.raises(WatcherError):
            raise ConfigurationError("bad config")


# =============================================================================
# WatcherWarning — base warning
# =============================================================================

class TestWatcherWarning:

    def test_is_user_warning(self):
        assert issubclass(WatcherWarning, UserWarning)

    def test_can_be_issued(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.warn("soft alert", WatcherWarning)
        assert len(caught) == 1
        assert issubclass(caught[0].category, WatcherWarning)

    def test_can_be_caught_by_category(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", WatcherWarning)
            warnings.warn("test", WatcherWarning)
        assert any(issubclass(w.category, WatcherWarning) for w in caught)

    def test_can_be_turned_into_error(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", WatcherWarning)
            with pytest.raises(WatcherWarning):
                warnings.warn("becomes error", WatcherWarning)


# =============================================================================
# ThresholdExceeded
# =============================================================================

class TestThresholdExceeded:

    def test_is_watcher_error(self):
        assert issubclass(ThresholdExceeded, WatcherError)

    def test_message_attribute(self):
        exc = ThresholdExceeded("loss exceeded 20%")
        assert exc.message == "loss exceeded 20%"

    def test_str_returns_message(self):
        exc = ThresholdExceeded("loss exceeded 20%")
        assert str(exc) == "loss exceeded 20%"

    def test_can_be_raised_and_caught_specifically(self):
        with pytest.raises(ThresholdExceeded) as exc_info:
            raise ThresholdExceeded("breach")
        assert exc_info.value.message == "breach"

    def test_can_be_caught_as_watcher_error(self):
        with pytest.raises(WatcherError):
            raise ThresholdExceeded("breach")

    def test_message_with_multiline_detail(self):
        msg = "[watcher] 'step' breached raise_on_loss=2.0%.\n  Rows: 1000 → 500"
        exc = ThresholdExceeded(msg)
        assert "raise_on_loss" in str(exc)
        assert "Rows" in str(exc)


# =============================================================================
# BackendError
# =============================================================================

class TestBackendError:

    def test_is_watcher_error(self):
        assert issubclass(BackendError, WatcherError)

    def test_basic_message(self):
        exc = BackendError("null_counts failed")
        assert "null_counts failed" in str(exc)

    def test_backend_name_in_str(self):
        exc = BackendError("failed", backend_name="pandas")
        assert "pandas" in str(exc)

    def test_original_exception_in_str(self):
        original = ValueError("underlying cause")
        exc = BackendError("failed", backend_name="pandas", original=original)
        result = str(exc)
        assert "ValueError" in result
        assert "underlying cause" in result

    def test_default_backend_name_is_unknown(self):
        exc = BackendError("failed")
        assert exc.backend_name == "unknown"

    def test_original_defaults_to_none(self):
        exc = BackendError("failed")
        assert exc.original is None

    def test_str_without_original(self):
        exc = BackendError("something broke", backend_name="polars")
        result = str(exc)
        assert "[watcher/polars]" in result
        assert "something broke" in result
        assert "Caused by" not in result

    def test_str_with_original(self):
        exc = BackendError(
            "compute failed",
            backend_name="pandas",
            original=RuntimeError("OOM"),
        )
        result = str(exc)
        assert "Caused by" in result
        assert "RuntimeError" in result
        assert "OOM" in result

    def test_message_attribute(self):
        exc = BackendError("msg", backend_name="x")
        assert exc.message == "msg"

    def test_can_be_raised_and_caught(self):
        with pytest.raises(BackendError) as exc_info:
            raise BackendError("boom", backend_name="pandas")
        assert exc_info.value.backend_name == "pandas"


# =============================================================================
# ConfigurationError
# =============================================================================

class TestConfigurationError:

    def test_is_watcher_error(self):
        assert issubclass(ConfigurationError, WatcherError)

    def test_message_preserved(self):
        exc = ConfigurationError("raise_on_loss must be in [0.0, 1.0]")
        assert "raise_on_loss" in str(exc)

    def test_can_be_raised_and_caught_specifically(self):
        with pytest.raises(ConfigurationError):
            raise ConfigurationError("bad argument")

    def test_can_be_caught_as_watcher_error(self):
        with pytest.raises(WatcherError):
            raise ConfigurationError("bad argument")

    def test_empty_message(self):
        exc = ConfigurationError("")
        assert str(exc) == ""
