"""Garde anti-emballement des reconstructions BM25 (fuite memoire 2026-09-21).

Contexte : chaque publish incrementale rechargeait l'index complet (~350 Mo de
metas Python) et lancait une reconstruction BM25 de plusieurs minutes epinglant
l'ancienne generation. Des publishes successifs avec des builds qui se
chevauchaient accumulaient les generations jusqu'a 12 Go RSS.

Le correctif : `_bm25_pret` ne lance un build que sur la generation courante du
disque, et `_construire_bm25` abandonne sans assigner si un publish a eu lieu
pendant sa construction.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np

import vault_mcp.index as I
from vault_mcp.index import Index


def _mini_index(repertoire, generation=1):
    repertoire.mkdir(parents=True, exist_ok=True)
    np.save(str(repertoire / f"vectors.{generation}.npy"),
            np.eye(3, 384, dtype=np.float32))
    (repertoire / "meta.json").write_text(json.dumps({
        "generation": generation,
        "vecteurs": f"vectors.{generation}.npy",
        "backlinks": f"backlinks.{generation}.json",
        "fragments": [
            {"chemin": f"n{i}.md", "rang": 0, "titre": f"N{i}",
             "apercu": f"apercu note {i}"}
            for i in range(3)],
    }), encoding="utf-8")
    (repertoire / f"backlinks.{generation}.json").write_text("{}", encoding="utf-8")
    (repertoire / ".writers.lock").write_text("", encoding="utf-8")
    (repertoire / ".publish.lock").write_text("", encoding="utf-8")


def _textes_courts(metas, racine):
    return [f"{m.titre} {m.apercu} contenu" for m in metas]


def test_pas_de_build_sur_generation_perimee(tmp_path, monkeypatch):
    """Un publish pendant l'attente n'entraine aucune reconstruction perimee."""
    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    monkeypatch.setattr(I, "textes_complets", _textes_courts)
    monkeypatch.setattr(I, "BM25_INTERVALLE_S", 0)
    _mini_index(tmp_path, generation=1)
    idx = Index()
    insta1 = idx._courant()
    assert idx._bm25_pret(insta1) is not None

    # Simule un publish : meme contenu logique, nouvelle signature disque.
    time.sleep(0.02)
    _mini_index(tmp_path, generation=2)

    avant = threading.active_count()
    idx._bm25_pret(insta1)
    time.sleep(0.2)
    assert threading.active_count() == avant


def test_build_perime_n_assigne_rien(tmp_path, monkeypatch):
    """Un build devenu perime ne remplace pas le BM25 courant."""
    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    monkeypatch.setattr(I, "textes_complets", _textes_courts)
    monkeypatch.setattr(I, "BM25_INTERVALLE_S", 0)
    _mini_index(tmp_path, generation=1)
    idx = Index()
    insta1 = idx._courant()
    assert idx._bm25_pret(insta1) is not None
    courant = idx._bm25

    time.sleep(0.02)
    _mini_index(tmp_path, generation=2)
    idx._construire_bm25(insta1)
    assert idx._bm25 is courant


def test_build_generation_courante_assigne(tmp_path, monkeypatch):
    """Le cas nominal (generation a jour) construit et assigne toujours."""
    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    monkeypatch.setattr(I, "textes_complets", _textes_courts)
    monkeypatch.setattr(I, "BM25_INTERVALLE_S", 0)
    _mini_index(tmp_path, generation=7)
    idx = Index()
    insta = idx._courant()
    assert idx._bm25_pret(insta) is not None
    assert idx._bm25.generation == insta.signature
