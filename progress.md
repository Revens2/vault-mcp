# progress — Incident CRITICAL Vault MCP 22/09/2026
STATE 13/13 | next: surveillance 48h + PR review | blocker: aucun (service E2E_HEALTHY, fix deploye)

## ROOT CAUSE
- Cas B prouve : prod tournait f82a721 (hashes index.py 43c842b4 / embed.py df808619 / server.py 6b28579f identiques GitHub). Correctif e501135/c01b1b2 charge, mais OOM quand meme : 16:18:15 UTC oom-kill PID 2021670, pic 7.9G + 2.7G swap.
- Mecanisme residuel (forensic A, ROOT_CAUSE_UNRESOLVED partiel) : H1 retention generations CONFIRMEE partiellement (deleted vectors.1790095681 528M via FD15 + anon 3.67G, `np.concatenate` 517M/publish dans `reindexer_chemins`), H2 builds BM25 co-facteur (`Instantane` ~350M epingle en thread daemon), H3 allocator (arenes 9x128M, THP 200M, HWM-RSS 850M non rendu), H4 ONNX amplificateur (threads=None cote service), H5 cache ecarte (653M disque, freelist 0), H6 sessions ecartee (16 threads/11 FD stables).
- Fix deploys : `np.frombuffer.copy()` (embed.py), `_fermer_vecteurs` + `_courant` close opportuniste + `reindexer_chemins` del/gc/log + `_construire_bm25` log/del/gc (index.py).

## FIXES
- Code (branche fix/vault-memory-liveness-20260922) : embed.py frombuffer.copy ; index.py gc import, _fermer_vecteurs, _courant close si refcount<=3, invalider(fermer), reindexer_chemins del+gc+log generation, _construire_bm25 log debut/fin + del textes + gc. Deploys en prod /opt/vault-mcp + restart controle 17:59:40 UTC.
- Systemd : 95-stabilisation-20260922.conf (VAULT_MCP_THREADS=2, MemorySwapMax=1G, High/Max 7G/8G inchanges) ; 40-seuil-dirty.conf (ReadWritePaths /srv/vault-spool/dirty pour vault-reindex, fix EROFS seuil). daemon-reload OK, threads/swap actifs apres restart.
- Forensic auto : /opt/vault-watchdog/collect-forensic.sh (pre-restart/pre-deploy testes, rotation 10, /var/log/vault-mcp/forensic-*).
- Healthcheck E2E : /opt/vault-watchdog/vault_e2e.py (EDGE_DOWN/AUTH_ALIVE/RUST_ALIVE/PYTHON_DOWN/INITIALIZE_FAILED/TOOLS_LIST_FAILED/E2E_HEALTHY, token via env file jamais argv). Prouve : E2E_HEALTHY tools=38.
- Worker : pas de changement prod (ATTENTE 3h vs timeout 4h incoherent, fix EROFS seuil deploys, B-variante ATTENTE 1h30 + SIGTERM proposee en branche, a valider). Full 12h→6h propose, non applique.
- Nginx : aucune modif (nginx -t vert avec sudo ; rouge sans sudo = snippet 0600 root, methode de test, pas regression). Warn 10.0.0.80:443 benin.

## LIVE STATE
- vault-mcp.service active (PID 1673454 depuis 17:59:40 UTC), Memory 55-61M, peak 61M, SwapMax 1G. vault-mcp-rs active (PID 1176126 depuis 18/09, 16.8M). nginx active. systemctl --failed : 0. Recv-Q :8787/:8788/:18987 = 0.
- Index : 352565 fragments, vectors 517M courant, meta 140M, embed_cache 653M. Worker idle success (dernier publish 19.6s).

## MEMORY BEFORE/AFTER
- BEFORE : OOM 7.9G peak + 2.7G swap (16:18), process 4.55G stable (RSS 4304796 kB = anon 3727464 + file 577k, Private_Dirty=anon, deleted mmap 528M, heap 48M, threads 16, FD 11).
- AFTER : 55.8M au boot → 61M apres 20 recherches + E2E (stable, pas de dent). Soak 200 recherches + 2 publishes naturels restant a observer sur 48h via forensic+e2e (instrumentation en place).

