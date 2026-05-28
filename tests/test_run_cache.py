"""
test_run_cache.py — Unit tests for core.run_cache.RunCache.

Run: python tests/test_run_cache.py
"""
import os
import sys
import shutil
import time
from datetime import datetime, timedelta

import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.run_cache import RunCache, _CACHE_DIR  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def assert_(cond, msg):
    print(f"  {'✅' if cond else '❌'}  {msg}")
    if not cond:
        sys.exit(1)


def main():
    print("\n  RunCache unit tests\n")

    # Use a sub-namespace under data/run_cache so we don't clobber real markers.
    test_namespace = "test_runcache_xyz123"
    cache_path     = os.path.join(_CACHE_DIR, f"{test_namespace}.json")
    if os.path.exists(cache_path):
        os.unlink(cache_path)

    cache = RunCache(test_namespace)

    # 1) Empty cache → not fresh.
    fresh, prev = cache.is_fresh(12)
    assert_(not fresh and prev is None, "empty cache → not fresh")

    # 2) Write a marker.
    cache.mark_success({"foo": 1, "bar": "ok"})
    assert_(os.path.exists(cache_path), "marker file created")

    # 3) Just-written marker IS fresh within a 1-hour window.
    fresh, prev = cache.is_fresh(1)
    assert_(fresh, "just-written marker is fresh within 1h")
    assert_(prev and prev["payload"]["foo"] == 1, "payload round-trips")

    # 4) max_age_hours=0 → never fresh (always re-run).
    fresh, _ = cache.is_fresh(0)
    assert_(not fresh, "max_age_hours=0 → never fresh")

    # 5) Forge an old ran_at and check is_fresh respects it.
    import json
    data = json.load(open(cache_path))
    data["ran_at"] = (datetime.now(IST) - timedelta(hours=25)).isoformat()
    json.dump(data, open(cache_path, "w"))
    fresh, _ = cache.is_fresh(12)
    assert_(not fresh, "25h-old marker is NOT fresh with 12h limit")

    # 6) Corrupt cache file → not fresh, no exception.
    open(cache_path, "w").write("not valid json {{")
    fresh, prev = cache.is_fresh(12)
    assert_(not fresh and prev is None, "corrupt cache → not fresh, no crash")

    # 7) Invalid name rejected.
    raised = False
    try:
        RunCache("../etc/passwd")
    except ValueError:
        raised = True
    assert_(raised, "invalid name rejected with ValueError")

    # 8) Atomic write — tmp file should NOT linger after success.
    cache.mark_success({"k": "v"})
    tmps = [f for f in os.listdir(_CACHE_DIR)
            if f.startswith(test_namespace + ".") and f.endswith(".tmp")]
    assert_(not tmps, "no leftover .tmp file after successful write")

    # 9) Clear works.
    cache.clear()
    assert_(not os.path.exists(cache_path), "clear() removes marker")
    cache.clear()  # idempotent
    assert_(True, "clear() is idempotent on missing marker")

    print("\n  ✅ All RunCache checks passed.\n")


if __name__ == "__main__":
    main()
