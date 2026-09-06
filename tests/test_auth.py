"""Tests de `vault_mcp.auth`.

Le middleware est le seul controle d'acces devant le vault : chaque cas de rejet ici
correspond a une facon de s'en passer.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest

from vault_mcp.auth import Authentification, chemin_secret_valide, token_valide

TOKEN = "t" * 64
SECRET = "s" * 32
CHEMIN_SECRET = f"/mcp/{SECRET}"


async def _application(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
    """Application factice : renvoie le chemin qu'elle a effectivement recu."""
    corps = scope["path"].encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(corps)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": corps})


def _client() -> httpx.AsyncClient:
    app = Authentification(
        _application, token=TOKEN, chemin_secret=CHEMIN_SECRET, chemin_public="/mcp"
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    ("entete", "attendu"),
    [
        (f"Bearer {TOKEN}", True),
        (f"bearer {TOKEN}", True),
        (f"Bearer  {TOKEN} ", True),
        (f"Bearer {TOKEN}x", False),
        (f"Bearer {TOKEN[:-1]}", False),
        (f"Basic {TOKEN}", False),
        (TOKEN, False),
        ("Bearer", False),
        ("", False),
        (None, False),
    ],
)
def test_token_valide(entete: str | None, attendu: bool) -> None:
    assert token_valide(entete, TOKEN) is attendu


def test_token_jamais_valide_si_attendu_vide() -> None:
    # Un token attendu vide signifierait "tout le monde passe" : refus explicite.
    assert token_valide("Bearer ", "") is False


@pytest.mark.parametrize(
    ("chemin", "attendu"),
    [
        (CHEMIN_SECRET, True),
        (CHEMIN_SECRET + "/", True),
        (CHEMIN_SECRET + "/sous", True),
        ("/mcp/" + "s" * 31, False),
        ("/mcp/" + "x" * 32, False),
        ("/mcp", False),
        ("/", False),
    ],
)
def test_chemin_secret_valide(chemin: str, attendu: bool) -> None:
    assert chemin_secret_valide(chemin, CHEMIN_SECRET) is attendu


async def test_sans_entete_laisse_passer_vers_le_sdk() -> None:
    # Le middleware ne refuse plus : c est le SDK qui repond 401, avec le
    # `WWW-Authenticate: Bearer resource_metadata="..."` necessaire a la decouverte.
    # Un refus ici rendait `/.well-known/*` et `/authorize` inaccessibles.
    async with _client() as client:
        reponse = await client.get("/mcp")
    assert reponse.status_code == 200
    assert reponse.text == "/mcp"


async def test_token_inconnu_laisse_passer_vers_le_sdk() -> None:
    # Un jeton OAuth valide est inconnu du middleware : le rejeter empecherait
    # toute authentification OAuth d aboutir.
    async with _client() as client:
        reponse = await client.get("/mcp", headers={"Authorization": "Bearer autre"})
    assert reponse.status_code == 200


async def test_token_bon_passe() -> None:
    async with _client() as client:
        reponse = await client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert reponse.status_code == 200
    assert reponse.text == "/mcp"


async def test_chemin_secret_passe_et_est_reecrit() -> None:
    async with _client() as client:
        reponse = await client.get(CHEMIN_SECRET)
    assert reponse.status_code == 200
    # L'application n'est montee que sur /mcp : sans reecriture elle renverrait 404.
    assert reponse.text == "/mcp"


async def test_mauvais_secret_dans_lurl_nest_pas_reecrit() -> None:
    # Le chemin reste tel quel : aucune route ne lui correspond en vrai, le SDK
    # repondra 404. Ce qui compte est qu il ne soit pas traite comme authentifie.
    async with _client() as client:
        reponse = await client.get("/mcp/" + "x" * 32)
    assert reponse.text == "/mcp/" + "x" * 32


async def test_la_reponse_ne_divulgue_pas_le_secret_attendu() -> None:
    async with _client() as client:
        reponse = await client.get("/mcp/" + "x" * 32)
    assert SECRET not in reponse.text
    assert TOKEN not in reponse.text


async def test_le_secret_durl_injecte_len_tete_authorization() -> None:
    """Le chemin secret doit emprunter le meme chemin que les autres : reecrit **et**
    porteur du jeton, pour que l authentification du SDK le reconnaisse."""
    vus: list[str | None] = []

    async def application(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        entete = None
        for cle, valeur in scope.get("headers", []):
            if cle.lower() == b"authorization":
                entete = valeur.decode()
        vus.append(entete)
        corps = scope["path"].encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(corps)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": corps})

    app = Authentification(
        application, token=TOKEN, chemin_secret=CHEMIN_SECRET, chemin_public="/mcp"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        reponse = await client.get(CHEMIN_SECRET)

    assert reponse.text == "/mcp"
    assert vus == [f"Bearer {TOKEN}"]


async def test_len_tete_injecte_remplace_celui_fourni() -> None:
    """Un client qui presenterait a la fois le secret d URL et un autre jeton ne doit
    pas pouvoir faire passer ce dernier : le secret gagne, sans ambiguite."""
    vus: list[str | None] = []

    async def application(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        for cle, valeur in scope.get("headers", []):
            if cle.lower() == b"authorization":
                vus.append(valeur.decode())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = Authentification(
        application, token=TOKEN, chemin_secret=CHEMIN_SECRET, chemin_public="/mcp"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(CHEMIN_SECRET, headers={"Authorization": "Bearer autre-chose"})

    assert vus == [f"Bearer {TOKEN}"]
