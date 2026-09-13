from __future__ import annotations

import json
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import REPO, Bench, RawClient

pytestmark = pytest.mark.e2e


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(root=tmp_path / "bench")
    b.start()
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture
def client(bench: Bench) -> Iterator[RawClient]:
    c = RawClient(bench.url)
    c.initialize()
    try:
        yield c
    finally:
        c.close()


def kill_consumer_at(bench: Bench, scenario: str, stop_at: str,
                     lease: int = 2, timeout: float = 60.0) -> dict[str, dict[str, Any]]:
    """Lance le consommateur, attend qu'il se gare a `stop_at`, puis SIGKILL.

    Rend les checkpoints atteints. Echoue si le consommateur termine ou meurt avant.
    """
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "tests.e2e.consumer", "--url", bench.url,
         "--scenario", scenario, "--stop-at", stop_at, "--lease", str(lease)],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen: dict[str, dict[str, Any]] = {}
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if line.startswith("CHECKPOINT "):
                _, name, data = line.rstrip("\n").split(" ", 2)
                seen[name] = json.loads(data)
            elif line.startswith("PARKED "):
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=10)
                return seen
        err = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"consommateur termine sans se garer a {stop_at}: {err[-2000:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
