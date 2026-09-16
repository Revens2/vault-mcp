# vault-mcp-rs — adaptateur Rust devant l'upstream Python fige

Miroir de `vault_mcp/server.py` (38 outils) : validation Bearer edge +
relais transparent vers `:8787` avec `Authorization` client conserve (meme
emetteur, meme magasin, loopback — l'upstream reste l'enforceur des portees
et de l'ecriture). Metier conserve : vault Obsidian + OAuth Python.

## Contrat conserve

`initialize`/sessions avec client passthrough, `tools/list` VERBATIM (38 outils, aucun
filtrage — l'upstream annonce), `tools/call` connus relayes (l'upstream gate
l'ecriture en texte `ERREUR: ...`), inconnu refuse en local (-32000),
`resources/list` + `prompts/list` verbatim, rewrite version 2026-07-28 →
2025-11-25. Clients sans session (ex. connecteur ChatGPT) : pont stateless
(session upstream poolée par credential, reponse sans `mcp-session-id`,
comme github/orch) — voir `src/stateless.rs`.

## Differences assumees (contrat outils intact)

401 au format framework (URL PRM exacte presente), PRM/AS au format framework
(`resource_name` present, scopes lecture+ecriture annonces, `none` seul —
le Python annonce PRM lecture seule + `header`, AS `[post, basic]` ; DCR
valide les deux portees des deux cotes), ajout `/health` + `/ready` (le
Python repond 404), outil inconnu -32000 au lieu de l'erreur SDK (edge only),
requetes sans session servies via pont stateless (le Python repond 400 :
reponse 200 sans `mcp-session-id`, alignement github/orch).

## Environnement (`VAULT_MCP_RS_*`)

| Variable | Defaut | Role |
|---|---|---|
| `VAULT_MCP_RS_ISSUER` | issuer ngrok prod | HTTPS requis |
| `VAULT_MCP_RS_UPSTREAM` | `http://127.0.0.1:8787` | loopback requis |
| `VAULT_MCP_RS_PORT` | `18987` (canary) | `8787` a la bascule (GATEE) |
| `VAULT_MCP_RS_TOKEN` / `_TOKEN_FILE` | `/opt/vault-mcp-rs/.mcp_token` | Bearer dedie >= 32, fail-closed |
| `VAULT_MCP_RS_TOKEN_SCOPES` | lecture+ecriture | quoté dans l'unit (lecon lot 2) |
| `VAULT_MCP_RS_CONSENT_HASH` | vide (= refuse) | PBKDF2 consentement |

## Preuves lot vault (adaptateur)

- `cargo fmt --check` 0, `cargo clippy --all-targets -- -D warnings` 0.
- `cargo test` : 38 outils connus, inconnu refuse, 401, health/PRM,
  retransmission `Authorization` via mock, reponse verbatim, pont stateless
  via mock stateful (200 sans session, 1 seul `initialize`, directs intacts).
- Canary VPS `:18987` + diff contrat zero vs `:8787` : voir `progress.md`.
- Bascule `:8787` GATEE (OAuth JWT meme famille + rotation eventuelle =
  exploitant) : prod Python intacte. `vault-context-mcp` = residu (code hors
  clone, a synchroniser avant transcription).
