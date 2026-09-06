"""Authentification du transport HTTP.

Deux voies coexistent volontairement, le temps que claude.ai bascule :

1. **`Authorization: Bearer <token>`** — la voie normale, pour Claude Code, AGY et tout
   client capable d'envoyer un en-tete.
2. **Secret dans le chemin d'URL** — la voie historique, seule possible pour un connecteur
   claude.ai qui ne sait pas envoyer d'en-tete personnalise. Le middleware reecrit alors
   le chemin vers le chemin public avant de passer la main a l'application.

La comparaison se fait par `hmac.compare_digest` : un `==` sur des chaines s'arrete au
premier octet different, ce qui laisse mesurer le secret octet par octet.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

# Types ASGI tels que les attendent Starlette et httpx.ASGITransport : des
# MutableMapping, pas des dict. Un alias en `dict` fait echouer la verification
# de type a l'appel, sans que rien ne soit faux a l'execution.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
Application = Callable[[Scope, Receive, Send], Awaitable[None]]


def token_valide(entete: str | None, attendu: str) -> bool:
    """Vrai si l'en-tete `Authorization` porte le bon jeton Bearer."""
    if not entete or not attendu:
        return False
    parties = entete.split(None, 1)
    if len(parties) != 2 or parties[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parties[1].strip(), attendu)


def chemin_secret_valide(chemin: str, chemin_secret: str) -> bool:
    """Vrai si le chemin demande correspond au chemin secret (ou a un sous-chemin)."""
    if not chemin_secret:
        return False
    if len(chemin) < len(chemin_secret):
        return False
    # compare_digest sur le prefixe : meme raisonnement que pour le token.
    if not hmac.compare_digest(chemin[: len(chemin_secret)], chemin_secret):
        return False
    reste = chemin[len(chemin_secret) :]
    return reste in {"", "/"} or reste.startswith("/")


class Authentification:
    """Middleware ASGI : Bearer, ou secret d'URL reecrit vers le chemin public."""

    def __init__(
        self,
        app: Application,
        *,
        token: str,
        chemin_secret: str,
        chemin_public: str = "/mcp",
    ) -> None:
        self._app = app
        self._token = token
        self._chemin_secret = chemin_secret
        self._chemin_public = chemin_public

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        chemin: str = scope.get("path", "")

        if chemin_secret_valide(chemin, self._chemin_secret):
            # Reecriture : l'application n'est montee que sur le chemin public, elle
            # n'a pas a connaitre le secret.
            suffixe = chemin[len(self._chemin_secret) :]
            nouveau = self._chemin_public + suffixe
            # L en-tete est injecte ici : la requete arrive au SDK comme n importe quelle
            # requete authentifiee, au lieu d emprunter un chemin d exception.
            entetes = [
                (cle, valeur)
                for cle, valeur in scope.get("headers", [])
                if cle.lower() != b"authorization"
            ]
            entetes.append((b"authorization", f"Bearer {self._token}".encode()))
            scope = {
                **scope,
                "path": nouveau,
                "raw_path": nouveau.encode(),
                "headers": entetes,
            }
            await self._app(scope, receive, send)
            return

        # Tout le reste passe : c est le SDK qui authentifie, et lui seul sait produire
        # le 401 avec `WWW-Authenticate: Bearer resource_metadata="..."` dont le client a
        # besoin pour decouvrir le serveur d autorisation.
        await self._app(scope, receive, send)


def _entete(scope: Scope, nom: bytes) -> str | None:
    entetes: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
    for cle, valeur in entetes:
        if cle.lower() == nom:
            return valeur.decode("latin-1")
    return None
