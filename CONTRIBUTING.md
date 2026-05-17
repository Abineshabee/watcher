# Contributing to watcher

Thanks for your interest in contributing. This document covers everything you need to get from zero to a working pull request.

---

## Table of contents

- [Project structure](#project-structure)
- [Setting up your environment](#setting-up-your-environment)
- [Running the tests](#running-the-tests)
- [How the code is organised](#how-the-code-is-organised)
- [Making changes](#making-changes)
- [Adding a new backend](#adding-a-new-backend)
- [Adding a new handler](#adding-a-new-handler)
- [Pull request checklist](#pull-request-checklist)
- [Code style](#code-style)
- [Reporting bugs](#reporting-bugs)
- [Requesting features](#requesting-features)

---

## Project structure

```
watcher/
├── watcher/
│   ├── __init__.py       # public API surface
│   ├── core.py           # @watch decorator, session(), StepResult, BackendRegistry
│   ├── stats.py          # column stats engine (nulls, dtypes, join explosion)
│   ├── reporter.py       # terminal output via Rich (or plain-text fallback)
│   ├── handlers.py       # HandlerBase, TerminalHandler, register/deregister
│   └── exceptions.py     # WatcherError, ThresholdExceeded, WatcherWarning, …
├── tests/
│   ├── conftest.py       # reloads watcher modules so coverage traces module-level lines — do not delete
│   ├── test_core.py
│   ├── test_stats.py
│   ├── test_coverage_boost.py
│   ├── test_exceptions.py
│   ├── test_handlers.py
│   └── test_reporter.py
├── examples/
│   ├── basic_pipeline.py
│   ├── explore_watcher
│   └── threshold_demo.py
├── pyproject.toml        # build, pytest, and coverage configuration — see [tool.coverage] sections
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

---

## Setting up your environment

You need Python 3.10 or later.

```bash
git clone https://github.com/Abineshabee/watcher
cd watcher

# Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install the package in editable mode with all dev dependencies
pip install -e ".[dev]"
```

Verify everything is working:

```bash
python examples/basic_pipeline.py
pytest
```

---

## Running the tests

```bash
# Run all tests with verbose output
pytest

# Run with coverage report
pytest --cov=watcher --cov-report=term-missing

# Run a specific test file
pytest tests/test_core.py

# Run a specific test by name
pytest tests/test_core.py::test_watch_drops_rows
```

The project enforces a minimum coverage of **80 %**. Pull requests that drop coverage below this threshold will fail CI.

> **Note — why `tests/conftest.py` exists**
> The editable install (`pip install -e .`) uses an import finder hook that
> causes pytest-cov to miss module-level lines (imports, class definitions)
> unless the watcher modules are reloaded after the coverage tracer starts.
> `tests/conftest.py` does exactly that — **do not delete it**.

---

## How the code is organised

Understanding the data flow helps you know where to make changes.

```
@watch decorator (core.py)
    │
    ├── measures memory before call (core.py → psutil / tracemalloc)
    ├── calls the decorated function
    ├── measures memory after call
    ├── calls compute_stats(before, after) ──→ stats.py
    │       ├── ColumnDiff   (schema drift)
    │       ├── DtypeChange  (narrowing / widening)
    │       ├── NullDelta    (per-column null changes)
    │       └── JoinExplosionDetail (fan-out detection)
    ├── builds StepResult
    ├── checks thresholds (warn_on_*, raise_on_*)
    └── dispatches on_step(step) to all registered handlers
            └── TerminalHandler (handlers.py)
                    └── Reporter.print_step(step) ──→ reporter.py
```

**Key rule:** `core.py` never prints anything directly. It always dispatches to handlers. `reporter.py` never imports from `core.py` at runtime (only under `TYPE_CHECKING`). This one-way dependency keeps the render layer swappable.

---

## Making changes

1. **Fork** the repository and create a branch from `main`:

   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Make your changes.** Keep commits focused — one logical change per commit.

3. **Add or update tests** in `tests/`. Every new behaviour needs a test. Every bug fix needs a regression test.

4. **Run the full test suite** and make sure it passes:

   ```bash
   pytest --cov=watcher
   ```

5. **Push your branch** and open a pull request against `main`.

---

## Adding a new backend

watcher is designed so new DataFrame backends (Polars, DuckDB, etc.) can be added without touching `core.py`.

**Step 1 — Create the stats backend** in `stats.py`:

```python
class _PolarsStatsBackend(_StatsBackend):

    @staticmethod
    def accepts(obj: Any) -> bool:
        try:
            import polars as pl
            return isinstance(obj, pl.DataFrame)
        except ImportError:
            return False

    @staticmethod
    def compute(before: Any, after: Any, sample_size: int) -> StepStats:
        # implement column diff, null deltas, dtype changes, join explosion
        ...
```

Add it to `_BACKENDS` at the bottom of `stats.py`:

```python
_BACKENDS: List[type[_StatsBackend]] = [
    _PandasStatsBackend,
    _PolarsStatsBackend,   # add here
]
```

**Step 2 — Register the core adapter** at the bottom of `core.py`:

```python
from watcher.stats import _PolarsStatsBackend
BackendRegistry.register(_PolarsStatsBackend)
```

**Step 3 — Add tests** in `tests/test_stats.py` using a Polars DataFrame.

That's all. Nothing in `core.py`, `reporter.py`, or `handlers.py` needs to change.

---

## Adding a new handler

A handler is any class that subclasses `HandlerBase` and overrides one or more of its three methods.

```python
from watcher.handlers import HandlerBase, register_handler
from watcher.core import StepResult, WatcherSession

class MyHandler(HandlerBase):

    def on_session_start(self, session: WatcherSession) -> None:
        print(f"Pipeline started: {session.name}")

    def on_step(self, step: StepResult) -> None:
        print(f"{step.func_name}: {step.rows_in} → {step.rows_out}")

    def on_session_end(self, session: WatcherSession) -> None:
        print(f"Pipeline finished in {session.summary()['total_elapsed_s']:.2f}s")

register_handler(MyHandler())
```

If you are contributing a handler that is generally useful (JSON logger, Slack notifier, CI reporter), open a PR and we will discuss adding it to the library.

---

## Pull request checklist

Before opening a PR, confirm all of the following:

- [ ] `pytest` passes with no failures
- [ ] Coverage is at or above 80 %
- [ ] New public functions and classes have docstrings
- [ ] `__all__` in the relevant module is updated if you added a public name
- [ ] `watcher/__init__.py` is updated if you added something to the public API
- [ ] Examples still run: `python examples/basic_pipeline.py` and `python examples/threshold_demo.py`
- [ ] The PR description explains *what* changed and *why*

---

## Code style

- **Python 3.10+** syntax throughout.
- Type hints on all public functions and methods.
- Docstrings on all public classes and functions (NumPy/Google style).
- No line longer than 100 characters.
- No direct `print()` calls in `core.py`, `stats.py`, or `handlers.py` — use handlers.
- No pandas imports at module level in `stats.py` — import inside methods to keep the module backend-agnostic.

There is no enforced formatter yet. Follow the style of the surrounding code.

---

## Reporting bugs

Open a [GitHub issue](https://github.com/Abineshabee/watcher/issues) with:

- Python version and OS
- watcher version (`pip show watcher`)
- A minimal reproducible example — the smallest DataFrame and code that triggers the bug
- The full traceback or unexpected output

---

## Requesting features

Open a [GitHub issue](https://github.com/Abineshabee/watcher/issues) and describe:

- The problem you are trying to solve
- What you expected watcher to do
- Any workarounds you are currently using

Feature requests that align with the roadmap (Polars backend, notebook renderer, `watcher.config`) are especially welcome.

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
