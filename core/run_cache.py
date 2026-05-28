"""
run_cache.py — Tiny utility for "I ran X successfully at TIME" markers.

Why
---
Several startup phases (data integrity, Strategy 4 30-day replay) are
expensive but only need to run once per trading day, not on every
process restart. This module stores a small JSON marker per named
phase so the next startup can check freshness and skip safely.

Marker file layout
------------------
    data/run_cache/<name>.json

    {
      "ran_at":  "2026-05-28T17:30:12+05:30",
      "payload": { ... arbitrary, set by the caller ... }
    }

Usage
-----
    from core.run_cache import RunCache

    cache = RunCache("data_integrity")
    fresh, prev = cache.is_fresh(max_age_hours=12)
    if fresh:
        print(f"Skipping; ran {prev['ran_at']}")
    else:
        do_the_expensive_thing()
        cache.mark_success({"fetched": 48, "issues": 0})

Design notes
------------
- All writes are atomic (tmp file + os.replace) so a crash never leaves
  a half-written marker that masquerades as a successful run.
- `is_fresh()` returns (False, None) on missing/corrupt cache — never
  raises. Default behaviour is "run the expensive thing again".
- Markers are stored under `data/` and gitignored.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")

_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_ROOT, "data", "run_cache")


def _atomic_write(path: str, data: dict) -> None:
    """Write dict as JSON to path atomically (no torn writes on crash)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


class RunCache:
    """One marker per (name)."""

    def __init__(self, name: str):
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"RunCache name must be alphanumeric: {name!r}")
        self.name = name
        self.path = os.path.join(_CACHE_DIR, f"{name}.json")

    # ─── Read ──────────────────────────────────────────────────────────
    def read(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def is_fresh(self, max_age_hours: float) -> tuple[bool, dict | None]:
        """Return (fresh, payload).
           fresh=True iff last run is within `max_age_hours`.
           max_age_hours <= 0 → never fresh (always re-run)."""
        if max_age_hours <= 0:
            return False, None
        prev = self.read()
        if not prev or "ran_at" not in prev:
            return False, None
        try:
            ran_at = datetime.fromisoformat(prev["ran_at"])
        except ValueError:
            return False, None
        if ran_at.tzinfo is None:
            ran_at = IST.localize(ran_at)
        age = datetime.now(IST) - ran_at
        return (age <= timedelta(hours=max_age_hours)), prev

    # ─── Write ─────────────────────────────────────────────────────────
    def mark_success(self, payload: dict | None = None) -> None:
        _atomic_write(self.path, {
            "ran_at":  datetime.now(IST).isoformat(),
            "payload": payload or {},
        })

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
