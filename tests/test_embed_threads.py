"""Cache d'embeddings utilisable depuis plusieurs threads (2026-09-21).

`_cache()` partageait une unique connexion SQLite entre tous les threads via
`@lru_cache`, donc chaque acces concurrent levait « SQLite objects created in a
thread can only be used in that same thread » et retombait sur un calcul ONNX
complet. Constate en production : une erreur par requete dans le journal.

Le correctif : une connexion par thread (`threading.local`), en WAL.
Ce test n'a pas besoin du modele : il exerce le cache lui-meme.
"""

from __future__ import annotations

import threading

import vault_mcp.embed as E


def test_cache_une_connexion_par_thread(tmp_path, monkeypatch):
    """Quatre threads demarrant ensemble obtiennent 4 connexions, sans erreur.

    La barriere maximise la contention sur la premiere creation (base
    inexistante) : sans le verrou de creation, c'est ici que la CI voyait
    `OperationalError('database is locked')` par intermittence (2026-09-21).
    """
    monkeypatch.setenv("VAULT_MCP_EMBED_CACHE", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(E, "CACHE_VECTEURS", tmp_path / "cache.sqlite")
    E._etat_fil.__dict__.clear()

    barriere = threading.Barrier(4)
    connexions = []
    erreurs = []
    verrou_listes = threading.Lock()

    def travail():
        try:
            barriere.wait(timeout=10)
            conn = E._cache()
            with verrou_listes:
                connexions.append(conn)
        except Exception as exc:  # noqa: BLE001
            with verrou_listes:
                erreurs.append(exc)

    fils = [threading.Thread(target=travail) for _ in range(4)]
    [t.start() for t in fils]
    [t.join() for t in fils]
    assert not erreurs
    assert len({id(c) for c in connexions}) == 4


def test_cache_reutilise_dans_le_meme_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_EMBED_CACHE", str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(E, "CACHE_VECTEURS", tmp_path / "cache.sqlite")
    E._etat_fil.__dict__.clear()
    assert E._cache() is E._cache()
