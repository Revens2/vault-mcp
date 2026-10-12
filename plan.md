# plan.md — RAG retrieval refactor (branche feat/rag-eval-retrieval)

## Objectif
Améliorer mesurablement `search_vault` (qualité top-k, exact recall, current-state) sans nouveau service,
sans envoi externe, sans casser le format d'index ni la chaîne ConvIA/Wiki.

## Stratégie
1. Harness d'eval offline (`scripts/eval_retrieval.py`) + jeu de requêtes étiquetées (`tests/eval/golden.jsonl`),
   exécuté sur le corpus réel (miroir vps-etude, lecture seule), métriques : hit@1/5/10, MRR, exact recall,
   stale@5 (note historique devant la canonique), p50/p95 latence, RAM/disque.
2. Baseline = code main 0a44ddf.
3. Ablations une par une :
   - A1 lexical BM25 sur texte complet des fragments (vs présence de mots sur aperçu 240c) ;
   - A2 agrégation note-level : moyenne vs max vs top-2 moyen / fragment-level + dédup ;
   - A3 prior d'autorité/date (current-state > docs > ConvIA/historique) ;
   - A4 reranker local (cross-encoder ONNX) sur top-50 — seulement si budget latence OK.
4. N'intégrer que ce qui bat la baseline sans régression de latence inacceptable.
5. Déploiement prod seulement avec backup/rollback, sinon branche poussée + rapport.

## Critères d'acceptation
- Hit@5 et MRR ≥ baseline sur toutes familles, gain net global.
- Exact recall strictement meilleur.
- p95 search ≤ 2× baseline et < 1.5 s sur vps-etude.
- Tests existants verts.
