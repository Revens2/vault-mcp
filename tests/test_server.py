"""Tests du contrat des outils MCP.

`server.py` lit l'environnement a l'import (FastMCP se construit au niveau module) :
les tests posent donc un environnement minimal avant d'importer, et remplacent le
store par un double. Aucun CouchDB, aucun reseau.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest

from vault_mcp.safety import CheminInvalideError
from vault_mcp.store import ResultatRecherche, StoreError

SECRET_DE_TEST = "x" * 32


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    tmp_path_oauth = tmp_path / "oauth"
    monkeypatch.setenv("MCP_SECRET", SECRET_DE_TEST)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    # Depuis la bascule OAuth, le module exige un emetteur HTTPS a l import.
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path_oauth))
    module = importlib.import_module("vault_mcp.server")
    return importlib.reload(module)


class _StoreDouble:
    def __init__(self, **reponses: Any) -> None:
        self._reponses = reponses

    def _rendre(self, cle: str) -> Any:
        valeur = self._reponses[cle]
        if isinstance(valeur, Exception):
            raise valeur
        return valeur

    def lister_chemins(self, prefix: str = "", limit: int = 200) -> Any:
        return self._rendre("lister")

    def lire_note(self, path: str) -> Any:
        return self._rendre("lire")

    def rechercher(self, query: str, limit: int = 20) -> Any:
        return self._rendre("chercher")


def test_refuse_de_demarrer_avec_un_secret_court(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_SECRET", "court")
    # Servir le vault derriere un secret devinable est pire que ne pas demarrer.
    with pytest.raises(RuntimeError, match="trop court"):
        serveur._config()


def test_sans_secret_la_voie_url_est_desactivee(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Depuis le retrait du secret d'URL (2026-08-15), son absence est l'etat normal :
    # le serveur demarre et la voie est simplement inerte.
    monkeypatch.delenv("MCP_SECRET", raising=False)
    secret, _ = serveur._config()
    assert secret == ""
    from vault_mcp.auth import chemin_secret_valide

    assert not chemin_secret_valide("/mcp", "")
    assert not chemin_secret_valide("/mcp/nimporte-quoi", "")


def test_config_accepte_un_secret_de_longueur_suffisante(serveur: Any) -> None:
    secret, port = serveur._config()
    assert secret == SECRET_DE_TEST
    assert port == 8799


def test_le_chemin_du_transport_contient_le_secret(serveur: Any) -> None:
    assert serveur.SECRET == SECRET_DE_TEST
    assert os.environ["MCP_SECRET"] == SECRET_DE_TEST


def test_read_note_pagine(serveur: Any) -> None:
    serveur._store = _StoreDouble(lire="abcdefghij")
    assert serveur.read_note("wiki/n.md", offset=2, limit=3) == "cde"


def test_read_note_absente_conserve_le_libelle_v1(serveur: Any) -> None:
    serveur._store = _StoreDouble(lire=None)
    # Libelle repris tel quel de la v1 : le connecteur claude.ai est deja configure
    # dessus, le changer serait une regression invisible cote client.
    assert serveur.read_note("wiki/x.md") == "NOT FOUND: wiki/x.md"


def test_read_note_chemin_invalide_ne_leve_pas(serveur: Any) -> None:
    serveur._store = _StoreDouble(lire=CheminInvalideError("chemin sortant du vault"))
    resultat = serveur.read_note("../../etc/passwd.md")
    # Un outil MCP qui leve renvoie une erreur de protocole opaque ; on prefere un
    # message exploitable par le modele appelant.
    assert resultat.startswith("ERREUR:")


def test_list_notes_transmet_la_liste(serveur: Any) -> None:
    serveur._store = _StoreDouble(lister=["wiki/a.md", "b.md"])
    assert serveur.list_notes() == ["wiki/a.md", "b.md"]


def test_list_notes_erreur_stockage_reste_une_liste(serveur: Any) -> None:
    serveur._store = _StoreDouble(lister=StoreError("stockage injoignable (ConnectError)"))
    resultat = serveur.list_notes()
    assert isinstance(resultat, list)
    assert resultat[0].startswith("ERREUR:")


def test_search_notes_forme_de_sortie(serveur: Any) -> None:
    serveur._store = _StoreDouble(chercher=[ResultatRecherche(path="wiki/a.md", snippet="extrait")])
    assert serveur.search_notes("aiguille") == [{"path": "wiki/a.md", "snippet": "extrait"}]


def test_search_notes_erreur_reste_une_liste_de_dicts(serveur: Any) -> None:
    serveur._store = _StoreDouble(chercher=StoreError("requete vide"))
    resultat = serveur.search_notes("   ")
    assert isinstance(resultat, list)
    assert resultat[0]["snippet"].startswith("ERREUR:")


def test_token_court_refuse(serveur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_MCP_TOKEN", "trop-court")
    with pytest.raises(RuntimeError, match="trop court"):
        serveur._token()


def test_token_absent_est_tolere(serveur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Token vide = voie Bearer desactivee, pas d'ouverture : `token_valide` refuse
    # systematiquement quand l'attendu est vide (teste dans test_auth).
    monkeypatch.delenv("VAULT_MCP_TOKEN", raising=False)
    assert serveur._token() == ""


def test_chemin_secret_derive_du_secret(serveur: Any) -> None:
    assert f"/mcp/{SECRET_DE_TEST}" == serveur.CHEMIN_SECRET
    assert serveur.CHEMIN_PUBLIC == "/mcp"


def test_application_construite_est_bien_protegee(serveur: Any) -> None:
    from vault_mcp.auth import Authentification

    assert isinstance(serveur.construire_application(), Authentification)
