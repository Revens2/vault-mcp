# progress — vault-mcp

## Revue convia-analysis-surface (commit 45d54ad, parent 621ff22) — 2026-09-09

### Périmètre
6 fichiers, +1614 / -91 (git diff 621ff22..45d54ad --stat).
- Nouveau : vault_mcp/wiki_jobs.py (958 lignes) — file Wiki SQLite WAL : claim atomique lease_id+fencing_token+expiration, read confiné, submit idempotent validé+spoolé, release borné, merge_pending zéro-LLM, quarantaine, status.
- Modifiés : vault_mcp/convia_mcp.py (+144/-63, wrappers wiki_* + ingest_status/status deprecated + ingest_start stub), vault_mcp/server.py (+113/-15, 5 outils MCP wiki_ingest_claim/read/submit/release/merge_pending + convia_scan/wiki_ingest_start redéfinis + DATA NOT INSTRUCTIONS sur read).
- Tests : tests/test_wiki_jobs.py (437 lignes, 28 tests), tests/test_convia_mcp.py (27 lignes retouchées, ingest_start deprecated).
- Hygiène : .claudeignore (11 lignes).
Base : main. Prod SDK 2.2.0 MCPServer hors diff (greffe sur place, 66 passed annoncés dont import SDK PASS) — non revue ici.

### Blast radius
- code-review-graph : build 43 fichiers / 697 noeuds / 5129 arêtes ; update --base main --brief : 13 fichiers changés, 161 fonctions/classes, 0 flux affecté, 98 test gaps, score risque 0.85.
- impact --depth 2 sur wiki_jobs/convia_mcp/server : 0 noeud (requête --files en un seul token comma-join, inopérante ; nouveau module sans appelants historiques). Repli manuel grep+lecture intégrale wiki_jobs.py (958 lignes), convia_mcp.py:260-421, server.py:1170-1306, tests 437 lignes.
- Graphe d'appels réel : wiki_jobs.claim/read_job/submit/release/merge_pending/status/sync_source/sync_directory/validate_extraction <- convia_mcp.wiki_claim/wiki_read/wiki_submit/wiki_release/wiki_merge_pending + ingest_status/ingest_start <- server.py wiki_ingest_claim/read/submit/release/merge_pending + wiki_ingest_status/start + convia_scan. Flux : unique tâche horaire ChatGPT claim->read->submit->merge_pending. convia_queue/projection/CAS hash non touchés. ingest_backlog (convia_mcp.py:337-356, appel INGEST_BIN --status) devient code mort (server.py convia_scan ne l'appelle plus) — conservé, inerte.

