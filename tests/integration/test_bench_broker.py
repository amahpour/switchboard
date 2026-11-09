"""The sizing harness exercises the real broker, not a mock (DESIGN.md §12, #690)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import child_env

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "linux", reason="broker RSS/CPU sampling uses Linux procfs")
def test_tiny_benchmark_measures_calls_and_open_waits(tmp_path: Path) -> None:
    """Run every phase with two agents; fail on broken setup, empty samples or waits that never open.

    Check results, not performance thresholds: shared CI machines need not match the sizing table.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "bench_broker.py"),
            "--agents",
            "2",
            "--wait-agents",
            "0",
            "2",
            "--seconds",
            "0.05",
        ],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    report = json.loads(result.stdout)
    assert {row["operation"] for row in report["throughput"]} == {"read", "hook", "say"}
    for row in report["throughput"]:
        assert row["agents"] == 2
        assert row["calls"] >= 2
        assert row["calls_s"] > 0
        assert 0 < row["p50_ms"] <= row["p99_ms"]
    assert [row["open_waits"] for row in report["idle"]] == [0, 2]
    for row in report["idle"]:
        assert row["rss_mib"] > 0
        assert row["cpu_cores"] >= 0
        assert row["fds"] > 0
