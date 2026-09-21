"""Autorite documentaire d'un chemin du vault, et detection d'intention historique.

92 % des fragments indexes sont des conversations brutes (`raw/`). Sans prior, un
transcript qui repete un identifiant dix fois passe devant la fiche canonique qui le
cite une fois. L'autorite est deduite du chemin, seule metadonnee presente dans
l'index pour chaque fragment :

  0  notes/, Architecture/, fichiers racine  -- etat courant, fiches verifiees
  1  wiki/entities, wiki/concepts            -- memoire durable synthetisee
  2  wiki/sources                            -- enrichissement
  3  raw/assets/ConvIA-Analysis              -- analyse d'episode
  4  raw/ (dont ConvIA brut)                 -- episode, historique
"""

from __future__ import annotations

import re

_HISTORIQUE = re.compile(
    r"\b(conversation|historique|discut|ancien|avant|hier|le \d{1,2}|du \d{1,2}|"
    r"\d{4}-\d{2}-\d{2}|janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|"
    r"septembre|octobre|novembre|decembre|décembre)\b",
    re.IGNORECASE,
)


def rang_autorite(chemin: str) -> int:
    if chemin.startswith("raw/assets/ConvIA-Analysis/"):
        return 3
    if chemin.startswith("raw/"):
        return 4
    if chemin.startswith("wiki/sources/"):
        return 2
    if chemin.startswith("wiki/"):
        return 1
    return 0


def historique(requete: str) -> bool:
    """Vrai si la requete vise un episode date ou une conversation passee."""
    return bool(_HISTORIQUE.search(requete))


__all__ = ["historique", "rang_autorite"]
