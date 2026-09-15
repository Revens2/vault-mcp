"""Isolation des tests vis-a-vis du systeme de fichiers de production.

`vault_mcp.server` construit a l IMPORT le magasin miroir (`VAULT_MCP_VAULT`,
defaut `/srv/vault-mirror`) et l index (`VAULT_MCP_INDEX`, defaut
`/opt/vault-mcp/index`). Un test qui ne posait pas ces variables retombait donc
silencieusement sur le vrai miroir de production : vert sur le VPS, rouge
partout ailleurs. Constate a la premiere CI qui lancait la suite complete
(2026-09-11) : 170 erreurs « miroir du vault introuvable ».

Par defaut, chaque test voit desormais un miroir et un index vides et jetables.
Un test qui a besoin des siens les pose lui-meme par monkeypatch, ce qui
l emporte sur ce defaut ; une variable deja presente dans l environnement du
lanceur est respectee.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isoler_des_chemins_de_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    racine = tmp_path_factory.mktemp("hors-production")
    for variable, nom in (("VAULT_MCP_VAULT", "miroir"), ("VAULT_MCP_INDEX", "index"),
                          ("VAULT_MCP_DIRTY", "dirty")):
        if variable not in os.environ:
            chemin = racine / nom
            chemin.mkdir()
            monkeypatch.setenv(variable, str(chemin))
    # Bases d etat : jamais celles de production. `convia_mcp.status()` lisait par
    # defaut /var/lib/vault-mcp/wiki_jobs.db (lecture seule sur le VPS, absente en
    # CI) : un test pouvait donc dependre de l etat reel de la file Wiki.
    for variable, nom in (("VAULT_MCP_TELEMETRY_DB", "telemetry.db"),
                          ("WIKI_JOBS_DB", "wiki_jobs.db"),
                          ("CONVIA_QUEUE_DB", "convia.db"),
                          ("VAULT_MCP_FRAIS", "frais.sqlite")):
        if variable not in os.environ:
            monkeypatch.setenv(variable, str(racine / nom))
