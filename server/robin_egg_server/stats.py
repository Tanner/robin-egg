"""Summarises the turn log: python -m robin_egg_server.stats --help

How many turns a day, how long the first audio took, how often the egg woke with nobody
talking to it, which turns failed, and what became of the messages Robin sent on its own.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .turn_log import ANSWERED, DEFAULT_TURN_LOG, DROPPED, FAILED, INTERRUPTED, LOCAL_COMMAND, NOBODY_SPOKE, NOTHING_RECOGNISED, SPOKEN, older

LOG = logging.getLogger("robin.stats")

NO_WAKE_WORD = "(started another way)"


def read(path: Path) -> list[dict[str, Any]]:
    """Every readable line of the log, oldest first, each with its `at` as a datetime."""
    entries: list[dict[str, Any]] = []
    for file in (older(path), path):
        if not file.exists():
            continue
        skipped = 0
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                entry["at"] = datetime.fromisoformat(entry["time"])
                entries.append(entry)
            except (ValueError, KeyError, TypeError):
                skipped += 1  # a line cut short by a crash or a full disk
        if skipped:
            LOG.warning("Skipped %d unreadable lines in %s", skipped, file)
    return entries


def percentile(values: list[float], p: float) -> float:
    """The p-th percentile, 0 to 100, reading between the two nearest values."""
    ordered = sorted(values)
    spot = (len(ordered) - 1) * p / 100
    low = int(spot)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (spot - low)


def _timing(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label:<34}no turns to measure"
    return f"  {label:<34}median {percentile(values, 50):.2f} s, 90th percentile {percentile(values, 90):.2f} s, over {len(values)} turns"


def _per_day(count: int, days: int) -> str:
    return f"{count / days:.1f} a day"


def _counts(counter: Counter) -> str:
    return ", ".join(f"{name} {n}" for name, n in counter.most_common())


def summarise(entries: list[dict[str, Any]], since: date, until: date) -> str:
    """The report for the days from `since` to `until`, both included."""
    if entries:
        since = max(since, min(e["at"].date() for e in entries))  # days before the log began would dilute the daily rates
    if since > until:
        return f"Nothing was recorded up to {until}."
    days = (until - since).days + 1
    period = [e for e in entries if since <= e["at"].date() <= until]
    turns = [e for e in period if e.get("event") == "turn"]
    messages = [e for e in period if e.get("event") == "message"]
    lines = [f"{since} to {until} ({days} {'day' if days == 1 else 'days'}): {len(turns)} turns, {len(messages)} messages Robin sent on its own", ""]
    if not turns and not messages:
        lines.append("Nothing was recorded in this period.")
        return "\n".join(lines)

    woken = [t for t in turns if t.get("started_by") == "wake word"]
    lines.append(f"Turns: {len(turns)}, {_per_day(len(turns), days)} ({len(woken)} by wake word, {len(turns) - len(woken)} follow-ups)")
    by_day = Counter(t["at"].date() for t in turns)
    silent_by_day = Counter(t["at"].date() for t in woken if t.get("outcome") == NOBODY_SPOKE)
    lines.append(f"  {'day':<12}{'turns':>6}{'woke with nobody speaking':>28}")
    for offset in range(days):
        day = since + timedelta(days=offset)
        lines.append(f"  {str(day):<12}{by_day[day]:>6}{silent_by_day[day]:>28}")
    lines += ["", f"Outcomes: {_counts(Counter(t.get('outcome', 'unknown') for t in turns)) or 'none'}", ""]

    answered = [t for t in turns if t.get("outcome") == ANSWERED]
    lines.append("From the end of speech, for answered turns:")
    lines.append(_timing("first audio ready", [t["first_audio_s"] for t in answered if t.get("first_audio_s") is not None]))
    lines.append(_timing("whole reply ready", [t["reply_done_s"] for t in answered if t.get("reply_done_s") is not None]))
    lines.append(_timing("speech-to-text, every turn", [t["stt_s"] for t in turns if t.get("stt_s") is not None]))
    commands = [t["first_audio_s"] for t in turns if t.get("outcome") == LOCAL_COMMAND and t.get("first_audio_s") is not None]
    if commands:
        lines.append(_timing("first audio, local commands", commands))
    lines.append("")

    silent = [t for t in woken if t.get("outcome") == NOBODY_SPOKE]
    lines.append(f"Wake-ups where nobody spoke: {len(silent)} of {len(woken)} wake-ups, {_per_day(len(silent), days)}")
    unrecognised = Counter((t.get("wake_word") or NO_WAKE_WORD) for t in woken if t.get("outcome") == NOTHING_RECOGNISED)
    silent_by_word = Counter((t.get("wake_word") or NO_WAKE_WORD) for t in silent)
    for word, total in Counter((t.get("wake_word") or NO_WAKE_WORD) for t in woken).most_common():
        lines.append(f"  {word:<24}{silent_by_word[word]} of {total}, {_per_day(silent_by_word[word], days)}; a sound but no words in {unrecognised[word]} more")
    lines.append("")

    failed = [t for t in turns if t.get("outcome") == FAILED]
    lines.append(f"Failed turns: {len(failed)}")
    for t in failed:
        lines.append(f"  {t['at'].strftime('%Y-%m-%d %H:%M:%S')}  {t.get('turn', '')}")
    lines.append("")

    lines.append(f"Messages Robin sent on its own: {len(messages)}")
    spoken = [m for m in messages if m.get("fate") == SPOKEN]
    waits = [m["waited_s"] for m in spoken if m.get("waited_s") is not None]
    waited = f", {len(waits)} after waiting for the egg (median wait {percentile(waits, 50):.1f} s)" if waits else ""
    lines.append(f"  {'spoken':<14}{len(spoken)}{waited}")
    for fate in (DROPPED, INTERRUPTED, FAILED):
        ended = [m for m in messages if m.get("fate") == fate]
        why = f": {_counts(Counter(m.get('why') or 'no reason given' for m in ended))}" if ended else ""
        lines.append(f"  {fate:<14}{len(ended)}{why}")
    other = Counter(m.get("fate", "unknown") for m in messages if m.get("fate") not in (SPOKEN, DROPPED, INTERRUPTED, FAILED))
    if other:
        lines.append(f"  {'other':<14}{_counts(other)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m robin_egg_server.stats", description="Summarise the turn log: turns a day, time to first audio, wake-ups where nobody spoke, failures, and Robin's own messages")
    p.add_argument("--turn-log", type=Path, default=DEFAULT_TURN_LOG, help="The turn log to read (default ~/.robin-egg/turns.jsonl)")
    p.add_argument("--days", type=int, default=7, help="How many days to cover, ending today (default 7)")
    p.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="First day to cover, instead of --days")
    p.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Last day to cover (default today)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.days < 1:
        p.error("--days must be 1 or more")
    path = args.turn_log.expanduser()
    if not path.exists() and not older(path).exists():
        p.error(f"no turn log at {path}: the server writes one as it runs, unless it was started with --no-turn-log, --demo or --echo")
    until = args.until or date.today()
    since = args.since or until - timedelta(days=args.days - 1)
    if since > until:
        p.error(f"--since {since} is after --until {until}")
    print(summarise(read(path), since, until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
