"""Tests adr/0023 (v4.1) : jetons sur OCTETS stockes et CAS Drive.

Ce fichier verrouille les trois proprietes du correctif des faux `conflit` :

  1. Les jetons (`sha256_raw` public, `md5_attendu` interne) sont calcules sur
     les OCTETS stockes -- un fichier CRLF ne peut plus produire un jeton
     normalise LF qui ne correspond a rien sur le disque.
  2. `read_note_versioned` expose contenu + `sha256_raw` + `taille_octets`
     depuis la MEME lecture ; aucun `md5_raw` ne sort par l API.
  3. Le CAS Drive (md5 natif) est alimente par le serveur via `md5_attendu`
     dans l intention ; le miroir n intervient plus.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

SECRET_DE_TEST = "x" * 32

# Contenu CRLF volontairement representatif d une note raw/ ecrite par un
# client Windows : CRLF partout, accent UTF-8, ligne finale sans retour.
OCTETS_CRLF = b"ligne 1\r\nligne 2 accentuee \xc3\xa9\r\nderniere ligne"
TEXTE_CRLF = "ligne 1\nligne 2 accentuee \u00e9\nderniere ligne"


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    vault = tmp_path / "vault"
    vault.mkdir()
    for sous in ("tmp", "queue", "done", "failed"):
        (tmp_path / "spool" / sous).mkdir(parents=True)
    monkeypatch.setenv("MCP_SECRET", SECRET_DE_TEST)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    monkeypatch.setenv("VAULT_MCP_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("VAULT_MCP_VAULT", str(vault))
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "1")
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


def _poser_note(serveur: Any, relatif: str, octets: bytes) -> None:
    cible = Path(serveur._store._racine) / relatif
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_bytes(octets)


def _intentions_uniques(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted((tmp_path / "spool" / "queue").glob("*.json"))
    ]


def _recu(tmp_path: Path, identifiant: str) -> Path:
    """Simule le recu ecrit par le pousseur : done/<id>.json, sans contenu."""
    fichier = next((tmp_path / "spool" / "queue").glob(f"*-{identifiant}.json"))
    intention = json.loads(fichier.read_text(encoding="utf-8"))
    intention.pop("contenu_b64", None)
    intention["resultat"] = "ok"
    intention["fini_a"] = "2026-09-05T00:00:00Z"
    recu = tmp_path / "spool" / "done" / f"{identifiant}.json"
    recu.write_text(json.dumps(intention), encoding="utf-8")
    fichier.unlink()
    return recu


# --- Hashs sur octets bruts -------------------------------------------------
def test_empreinte_octets_differe_du_hash_du_texte_normalise() -> None:
    # Un fichier CRLF a deux hashes possibles : celui des octets stockes (le
    # bon) et celui du texte re-normalise (l ancien bug). Ils doivent differer.
    from vault_mcp.ecriture import empreinte, empreinte_octets

    brut = hashlib.sha256(OCTETS_CRLF).hexdigest()
    assert empreinte_octets(OCTETS_CRLF) == brut
    assert empreinte(TEXTE_CRLF) != brut


def test_empreinte_md5_est_le_md5_des_octets() -> None:
    from vault_mcp.ecriture import empreinte_md5

    assert empreinte_md5(OCTETS_CRLF) == hashlib.md5(OCTETS_CRLF).hexdigest()


@pytest.mark.parametrize(
    "octets",
    [
        b"",
        b"ligne 1\nligne 2\n",
        b"ligne 1\r\nligne 2\r\n",  # CRLF partout
        b"ligne 1\rligne 2\r",  # CR seuls
        b"a\r\r\nb",  # CR double puis CRLF
        b"a\r\n\r\nb",
        b"d\xe9but invalide \xff ici",  # UTF-8 invalide -> U+FFFD
        b"accent \xc3\xa9 \xc3\xa8 fin",  # UTF-8 valide
        b"fin sans newline",
        b"fin sur \r\n",
        "\u00e9\u4e2d".encode("utf-8"),  # multi-octets aux frontieres
    ],
)
def test_decodage_lecture_equivaut_a_read_text(
    tmp_path: Path, octets: bytes
) -> None:
    """Le texte rendu doit etre identique a read_text(utf-8, errors=replace)."""
    from vault_mcp.ecriture import decodage_lecture

    fichier = tmp_path / "note.md"
    fichier.write_bytes(octets)
    attendu = fichier.read_text(encoding="utf-8", errors="replace")
    assert decodage_lecture(octets) == attendu


# --- lire_octets / lire_note : parite sur le miroir -------------------------
def test_lire_octets_rend_les_octets_et_lire_note_le_texte_normalise(
    serveur: Any, tmp_path: Path
) -> None:
    relatif = "wiki/e2e-crlf.md"
    _poser_note(serveur, relatif, OCTETS_CRLF)
    assert serveur._store.lire_octets(relatif) == OCTETS_CRLF
    assert serveur._store.lire_note(relatif) == TEXTE_CRLF


# --- read_note_versioned ----------------------------------------------------
def test_read_note_versioned_expose_jeton_et_taille_sans_md5(
    serveur: Any, tmp_path: Path
) -> None:
    relatif = "wiki/e2e-crlf.md"
    _poser_note(serveur, relatif, OCTETS_CRLF)
    resultat = serveur.read_note_versioned(path=relatif)
    # Clefs publiques exactes : contenu, sha256_raw, taille_octets, path.
    # AUCUN md5_raw : le md5 est un version-token interne, jamais expose.
    assert set(resultat) == {"path", "contenu", "sha256_raw", "taille_octets"}
    assert resultat["contenu"] == TEXTE_CRLF  # normalise LF
    assert resultat["sha256_raw"] == hashlib.sha256(OCTETS_CRLF).hexdigest()
    # La vraie taille BRUTE : len(octets), pas len(texte re-encode en UTF-8).
    assert resultat["taille_octets"] == len(OCTETS_CRLF)
    assert resultat["taille_octets"] != len(TEXTE_CRLF.encode("utf-8"))


def test_read_note_versioned_parite_avec_read_note(serveur: Any) -> None:
    relatif = "wiki/e2e-crlf.md"
    _poser_note(serveur, relatif, OCTETS_CRLF)
    assert serveur.read_note(path=relatif) == serveur.read_note_versioned(
        path=relatif
    )["contenu"]


def test_read_note_versioned_absente(serveur: Any) -> None:
    resultat = serveur.read_note_versioned(path="wiki/absente.md")
    assert "NOT FOUND" in resultat["erreur"]


def test_read_note_versioned_lecture_seule_suffit(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outil de LECTURE : le jeton mcp:lecture doit suffire, sans mcp:ecriture.
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "0")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture"])
    _poser_note(serveur, "a.md", b"x")
    assert serveur.read_note_versioned(path="a.md")["sha256_raw"]


# --- update_note : jeton conditionnel ---------------------------------------
def test_update_note_avec_bon_jeton_depose_md5_attendu(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    relatif = "wiki/e2e-crlf.md"
    _poser_note(serveur, relatif, OCTETS_CRLF)
    jeton = serveur.read_note_versioned(path=relatif)["sha256_raw"]

    resultat = serveur.update_note(path=relatif, content="nouveau", expected_sha256=jeton)
    assert resultat["etat"] == "en_attente"

    intention = _intentions_uniques(tmp_path)[0]
    assert intention["sha256_attendu"] == jeton
    # md5_attendu transporte le md5 des octets que le client a lus.
    assert intention["md5_attendu"] == hashlib.md5(OCTETS_CRLF).hexdigest()
    assert base64.b64decode(intention["contenu_b64"]).decode("utf-8") == "nouveau"


def test_update_note_avec_mauvais_jeton_refuse_sans_deposer(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    _poser_note(serveur, "wiki/a.md", OCTETS_CRLF)
    resultat = serveur.update_note(
        path="wiki/a.md", content="nouveau", expected_sha256="f" * 64
    )
    assert resultat["etat"] == "refuse"
    assert "conflit" in resultat["message"]
    assert list((tmp_path / "spool" / "queue").glob("*.json")) == []


def test_update_note_sans_jeton_depose_sans_cas(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    _poser_note(serveur, "wiki/a.md", OCTETS_CRLF)
    serveur.update_note(path="wiki/a.md", content="sans jeton")
    intention = _intentions_uniques(tmp_path)[0]
    assert intention["sha256_attendu"] is None
    # md5_attendu est un champ VRAIMENT optionnel (adr/0023) : sans jeton CAS,
    # la cle est ABSENTE pour garder le schema v1 byte-identique (cf. spool.py).
    assert "md5_attendu" not in intention


# --- Les outils a lecture-transformation portent les deux jetons -------------
@pytest.mark.parametrize(
    ("appel", "cle_attendue"),
    [
        (
            lambda s: s.delete_note(path="wiki/a.md"),
            "delete",
        ),
        (
            lambda s: s.append_note(path="wiki/a.md", content="ajout"),
            "update",
        ),
        (
            lambda s: s.patch_note(
                path="wiki/a.md", old_string="ligne 1", new_string="ligne UN"
            ),
            "update",
        ),
        (
            lambda s: s.set_frontmatter(path="wiki/a.md", fields={"cle": "valeur"}),
            "update",
        ),
        (
            lambda s: s.move_note(
                path="wiki/a.md", new_path="wiki/b.md", rewrite_backlinks=False
            ),
            "move",
        ),
    ],
)
def test_outils_deposent_sha256_et_md5_des_octets_lus(
    serveur: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    appel: Any,
    cle_attendue: str,
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    _poser_note(serveur, "wiki/a.md", OCTETS_CRLF)
    resultat = appel(serveur)
    assert resultat["etat"] == "en_attente"
    intention = _intentions_uniques(tmp_path)[0]
    assert intention["op"] == cle_attendue
    assert intention["sha256_attendu"] == hashlib.sha256(OCTETS_CRLF).hexdigest()
    assert intention["md5_attendu"] == hashlib.md5(OCTETS_CRLF).hexdigest()


def test_deposer_octets_transporte_les_deux_jetons(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    _poser_note(serveur, "wiki/a.md", OCTETS_CRLF)
    octets = serveur._store.lire_octets("wiki/a.md")
    serveur._deposer_octets("update", "wiki/a.md", octets, contenu="x")
    intention = _intentions_uniques(tmp_path)[0]
    assert intention["sha256_attendu"] == hashlib.sha256(OCTETS_CRLF).hexdigest()
    assert intention["md5_attendu"] == hashlib.md5(OCTETS_CRLF).hexdigest()


# --- sync_now bloque jusqu au vrai sync --------------------------------------
def test_sync_now_sans_fin_d_attente_rend_en_attente_sans_faux_ok(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    # Neutralise les sleep : le poll boucle sans attendre reellement.
    monkeypatch.setattr("time.sleep", lambda _: None)
    resultat = serveur.sync_now(timeout_s=1)
    assert resultat["etat"] == "en_attente"
    assert "toujours en cours" in resultat["message"]
    assert "resultat" not in resultat
    # L intention est bien restee en file (pas d application simulee).
    assert [i["op"] for i in _intentions_uniques(tmp_path)] == ["admin/sync"]


def test_sync_now_rend_applique_quand_le_pousseur_a_fini(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    monkeypatch.setattr("time.sleep", lambda _: None)

    identifiant = serveur._spool.deposer("admin/sync", "-", client_id="test")
    _recu(tmp_path, identifiant)

    # sync_now poll SON intention ; on force le depot a rendre le meme id,
    # comme si le pousseur l avait deja consommee.
    vrai_deposer = serveur._spool.deposer

    def _deposer_vers_le_meme(*args: Any, **kwargs: Any) -> str:
        return identifiant

    monkeypatch.setattr(serveur._spool, "deposer", _deposer_vers_le_meme)
    try:
        resultat = serveur.sync_now(timeout_s=5)
    finally:
        monkeypatch.setattr(serveur._spool, "deposer", vrai_deposer)
    assert resultat["etat"] == "applique"
    assert resultat["resultat"] == "ok"


def test_sync_now_rend_echec_si_le_sync_a_echoue(
    serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoriser_ecriture(serveur, monkeypatch)
    monkeypatch.setattr("time.sleep", lambda _: None)

    identifiant = serveur._spool.deposer("admin/sync", "-", client_id="test")
    recu = _recu(tmp_path, identifiant)
    recu.rename(tmp_path / "spool" / "failed" / f"{identifiant}.json")

    vrai_deposer = serveur._spool.deposer

    def _deposer_vers_le_meme(*args: Any, **kwargs: Any) -> str:
        return identifiant

    monkeypatch.setattr(serveur._spool, "deposer", _deposer_vers_le_meme)
    try:
        resultat = serveur.sync_now(timeout_s=5)
    finally:
        monkeypatch.setattr(serveur._spool, "deposer", vrai_deposer)
    assert resultat["etat"] == "echec"
