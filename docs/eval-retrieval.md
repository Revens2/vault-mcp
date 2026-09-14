# Évaluation retrieval — 2026-09-14

Banc : `scripts/eval_retrieval.py`, jeu `tests/eval/golden.jsonl` (40 requêtes : 10 current-state,
10 identifiants exacts, 13 sémantiques, 7 historiques ; 5 portent des notes périmées à ne pas
remonter). Index réel de vps-etude (génération 1789419237055115284, 244 605 fragments), copie privée,
miroir `/srv/vault-mirror` en lecture seule, `nice 19`, 1 thread ONNX, VPS chargé (loadavg ~6/4).

## Constats
- 92 % des fragments sont des conversations `raw/` : sans prior, un transcript noie la fiche canonique.
- Le lexical historique ne voit que `titre + aperçu(240) + chemin` et coûte ~2 s (balayage Python).
- `.trash-wiki-publish/` n'était pas exclu : 2 065 fragments de corbeille remontaient.
- 49 k fragments ConvIA ont changé depuis l'indexation : ils retombent sur l'aperçu (biais défavorable au BM25).

## Résultats (k=10)
| variante | MRR | hit@1 | hit@5 | stale@5 | p50 ms | p95 ms | current | exact | sém. | hist. |
|---|---|---|---|---|---|---|---|---|---|---|
| base_vecteur (moyenne) | 0.249 | 0.15 | 0.375 | 0 | 447 | 765 | 3/10 | 3/10 | 5/13 | 4/7 |
| base_lexical | 0.461 | 0.35 | 0.65 | 3 | 2049 | 2780 | 5/10 | 5/10 | 10/13 | 6/7 |
| **base_hybride (prod actuelle)** | **0.440** | 0.325 | 0.575 | 1 | 2757 | 3684 | 5/10 | 5/10 | 8/13 | 5/7 |
| bm25 plein texte seul | 0.553 | 0.475 | 0.625 | 3 | 113 | 172 | 4/10 | 5/10 | 10/13 | 6/7 |
| vec mix (0.5 max + 0.5 moy) | 0.468 | 0.40 | 0.525 | 1 | 275 | 388 | 5/10 | 3/10 | 9/13 | 4/7 |
| hybride vec-mix + BM25 | 0.571 | 0.45 | 0.70 | 2 | 366 | 493 | 5/10 | 5/10 | 11/13 | 7/7 |
| + exclusion corbeille | 0.558 | 0.45 | 0.725 | 1 | 358 | 538 | 5/10 | 5/10 | 12/13 | 7/7 |
| + prior autorité 0.5 | 0.654 | 0.55 | 0.80 | 1 | 344 | 560 | 7/10 | 7/10 | 12/13 | 6/7 |
| + prior autorité 1.0 | 0.693 | 0.60 | 0.80 | 1 | 426 | 763 | 7/10 | 8/10 | 11/13 | 6/7 |
| **+ prior 2.0 + routeur historique (retenu)** | **0.713** | 0.625 | 0.825 | 1 | 338 | 448 | 7/10 | 9/10 | 11/13 | 6/7 |

Ablations écartées : agrégation max seule / top-2 (inférieures au mix), prior sans routeur (≈ égal).

## Retenu
1. BM25 plein texte (`vault_mcp/lexical.py`), reconstruit depuis le miroir en arrière-plan
   (~80 s CPU, au plus toutes les 15 min, ancien lexical servi tant qu'il n'est pas prêt).
2. Agrégation vectorielle mix dans l'hybride (`search_vault` mode `vecteur` inchangé).
3. Prior d'autorité par chemin (`vault_mcp/autorite.py`), désactivé pour les requêtes historiques ;
   `VAULT_MCP_POIDS_AUTORITE=0` = rollback à chaud du prior.
4. Exclusion `.trash-wiki-publish/` (effective à la requête, et à l'index au prochain full).

## Coût
- Mémoire : +~1 Go RSS estimé pour le BM25 (postings int32/float32) ; hôte : 13 Gi disponibles, pas de MemoryMax.
- Latence hybride : p95 3,7 s → 0,45 s (le lexical Python était le goulot).

## Limites
- 40 requêtes écrites en même temps que les variantes : risque de sur-ajustement du poids ; le gain
  du BM25 et du prior est large (MRR +0,27), le choix 1.0 vs 2.0 ne l'est pas.
- Historique : 7/7 → 6/7 avec prior ; le routeur regex ne rattrape pas toutes les formulations.
- Non retenus faute de besoin mesuré : reranker cross-encoder, Qdrant, Graphiti, GraphRAG.
