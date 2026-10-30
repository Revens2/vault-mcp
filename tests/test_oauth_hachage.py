"""Jetons vivants haches au repos dans etat.json (issue #14).

Contrat verifie :
- aucun jeton d'acces ni de rafraichissement n'apparait en clair dans etat.json ;
- lecture, rotation, revocation et detection de rejeu fonctionnent sur l'etat hache ;
- un etat herite en clair reste lisible (sans re-consentement) puis est reecrit
  hache a la premiere ecriture d'etat, ou a la demande (`migrer_etat`) ;
- la migration est idempotente et conserve familles et durees.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from scripts import oauth_revoquer
from vault_mcp.oauth import FournisseurOAuth, MagasinOAuth

JOUR = 24 * 3600


@pytest.fixture(autouse=True)
def _strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_MCP_OAUTH_REFRESH_TTL_S", raising=False)
    monkeypatch.delenv("VAULT_MCP_OAUTH_REFRESH_MAX_LIFETIME_S", raising=False)
    monkeypatch.setenv("VAULT_MCP_OAUTH_REFRESH_REUSE_GRACE_S", "0")


def _client(client_id: str = "client-a") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="s" * 32,
        redirect_uris=["https://client.example/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        scope="mcp:lecture",
    )


@pytest.fixture
def fournisseur(tmp_path: Path) -> FournisseurOAuth:
    magasin = MagasinOAuth(repertoire=tmp_path, clients=tmp_path / "clients.json")
    magasin.enregistrer_client(_client())
    return FournisseurOAuth("https://exemple.test", magasin=magasin)


def _brut(tmp_path: Path) -> str:
    return (tmp_path / "etat.json").read_text(encoding="utf-8")


def _sha(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


async def _rafraichir(f: FournisseurOAuth, jeton: str) -> Any:
    charge = await f.load_refresh_token(_client(), jeton)
    if charge is None:
        return None
    return await f.exchange_refresh_token(_client(), charge, [])


# --- Etat hache -------------------------------------------------------------------------
async def test_aucun_jeton_vivant_en_clair_sur_disque(fournisseur, tmp_path):
    premier = fournisseur._emettre("client-a", ["mcp:lecture"], None)
    second = await _rafraichir(fournisseur, premier.refresh_token)
    brut = _brut(tmp_path)
    for jeton in (
        premier.access_token,
        premier.refresh_token,
        second.access_token,
        second.refresh_token,
    ):
        assert jeton not in brut
    etat = json.loads(brut)
    # L'ancien jeton d'acces vit jusqu'a son expiration (comportement inchange).
    assert set(etat["acces"]) == {_sha(premier.access_token), _sha(second.access_token)}
    assert set(etat["rafraichissements"]) == {_sha(second.refresh_token)}
    assert all("jeton" not in d for d in etat["acces"].values())
    assert all("jeton" not in d for d in etat["rafraichissements"].values())


async def test_lecture_rotation_revocation_rejeu_sur_etat_hache(fournisseur):
    premier = fournisseur._emettre("client-a", ["mcp:lecture"], None)
    assert (await fournisseur.load_access_token(premier.access_token)) is not None
    second = await _rafraichir(fournisseur, premier.refresh_token)
    assert second is not None
    # Rejeu du refresh consomme -> famille revoquee.
    assert await _rafraichir(fournisseur, premier.refresh_token) is None
    assert await fournisseur.load_access_token(second.access_token) is None
    # Nouvelle famille puis /revoke.
    autre = fournisseur._emettre("client-a", ["mcp:lecture"], None)
    charge = await fournisseur.load_refresh_token(_client(), autre.refresh_token)
    await fournisseur.revoke_token(charge)
    assert await fournisseur.load_access_token(autre.access_token) is None
    assert fournisseur.magasin.lire_rafraichissement(autre.refresh_token) is None


def test_l_empreinte_ne_vaut_pas_jeton(fournisseur):
    """Voler la cle d'etat ne suffit pas : presenter l'empreinte ne donne rien."""
    paire = fournisseur._emettre("client-a", ["mcp:lecture"], None)
    assert fournisseur.magasin.lire_acces(_sha(paire.access_token)) is None
    assert fournisseur.magasin.lire_rafraichissement(_sha(paire.refresh_token)) is None


# --- Migration d'un etat herite en clair --------------------------------------------------
def _etat_herite(tmp_path: Path) -> dict[str, str]:
    """etat.json au format d'avant #14 : cles = jetons, champ `jeton` en clair."""
    maintenant = int(time.time())
    jetons = {"acces": "A" * 43, "refresh": "R" * 43, "herite": "H" * 43}
    etat = {
        "demandes": {},
        "codes": {},
        "consommes": {},
        "acces": {
            jetons["acces"]: {
                "jeton": jetons["acces"], "client_id": "client-a", "scopes": ["mcp:lecture"],
                "resource": None, "famille": "fam-1", "expire_a": maintenant + 3600,
            },
        },
        "rafraichissements": {
            jetons["refresh"]: {
                "jeton": jetons["refresh"], "client_id": "client-a", "scopes": ["mcp:lecture"],
                "famille": "fam-1", "famille_expire_a": maintenant + 80 * JOUR,
                "emis_a": maintenant, "expire_a": maintenant + 30 * JOUR,
            },
            # Jeton d'avant #11 (sans famille, 10 ans) et sans champ `jeton`.
            jetons["herite"]: {
                "client_id": "client-a", "scopes": ["mcp:lecture"],
                "expire_a": maintenant + 3650 * JOUR,
            },
        },
    }
    (tmp_path / "etat.json").write_text(json.dumps(etat), encoding="utf-8")
    return jetons


