"""BM25 en memoire sur le texte complet des fragments.

L'ancien lexical comptait la presence des mots de la requete dans `titre + apercu(240)
+ chemin` : un identifiant exact situe apres le 240e caractere d'un fragment etait
invisible, et un mot frequent (« vps ») pesait autant qu'un identifiant rare.

Matrice creuse CSR terme -> fragments, poids BM25 precalcules : une requete coute
quelques lectures de colonnes, pas un balayage des 240k fragments.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

K1 = 1.2
B = 0.75

# Mots contenant tirets/points/underscores gardes entiers (ORA-01555, ProtectSystem=strict
# est coupe sur `=`), ET leurs sous-parties : « unattended-upgrades » matche « unattended ».
_JETON = re.compile(r"[\w][\w.\-]*[\w]|\w", re.UNICODE)
_SOUS = re.compile(r"[^\W_]+", re.UNICODE)
_VIDES = frozenset(
    [
        "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "en", "au", "aux",
        "a", "pour", "par", "sur", "dans", "avec", "est", "sont", "ce", "ces", "qui", "que",
        "quoi", "comment", "pourquoi", "quel", "quelle",
        "the", "of", "and", "to", "in", "for", "on", "is", "it", "with",
    ]
)


def _plier(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.lower())
    return "".join(c for c in texte if not unicodedata.combining(c))


def tokens(texte: str) -> list[str]:
    sortie: list[str] = []
    for jeton in _JETON.findall(_plier(texte)):
        parties = _SOUS.findall(jeton)
        if len(parties) > 1:
            sortie.append(jeton)
        sortie.extend(p for p in parties if p not in _VIDES and (len(p) > 1 or p.isdigit()))
    return sortie


@dataclass
class IndexBM25:
    vocab: dict[str, int]
    indptr: NDArray[np.int64]
    lignes: NDArray[np.int32]
    poids: NDArray[np.float32]
    n: int

    @classmethod
    def construire(cls, textes: Sequence[str]) -> IndexBM25:
        # Postings accumules en petits tableaux numpy par fragment, puis tries par terme :
        # des listes Python par terme couteraient plusieurs Go sur 240k fragments.
        vocab: dict[str, int] = {}
        termes_parts: list[NDArray[np.int32]] = []
        tf_parts: list[NDArray[np.int32]] = []
        docs_parts: list[NDArray[np.int32]] = []
        n = len(textes)
        longueurs = np.zeros(n, dtype=np.float32)
        for i, texte in enumerate(textes):
            jetons = tokens(texte)
            longueurs[i] = len(jetons)
            if not jetons:
                continue
            ids = np.fromiter(
                (vocab.setdefault(j, len(vocab)) for j in jetons), dtype=np.int32, count=len(jetons)
            )
            uniques, comptes = np.unique(ids, return_counts=True)
            termes_parts.append(uniques.astype(np.int32))
            tf_parts.append(comptes.astype(np.int32))
            docs_parts.append(np.full(len(uniques), i, dtype=np.int32))
        moyenne = float(longueurs.mean()) if n else 1.0
        if termes_parts:
            termes = np.concatenate(termes_parts)
            ordre = np.argsort(termes, kind="stable")
            termes = termes[ordre]
            lignes = np.concatenate(docs_parts)[ordre]
            tf = np.concatenate(tf_parts)[ordre].astype(np.float32)
        else:
            termes = lignes = np.empty(0, dtype=np.int32)
            tf = np.empty(0, dtype=np.float32)
        del termes_parts, tf_parts, docs_parts
        indptr = np.zeros(len(vocab) + 1, dtype=np.int64)
        np.cumsum(np.bincount(termes, minlength=len(vocab)), out=indptr[1:])
        df = np.diff(indptr).astype(np.float32)
        idf = np.log1p((n - df + 0.5) / (df + 0.5)).astype(np.float32)
        norme = K1 * (1 - B + B * longueurs[lignes] / max(moyenne, 1.0))
        poids = np.repeat(idf, np.diff(indptr)) * tf * (K1 + 1) / (tf + norme)
        return cls(vocab, indptr, lignes, poids.astype(np.float32), n)

    def scores(self, requete: str) -> NDArray[np.float32]:
        s = np.zeros(self.n, dtype=np.float32)
        for j in dict.fromkeys(tokens(requete)):
            t = self.vocab.get(j)
            if t is None:
                continue
            a, b = self.indptr[t], self.indptr[t + 1]
            np.add.at(s, self.lignes[a:b], self.poids[a:b])
        return s


__all__ = ["IndexBM25", "tokens"]
