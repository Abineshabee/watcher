# -----------------------------------------------------------------------------
# watcher/reporter.py
#
# Terminal output formatting via Rich.
#
# This module is intentionally decoupled from core.py — it receives
# StepResult / WatcherSession objects and renders them; it never imports
# anything from watcher.core.  That one-way dependency keeps the render
# layer swappable (notebook HTML, JSON logger, CI mode) without touching
# the engine.
#
# Rich is a soft dependency.  When it is absent, the module falls back to
# plain-text output so watcher stays usable in minimal environments.
#
# License : MIT
# Docs    : https://github.com/Abineshabee/watcher
# -----------------------------------------------------------------------------

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Avoid circular imports — core types are referenced by annotation only.
    from watcher.core import StepResult, WatcherSession

__all__ = ["Reporter"]

# -----------------------------------------------------------------------------
# Rich availability — graceful degradation
# -----------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RICH_AVAILABLE = False

# One shared console instance — avoids re-creating it on every print_step call
_console: Any = None


def _get_console() -> Any:
    """
    Return the module-level Rich ``Console``, creating it on first access.

    Returns a no-op stub when Rich is not installed so callers never need
    to guard against ``None``.
    """
    global _console
    if _console is None:
        if _RICH_AVAILABLE:
            _console = Console(stderr=False, highlight=False)
        else:
            _console = _PlainConsole()
    return _console


# -----------------------------------------------------------------------------
# Plain-text fallback console
# -----------------------------------------------------------------------------


