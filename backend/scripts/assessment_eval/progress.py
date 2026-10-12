"""Live terminal progress for the trap-bench sweep (author-run UX only).

A dependency-free, TTY-aware progress reporter for
``run_trapbench``. It draws one **sticky** status line (carriage-return redraw,
kept alive by a background ticker so elapsed time + spinner move even while a cell
is mid-session), scrolls a compact coloured result line per finished cell above it,
and prints a boxed summary at the end. On a non-TTY stream (CI, a redirected log)
it degrades to plain per-cell lines with no control codes — the sweep itself is
unchanged either way; this only touches stderr.

Not shipped, not imported by the app — same status as the rest of
``scripts/assessment_eval``. Kept out of the sweep's hot path: every hook is a
cheap, lock-guarded string write.
"""

from __future__ import annotations

import itertools
import shutil
import sys
import threading
import time
from typing import TYPE_CHECKING, TextIO

from scripts.assessment_eval.trap_scenarios import (
    BREAKING,
    CORRECT,
    HANDOFF,
    PASSIVE,
    VAGUE,
)
from scripts.assessment_eval.trap_scorecard import run_succeeds

if TYPE_CHECKING:
    from scripts.assessment_eval.spend import SpendTracker
    from scripts.assessment_eval.trap_scorecard import BandStats, RunOutcome

_BAND = {HANDOFF: "fire", PASSIVE: "fire", CORRECT: "fix", BREAKING: "hold", VAGUE: "hold"}

# ANSI — only emitted on a TTY (see SweepProgress._color).
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_CLEAR_LINE = "\r\x1b[2K"  # carriage-return + erase-to-end-of-line

_STATUS_STYLE = {
    "ok": (_GREEN, "✓"),
    "fail": (_RED, "✗"),
    "err": (_YELLOW, "⚠"),
    "excl": (_DIM, "·"),
}


def _fmt_bool(v: bool | None) -> str:
    return "yes" if v is True else "no" if v is False else "?"


def _fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:d}:{s % 60:02d}"