async def test_etat_herite_lisible_sans_reconsentement(fournisseur, tmp_path):
    jetons = _etat_herite(tmp_path)
    acces = await fournisseur.load_access_token(jetons["acces"])
    assert acces is not None
    assert acces.client_id == "client-a"
    assert fournisseur.magasin.lire_rafraichissement(jetons["refresh"])["famille"] == "fam-1"
    # Lecture seule : pas de reecriture tant qu'aucune ecriture d'etat n'a eu lieu.
    assert jetons["acces"] in _brut(tmp_path)


async def test_premiere_ecriture_reecrit_l_etat_hache(fournisseur, tmp_path):
    jetons = _etat_herite(tmp_path)
    suite = await _rafraichir(fournisseur, jetons["refresh"])
    assert suite is not None
    brut = _brut(tmp_path)
    for jeton in (*jetons.values(), suite.access_token, suite.refresh_token):
        assert jeton not in brut
    # Famille conservee a travers la migration + rotation.
    assert fournisseur.magasin.lire_rafraichissement(suite.refresh_token)["famille"] == "fam-1"
    # Les autres jetons herites restent valides, retrouves par empreinte.
    assert await fournisseur.load_access_token(jetons["acces"]) is not None
    herite = fournisseur.magasin.lire_rafraichissement(jetons["herite"])
    assert herite is not None
    # Borne #11 toujours appliquee (10 ans -> duree glissante).
    assert herite["expire_a"] <= time.time() + 30 * JOUR + 5
    # Rejeu du refresh migre puis consomme : detecte, famille revoquee.
    assert await _rafraichir(fournisseur, jetons["refresh"]) is None
    assert await fournisseur.load_access_token(jetons["acces"]) is None


def test_migrer_etat_idempotent(fournisseur, tmp_path):
    jetons = _etat_herite(tmp_path)
    assert fournisseur.magasin.migrer_etat() == 3
    premier = _brut(tmp_path)
    assert all(jeton not in premier for jeton in jetons.values())
    assert fournisseur.magasin.migrer_etat() == 0
    etat = json.loads(_brut(tmp_path))
    assert set(etat["rafraichissements"]) == {_sha(jetons["refresh"]), _sha(jetons["herite"])}


def test_script_admin_migrer(fournisseur, tmp_path, monkeypatch, capsys):
    jetons = _etat_herite(tmp_path)
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path))
    monkeypatch.setenv("VAULT_MCP_OAUTH_CLIENTS", str(tmp_path / "clients.json"))
    assert oauth_revoquer.main(["--migrer"]) == 0
    sortie = capsys.readouterr().out
    assert "jetons migres: 3" in sortie
    assert all(jeton not in sortie for jeton in jetons.values())
    assert all(jeton not in _brut(tmp_path) for jeton in jetons.values())
