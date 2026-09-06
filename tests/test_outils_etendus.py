"""Tests des outils etendus : create_folder, fix_links, reindex_note.

Ajoutes le 2026-09-05 pour couvrir l'integration d'outils manquants a la liste
souhaitee (`create_folder`, `fix_links`, `reindex_note`). Les gardes d'ecriture
(scope, drapeau) sont deja testees dans `test_server_ecriture.py` via la liste
`OUTILS_D_ECRITURE` ; ce fichier teste la LOGIQUE de chacun : validation des
chemins de dossier, reecriture conservative des liens, reindexation locale
d'une seule note.
"""

from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vault_mcp.index import Index, MetaFragment, sauvegarder
from vault_mcp.safety import CheminInvalideError, valider_dossier

SECRET_DE_TEST = "x" * 32


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    for sous in ("tmp", "queue", "done", "failed"):
        (tmp_path / "spool" / sous).mkdir(parents=True)
    monkeypatch.setenv("MCP_SECRET", SECRET_DE_TEST)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    monkeypatch.setenv("VAULT_MCP_SPOOL", str(tmp_path / "spool"))
    module = importlib.import_module("vault_mcp.server")
    return importlib.reload(module)


class _JetonDouble:
    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes
        self.client_id = "client-de-test"


def _poser_jeton(serveur: Any, monkeypatch: pytest.MonkeyPatch, scopes: list[str]) -> None:
    monkeypatch.setattr(serveur, "get_access_token", lambda: _JetonDouble(scopes))


def _autoriser_ecriture(serveur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "1")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture", "mcp:ecriture"])


def _intentions(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted((tmp_path / "spool" / "queue").glob("*.json"))
    ]


class _VaultDouble:
    """Double du miroir : quelques notes en memoire, interface de MirrorStore.

    Interface depuis adr/0023 : `lire_note` (texte) ET `lire_octets` (octets)
    doivent rester coherents -- le texte derive des octets stockes.
    """

    def __init__(self, notes: dict[str, str]) -> None:
        self._notes = notes

    def lister_chemins(self, prefix: str = "", limit: int = 200) -> list[str]:
        return list(self._notes)

    def lire_octets(self, path: str) -> bytes | None:
        texte = self._notes.get(path)
        return None if texte is None else texte.encode("utf-8")

    def lire_note(self, path: str) -> str | None:
        return self._notes.get(path)


# --- valider_dossier (safety) ------------------------------------------------
@pytest.mark.parametrize("chemin", ["nouveau-dossier", "bac-a-sable/test", "a/b/c"])
def test_valider_dossier_accepte(chemin: str) -> None:
    assert valider_dossier(chemin).relatif == chemin


