# vault-mcp

Serveur MCP unifié du vault Obsidian : lecture, recherche sémantique (hybride), graphe
et écriture soumise à consentement. Pensé pour exposer un vault de connaissances à des
agents IA (Claude, Codex, ChatGPT…) derrière une passerelle d'authentification.

Version courante : 2.0.0 (`pyproject.toml`).

## Fonctionnalités

- Lecture / recherche **lexicale + vectorielle** (moteur hybride par fusion à rang réciproque).
- Graphe de notes (backlinks, liens sortants, liens non résolus).
- Écriture **par lots d'intentions** : consentement, détection de conflits, spool.
- Serveur MCP streamable-http + serveur HTTP séparé (`server_http.py` historique, non inclus).

## Structure

```
vault_mcp/          paquet principal (serveur, store, index, chunk, spool, auth, oauth…)
scripts/            utilitaires d'exploitation (reindex, parité)
tests/              suites pytest (+ jeu de requêtes de référence queries.json)
pyproject.toml      métadonnées + config ruff (bandit activé) et mypy strict
```

## Exécution

```bash
pip install -r requirements.txt
python -m vault_mcp.server          # service MCP (variables via l'environnement)
pytest                              # tests
```

Variables d'environnement : voir `.env.example` (aucune valeur réelle n'est versionnée —
le fichier d'env du service vit hors Git, en 0600).

## Déploiement

Service systemd dédié (`User=<compte de service>`, `ProtectSystem=strict`,
`PrivateTmp=yes`…), alimenté par un miroir CouchDB du vault ; l'écriture remonte par
spool vers l'instance de référence. Voir `scripts/reindex.py` (reconstruction de l'index)
et `scripts/parite.py` (vérification de parité miroir ↔ source).
