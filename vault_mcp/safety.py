"""Normalisation et validation des chemins de notes.

Ce module est la seule porte d'entree autorisee pour transformer un chemin fourni
par un client MCP en chemin utilisable. Il est volontairement pur : aucune E/S, aucun
acces reseau, aucune dependance. C'est ce qui le rend testable exhaustivement.

Regle de fond : on valide *apres* normalisation, jamais avant. Verifier la chaine
brute laisse passer `a/../../etc/passwd`, qui ne contient pourtant aucun prefixe suspect.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

# 1 Mio. Au-dela on refuse ; jamais de troncature silencieuse, qui produirait une
# note incomplete indiscernable d'une note complete.
MAX_NOTE_BYTES = 1024 * 1024

# Prefixes interdits en ecriture, quel que soit le mode. Ce sont soit des zones de
# travail d'outils (`.hermes`, `.staging`), soit l'etat local d'Obsidian (`.obsidian`),
# soit la zone d'ingestion brute (`raw/`) qui est alimentee par la chaine llm-wiki.
# NOTE 2026-09-05 (adr/0023) : `raw/` a ete RETIRE de cette liste. La zone d ingestion
# brute est desormais ecrivable par le MCP. Deux ecrivains y cohabitent, le pousseur de
# spool et la chaine llm-wiki. C est tenable parce que llm_wiki_sync.sh ne fait plus de
# `rclone copy` vers raw/ et que llm_wiki_ingest.sh ne fait que LIRE $RAW_DIR : le seul
# `rclone sync` en jeu descend Drive vers le miroir, et les ecritures MCP passent par
# Drive en premier. Une note ecrite ici sera ingeree par le cycle du dimanche 23:00.
PREFIXES_INTERDITS_EN_ECRITURE = (
    ".hermes/",
    ".staging/",
    ".obsidian/",
    ".trash/",
    ".temp/",
)

# `C:\`, `\serveur\partage`, `C:/`... Un client Windows mal configure peut envoyer
# un chemin absolu local ; il n'a aucun sens cote vault et doit etre refuse net.
_CHEMIN_ABSOLU_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class CheminInvalideError(ValueError):
    """Chemin refuse. Le message est sur : il ne divulgue aucun chemin systeme."""


@dataclass(frozen=True)
class CheminNote:
    """Chemin de note valide, relatif a la racine du vault, separateurs POSIX."""

    relatif: str

    @property
    def est_ecrivable(self) -> bool:
        return not self.relatif.startswith(PREFIXES_INTERDITS_EN_ECRITURE)


def normalize_path(path: str, *, exiger_md: bool = True) -> CheminNote:
    """Normalise un chemin de note et le valide, ou leve `CheminInvalideError`.

    `exiger_md=False` sert aux appels qui acceptent un dossier (filtre de prefixe
    sur `list_notes`, par exemple), ou l'extension n'a pas de sens.
    """
    if not isinstance(path, str):
        raise CheminInvalideError("chemin absent ou de type invalide")

    if "\x00" in path:
        raise CheminInvalideError("chemin contenant un octet nul")

    brut = path.strip()
    if not brut:
        raise CheminInvalideError("chemin vide")

    if _CHEMIN_ABSOLU_WINDOWS.match(brut):
        raise CheminInvalideError("chemin absolu Windows refuse")

    # Uniformiser avant normalisation : `dossier\note.md` doit se comporter comme
    # `dossier/note.md`, sans quoi le backslash survivrait dans un `_id` CouchDB.
    unifie = brut.replace("\\", "/")

    if unifie.startswith("/"):
        raise CheminInvalideError("chemin absolu refuse")

    normalise = posixpath.normpath(unifie)

    # normpath reduit `a/../..` a `..` et `.` a `.` : c'est ici, et seulement ici,
    # que la remontee hors du vault devient detectable de facon fiable.
    if normalise == ".." or normalise.startswith("../"):
        raise CheminInvalideError("chemin sortant du vault")
    if normalise in {".", ""}:
        raise CheminInvalideError("chemin vide apres normalisation")
    if normalise.startswith("/"):
        raise CheminInvalideError("chemin absolu refuse")

    # Un `_id` CouchDB commencant par `_` est reserve (`_design`, `_local`...).
    if normalise.startswith("_"):
        raise CheminInvalideError("prefixe reserve refuse")

    if exiger_md and not normalise.endswith(".md"):
        raise CheminInvalideError("extension refusee : seules les notes .md sont acceptees")

    return CheminNote(relatif=normalise)


def verifier_taille(contenu: str) -> int:
    """Renvoie la taille en octets UTF-8, ou leve si elle depasse `MAX_NOTE_BYTES`."""
    taille = len(contenu.encode("utf-8"))
    if taille > MAX_NOTE_BYTES:
        raise CheminInvalideError(
            f"note trop volumineuse : {taille} octets, maximum {MAX_NOTE_BYTES}"
        )
    return taille


# ---------------------------------------------------------------------------
# Ecriture (adr/0020). Tout ce qui suit a ete ajoute le 2026-09-05 avec les
# outils d ecriture ; rien au-dessus n a ete modifie.
# ---------------------------------------------------------------------------

# Exclusions de LECTURE du miroir. Elles vivent ici, et non dans `mirror_store`,
# pour que `safety` reste la seule source de verite des frontieres du vault :
# deux listes maintenues en parallele divergent, et une divergence signifie ici
# soit une note lisible qu on croyait exclue, soit une note ecrite dans un trou
# noir. `mirror_store` les importe -- jamais l inverse, `safety` doit rester sans
# dependance pour rester testable exhaustivement.
#
# `.trash-mcp/` est la corbeille Drive du pousseur (adr/0020 §9.1). Sans elle
# ici ET dans `scripts/reindex.py`, une note supprimee resterait lisible et
# indexee : une suppression qui ne supprime rien.
PREFIXES_EXCLUS_LECTURE = (
    ".obsidian/",
    ".trash/",
    ".temp/",
    ".git/",
    ".staging/",
    ".claude/",
    ".hermes/",
    ".mdinbox/",
    ".trash-mcp/",
)

# Union stricte des deux listes. L union est necessaire parce qu elles different :
# `raw/` n est que dans les interdits d ecriture (il est lisible, c est la zone
# d ingestion de la chaine llm-wiki), tandis que `.claude/`, `.git/`, `.mdinbox/`
# ne sont que dans les exclusions de lecture. Ecrire dans l une ou l autre est
# refuse.
PREFIXES_EXCLUS_ECRITURE: tuple[str, ...] = tuple(
    sorted(set(PREFIXES_INTERDITS_EN_ECRITURE) | set(PREFIXES_EXCLUS_LECTURE))
)

# `scripts/reindex.py::FICHIERS_EXCLUS` : ces deux notes de racine sont ecartees
# de l indexation (index.md pese 152 fragments pour aucune valeur semantique).
# Y ecrire produirait une note invisible de `search_vault` : un trou noir. On
# refuse plutot que de laisser croire a une ecriture utile.
FICHIERS_NON_INDEXES = ("index.md", "log.md")


def valider_ecriture(path: str, contenu: str | None = None) -> CheminNote:
    """Valide un chemin destine a l ECRITURE, ou leve `CheminInvalideError`.

    Porte unique de tous les outils d ecriture, appelee avant tout acces disque.
    Plus stricte que `normalize_path` : celle-ci garantit qu un chemin est bien
    formule et reste dans le vault, celle-ci garantit en plus qu y ecrire a un
    sens et n ecrase rien d intouchable.

    Attention : cette fonction est un controle de CHAINE. Elle ne voit pas les
    liens symboliques. Le confinement reel (`realpath` + verification) incombe
    au pousseur, qui est le seul processus a ecrire sur le disque (adr/0020).
    """
    note = normalize_path(path)

    if not note.est_ecrivable:
        raise CheminInvalideError("zone interdite en ecriture")

    if note.relatif.startswith(PREFIXES_EXCLUS_ECRITURE):
        raise CheminInvalideError("chemin exclu du vault")

    nom = note.relatif.rsplit("/", 1)[-1]

    # Un nom commencant par un point est masque par Obsidian et ecarte par la
    # plupart des balayages : l ecrire revient a perdre la note.
    if nom.startswith("."):
        raise CheminInvalideError("nom de note masque refuse")

    if nom.startswith("livesync_log_"):
        raise CheminInvalideError("nom reserve aux journaux LiveSync")

    if note.relatif in FICHIERS_NON_INDEXES:
        raise CheminInvalideError("note exclue de l indexation")

    if contenu is not None:
        verifier_taille(contenu)

    return note


def valider_dossier(path: str) -> CheminNote:
    """Valide un chemin de DOSSIER destine a `create_folder`, ou leve.

    Memes frontieres que `valider_ecriture`, avec deux differences : l extension
    `.md` n est pas exigee (un dossier n est pas une note), et un nom se terminant
    par `.md` est refuse -- dans le vault, `x.md` designe une note, pas un
    dossier ; accepter les deux pour la meme chaine creerait une ambiguite de
    chemin entre `x.md` fichier et `x.md` dossier.
    """
    note = normalize_path(path, exiger_md=False)

    if not note.est_ecrivable:
        raise CheminInvalideError("zone interdite en ecriture")

    if note.relatif.startswith(PREFIXES_EXCLUS_ECRITURE):
        raise CheminInvalideError("chemin exclu du vault")

    nom = note.relatif.rsplit("/", 1)[-1]

    if nom.startswith("."):
        raise CheminInvalideError("nom de dossier masque refuse")

    if nom.endswith(".md"):
        raise CheminInvalideError("extension .md refusee pour un dossier")

    return note
