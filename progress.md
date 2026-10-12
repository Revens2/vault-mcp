# progress.md — snapshot (mission frais + vault-context)

Branche `feat/frais-contexte-mcp` poussée : 9d67c39, 1cc07b7 (base c2fe134). Pas de PR, pas de merge.

## Étape courante
TERMINÉ 2026-09-15 ~09:10 UTC. Reste : Scheduled Tasks ChatGPT (refonte fournie, non appliquée), route publique vault-context (hostname externe à créer), wiki sync VPS non fait.

## Fait
- Tentative 1 (14/09) : frais + vault-context-mcp déjà déployés ; bug live : 1710/2352 notes fraîches déjà indexées (MRR servi 0.691).
- Déployé 15/09 : frais.py (exclusion des notes indexées) + index.py (POIDS_FRAIS défaut 1.0). Restart vault-mcp, vault-context-mcp.
- Backup/rollback : /var/backups/vault-mcp-frais-fix-20260915T084647Z/rollback.sh (SHA256SUMS.before/after).
- Tests VPS (staging /tmp/vm-stage, venv prod) : rc=0.
- Sonde servie finale : MRR 0.743, hit@5 0.875, exact 10/10, p95 716 ms (avant : 0.691 / 0.80 / 786 ms).
- Poids 2.0 mesuré : MRR 0.62 → abandonné.
- Preuve ConvIA : 3 ConvIA non indexées (5 min) trouvées rang 0 (lexical, origine frais) et rang 1 (hybride), sans consumer LLM.
- vault-context : 401 sans jeton, 6 outils lecture seule, 127.0.0.1:8810/mcp.

## Décisions
- Rollback à chaud : VAULT_MCP_POIDS_FRAIS=0.
- Pas de resync nécessaire du MCP général (aucun outil ajouté/retiré, champs additifs).

## Points connus
- Pendant le warm-up BM25 (<15 min après restart), les résultats frais sont étiquetés "hybride" (fallback RRF) : rang correct, étiquette perdue.