## HEALTHCHECK
- Topologie prouvee : nginx :8788 (127.0.0.1 + 10.200.114.203) → Rust 127.0.0.1:18987 → Python 127.0.0.1:8787. Public :443 /vault/mcp → :18987, well-known/authorize/token → :8787.
- Sans auth : Rust 401 0.6ms, Python 401 1.2ms, edge 401 0.9ms, public 401 6.2ms = EDGE_AUTH_ALIVE (jamais E2E_HEALTHY). Rust /health 200, Python well-known 200.
- E2E authentifie : initialize 200 10ms + tools/list 200 10-20ms, tools=38 → E2E_HEALTHY (interne :18987 et :8787).

## INDEX WORKER
- activating 4h explique : Type=oneshot + TimeoutStartSec=4h, kill a 4h00 pile x3 (21/09 00:18, 22/09 08:18, reconciliation 11:20→15:20). Publish = rewrite complete 541M+145M pour 1 note (20s cache chaud → 4h cache froid), ATTENTE 3h + publish >1h = kill mid-publish (5 meta.tmp 0 octets, inflight recupere, zero perte). Contention sqlite + EROFS seuil (ReadOnlyPaths, seuil fige 15/09 → 5084 ecarts, `laisse au full` toutes les 10 min). Actuellement nominal (runs 18-45s success).

## NGINX
- `sudo nginx -t` vert (syntax ok, test successful, seul warn netbird:31). Sans sudo : emerg snippet 0600 root (excalidraw-biblio-auth.conf, mtime 16/09 inchange) → divergence methode, pas regression. Aucun reload effectue (non necessaire). Rollback N/A.

## TESTS
- Staging isole /tmp/vault-staging (PYTHONPATH src, VAULT_MCP_INDEX/EMBED_CACHE tmp, sudo -u juliann-app) : test_memory_bounds 4 passed (mmap close, courant, invalider, cache copy) ; test_index+test_embed+test_index_incremental 45 passed. Windows local : py_compile OK, ruff (restes pre-existants S608/N818 + 0 nouveau apres fix).
- E2E live : initialize + tools/list 200, 38 outils, latences <30ms. 20 recherches read-only OK (mode degrade session, a rejouer en soak propre).

## GIT SHA / PR
- Base : origin/main f82a721 (Merge PR #4 prod-sync-20260921). Correctifs : e501135 (publish/BM25) + c01b1b2 (embed threads). Local : prod-sync-20260921 a205246 (= f82a721 + docs revue, 1 commit). Branche : fix/vault-memory-liveness-20260922 (code + systemd versionnes + scripts + tests). Prod /opt/vault-mcp non versionne mais hashes == f82a721 avant deploy, + nos 2 fichiers apres (backups /root/vault-mcp-rollback-20260922T175627Z). PR non creee (push https sans token en session ; a pousser + PR vers main, CI attendue).

## ROLLBACK
- Systemd : /root/vault-mcp-rollback-20260922T175627Z/{vault-mcp.service.d,vault-reindex.service.d,vault-index-worker.service.d} + index.py/embed.py. Restaurer : sudo cp -a backup → /etc/systemd/system/... + /opt/vault-mcp/vault_mcp/..., daemon-reload, restart vault-mcp, verifier E2E_HEALTHY. Forensic/watchdog : supprimer /opt/vault-watchdog + drop-ins 95/40. Nginx : aucun changement → aucun rollback. DB/index : jamais touches (pas de reindex, pas de purge cache).

## OPEN RISKS
- Attribution exacte 3.67G anon (copies vs BM25 vs fragmentation) exige tracemalloc/py-spy en staging charge (publishes 10min + 5 req/min + BM25 fond) ; instrumentation pre-restart en place pour le prochain incident.
- Worker : kill 4h encore possible avant ATTENTE 1h30 + SIGTERM + full 6h (proposes, non deploys) ; notify-failure bruyant ; MemoryMax worker 3G vs pic 2.2G marge fine.
- EROFS seuil corrige (drop-in) mais seuil non repose avant prochain full 03:30 → reconciliation decorative jusque-la.
- Soak 48h (RSS<800M, 0 OOM, readyz p99<2s) non encore observe ; swap borne 1G a valider au prochain pic.
- meta.tmp 0-octets (5) non purges (glob sudo a fiabiliser) ; inoffensifs.
- Docs architecture (notes/infra/VPS-Etude-etat-reel.md) a MAJ via canal Vault (topologie nginx→rs→python + 401 vs E2E + backlog + forensic + rollback + MemoryMax/OOM + worker + nginx sudo).
