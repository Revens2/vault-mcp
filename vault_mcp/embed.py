"""Vectorisation locale des textes.

`all-MiniLM-L6-v2`, 384 dimensions, execute sur place via `fastembed` (ONNX Runtime).
Aucun appel a une API externe : le vault ne sort pas de la machine.

Le modele est charge paresseusement et une seule fois par processus. Le charger a
l'import ferait payer ~3 s a chaque demarrage du service, y compris quand aucune
recherche n'est faite.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

NOM_MODELE = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSIONS = 384
CACHE_DEFAUT = Path("/opt/vault-mcp/models")

# ------------------------------------------------------------------- cache
# Le full (`scripts/reindex.py`) revectorise TOUT le miroir a chaque passage :
# ~245 000 fragments, 7 h 45 de CPU le 2026-09-18 et un echec par timeout. Or d'un
# jour a l'autre la quasi-totalite des fragments est identique au caractere pres.
# Ce cache rend le full proportionnel a ce qui a CHANGE, sans toucher a sa
# semantique : meme modele, meme normalisation, meme resultat.
#
# Cle : sha256 du texte exact. Un changement de modele changerait les vecteurs sans
# changer les cles, d'ou NOM_MODELE dans la cle. Purge : voir `purger_cache`.
CACHE_VECTEURS = Path(os.environ.get(
    "VAULT_MCP_EMBED_CACHE", "/var/lib/vault-mcp/embed_cache.sqlite"))
CACHE_ACTIF = os.environ.get("VAULT_MCP_EMBED_CACHE_ACTIF", "1") not in ("0", "", "non")


# Une connexion SQLite par thread : l'objet `sqlite3.Connection` est lie au
# thread qui l'a cree. L'ancien `@lru_cache` partageait UNE connexion entre
# tous les threads du serveur, donc chaque acces concurrent levait
# « SQLite objects created in a thread can only be used in that same thread »
# et retombait sur un calcul ONNX complet (constate en prod 2026-09-21).
# En WAL + busy_timeout, lecteurs concurrents et ecrivain bref coexistent.
# Le verrou ne couvre que la PREMIERE creation par thread : sans lui, N threads
# demarrant ensemble (boot du serveur) peuvent se disputer le DDL initial
# (« database is locked », constate en CI 2026-09-21). Apres creation, chaque
# thread n'utilise que sa connexion, sans verrou.
_etat_fil = threading.local()
_verrou_creation = threading.Lock()


def _cache() -> Any:
    import sqlite3

    conn = getattr(_etat_fil, "conn", None)
    if conn is not None:
        return conn
    with _verrou_creation:
        conn = getattr(_etat_fil, "conn", None)
        if conn is not None:
            return conn
        CACHE_VECTEURS.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(CACHE_VECTEURS, timeout=30)
        # WAL : le worker incremental et le full peuvent lire en meme temps ; les
        # ecritures sont rares et courtes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE TABLE IF NOT EXISTS vecteurs ("
                     " cle TEXT PRIMARY KEY, vecteur BLOB NOT NULL,"
                     " vu_le INTEGER NOT NULL)")
        conn.commit()
        _etat_fil.conn = conn
    return conn


def _cle(texte: str) -> str:
    h = hashlib.sha256()
    h.update(NOM_MODELE.encode("utf-8"))
    h.update(b"\x00")
    h.update(texte.encode("utf-8"))
    return h.hexdigest()


def _lire_cache(cles: list[str]) -> dict[str, NDArray[np.float32]]:
    trouves: dict[str, NDArray[np.float32]] = {}
    conn = _cache()
    for debut in range(0, len(cles), 900):  # limite de variables liees de SQLite
        lot = cles[debut : debut + 900]
        marques = ",".join("?" * len(lot))  # que des placeholders : pas d'injection
        for cle, blob in conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                f"SELECT cle, vecteur FROM vecteurs WHERE cle IN ({marques})", lot):
            trouves[cle] = np.frombuffer(blob, dtype=np.float32)
        # `vu_le` marque ce qui sert encore : sans cela la purge jetterait les
        # fragments les plus stables, precisement ceux que le cache doit garder.
        conn.execute(f"UPDATE vecteurs SET vu_le=? WHERE cle IN ({marques})",  # nosemgrep: sqlalchemy-execute-raw-query
                     [int(time.time()), *lot])
    conn.commit()
    return trouves


def _ecrire_cache(paires: list[tuple[str, NDArray[np.float32]]]) -> None:
    if not paires:
        return
    conn = _cache()
    vu = int(time.time())
    conn.executemany(
        "INSERT INTO vecteurs (cle, vecteur, vu_le) VALUES (?,?,?)"
        " ON CONFLICT(cle) DO UPDATE SET vu_le=excluded.vu_le",
        [(cle, v.astype(np.float32).tobytes(), vu) for cle, v in paires])
    conn.commit()


def purger_cache(jours: int = 30) -> int:
    """Retire les entrees non revues depuis `jours` (notes supprimees, fragments
    reecrits). Un full les recreerait au besoin : la purge ne perd aucune donnee."""
    conn = _cache()
    limite = int(time.time()) - max(1, jours) * 86400
    n = conn.execute("DELETE FROM vecteurs WHERE vu_le < ?", (limite,)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    return int(n or 0)




def cache_modele() -> Path:
    return Path(os.environ.get("VAULT_MCP_MODEL_CACHE", str(CACHE_DEFAUT)))


def fils() -> int | None:
    """Nombre de fils passe a onnxruntime, ou None pour le defaut (tous les coeurs)."""
    brut = os.environ.get("VAULT_MCP_THREADS", "").strip()
    if not brut:
        return None
    valeur = int(brut)
    return valeur if valeur > 0 else None


@lru_cache(maxsize=1)
def _modele() -> Any:
    from fastembed import TextEmbedding

    # `threads` alimente `intra_op_num_threads` de la session ONNX. C'est le seul
    # levier qui bride reellement : `OMP_NUM_THREADS` est ignore par onnxruntime.
    return TextEmbedding(NOM_MODELE, cache_dir=str(cache_modele()), threads=fils())


def _vectoriser_modele(textes: Sequence[str]) -> NDArray[np.float32]:
    vecteurs: Iterable[NDArray[np.float32]] = _modele().embed(list(textes))
    matrice: NDArray[np.float32] = np.asarray(list(vecteurs), dtype=np.float32)
    if matrice.shape[1] != DIMENSIONS:
        raise RuntimeError(f"dimension inattendue : {matrice.shape[1]} au lieu de {DIMENSIONS}")
    return normaliser(matrice)


def vectoriser(textes: Sequence[str]) -> NDArray[np.float32]:
    """Vectorise une sequence de textes. Renvoie une matrice (n, 384) float32.

    Les textes deja vus ressortent du cache : un full ne repaye que ce qui a change.
    Le cache est un accelerateur, jamais une source de verite -- toute panne d'acces
    se degrade en calcul normal.
    """
    if not textes:
        return np.empty((0, DIMENSIONS), dtype=np.float32)
    if not CACHE_ACTIF:
        return _vectoriser_modele(textes)
    try:
        cles = [_cle(t) for t in textes]
        connus = _lire_cache(list(dict.fromkeys(cles)))
    except Exception as exc:  # noqa: BLE001 - cache defaillant : on calcule
        print(f"cache d'embeddings indisponible ({exc}) : calcul direct", file=sys.stderr)
        return _vectoriser_modele(textes)
    manquants = [i for i, cle in enumerate(cles) if cle not in connus]
    if manquants:
        calcules = _vectoriser_modele([textes[i] for i in manquants])
        for rang, i in enumerate(manquants):
            connus[cles[i]] = calcules[rang]
        with contextlib.suppress(Exception):
            _ecrire_cache([(cles[i], connus[cles[i]]) for i in manquants])
    return np.vstack([connus[cle] for cle in cles]).astype(np.float32)


def vectoriser_un(texte: str) -> NDArray[np.float32]:
    # Lindexation dun ndarray est typee `Any` : on reancre le type explicitement.
    vecteur: NDArray[np.float32] = vectoriser([texte])[0]
    return vecteur


def normaliser(matrice: NDArray[np.float32]) -> NDArray[np.float32]:
    """Normalise chaque ligne en L2, pour que le produit scalaire soit le cosinus.

    Une norme nulle (texte vide apres tokenisation) donnerait une division par zero
    puis des NaN qui contaminent tout le classement : on la ramene a 1.
    """
    normes = np.linalg.norm(matrice, axis=1, keepdims=True)
    normes[normes == 0] = 1.0
    return (matrice / normes).astype(np.float32)
