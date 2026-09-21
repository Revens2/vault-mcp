"""Voie fraiche : recherche lexicale IMMEDIATE des notes pas encore vectorisees.

POURQUOI
--------
Une ConvIA arrive dans le miroir par `rclone` : aucune intention de spool, donc
aucun chemin sale. Elle n'entre dans l'index qu'a la reconciliation suivante (le
worker enchaine des lots de 400 notes a ~75 min chacun), puis attend son tour
d'embedding derriere une file de plus de 1 700 chemins (mesure 2026-09-14).
Resultat : plusieurs heures pendant lesquelles `search_vault` ne la voit pas.

La voie fraiche comble exactement cet ecart, sans LLM ni embedding :
  - l'ensemble « frais » = chemins sales (file, lots en vol, differes)
    + notes du miroir ecrites depuis le dernier parcours de reconciliation ;
  - un worker de quelques secondes (`scripts/frais_worker.py`) le recopie dans une
    base SQLite FTS5 et retire ce qui a ete publie dans l'index ;
  - `Index.recherche_hybride` / `recherche_lexicale` fusionnent ce classement.

Aucune dependance a l'enrichissement semantique (Scheduled Tasks ConvIA/Wiki) :
celles-ci ne font plus qu'ajouter du sens, jamais de la trouvabilite.

`VAULT_MCP_POIDS_FRAIS=0` desactive la fusion a chaud, sans redeploiement.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from vault_mcp import dirty
from vault_mcp.index import APERCU_CARACTERES, Resultat, repertoire_index
from vault_mcp.lexical import tokens
from vault_mcp.selection import indexable, notes, repertoire_vault

FRAIS_DEFAUT = Path("/var/lib/vault-mcp/frais.sqlite")

# Borne de l'ensemble frais : au-dela, c'est un index a reconstruire, pas un ecart.
# Les notes les plus recentes sont gardees.
MAXIMUM_NOTES = int(os.environ.get("VAULT_MCP_FRAIS_MAX", "5000"))

# Une ConvIA brute peut peser plusieurs centaines de Ko ; le debut suffit a la trouver.
TEXTE_MAXIMUM = 200_000

_SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE IF NOT EXISTS etat(chemin TEXT PRIMARY KEY, date_ns INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    chemin UNINDEXED, lieu, titre, texte, tokenize='unicode61 remove_diacritics 2'
);
"""


def base() -> Path:
    return Path(os.environ.get("VAULT_MCP_FRAIS", str(FRAIS_DEFAUT)))


def poids() -> float:
    return float(os.environ.get("VAULT_MCP_POIDS_FRAIS", "1.0"))


