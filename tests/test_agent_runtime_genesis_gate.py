"""Pure-unit tests for the genesis spawn-gate logic (no DB).

The supervisor blocks spawning a host user ONLY while an import genesis is
actively running. No genesis_state (fresh start / never uploaded) and done/failed
both fall through to spawn — so a 0-upload host user never deadlocks. See spec §5
("先 genesis 后 spawn") + §2.1 (fresh start) + §11.8.

This file is DB-free: it imports `supervisor` (which has no module-level DB dep)
and exercises only the pure status→bool decision. The DB-backed tick wiring is in
tests/test_agent_runtime_supervisor.py (needs Postgres; runs in CI).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import supervisor


def test_blocks_only_while_genesis_actively_running():
    for status in ("uploaded", "finalizing", "processing"):
        assert supervisor._genesis_status_blocks_spawn({"status": status}) is True
    # tolerant of case / surrounding whitespace
    assert supervisor._genesis_status_blocks_spawn({"status": " Processing "}) is True


def test_allows_fresh_start_done_failed_and_malformed():
    assert supervisor._genesis_status_blocks_spawn(None) is False        # no genesis = fresh start
    assert supervisor._genesis_status_blocks_spawn({}) is False          # malformed/empty
    assert supervisor._genesis_status_blocks_spawn({"status": ""}) is False
    assert supervisor._genesis_status_blocks_spawn({"status": "done"}) is False
    assert supervisor._genesis_status_blocks_spawn({"status": "failed"}) is False
    assert supervisor._genesis_status_blocks_spawn("not-a-dict") is False