### Risques
- **Bloquant** — aucun (pas de perte de données silencieuse, pas de secret, pas d'écrasement silencieux, BEGIN IMMEDIATE partout, double-claim/double-merge testés).
- **Majeur**
  - Leases expirés non récupérables par claim -> starvation (wiki_jobs.py:328-335). claim ne sélectionne que status IN (pending,deferred) ; un job leased dont expires_at passe ne redevient pending que dans read_job (390-395) ou submit (671-677), jamais appelés sans lease valide par un tiers. Crash client entre claim et read = job coincé leased indéfiniment, invisible au status pending. Pas de reaper. Repro : claim lease 60s, abandon, attendre, claim -> leased=0 alors que le job existe. Fix : inclure (status=leased AND expires_at<now) dans claim OU tâche de réclamation.
  - renew ressuscite un bail expiré sans contrôle (wiki_jobs.py:771-780). release(action=renew) ne vérifie jamais expires_at, prolonge de DEFAULT_LEASE_S (3600, pas le lease_seconds d'origine) jusqu'à MAX_RENEWS=3. Combiné au précédent : le détenteur expiré peut garder le job pendant que les autres attendent. Fix : refuser renew si expires_at<now (lease-expired, remettre pending).
  - Extraction non bornée -> remplissage spool/disque via MCP (wiki_jobs.py:434-594, server.py:1250-1269). validate_extraction ne borne ni sections markdown (total), ni entities definition/name/aliases, ni relations type/evidence, et ne valide jamais doc[issues] (transmis tel quel lignes 718, 937). Aucune taille max sur extraction dict côté server. Un client mcp:ecriture peut spooler des enveloppes géantes (sanos limite) puis merge.py écrira des fiches géantes. Fix : borne (ex. extraction JSON <= 200 Ko, sections <= 50 Ko, entities <= 100, issues validé comme relations, evidence déjà tronquée à 200 OK ligne 537-538).
- **Mineur**
  - Idempotence avant authent du bail (wiki_jobs.py:659-668) : sur job submitted/merged, submit retourne duplicate/conflit SANS vérifier lease_id/fencing/expiry, et le lease n'est jamais effacé après submit (722-725). job_id déterministe sha(source_hash|chunk_hash|idx|contract)[:24] (197-199), recalculable. Impact faible (receipt déterministe, pas d'écrasement, read toujours protégé) mais viole le triple-check annoncé ligne 11. Fix : vérifier lease avant la branche idempotente.
  - ValueError non mappée -> 500 au lieu d'erreur propre : convia_mcp.wiki_submit int(fencing_token) (convia_mcp.py:380-387), wiki_claim/merge passthrough server.py:1228-1230,1301 ; except ne prend que (ConviaError,OSError). Fix : attraper ValueError/TypeError -> ConviaError.
  - Manifeste append dans la transaction sans atomicité fichier (wiki_jobs.py:917-933 : UPDATE merged, _manifest_append 926-932, COMMIT 933). Crash entre append et COMMIT -> doublon au retry (bénin last-wins, mais double ligne schema 4). _manifest_append (836-841) en append direct, pas tmp+rename -> ligne déchirée si ENOSPC/crash. Fix : COMMIT puis append avec dédup, ou append atomique + fsync dir déjà OK côté spool.
  - _write_atomic_json tmp pid-only (607 : .tmp.{pid}) : 2 threads même process, même spool path, même pid -> écritures entrelacées. Sérialisé inter-process (pids distincts + os.replace), mais pas inter-thread. Faible (contenu idempotent identique). Fix : suffixe pid+threadid/uuid.
  - sync_source boucle morte (242-244 for c in chunks: pass) + stale bump attempts même sur leased actif (247-255) cumulé avec submit stale (651-658) -> double incrément, quarantaine prématurée après 2-3 edits source. Nettoyer la boucle, ne pas incrémenter si déjà leased par le même cycle.
  - Code mort privilégié conservé : _systemctl(privileged=True)+sudo (convia_mcp.py:273-291), INGEST_REQUEST (38-39), ingest_backlog (337-356) — plus appelés (ingest_start stub). Inertes, mais _systemctl show reste utilisé pour running best-effort (303-307). Ne pas réactiver sans revue sudoers/NoNewPrivileges.
  - read_job reset expiré sans BEGIN IMMEDIATE (390-394) : 2 read concurrents sur même expiré font 2 UPDATE pending — bénin (les deux refusent le contenu). Pas de fuite.
  - server.py passthrough négatifs : limit/max_ms négatifs (truthy) -> merge_pending limit<0 rend 0 immédiatement (bénin) ; claim négatifs clampés côté wiki_jobs (321-322). OK.
  - Sécurité MCP vérifiée : read confiné DB uniquement (jamais de open(path) sur entrée appelant), claim clampé (<=10, 60-86400s), write tools sous _exiger_ecriture sauf wiki_ingest_read (lecture seule, voulu), DATA NOT INSTRUCTIONS sur read (server.py:1236-1241) + data_notice (wiki_jobs.py:411-413). Aucun secret dans le diff. Aucune régression ConvIA (queue/projection/CAS hash intacts). Écart prod SDKv2 non aggravé (nouveaux outils même pattern @mcp.tool, aucune API FastMCP-spécifique).
  - Tests manquants : reclaim expiré, renew expiré, submit sans lease après submitted, payload géant/issues, doublon manifeste après crash, fencing non-numérique.

### Tests à lancer
- Ciblé (vérifié 2026-09-09 : collect 28+25, run OK 1 skipped) : python -m pytest tests/test_wiki_jobs.py tests/test_convia_mcp.py -q
- Complet (66 passed annoncés) : python -m pytest -q
- Fuzz manuel avant GO : claim abandonné puis reclaim ; renew après expiry ; submit submitted sans lease ; submit 5 Mo/issues imbriqués ; kill -9 entre manifest append et COMMIT puis merge_pending x2 (compter lignes manifest) ; fencing_token=abc.
- Test gaps outil : 98 (env, interdit, _tool, build_fixture, wj) — couvrir au moins les 6 ci-dessus.

### Verdict
À corriger : 3 majeurs cernés (reclaim expiré, renew sans check expiry, extraction non bornée) + 2 mineurs faciles (lease avant idempotence, ValueError->ConviaError). Pas de refonte, pas de régression ConvIA, concurrence saine (BEGIN IMMEDIATE + tests 2-threads verts). GO après ces 5 fixes + 6 tests.
