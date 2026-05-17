# -----------------------------------------------------------------------------
# watcher/handlers.py
#
# Event-driven handler layer.
#
# core.py never prints anything directly — it calls on_step / on_session_end
# on every registered handler.  This keeps the engine decoupled from any one
# rendering target (terminal, notebook, JSON logger, CI mode, …).
#
# Public surface
# --------------
# HandlerBase      — base class / interface every handler must implement
# TerminalHandler  — default handler: delegates to reporter.Reporter
# _get_handlers()  — returns the process-global handler list
# register_handler()   — add a custom handler
# deregister_handler() — remove a handler
#
# License : MIT
# Docs    : https://github.com/Abineshabee/watcher
# -----------------------------------------------------------------------------

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from watcher.core import StepResult, WatcherSession

__all__ = [
    "HandlerBase",
    "TerminalHandler",
    "register_handler",
    "deregister_handler",
    "_get_handlers",
]


# ---------------------------------------------------------------------------
# HandlerBase — interface every handler must satisfy
# ---------------------------------------------------------------------------


class HandlerBase:
    """
    Base class for watcher event handlers.

    Subclass this and override the methods you care about, then register
    your handler via :func:`register_handler`.

    All three methods have no-op defaults so subclasses only override
    what they need.
    """

    def on_session_start(self, session: WatcherSession) -> None:
        """Called once when a :func:`~watcher.core.session` block opens."""

    def on_step(self, step: StepResult) -> None:
        """Called once per decorated function call, after execution."""

    def on_session_end(self, session: WatcherSession) -> None:
        """Called once when a :func:`~watcher.core.session` block closes."""


# ---------------------------------------------------------------------------
# TerminalHandler — default implementation via Reporter
# ---------------------------------------------------------------------------


class TerminalHandler(HandlerBase):
    """
    Default handler — renders output to the terminal via
    :class:`~watcher.reporter.Reporter`.

    Installed automatically as the sole handler when the library is first
    imported.  Replace or extend it by calling :func:`register_handler`.
    """

    def __init__(self) -> None:
        # Lazy import to avoid circular imports at module level
        from watcher.reporter import Reporter

        self._reporter = Reporter()

    def on_session_start(self, session: WatcherSession) -> None:
        self._reporter.print_session_header(session.name)

    def on_step(self, step: StepResult) -> None:
        self._reporter.print_step(step)

    def on_session_end(self, session: WatcherSession) -> None:
        self._reporter.print_session_footer(session)


# ---------------------------------------------------------------------------
# Process-global handler registry
# ---------------------------------------------------------------------------

_handlers: List[HandlerBase] = [TerminalHandler()]


def _get_handlers() -> List[HandlerBase]:
    """Return the current list of registered handlers (internal use)."""
    return _handlers


def register_handler(handler: HandlerBase) -> None:
    """
    Add *handler* to the global handler list.

    Parameters
    ----------
    handler : HandlerBase
        The handler instance to register.
    """
    if handler not in _handlers:
        _handlers.append(handler)


def deregister_handler(handler: HandlerBase) -> None:
    """
    Remove *handler* from the global handler list.

    Parameters
    ----------
    handler : HandlerBase
        The handler instance to remove.  No-op if it was never registered.
    """
    try:
        _handlers.remove(handler)
    except ValueError:
        pass