def chemins_indexes() -> set[str]:
    """Notes presentes dans l'index publie, lues depuis `meta.json` (sans verrou ni vecteurs)."""
    try:
        document = json.loads((repertoire_index() / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    fragments = document if isinstance(document, list) else document.get("fragments", [])
    return {fragment["chemin"] for fragment in fragments}


def ensemble_frais() -> dict[str, int]:
    """{chemin: date_ns} des notes du miroir ABSENTES de l'index publie et en attente.

    Une note deja indexee mais modifiee n'entre pas : sa version indexee reste servie
    jusqu'a son lot. L'y inclure faisait regresser le banc (MRR 0,751 -> 0,692, p95
    x2, mesure 2026-09-14) : ~2 000 notes au ctime touche doublaient leur classement.
    """
    racine = repertoire_vault()
    if not racine.is_dir():
        return {}
    sales = dirty.en_attente()
    seuil = dirty.seuil_reconciliation()
    indexes = chemins_indexes()
    dates: dict[str, int] = {}
    for fichier in notes(racine):
        relatif = fichier.relative_to(racine).as_posix()
        if relatif in indexes:
            continue
        try:
            etat = fichier.stat()
        except OSError:
            continue
        # ctime et non mtime seul : rclone recopie la date de Drive (cf. index_worker).
        date = max(etat.st_ctime_ns, etat.st_mtime_ns)
        if relatif in sales or (seuil and date >= seuil):
            dates[relatif] = date
    if len(dates) > MAXIMUM_NOTES:
        gardes = sorted(dates.items(), key=lambda kv: -kv[1])[:MAXIMUM_NOTES]
        dates = dict(gardes)
    return dates


def _titre(chemin: str, contenu: str) -> str:
    for ligne in contenu.splitlines()[:40]:
        if ligne.startswith("# "):
            return ligne[2:].strip()
    return chemin.rsplit("/", 1)[-1].removesuffix(".md")


def rafraichir() -> dict[str, int]:
    """Aligne la base fraiche sur l'ensemble frais. Idempotent, rejouable."""
    cible = base()
    cible.parent.mkdir(parents=True, exist_ok=True)
    racine = repertoire_vault()
    voulus = ensemble_frais()
    connexion = sqlite3.connect(cible, timeout=30)
    try:
        connexion.executescript(_SCHEMA)
        connus = dict(connexion.execute("SELECT chemin, date_ns FROM etat"))
        ajoutees = retirees = 0
        with connexion:
            for chemin in set(connus) - set(voulus):
                connexion.execute("DELETE FROM fts WHERE chemin = ?", (chemin,))
                connexion.execute("DELETE FROM etat WHERE chemin = ?", (chemin,))
                retirees += 1
            for chemin, date in voulus.items():
                if connus.get(chemin) == date:
                    continue
                try:
                    contenu = (racine / chemin).read_text(encoding="utf-8")[:TEXTE_MAXIMUM]
                except (OSError, UnicodeDecodeError):
                    continue
                lieu = chemin.replace("/", " ").replace("_", " ")
                connexion.execute("DELETE FROM fts WHERE chemin = ?", (chemin,))
                connexion.execute(
                    "INSERT INTO fts(chemin, lieu, titre, texte) VALUES (?, ?, ?, ?)",
                    (chemin, lieu, _titre(chemin, contenu), contenu),
                )
                connexion.execute(
                    "INSERT OR REPLACE INTO etat(chemin, date_ns) VALUES (?, ?)", (chemin, date)
                )
                ajoutees += 1
        total = connexion.execute("SELECT count(*) FROM etat").fetchone()[0]
    finally:
        connexion.close()
    return {"notes": int(total), "ajoutees": ajoutees, "retirees": retirees}


def rechercher(requete: str, limit: int = 10) -> list[Resultat]:
    """Classement BM25 (FTS5) des notes fraiches. Base absente ou occupee : rien."""
    cible = base()
    mots = list(dict.fromkeys(tokens(requete)))
    if limit <= 0 or not mots or not cible.exists():
        return []
    expression = " OR ".join('"' + mot.replace('"', '""') + '"' for mot in mots)
    try:
        connexion = sqlite3.connect(f"{cible.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            lignes = connexion.execute(
                "SELECT chemin, titre, snippet(fts, 3, '', '', ' … ', 40), "
                "bm25(fts, 0.0, 2.0, 5.0, 1.0) AS s FROM fts WHERE fts MATCH ? "
                "ORDER BY s LIMIT ?",
                (expression, limit),
            ).fetchall()
        finally:
            connexion.close()
    except sqlite3.Error:
        return []
    return [
        Resultat(
            chemin=chemin,
            titre=titre,
            apercu=" ".join(extrait.split())[:APERCU_CARACTERES],
            score=-float(score),
            origine="frais",
        )
        for chemin, titre, extrait, score in lignes
        if indexable(chemin)
    ]


def statistiques() -> dict[str, object]:
    cible = base()
    try:
        age = round(time.time() - cible.stat().st_mtime, 1)
        connexion = sqlite3.connect(f"{cible.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            notes_fraiches = connexion.execute("SELECT count(*) FROM etat").fetchone()[0]
        finally:
            connexion.close()
    except (OSError, sqlite3.Error):
        return {"frais_disponible": False}
    return {"frais_disponible": True, "frais_notes": int(notes_fraiches), "frais_age_s": age}


__all__ = ["base", "ensemble_frais", "poids", "rafraichir", "rechercher", "statistiques"]
