"""Verrou du miroir pris par le worker pendant ses lectures (29/09/2026)."""
from __future__ import annotations

import fcntl
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "index_worker.py"
_spec = importlib.util.spec_from_file_location("index_worker", _SCRIPT)
assert _spec is not None and _spec.loader is not None
index_worker = importlib.util.module_from_spec(_spec)
sys.modules["index_worker"] = index_worker
_spec.loader.exec_module(index_worker)


def test_verrou_absent_ne_bloque_pas(tmp_path: Path) -> None:
    verrou = index_worker.VerrouMiroir(str(tmp_path / "absent.lock"), attente_s=0.1)
    verrou.prendre()
    verrou.lacher()


def test_sync_en_cours_leve_miroir_occupe(tmp_path: Path) -> None:
    chemin = tmp_path / "vault-mirror.lock"
    chemin.touch()
    fd = os.open(chemin, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        verrou = index_worker.VerrouMiroir(str(chemin), attente_s=0.2)
        with pytest.raises(index_worker.MiroirOccupe):
            verrou.prendre()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_partage_bloque_le_sync_puis_le_relache(tmp_path: Path) -> None:
    chemin = tmp_path / "vault-mirror.lock"
    chemin.touch()
    verrou = index_worker.VerrouMiroir(str(chemin), attente_s=0.2)
    verrou.prendre()
    verrou.prendre()  # idempotent pendant un lot
    fd = os.open(chemin, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verrou.lacher()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
