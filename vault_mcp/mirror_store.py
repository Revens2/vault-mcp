"""Acces en lecture seule au miroir Drive du vault (`/srv/vault-mirror`).

POURQUOI CE MODULE EXISTE
-------------------------
`search_vault` interrogeait l index construit sur le miroir, tandis que `read_note`,
`list_notes` et `search_notes` passaient par CouchDB `vault_rag` -- un instantane
mort de juin 2026 (adr/0012). Consequence mesuree le 2026-08-16 : une note remontee
par la recherche renvoyait `NOT FOUND` a la lecture. Le RAG etait trouvable mais
pas lisible, ce qui est pire qu un RAG absent : l appelant croit avoir une source.

Ce module remet les quatre outils sur la MEME source de verite (invariant 7).

MODELE DE MENACE
----------------
`path` vient du RESEAU : ce service est le seul composant du VPS expose a internet.
La defense est a deux etages, et les deux sont necessaires :

  1. `vault_mcp.safety.normalize_path` -- etage CHAINE. Rejette l octet nul, les
     chemins absolus POSIX et Windows, les prefixes reserves, et la remontee `..`
     APRES normalisation. Cet etage est pur, donc testable exhaustivement.

  2. `os.path.realpath` + verification de confinement -- etage SYSTEME DE FICHIERS.
     Indispensable et non redondant : un LIEN SYMBOLIQUE place dans le vault et
     pointant vers `/etc/shadow` a un chemin relatif parfaitement legitime. Aucune
     analyse de chaine ne peut le voir ; seule la resolution reelle le peut.
     Le miroir est reconstruit par `rclone sync` depuis Drive, donc un lien y est
     improbable -- mais << improbable >> n est pas un controle d acces.

Regle heritee de `store.py` et maintenue ici : aucun message d erreur ne divulgue
de chemin systeme absolu. L appelant distant apprend que son chemin est refuse,
jamais ou se trouve le vault.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from vault_mcp.ecriture import decodage_lecture
from vault_mcp.safety import (
    PREFIXES_EXCLUS_LECTURE,
    CheminInvalideError,
    normalize_path,
)
from vault_mcp.store import ResultatRecherche, StoreError, _borner

VAULT_DEFAUT = "/srv/vault-mirror"

# Meme liste que `scripts/reindex.py` : ce qui n est pas indexe ne doit pas etre
# lisible non plus. Sans cet alignement, `read_note` exposerait la configuration
# d agents (`.claude/`, `.hermes/`) que l indexation ecarte deliberement, sur un
# service joignable depuis internet.
# Declarees dans `safety` depuis adr/0020 : les frontieres du vault ont une seule
# source de verite. C est bien la liste de LECTURE qui est importee, pas l union
# avec les interdits d ecriture -- `raw/` doit rester lisible.
PREFIXES_EXCLUS = PREFIXES_EXCLUS_LECTURE

# 8 Mio : au-dela on refuse plutot que de materialiser le fichier en memoire.
# Volontairement plus large que MAX_NOTE_BYTES (1 Mio, plafond d ECRITURE) : le
# vault contient des transcriptions de 450 ko qu il faut pouvoir relire.
MAX_LECTURE_OCTETS = 8 * 1024 * 1024

# Borne du balayage de `rechercher` : le miroir compte ~5 600 notes et 210 Mo.
# Sans plafond, une requete distante devient un seau perce a lire tout le disque.
MAX_FICHIERS_BALAYES = 50_000
MAX_OCTETS_BALAYES = 512 * 1024 * 1024


class MirrorStore:
    """Lecture seule sur le miroir Drive. Interface identique a `CouchStore`."""

    def __init__(self, racine: str = VAULT_DEFAUT) -> None:
        # realpath une fois pour toutes a la construction : la racine de reference
        # doit elle-meme etre resolue, sinon la comparaison de confinement porterait
        # sur un chemin qui n est pas celui reellement ouvert.
        self._racine = Path(os.path.realpath(racine))
        if not self._racine.is_dir():
            raise StoreError("miroir du vault introuvable")

    @classmethod
    def depuis_env(cls) -> MirrorStore:
        return cls(os.environ.get("VAULT_MCP_VAULT", VAULT_DEFAUT))

    # ------------------------------------------------------------ confinement
    def _resoudre(self, path: str, *, exiger_md: bool = True) -> Path:
        """Chemin client -> chemin absolu prouve a l interieur du miroir.

        Leve `CheminInvalideError` sur tout ce qui sort du vault. Ne revele
        jamais le chemin systeme dans le message.
        """
        note = normalize_path(path, exiger_md=exiger_md)
        if note.relatif.startswith(PREFIXES_EXCLUS):
            raise CheminInvalideError("chemin exclu du vault")

        candidat = self._racine / note.relatif
        reel = Path(os.path.realpath(candidat))

        # `is_relative_to` compare des chemins DEJA resolus des deux cotes. C est
        # le seul controle qui attrape un lien symbolique sortant.
        if reel != self._racine and not reel.is_relative_to(self._racine):
            raise CheminInvalideError("chemin sortant du vault")
        return reel

    # ------------------------------------------------------------------ outils
    def lire_octets(self, path: str) -> bytes | None:
        """Octets bruts d'une note, sans traduction de newlines.

        Source UNIQUE des jetons de concurrence (adr/0023) : hasher le texte
        normalise (CRLF -> LF) ne correspondrait jamais au `sha256sum` du
        fichier stocke. Les memes gardes que `lire_note` s appliquent :
        confinement, fichier ordinaire, plafond de taille.
        """
        reel = self._resoudre(path)
        if not reel.exists():
            return None
        # Refus explicite de tout ce qui n est pas un fichier ordinaire : un FIFO
        # ferait bloquer le service indefiniment, un peripherique le ferait lire
        # a l infini. `is_file()` suit les liens, mais `_resoudre` a deja prouve
        # le confinement de la cible.
        if not reel.is_file():
            raise StoreError("la cible n'est pas une note")
        taille = reel.stat().st_size
        if taille > MAX_LECTURE_OCTETS:
            raise StoreError(
                f"note trop volumineuse : {taille} octets, maximum {MAX_LECTURE_OCTETS}"
            )
        try:
            return reel.read_bytes()
        except OSError:
            # Jamais l exception brute : elle porte le chemin absolu.
            raise StoreError("lecture impossible") from None

    def lire_note(self, path: str) -> str | None:
        """Contenu texte d'une note (newlines normalises), ou None si absente.

        Reimplementee PAR-DESSUS `lire_octets` : le texte rendu et les octets
        hachables derivent des MEMES octets, donc aucune derive possible entre
        ce que lit l'API et ce que les jetons representent (adr/0023).
        """
        octets = self.lire_octets(path)
        if octets is None:
            return None
        return decodage_lecture(octets)

    def _notes(self) -> Iterator[tuple[str, Path]]:
        """Toutes les notes du miroir, chemins relatifs POSIX, exclusions appliquees."""
        for chemin in sorted(self._racine.rglob("*.md")):
            relatif = chemin.relative_to(self._racine).as_posix()
            if relatif.startswith(PREFIXES_EXCLUS):
                continue
            if Path(relatif).name.startswith("livesync_log_"):
                continue
            yield relatif, chemin

    def lister_chemins(self, prefix: str = "", limit: int = 200) -> list[str]:
        borne = _borner(limit)
        filtre = ""
        if prefix.strip():
            # exiger_md=False : un prefixe est un dossier, pas une note.
            filtre = normalize_path(prefix, exiger_md=False).relatif.rstrip("/")
        sortie: list[str] = []
        for relatif, _ in self._notes():
            if filtre and not (relatif == filtre or relatif.startswith(filtre + "/")):
                continue
            sortie.append(relatif)
            if len(sortie) >= borne:
                break
        return sortie

    def rechercher(self, query: str, limit: int = 20) -> list[ResultatRecherche]:
        besoin = query.strip()
        if not besoin:
            raise StoreError("requete vide")
        borne = _borner(limit)
        aiguille = besoin.lower()
        sortie: list[ResultatRecherche] = []
        fichiers = octets = 0
        for relatif, chemin in self._notes():
            fichiers += 1
            if fichiers > MAX_FICHIERS_BALAYES or octets > MAX_OCTETS_BALAYES:
                break
            try:
                contenu = chemin.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            octets += len(contenu)
            pos = contenu.lower().find(aiguille)
            if pos < 0:
                continue
            debut = max(0, pos - 80)
            extrait = contenu[debut : pos + len(besoin) + 80].replace("\n", " ")
            sortie.append(ResultatRecherche(path=relatif, snippet=extrait.strip()))
            if len(sortie) >= borne:
                break
        return sortie


__all__ = ["MirrorStore"]
