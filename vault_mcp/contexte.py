"""Second serveur MCP `vault-context` : recuperation de contexte en LECTURE SEULE.

Destine aux agents qui n'ont besoin que de retrouver et lire (Claude Code, Codex,
agents planifies) : aucune ecriture, aucun outil ConvIA/Wiki, aucun reindex, aucun
OAuth ni consentement. La surface est figee par `OUTILS_LECTURE` et verifiee par test.

Separation volontaire du serveur principal :
  - processus et unite systemd distincts (`vault-context-mcp.service`) : une panne
    ou une surcharge de l'un n'atteint pas l'autre ;
  - jeton Bearer DEDIE (`VAULT_CONTEXT_TOKEN`), refuse s'il vaut celui du principal :
    fuiter ce jeton ne donne jamais l'ecriture ;
  - ecoute sur 127.0.0.1 uniquement. Toute exposition publique est un changement
    externe explicite (voir docs/mcp-contexte.md), jamais un defaut.

Memes sources de verite que le principal : miroir Drive + index + voie fraiche.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from vault_mcp import __version__, frais
from vault_mcp.auth import Application, Receive, Scope, Send, _entete, token_valide
from vault_mcp.index import Index, repertoire_index
from vault_mcp.mirror_store import MirrorStore
from vault_mcp.safety import CheminInvalideError, normalize_path
from vault_mcp.secrets import masquer
from vault_mcp.store import StoreError

CHEMIN_PUBLIC = "/mcp"
OUTILS_LECTURE = frozenset(
    {"list_notes", "read_note", "search_notes", "search_vault", "get_graph_context", "context_status"}
)


def _jeton() -> str:
    jeton = os.environ.get("VAULT_CONTEXT_TOKEN", "")
    if len(jeton) < 32:
        raise RuntimeError("VAULT_CONTEXT_TOKEN absent ou trop court : 32 caracteres minimum")
    if jeton == os.environ.get("VAULT_MCP_TOKEN", ""):
        raise RuntimeError("VAULT_CONTEXT_TOKEN doit differer du jeton du serveur principal")
    return jeton


mcp = MCPServer(
    "vault-context",
    version=__version__,
    instructions=(
        "Read-only context retrieval over the Obsidian vault. search_vault first, "
        "then read_note on the returned paths. No write tool exists on this server."
    ),
)
_store = MirrorStore.depuis_env()
_index = Index()


@mcp.tool()
def list_notes(prefix: str = "", limit: int = 500) -> list[str]:
    """List note paths in the vault. Optional folder `prefix` filter. Read-only."""
    try:
        return _store.lister_chemins(prefix=prefix, limit=limit)
    except (StoreError, CheminInvalideError) as exc:
        return [f"ERREUR: {exc}"]


@mcp.tool()
def read_note(path: str, offset: int = 0, limit: int = 200000) -> str:
    """Markdown content of a note by vault-relative path. Page with offset/limit (chars)."""
    try:
        contenu = _store.lire_note(path)
    except (StoreError, CheminInvalideError) as exc:
        return f"ERREUR: {exc}"
    if contenu is None:
        return f"NOT FOUND: {path}"
    debut = max(0, offset)
    return masquer(contenu[debut:] if limit <= 0 else contenu[debut : debut + limit])


@mcp.tool()
def search_notes(query: str, limit: int = 50) -> list[dict[str, str]]:
    """Exact full-text search across note contents (case-insensitive). [{path, snippet}]."""
    try:
        resultats = _store.rechercher(query, limit=limit)
    except (StoreError, CheminInvalideError) as exc:
        return [{"path": "", "snippet": f"ERREUR: {exc}"}]
    return [{"path": r.path, "snippet": masquer(r.snippet)} for r in resultats]


@mcp.tool()
def search_vault(query: str, limit: int = 10, mode: str = "hybride") -> list[dict[str, object]]:
    """Ranked retrieval: "hybride" (default), "vecteur" or "lexical".

    `origine: "frais"` marks a note found before its embedding (e.g. a ConvIA
    that just arrived): its content is current, its semantic rank is lexical.
    """
    if not query.strip():
        return [{"chemin": "", "apercu": "ERREUR: requete vide"}]
    moteurs = {
        "hybride": _index.recherche_hybride,
        "vecteur": _index.recherche_vectorielle,
        "lexical": _index.recherche_lexicale,
    }
    moteur = moteurs.get(mode)
    if moteur is None:
        return [{"chemin": "", "apercu": f"ERREUR: mode inconnu {mode!r}"}]
    borne = min(max(limit, 1), 100)
    if not _index.disponible:
        resultats = frais.rechercher(query, borne) if mode != "vecteur" else []
    else:
        resultats = moteur(query, borne)
    return [
        {
            "chemin": r.chemin,
            "titre": r.titre,
            "apercu": masquer(r.apercu),
            "score": round(float(r.score), 4),
            "origine": r.origine,
        }
        for r in resultats
    ]


@mcp.tool()
def get_graph_context(path: str, limit: int = 50) -> dict[str, object]:
    """Backlinks, outgoing links and unresolved links of a note."""
    try:
        chemin = normalize_path(path)
    except CheminInvalideError as exc:
        return {"erreur": f"ERREUR: {exc}"}
    if not _index.disponible:
        return {"erreur": "ERREUR: index absent"}
    return _index.contexte_graphe(chemin.relatif, min(max(limit, 1), 500))


@mcp.tool()
def context_status() -> dict[str, object]:
    """Freshness of the retrieval surface: index and fresh (pre-embedding) lane."""
    etat: dict[str, object] = {"lecture_seule": True, "index_disponible": _index.disponible}
    try:
        import time

        etat["index_age_s"] = round(
            time.time() - (repertoire_index() / "meta.json").stat().st_mtime, 1
        )
    except OSError:
        etat["index_age_s"] = None
    etat.update(frais.statistiques())
    return etat


class BearerObligatoire:
    """Refuse toute requete HTTP sans le jeton dedie. Aucune autre voie d'entree."""

    def __init__(self, app: Application, jeton: str) -> None:
        self._app = app
        self._jeton = jeton

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not token_valide(
            _entete(scope, b"authorization"), self._jeton
        ):
            debut: MutableMapping[str, Any] = {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
            await send(debut)
            await send({"type": "http.response.body", "body": b'{"error":"invalid_token"}'})
            return
        await self._app(scope, receive, send)


def construire_application() -> Application:
    return BearerObligatoire(
        mcp.streamable_http_app(
            streamable_http_path=CHEMIN_PUBLIC,
            # Le Bearer est la seule garde ; un futur proxy public presentera son
            # propre Host, que la protection anti-rebind rejetterait en 421.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        _jeton(),
    )


def main() -> None:
    uvicorn.run(
        construire_application(),
        host="127.0.0.1",
        port=int(os.environ.get("VAULT_CONTEXT_PORT", "8810")),
        log_level="info",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