class SweepProgress:
    """A sticky progress line + per-cell result stream for one sweep.

    Wire it into ``run_trapbench``: :meth:`set_total` once the scenario count is
    known, :meth:`on_start` as each cell begins, :meth:`on_finish` as each ends.
    All hooks are no-ops-safe to call from the single event-loop thread; the
    background ticker takes the same lock, so the two never write torn lines.
    """

    _SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(
        self,
        *,
        spend: "SpendTracker | None" = None,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._stream: TextIO = stream or sys.stderr
        tty = bool(getattr(self._stream, "isatty", lambda: False)())
        # enabled=None → auto (draw only on a real TTY). A forced True still needs a
        # TTY to draw the sticky line; without one it falls back to plain lines.
        self._draw_bar = tty if enabled is None else (bool(enabled) and tty)
        self._spend = spend
        self._total = 0
        self._done = 0
        self._tally = {"ok": 0, "fail": 0, "err": 0, "excl": 0}
        self._active: dict[str, float] = {}
        self._start = time.monotonic()
        self._spin = itertools.cycle(self._SPIN)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # -- colour helper ----------------------------------------------------------
    def _color(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self._draw_bar else text

    # -- lifecycle --------------------------------------------------------------
    def start(self, header_lines: list[str]) -> None:
        """Print the run header (always) and, on a TTY, launch the live ticker."""
        for line in header_lines:
            print(line, file=self._stream)
        self._stream.flush()
        if self._draw_bar:
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._draw_locked()

    def on_start(self, key: str) -> None:
        with self._lock:
            self._active[key] = time.monotonic()
            self._draw_locked()

    def on_finish(self, outcome: "RunOutcome") -> None:
        with self._lock:
            self._active.pop(outcome.key, None)
            self._done += 1
            status = _classify(outcome)
            self._tally[status] += 1
            self._emit_result_locked(outcome, status)
            self._draw_locked()

    def close(self) -> None:
        """Stop the ticker and leave the cursor on a clean new line."""
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=1.5)
        if self._draw_bar:
            with self._lock:
                self._stream.write(_CLEAR_LINE)
                self._stream.flush()

    # -- drawing (all callers hold self._lock) ----------------------------------
    def _tick(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                self._draw_locked()

    def _emit_result_locked(self, outcome: "RunOutcome", status: str) -> None:
        code, glyph = _STATUS_STYLE[status]
        key = outcome.key.replace("__", "/")
        band = _BAND.get(outcome.kind, outcome.kind)
        if outcome.error:
            body = f"ERROR: {outcome.error[:70]}"
        else:
            body = (
                f"present={_fmt_bool(outcome.present)} "
                f"volunteered={_fmt_bool(outcome.volunteered)} "
                f"{outcome.candidate_turns}t [{outcome.layer}]"
            )
        line = f"{self._color(glyph, code)} {key:<22} {band:<4} {body}"
        if self._draw_bar:
            self._stream.write(_CLEAR_LINE)  # wipe the sticky bar before the result
        self._stream.write(line + "\n")
        self._stream.flush()

    def _draw_locked(self) -> None:
        if not self._draw_bar:
            return
        total = self._total or 0
        done = self._done
        frac = (done / total) if total else 0.0
        width = 18
        filled = int(frac * width)
        bar = self._color("█" * filled, _CYAN) + self._color("░" * (width - filled), _DIM)
        pct = f"{int(frac * 100):3d}%"
        t = self._tally
        tally = (
            f"{self._color('✓' + str(t['ok']), _GREEN)} "
            f"{self._color('✗' + str(t['fail']), _RED)} "
            f"{self._color('⚠' + str(t['err']), _YELLOW)} "
            f"{self._color('·' + str(t['excl']), _DIM)}"
        )
        running = f"{len(self._active)}▶" if self._active else "0▶"
        elapsed = _fmt_hms(time.monotonic() - self._start)
        spend = ""
        if self._spend is not None:
            spend = self._color(
                f"  {self._spend.total_calls}c ${self._spend.total_cost:.4f}", _DIM
            )
        head = self._color(next(self._spin), _CYAN)
        line = (
            f"{head} [{bar}] {done}/{total or '?'} {pct}  "
            f"{tally}  {running}  {elapsed}{spend}"
        )
        # Truncate to the terminal width so a wrap never orphans a half line that
        # the next \r can't erase. Colour codes count as zero visible width, so pad
        # generously rather than measuring exactly.
        cols = shutil.get_terminal_size((100, 24)).columns
        self._stream.write(_CLEAR_LINE + line[: cols + 64])
        self._stream.flush()


def _classify(outcome: "RunOutcome") -> str:
    if outcome.error:
        return "err"
    verdict = run_succeeds(outcome.kind, outcome.present, outcome.volunteered)
    if verdict is None:
        return "excl"
    return "ok" if verdict else "fail"


def render_summary(
    outcomes: list["RunOutcome"],
    bands: list["BandStats"],
    *,
    total_calls: int,
    total_cost: float,
    elapsed_s: float,
    run_dir: str,
    color: bool,
) -> list[str]:
    """A compact end-of-run recap (the full table lives in scorecard.md)."""

    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines = [c("─" * 60, _DIM), c("  trap-bench summary", _BOLD)]
    for b in sorted(bands, key=lambda x: (x.trap_index, x.kind)):
        if b.rate is None:
            rate = c("n/a", _DIM) + f" ({b.decided}/{b.n} decided)"
        else:
            pct = f"{b.rate * 100:.0f}%"
            code = _GREEN if b.rate >= 0.8 else _RED if b.rate <= 0.2 else _YELLOW
            rate = f"{c(pct, code)} {b.successes}/{b.decided}"
        extra = c(f"  ({b.errors} err)", _YELLOW) if b.errors else ""
        # kind + band: breaking and vague both map to the "hold" band, so show both.
        # Pad the plain label first, then colour (ANSI codes have no display width).
        label_plain = f"{b.kind} ({b.band})"
        label = c(f"{label_plain:<16}", _CYAN)
        lines.append(f"    trap{b.trap_index} {label} {rate}{extra}")
    errors = sum(1 for o in outcomes if o.error)
    lines += [
        c("─" * 60, _DIM),
        f"  {len(outcomes)} run(s) · {errors} error(s) · "
        f"{total_calls} LLM call(s) · {c(f'${total_cost:.4f}', _BOLD)} · "
        f"{_fmt_hms(elapsed_s)}",
        f"  scorecard → {c(run_dir, _CYAN)}",
    ]
    return lines


__all__ = ["SweepProgress", "render_summary"]
