# Ingestion immédiate ConvIA et MCP `vault-context` (lecture seule)

## Problème mesuré (vps-etude, 2026-09-14)

- Les ConvIA arrivent dans le miroir par `rclone` : aucun chemin sale, donc seule la
  réconciliation les voit (elle passe après chaque lot du worker, soit 1 h 50 par lot).
- Ensuite, attente d'embedding : 1 730 chemins en file, environ 400 notes par tranche de 75 min.
- Effet : 199 ConvIA du 2026-09-14 étaient introuvables par `search_vault` pendant plusieurs heures.

## Voie fraîche (`vault_mcp/frais.py`)

- Ensemble frais = notes du miroir **absentes de l'index publié** qui sont soit sales
  (file, lots en cours, différés), soit écrites depuis `reconcile.seuil`.
  Borne : `VAULT_MCP_FRAIS_MAX` (5000). Une note déjà indexée puis modifiée garde sa
  version indexée jusqu'à son lot. L'inclure faisait régresser le banc
  (MRR 0,751 → 0,692, p95 multiplié par 2).
- `vault-frais.timer` relance `vault-frais.service` 60 s après la fin du passage précédent.
  Le service tient en quelques secondes : pas de LLM, pas d'embedding, pas de réseau
  (`PrivateNetwork=yes`). Il écrit `/var/lib/vault-mcp/frais.sqlite` (FTS5).
- `search_vault` (hybride et lexical) fusionne ce classement pour les seules notes
  absentes de l'index, qui ressortent avec `origine: "frais"`.
- Une note publiée dans l'index sort de la base au passage suivant.
- Les Scheduled Tasks ConvIA/Wiki ne servent plus qu'à l'enrichissement sémantique :
  la trouvabilité n'en dépend plus.
- Rollback à chaud : `VAULT_MCP_POIDS_FRAIS=0` (drop-in, puis redémarrage).
  Rollback complet : `/var/backups/vault-mcp-frais-20260914T225115Z/rollback.sh`.

## Second MCP `vault-context` (`vault_mcp/contexte.py`)

- Outils : `search_vault`, `read_note`, `list_notes`, `search_notes`,
  `get_graph_context`, `context_status`. Surface figée par test : aucune écriture,
  aucun outil ConvIA/Wiki/reindex.
- Unité `vault-context-mcp.service`, écoute sur `127.0.0.1:8810` uniquement
  (`IPAddressAllow=localhost`). Seuls accès en écriture : les fichiers de verrou flock
  de l'index.
- Authentification : Bearer dédié `VAULT_CONTEXT_TOKEN` dans `/opt/vault-mcp/context.env`
  (600, juliann-app). Refusé s'il est égal au jeton principal. Pas d'OAuth, donc pas
  d'écriture accordable.
- Smoke : `scripts/smoke_contexte.py URL REQUETE [CHEMIN]`.

## Seul changement externe restant : route publique

Non fait volontairement. Le tunnel `vault-ngrok` sert un seul domaine statique vers le
serveur principal. Mettre `/context` derrière ce même domaine obligerait à insérer un
proxy devant le MCP principal et son OAuth, ce qui modifie un endpoint en production.

Pour exposer le serveur, une seule action externe suffit : créer un second hostname
public (ngrok, domaine réservé supplémentaire, ou nom Cloudflare Tunnel) qui pointe vers
`http://127.0.0.1:8810`, puis ajouter une unité tunnel calquée sur `vault-ngrok.service`,
avec `After/Requires=vault-context-mcp.service`. Aucun code à changer : la protection
anti-rebind est déjà désactivée et le Bearer reste la seule garde. Côté client, déclarer
l'URL `https://<hostname>/mcp` avec l'en-tête `Authorization: Bearer <VAULT_CONTEXT_TOKEN>`.
