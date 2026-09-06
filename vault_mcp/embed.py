"""Vectorisation locale des textes.

`all-MiniLM-L6-v2`, 384 dimensions, execute sur place via `fastembed` (ONNX Runtime).
Aucun appel a une API externe : le vault ne sort pas de la machine.

Le modele est charge paresseusement et une seule fois par processus. Le charger a
l'import ferait payer ~3 s a chaque demarrage du service, y compris quand aucune
recherche n'est faite.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

NOM_MODELE = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSIONS = 384
CACHE_DEFAUT = Path("/opt/vault-mcp/models")


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


def vectoriser(textes: Sequence[str]) -> NDArray[np.float32]:
    """Vectorise une sequence de textes. Renvoie une matrice (n, 384) float32."""
    if not textes:
        return np.empty((0, DIMENSIONS), dtype=np.float32)
    vecteurs: Iterable[NDArray[np.float32]] = _modele().embed(list(textes))
    matrice: NDArray[np.float32] = np.asarray(list(vecteurs), dtype=np.float32)
    if matrice.shape[1] != DIMENSIONS:
        raise RuntimeError(f"dimension inattendue : {matrice.shape[1]} au lieu de {DIMENSIONS}")
    return normaliser(matrice)


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
