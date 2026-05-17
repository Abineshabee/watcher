# -----------------------------------------------------------------------------
# watcher/exceptions.py
#
# Public exception and warning hierarchy for the watcher library.
#
# All user-facing errors and warnings inherit from a single base class
# (WatcherError / WatcherWarning) so callers can catch the entire family
# with one except clause while still handling specific conditions
# individually when needed.
#
# License : MIT
# Docs    : https://github.com/Abineshabee/watcher
# -----------------------------------------------------------------------------

from __future__ import annotations

__all__ = [
    "WatcherError",
    "WatcherWarning",
    "ThresholdExceeded",
    "BackendError",
    "ConfigurationError",
]


# -----------------------------------------------------------------------------
# Base classes
# -----------------------------------------------------------------------------


class WatcherError(Exception):
    """
    Root exception for all watcher runtime errors.

    Catching ``WatcherError`` is sufficient to handle every hard failure
    this library can raise.  Sub-class to handle specific conditions.

    Hierarchy
    ---------
    ::

        WatcherError
        ├── ThresholdExceeded
        ├── BackendError
        └── ConfigurationError
    """


class WatcherWarning(UserWarning):
    """
    Root warning for all watcher soft-failure conditions.

    Issued via :func:`warnings.warn` rather than ``raise`` so pipelines
    can continue running while still surfacing the problem.

    To turn all watcher warnings into hard errors during testing::

        import warnings
        warnings.filterwarnings("error", category=WatcherWarning)

    To silence them entirely::

        warnings.filterwarnings("ignore", category=WatcherWarning)
    """


# -----------------------------------------------------------------------------
# Specific exceptions
# -----------------------------------------------------------------------------


class ThresholdExceeded(WatcherError):
    """
    Raised when a row-change threshold configured on ``@watch`` is breached.

    Triggered by ``raise_on_gain`` or ``raise_on_loss`` arguments to the
    decorator.  Use :class:`WatcherWarning` (via ``warn_on_gain`` /
    ``warn_on_loss``) for non-fatal threshold breaches.

    Attributes
    ----------
    message : str
        Human-readable description of the breach including step name,
        row counts, percentage change, and (where applicable) a hint
        about the likely root cause (e.g. join fan-out).

    Example
    -------
    >>> @watch(raise_on_loss=0.02)
    ... def filter_active(df):
    ...     return df[df["status"] == "active"]
    ...
    ... # Raises ThresholdExceeded if more than 2 % of rows are dropped.

    Notes
    -----
    In CI pipelines, catching ``ThresholdExceeded`` lets you emit a
    structured failure message before re-raising::

        try:
            result = pipeline(df)
        except ThresholdExceeded as exc:
            logger.error("Pipeline data contract violated: %s", exc)
            raise
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class BackendError(WatcherError):
    """
    Raised when a backend adapter fails to introspect a DataFrame.

    Covers situations such as:

    * A registered :class:`~watcher.core.BackendAdapter` raising an
      unexpected exception during ``null_counts()`` or ``dtypes()``.
    * An object passing the :class:`~watcher.core.DataFrameLike`
      protocol check but failing at runtime (e.g. a partially
      implemented wrapper class).

    Attributes
    ----------
    backend_name : str
        The name of the backend that failed (e.g. ``"pandas"``).
    original : Exception
        The underlying exception raised by the backend.

    Example
    -------
    >>> raise BackendError(
    ...     backend_name="pandas",
    ...     original=ValueError("..."),
    ...     message="null_counts() failed on step 'clean'",
    ... )
    """

    def __init__(
        self,
        message: str,
        *,
        backend_name: str = "unknown",
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.backend_name = backend_name
        self.original = original

    def __str__(self) -> str:
        parts = [f"[watcher/{self.backend_name}] {self.message}"]
        if self.original is not None:
            parts.append(f"  Caused by: {type(self.original).__name__}: {self.original}")
        return "\n".join(parts)


class ConfigurationError(WatcherError):
    """
    Raised when the library is misconfigured before any data flows.

    Triggered by invalid arguments (e.g. an unknown ``MemoryMode``
    string, a threshold value outside ``[0.0, 1.0]``, or registering
    a backend adapter that does not implement the required interface).

    Example
    -------
    >>> @watch(raise_on_loss=1.5)   # invalid — must be in [0.0, 1.0]
    ... def clean(df): ...
    ...
    ... # Raises ConfigurationError at decoration time.
    """
