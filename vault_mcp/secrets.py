"""Masquage des secrets dans tout ce que le serveur renvoie.

Raison d'etre, constatee le 2026-08-14 : le mot de passe CouchDB figure **en clair dans
trois notes du vault** (transcriptions de conversations d'audit). Ces notes sont
synchronisees sur Drive, repliquees sur le VPS, indexees dans le RAG, et donc
interrogeables via un serveur MCP joignable depuis internet. Une recherche anodine
pouvait les remonter.

Le vrai correctif est de retirer le secret des notes et de tourner le mot de passe. Ce
module est la ceinture qui va avec les bretelles : quoi qu'il arrive dans le vault, une
valeur connue comme sensible ne sort pas d'ici.

Principe : on ne devine pas ce qui est un secret (les heuristiques a base d'entropie
produisent surtout des faux positifs sur du markdown technique). On masque exactement
les valeurs que le service connait, parce qu'il les a dans son environnement.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

REMPLACEMENT = "<secret masque>"

# En dessous de cette longueur, une valeur est trop courte pour etre masquee sans
# risque : masquer une chaine de 4 caracteres mutilerait le texte partout.
LONGUEUR_MIN = 12

# Variables d'environnement dont la valeur ne doit jamais sortir.
VARIABLES = ("MCP_SECRET", "VAULT_MCP_TOKEN", "COUCH_URL")

_MOT_DE_PASSE_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^:/@]+:([^@]+)@")


def _valeurs(environnement: dict[str, str]) -> tuple[str, ...]:
    trouvees: list[str] = []
    for nom in VARIABLES:
        brut = environnement.get(nom, "").strip()
        if not brut:
            continue
        if len(brut) >= LONGUEUR_MIN:
            trouvees.append(brut)
        # Une URL de connexion porte le mot de passe en son milieu : c'est cette
        # sous-chaine qui se retrouve recopiee dans les notes, pas l'URL entiere.
        correspondance = _MOT_DE_PASSE_URL.match(brut)
        if correspondance:
            motdepasse = correspondance.group(1)
            if len(motdepasse) >= LONGUEUR_MIN:
                trouvees.append(motdepasse)
    # Les plus longues d'abord : masquer l'URL entiere avant son mot de passe evite
    # de laisser un fragment d'URL reconnaissable derriere le remplacement.
    return tuple(sorted(set(trouvees), key=len, reverse=True))


@lru_cache(maxsize=1)
def valeurs_sensibles() -> tuple[str, ...]:
    return _valeurs(dict(os.environ))


def masquer(texte: str, valeurs: tuple[str, ...] | None = None) -> str:
    """Remplace toute occurrence d'une valeur sensible connue."""
    if not texte:
        return texte
    for valeur in valeurs if valeurs is not None else valeurs_sensibles():
        if valeur in texte:
            texte = texte.replace(valeur, REMPLACEMENT)
    return texte