class _PlainConsole:
    """
    Minimal console shim used when Rich is not installed.

    Writes to ``stdout`` using only ANSI codes that most terminals support.
    The output is less polished than Rich but fully functional.
    """

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Write *args* to stdout, ignoring Rich-specific keyword arguments."""
        # Strip Rich markup tags like [bold], [green], etc.
        import re

        text = " ".join(str(a) for a in args)
        text = re.sub(r"\[/?[^\]]*\]", "", text)
        print(text, file=sys.stdout)

    def rule(self, title: str = "", **kwargs: Any) -> None:
        """Print a plain-text horizontal rule."""
        width = 72
        if title:
            pad = (width - len(title) - 2) // 2
            print(f"{'─' * pad} {title} {'─' * pad}")
        else:
            print("─" * width)


# -----------------------------------------------------------------------------
# Visual constants
# -----------------------------------------------------------------------------

# Symbols used in the step summary line
_SYMBOL_GAIN = "▲"  # rows gained
_SYMBOL_LOSS = "▼"  # rows lost
_SYMBOL_STABLE = "●"  # no row change
_SYMBOL_WARN = "⚠"  # threshold triggered
_SYMBOL_EXPLODE = "💥"  # join explosion

# Rich colour tokens
_COLOR_GAIN = "green"
_COLOR_LOSS = "red"
_COLOR_STABLE = "dim"
_COLOR_WARN = "yellow"
_COLOR_EXPLODE = "bold red"
_COLOR_HEADER = "bold cyan"
_COLOR_DRIFT = "magenta"
_COLOR_DIM = "dim"


# -----------------------------------------------------------------------------
# Reporter — public class
# -----------------------------------------------------------------------------


class Reporter:
    """
    Formats and prints watcher output to the terminal.

    Uses Rich when available for coloured, aligned tables.  Falls back to
    plain text with minimal ANSI codes otherwise.

    You do not normally instantiate this class directly — it is used
    internally by the handler layer.  The public surface is three methods:

    * :meth:`print_step` — one decorated function call.
    * :meth:`print_session_header` — opening banner for a session.
    * :meth:`print_session_footer` — closing summary for a session.

    Parameters
    ----------
    width : int, optional
        Maximum terminal width for Rich output.  Defaults to ``100``.
    show_memory : bool, optional
        Include the memory-delta column in step output.  Defaults to
        ``True``.
    show_null_deltas : bool, optional
        Show per-column null-count changes beneath the step line.
        Defaults to ``True``.
    show_schema_drift : bool, optional
        Show added/removed columns beneath the step line.  Defaults to
        ``True``.
    show_join_detail : bool, optional
        Show join-explosion offender table when detected.  Defaults to
        ``True``.
    """

    def __init__(
        self,
        width: int = 100,
        show_memory: bool = True,
        show_null_deltas: bool = True,
        show_schema_drift: bool = True,
        show_join_detail: bool = True,
    ) -> None:
        self.width = width
        self.show_memory = show_memory
        self.show_null_deltas = show_null_deltas
        self.show_schema_drift = show_schema_drift
        self.show_join_detail = show_join_detail

    # ------------------------------------------------------------------
    # Public rendering methods
    # ------------------------------------------------------------------

    def print_session_header(self, name: str) -> None:
        """
        Print the opening banner for a :func:`~watcher.core.session` block.

        Parameters
        ----------
        name : str
            The session label passed to ``watcher.session(name)``.
        """
        console = _get_console()
        if _RICH_AVAILABLE:
            console.rule(f"[{_COLOR_HEADER}]watcher · {name}[/]")
        else:
            console.rule(f"watcher · {name}")

    def print_session_footer(self, session: WatcherSession) -> None:
        """
        Print the closing summary table for a completed session.

        Shows total rows in/out, net change, total memory delta, and
        elapsed time across all steps.

        Parameters
        ----------
        session : WatcherSession
            The completed session whose :attr:`~WatcherSession.steps`
            are summarised.
        """
        console = _get_console()
        summary = session.summary()

        if _RICH_AVAILABLE:
            self._rich_session_footer(console, summary)
        else:
            self._plain_session_footer(console, summary)

    def print_step(self, step: StepResult) -> None:
        """
        Print the output for a single decorated pipeline step.

        Includes:

        * The primary step line — function name, row counts, diff, time,
          memory delta.
        * Schema drift block — added / removed columns (when present).
        * Null delta block — columns with changed null counts (when
          present).
        * Join explosion detail — offending key columns and top repeat
          counts (when detected).

        Parameters
        ----------
        step : StepResult
            The result produced by a single ``@watch`` call.
        """
        console = _get_console()

        if _RICH_AVAILABLE:
            self._rich_step(console, step)
        else:
            self._plain_step(console, step)

    # ------------------------------------------------------------------
    # Rich rendering
    # ------------------------------------------------------------------

    def _rich_step(self, console: Any, step: StepResult) -> None:
        """Render a full step report using Rich."""
        # ---- Primary line -------------------------------------------
        row_symbol, row_color = _row_direction(step)
        warn_tag = f" [{_COLOR_WARN}]{_SYMBOL_WARN}[/]" if step.warned else ""
        explode_tag = (
            f" [{_COLOR_EXPLODE}]{_SYMBOL_EXPLODE} join explosion[/]"
            if step.is_join_explosion
            else ""
        )

        diff_str = _fmt_diff(step.row_diff, step.row_diff_pct)
        mem_str = (
            f"  [{_COLOR_DIM}]mem {step.memory_delta_mb:+.1f} MB "
            f"({step.memory_mode.value})[/]"
            if self.show_memory
            else ""
        )
        elapsed_str = f"[{_COLOR_DIM}]{step.elapsed_s * 1000:.1f} ms[/]"

        primary = (
            f"[bold]{step.func_name}()[/]  "
            f"[{_COLOR_DIM}]{step.rows_in:,}[/] → "
            f"[{row_color}]{step.rows_out:,}[/]  "
            f"[{row_color}]{row_symbol} {diff_str}[/]"
            f"{warn_tag}{explode_tag}  {elapsed_str}{mem_str}"
        )
        console.print(primary)

        # ---- Schema drift -------------------------------------------
        if self.show_schema_drift and step.stats.column_diff.has_drift:
            self._rich_schema_drift(console, step)

        # ---- Null deltas --------------------------------------------
        if self.show_null_deltas and step.stats.null_deltas:
            self._rich_null_deltas(console, step)

        # ---- Join explosion detail ----------------------------------
        if self.show_join_detail and step.is_join_explosion:
            self._rich_join_detail(console, step)

        # ---- Sampling footnote -------------------------------------
        if step.stats.sampled:
            console.print(
                f"  [{_COLOR_DIM}]* column stats computed on sample of "
                f"{step.stats.sample_size:,} rows[/]"
            )

    def _rich_schema_drift(self, console: Any, step: StepResult) -> None:
        """Render the schema drift block."""
        diff = step.stats.column_diff
        if diff.added:
            cols = ", ".join(f"[{_COLOR_DRIFT}]+{c}[/]" for c in diff.added)
            console.print(f"  columns added   : {cols}")
        if diff.removed:
            cols = ", ".join(f"[{_COLOR_LOSS}]-{c}[/]" for c in diff.removed)
            console.print(f"  columns removed : {cols}")
        for dc in step.stats.dtype_changes:
            color = _COLOR_DIM if dc.is_widening else _COLOR_WARN
            console.print(
                f"  dtype change    : [{color}]{dc.column} {dc.before} → {dc.after}[/]"
            )

    def _rich_null_deltas(self, console: Any, step: StepResult) -> None:
        """Render the null-count delta block (top 5 worst changes)."""
        top = step.stats.null_deltas[:5]
        for nd in top:
            sign = "+" if nd.delta > 0 else ""
            color = _COLOR_WARN if nd.delta > 0 else _COLOR_GAIN
            console.print(
                f"  nulls [{color}]{sign}{nd.delta:,}[/]  "
                f"[{_COLOR_DIM}]{nd.column}  "
                f"({nd.before:,} → {nd.after:,})[/]"
            )

    def _rich_join_detail(self, console: Any, step: StepResult) -> None:
        """Render the join-explosion offender table."""
        expl = step.stats.join_explosion
        if not expl.offending_columns:
            return

        table = Table(
            title=f"join explosion · duplication ratio {expl.duplication_ratio:.1%}",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style=f"bold {_COLOR_EXPLODE}",
            title_style=f"bold {_COLOR_WARN}",
            width=min(self.width, 80),
        )
        table.add_column("key column", style="bold")
        table.add_column("top value", style=_COLOR_DIM)
        table.add_column("repeat count", justify="right", style=_COLOR_LOSS)

        for col in expl.offending_columns[:3]:  # show worst 3 columns
            for value, count in expl.top_offenders.get(col, [])[:3]:
                table.add_row(col, str(value), f"{count:,}")

        console.print(table)

    def _rich_session_footer(self, console: Any, summary: dict) -> None:
        """Render the session summary panel using Rich."""
        rows_in = summary["total_rows_in"]
        rows_out = summary["total_rows_out"]
        net = rows_out - rows_in
        sign = "+" if net >= 0 else ""
        mem = summary["total_memory_delta_mb"]
        elapsed = summary["total_elapsed_s"]

        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("step")
        table.add_column("rows in", justify="right")
        table.add_column("rows out", justify="right")
        table.add_column("Δ rows", justify="right")
        table.add_column("time (ms)", justify="right")
        table.add_column("mem (MB)", justify="right")

        for s in summary["steps"]:
            diff = s["diff"]
            color = (
                _COLOR_GAIN
                if diff < 0
                else (_COLOR_LOSS if diff > 0 else _COLOR_STABLE)
            )
            sign_ = "+" if diff > 0 else ""
            table.add_row(
                s["func"],
                f"{s['rows_in']:,}",
                f"{s['rows_out']:,}",
                f"[{color}]{sign_}{diff:,}[/]",
                f"{s['elapsed_s'] * 1000:.1f}",
                f"{s['memory_delta_mb']:+.1f}",
            )

        footer_text = (
            f"[bold]total[/]  "
            f"{rows_in:,} → {rows_out:,}  "
            f"({sign}{net:,} rows)  "
            f"mem {mem:+.1f} MB  "
            f"{elapsed * 1000:.0f} ms"
        )

        panel = Panel(
            table,
            title=f"[{_COLOR_HEADER}]watcher · {summary['name']} · summary[/]",
            subtitle=footer_text,
            border_style="cyan",
        )
        console.print(panel)

    # ------------------------------------------------------------------
    # Plain-text fallback rendering
    # ------------------------------------------------------------------

    def _plain_step(self, console: _PlainConsole, step: StepResult) -> None:
        """Render a step line without Rich."""
        sym, _ = _row_direction(step)
        diff = _fmt_diff(step.row_diff, step.row_diff_pct)
        warn = f"  {_SYMBOL_WARN}" if step.warned else ""
        expl = f"  {_SYMBOL_EXPLODE} join explosion" if step.is_join_explosion else ""
        mem = (
            f"  mem {step.memory_delta_mb:+.1f} MB ({step.memory_mode.value})"
            if self.show_memory
            else ""
        )
        console.print(
            f"{step.func_name}()  "
            f"{step.rows_in:,} → {step.rows_out:,}  "
            f"{sym} {diff}{warn}{expl}  "
            f"{step.elapsed_s * 1000:.1f} ms{mem}"
        )

        if self.show_schema_drift and step.stats.column_diff.has_drift:
            diff_ = step.stats.column_diff
            if diff_.added:
                console.print(f"  + {', '.join(diff_.added)}")
            if diff_.removed:
                console.print(f"  - {', '.join(diff_.removed)}")

        if self.show_schema_drift and step.stats.dtype_changes:
            for dc in step.stats.dtype_changes:
                console.print(
                    f"  dtype change    : {dc.column} {dc.before} → {dc.after}"
                )

        if self.show_null_deltas:
            for nd in step.stats.null_deltas[:5]:
                sign = "+" if nd.delta > 0 else ""
                console.print(f"  nulls {sign}{nd.delta:,}  {nd.column}")

        if self.show_join_detail and step.is_join_explosion:
            expl_ = step.stats.join_explosion
            for col in expl_.offending_columns[:3]:
                for val, cnt in expl_.top_offenders.get(col, [])[:3]:
                    console.print(f"  {col}={val!r} repeated {cnt:,}×")

    def _plain_session_footer(self, console: _PlainConsole, summary: dict) -> None:
        """Render a plain-text session summary."""
        console.rule(f"watcher · {summary['name']} · summary")
        for s in summary["steps"]:
            sign = "+" if s["diff"] > 0 else ""
            console.print(
                f"  {s['func']:30s}  "
                f"{s['rows_in']:>10,} → {s['rows_out']:>10,}  "
                f"{sign}{s['diff']:>+10,}  "
                f"{s['elapsed_s'] * 1000:>8.1f} ms  "
                f"{s['memory_delta_mb']:>+7.1f} MB"
            )
        console.rule()
        net = summary["total_rows_out"] - summary["total_rows_in"]
        sign = "+" if net > 0 else ""
        console.print(
            f"  {'TOTAL':30s}  "
            f"{summary['total_rows_in']:>10,} → {summary['total_rows_out']:>10,}  "
            f"{sign}{net:>+10,}  "
            f"{summary['total_elapsed_s'] * 1000:>8.1f} ms  "
            f"{summary['total_memory_delta_mb']:>+7.1f} MB"
        )


# -----------------------------------------------------------------------------
# Shared formatting helpers
# -----------------------------------------------------------------------------


def _row_direction(step: StepResult) -> tuple[str, str]:
    """
    Return the directional symbol and Rich colour for a step's row change.

    Parameters
    ----------
    step : StepResult
        The step to evaluate.

    Returns
    -------
    tuple[str, str]
        ``(symbol, rich_color_name)``
    """
    if step.lost_rows:
        return _SYMBOL_LOSS, _COLOR_LOSS
    if step.gained_rows:
        return _SYMBOL_GAIN, _COLOR_GAIN
    return _SYMBOL_STABLE, _COLOR_STABLE


def _fmt_diff(row_diff: int, row_diff_pct: float) -> str:
    """
    Format a signed row difference with its percentage.

    Parameters
    ----------
    row_diff : int
        Signed row count change.
    row_diff_pct : float
        Fractional change relative to input size.

    Returns
    -------
    str
        E.g. ``"+105,769 rows (+10.6%)"`` or ``"-216,861 rows (-19.6%)"``.
    """
    sign = "+" if row_diff >= 0 else ""
    return f"{sign}{row_diff:,} rows ({sign}{row_diff_pct:.1%})"
