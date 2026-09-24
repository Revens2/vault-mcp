# Runbook déploiement vault-mcp (vps-etude, /opt/vault-mcp)

Répond à l'issue #6 : le `cp + chown + restart` manuel ne garantissait pas
que l'octet déployé == `main`. Procédure reproductible ci-dessous.
**Le runtime actuel n'est pas modifié par ces fichiers** (que des ajouts versionnés).

## Références

- Repo : `https://github.com/Revens2/vault-mcp.git`, branche `main`.
- VPS : `ssh vps-etude`, cible `/opt/vault-mcp`, service `vault-mcp.service` (:8787),
  canary `vault-mcp-rs.service` (:18987 → upstream :8787), nginx :8788.
- Unité de base figée : `deploy/systemd/vault-mcp.service` (+ 9 drop-ins dans
  `deploy/systemd/vault-mcp.service.d/`, dont 80/90-memoire déjà versionnés avant).
- Recette historique PR #4 : `index.py 43c842b4 / embed.py df808619 / server.py 6b28579f`
  (empreintes 8 car. calculées sur VPS, méthode désormais standardisée dans
  `scripts/verify_deploy.sh` : `sha256sum` tronqué à 8).

## Procédure (déploiement normal)

1. **Poste — geler la référence** : `git rev-parse HEAD` (= `main` à jour),
   `git status --short --branch` propre.
2. **Poste — construire le tarball** :
   `git archive --format=tar.gz --prefix=vault-mcp/ main -o /tmp/vault-mcp-main.tar.gz`
   puis `SHA=$(sha256sum /tmp/vault-mcp-main.tar.gz | cut -d' ' -f1)` (noter le SHA).
3. **Poste — expédier** : `scp /tmp/vault-mcp-main.tar.gz vps-etude:/tmp/`
   (+ `scripts/deploy_sync.sh`, `scripts/verify_deploy.sh` si modifiés depuis).
4. **VPS — dry-run obligatoire** (lecture seule) :
   `sudo bash deploy_sync.sh /tmp/vault-mcp-main.tar.gz $SHA`
   → `SHA-OK` + diff relu. Tout écart inattendu = STOP.
5. **VPS — apply** : `sudo bash deploy_sync.sh /tmp/vault-mcp-main.tar.gz $SHA --apply`
   → backup `/opt/vault-mcp-backup-<ts>`, rsync (exclusions : `index/`, `models/`,
   `oauth/`, `venv/`, `*.env`, `*.bak*`, `__pycache__`), `chown juliann-app`, `daemon-reload`.
6. **VPS — restart gaté** : workers idle (`vault-index-worker` pas en `activating`
   avec publish en cours), puis `sudo systemctl restart vault-mcp`.
7. **VPS — vérification** : `sudo bash verify_deploy.sh /tmp/vault-mcp-main.tar.gz $SHA`
   → `VERIFY-OK` (SHA, 3 fichiers clés, diff vide, unit active, :8787).
   Compléter par `scripts/smoke_contexte.py` (depuis le poste, `VAULT_CONTEXT_TOKEN`)
   et `tools/list` (38 outils attendus).

## Rollback

`cp -a /opt/vault-mcp-backup-<ts>/* /opt/vault-mcp/` + `chown -R juliann-app:juliann-app`
+ `daemon-reload` + `restart`, puis `verify_deploy.sh` avec le tarball précédent.
Ne jamais `rm -rf /opt/vault-mcp` (l'index 1.4 Go + `embed_cache.sqlite` 671 M
seraient perdus : exclusions impératives, jamais purger le cache).

## Garde-fous

- `mcp.env` (0600) n'est jamais dans le tarball (`*.env` exclu) ni affiché.
- Comparer `sha256sum` ET `git hash-object` en cas de doute CRLF/LF.
- Premier déploiement réel avec cette procédure : hors passe (décision exploitant),
  ~30 min + soak 48 h.

## Constat dry-run 2026-09-24 (tarball `main` = f82a721, `--dry-run` seul, rien modifié)

- `SHA-OK b770ffa9…` : mécanique tarball → staging OK.
- Dérive confirmée (cœur de l'issue #6) : `embed.py` déployé `c32ebddc` vs `9568100a`,
  `index.py` `1091f0ea` vs `58f4cdcb`, `server.py` `6b28579f` (= SHA recette PR #4)
  vs `e03ce3c2` — fixes live post-22/09 jamais reversionnés ; ~30 fichiers diffèrent
  (contenu ou perms/owner).
- Premier `--apply` SUPPRIMERAIT (à valider explicitement avant) : `backups/`
  (sauvegardes pré-merge 13/09), `src/` (copie root Sept 8, sans référence) —
  absents de `main`. `bin/` (binaire ngrok du tunnel `vault-ngrok.service`, en
  cours d'exécution) est EXCLU du rsync : ne jamais le supprimer.
- Observé, hors passe : `vault-mcp-healthcheck.service` en `failed`,
  port :8788 absent, `PORT-OK :8787`, unit active.