@pytest.mark.parametrize(
    "chemin",
    [
        "x.md",  # un .md est une note, jamais un dossier
        ".cachedir",  # masque par Obsidian
        ".claude/travail",  # exclusions de lecture/ecriture
        "../../etc",  # sortie du vault
        "/absolu",
        "",
        "C:/windows",
    ],
)
def test_valider_dossier_refuse(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_dossier(chemin)


# --- create_folder -----------------------------------------------------------
def test_create_folder_depose_une_intention_mkdir(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble({})

    resultat = serveur.create_folder(path="bac-a-sable/nouveau-dossier")

    assert resultat["etat"] == "en_attente"
    (intention,) = _intentions(tmp_path)
    assert intention["op"] == "mkdir"
    assert intention["path"] == "bac-a-sable/nouveau-dossier"
    assert intention["client_id"] == "client-de-test"
    assert intention["contenu_b64"] is None


def test_create_folder_refuse_un_nom_de_note(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble({})

    resultat = serveur.create_folder(path="bac-a-sable/note.md")

    assert resultat["etat"] == "refuse"
    assert ".md" in resultat["message"]
    assert _intentions(tmp_path) == []


# --- fix_links ---------------------------------------------------------------
def test_fix_links_reecrit_la_casse_unique(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble(
        {
            "a.md": "Voir [[chatgpt]] plus bas.\n",
            "ChatGPT.md": "# ChatGPT\n",
        }
    )

    resultat = serveur.fix_links(path="a.md")

    assert resultat["etat"] == "applique"
    assert resultat["notes_modifiees"] == ["a.md"]
    assert resultat["cibles_cassees"] == 1
    (intention,) = _intentions(tmp_path)
    assert intention["op"] == "update"
    contenu = base64.b64decode(intention["contenu_b64"]).decode("utf-8")
    assert "[[ChatGPT]]" in contenu
    assert resultat["intents"] == [intention["id"]]


def test_fix_links_portee_vault_entier_par_defaut(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble(
        {
            "a.md": "Voir [[chatgpt]].\n",
            "b.md": "Rien a reparer ici.\n",
            "ChatGPT.md": "# ChatGPT\n",
        }
    )

    resultat = serveur.fix_links()

    assert resultat["notes_examinees"] == 3
    assert resultat["notes_modifiees"] == ["a.md"]
    assert len(_intentions(tmp_path)) == 1


def test_fix_links_refuse_une_cible_ambigue(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble(
        {
            "a.md": "Voir [[CHATGPT]].\n",
            "ChatGPT.md": "# ChatGPT\n",
            "chatgpt.md": "# chatgpt\n",
        }
    )

    resultat = serveur.fix_links(path="a.md")

    assert resultat["notes_modifiees"] == []
    assert resultat["non_resolus"]["CHATGPT"]["motif"] == "cible ambigue"
    assert _intentions(tmp_path) == []


def test_fix_links_epargne_les_blocs_de_code(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble(
        {
            "a.md": "Exemple :\n```\n[[chatgpt]]\n```\n",
            "ChatGPT.md": "# ChatGPT\n",
        }
    )

    resultat = serveur.fix_links(path="a.md")

    # La cible est detectee, mais rien n'est reecrit : le bloc de code documente
    # une syntaxe, il ne pointe nulle part.
    assert resultat["notes_modifiees"] == []
    assert _intentions(tmp_path) == []


def test_fix_links_note_absente(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    serveur._store = _VaultDouble({"ChatGPT.md": "# ChatGPT\n"})

    resultat = serveur.fix_links(path="introuvable.md")

    assert resultat["etat"] == "refuse"
    assert "NOT FOUND" in resultat["message"]


# --- reindex_note ------------------------------------------------------------
def _index_minimal(tmp_path: Path, repertoire: Path) -> None:
    """Construit un index a deux notes, une ligne de vecteurs chacune."""
    metas = [
        MetaFragment(chemin="a.md", rang=0, titre="A", apercu="ancien a"),
        MetaFragment(chemin="b.md", rang=0, titre="B", apercu="ancien b"),
    ]
    vecteurs = np.full((2, 384), 1.0 / np.sqrt(384), dtype=np.float32)
    sauvegarder(repertoire, vecteurs, metas, {})


def test_reindex_note_remplace_les_fragments_de_la_note(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    repertoire = tmp_path / "index"
    _index_minimal(tmp_path, repertoire)
    serveur._index = Index(repertoire)
    serveur._store = _VaultDouble(
        {
            "a.md": "# Nouveau contenu\n\nReference [[b]] maintenant.\n",
            "b.md": "# B\n",
        }
    )

    # Pas de vrai modele d'embedding en test : on rejoue la normalisation.
    def _faux_vectoriser(textes: list[str]) -> np.ndarray:
        return np.full((len(textes), 384), 1.0 / np.sqrt(384), dtype=np.float32)

    monkeypatch.setattr("vault_mcp.index.vectoriser", _faux_vectoriser)

    resultat = serveur.reindex_note(path="a.md")

    assert resultat["etat"] == "applique"
    assert resultat["fragments_avant"] == 2
    assert resultat["fragments_nouveaux"] >= 1

    relu = Index(repertoire)
    assert "a.md" in relu.fragments_par_note
    assert "b.md" in relu.fragments_par_note
    # La note reindexee cite [[b]] : son apport au graphe est rejoue.
    assert "b" in relu.backlinks and "a.md" in relu.backlinks["b"]
    # Seule la note reindexee a change : 1 ligne (b) + les nouveaux fragments de a.
    assert len(relu.metas) == 1 + resultat["fragments_nouveaux"]


def test_reindex_note_refuse_les_notes_exclues(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    repertoire = tmp_path / "index"
    _index_minimal(tmp_path, repertoire)
    serveur._index = Index(repertoire)
    serveur._store = _VaultDouble({"index.md": "# index\n"})

    resultat = serveur.reindex_note(path="index.md")

    assert resultat["etat"] == "refuse"
    assert "exclue" in resultat["message"]
