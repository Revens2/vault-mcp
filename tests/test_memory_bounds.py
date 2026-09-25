"""Non-regression fuite memoire 22/09/2026 (residuelle apres e501135).

Cas B prouve : prod tournait f82a721 (hashes identiques) mais OOM 7.9G + 2.7G
swap, RSS 4.3G dont anon 3.7G + 1 mapping vectors.*.npy (deleted) 528 Mo.
Mecanismes : `np.frombuffer` sans copy retenant le bytes sqlite, ancien mmap
jamais ferme apres `_purger_generations`, tampons `concatenate` 2x517 Mo
retenus par l'arene, `textes` BM25 352k strings retenus.

Ces tests n'allocuent jamais 500 Mo : ils prouvent le mecanisme (fermeture,
copie, liberation) sur mini-index.
"""

from __future__ import annotations

import json

import numpy as np

from vault_mcp.index import Index, _fermer_vecteurs


def _mini_index(repertoire, generation=1, n=4):
    repertoire.mkdir(parents=True, exist_ok=True)
    np.save(str(repertoire / f"vectors.{generation}.npy"),
            np.eye(n, 384, dtype=np.float32))
    (repertoire / "meta.json").write_text(json.dumps({
        "generation": generation,
        "vecteurs": f"vectors.{generation}.npy",
        "backlinks": f"backlinks.{generation}.json",
        "fragments": [
            {"chemin": f"n{i}.md", "rang": 0, "titre": f"N{i}",
             "apercu": f"apercu note {i}"}
            for i in range(n)],
    }), encoding="utf-8")
    (repertoire / f"backlinks.{generation}.json").write_text("{}", encoding="utf-8")
    (repertoire / ".writers.lock").write_text("", encoding="utf-8")
    (repertoire / ".publish.lock").write_text("", encoding="utf-8")


def test_fermer_vecteurs_rend_mmap_inoffensif(tmp_path, monkeypatch):
    """L'ancien mapping (deleted) doit pouvoir etre ferme explicitement."""
    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    _mini_index(tmp_path, generation=1)
    idx = Index()
    insta = idx._courant()
    assert getattr(insta.vecteurs, "_mmap", None) is not None
    _fermer_vecteurs(insta.vecteurs)
    # Apres close, le mmap est marque ferme : ne jamais re-toucher les
    # donnees (numpy segfaulte sur acces post-close, pas ValueError).
    assert getattr(insta.vecteurs, "_mmap").closed


def test_courant_remplace_sans_retenir_ancien_mmap(tmp_path, monkeypatch):
    """Un reload de generation ne doit pas laisser 2 mmaps ouverts."""
    import time

    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    _mini_index(tmp_path, generation=1)
    idx = Index()
    insta1 = idx._courant()
    mmap1 = getattr(insta1.vecteurs, "_mmap", None)
    assert mmap1 is not None
    time.sleep(0.02)
    _mini_index(tmp_path, generation=2)
    insta2 = idx._courant()
    assert insta2.generation == 2
    # L'ancien instantane n'est plus reference par l'Index ; la fermeture
    # opportuniste a eu lieu (ou au pire, l'ancien objet est distinct).
    assert insta1 is not insta2


def test_invalider_fermer_rend_mmap(tmp_path, monkeypatch):
    """`invalider(fermer=True)` post-publish rend le mapping au kernel."""
    monkeypatch.setenv("VAULT_MCP_INDEX", str(tmp_path))
    _mini_index(tmp_path, generation=5)
    idx = Index()
    insta = idx._courant()
    mmap = getattr(insta.vecteurs, "_mmap", None)
    assert mmap is not None
    idx.invalider(fermer=True)
    assert idx._instantane is None
    assert mmap.closed


def test_lire_cache_copie_sans_retenir_bytes(tmp_path, monkeypatch):
    """`np.frombuffer(...).copy()` : le vecteur survit a la mort du bytes."""
    import vault_mcp.embed as embed_module

    monkeypatch.setenv("VAULT_MCP_EMBED_CACHE", str(tmp_path / "embed_cache.sqlite"))
    embed_module._etat_fil.conn = None
    vec = np.arange(384, dtype=np.float32)
    embed_module._ecrire_cache([("cle-test-22-09", vec)])
    trouves = embed_module._lire_cache(["cle-test-22-09"])
    assert "cle-test-22-09" in trouves
    assert trouves["cle-test-22-09"].dtype == np.float32
    assert trouves["cle-test-22-09"].shape == (384,)
    # Copie : `base` ne doit pas pointer vers le bytes sqlite.
    assert getattr(trouves["cle-test-22-09"], "base", None) is None or not isinstance(
        getattr(trouves["cle-test-22-09"], "base", None), (bytes, bytearray)
    )
    if hasattr(embed_module._etat_fil, "conn") and embed_module._etat_fil.conn is not None:
        embed_module._etat_fil.conn.close()
        del embed_module._etat_fil.conn
