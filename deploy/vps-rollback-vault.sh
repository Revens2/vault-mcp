#!/bin/bash
# Rollback canary vault : stoppe le canary, verifie la prod intacte.
# Aucune modification du service prod (jamais pointe vers le binaire Rust).
set -euo pipefail
sudo systemctl stop vault-mcp-rs.service || true
sudo systemctl is-active vault-mcp.service
curl -s -o /dev/null -w "prod :8787 -> %{http_code}\n" --max-time 5 \
  -X POST http://127.0.0.1:8787/mcp -H "content-type: application/json" -d '{}' || true
echo "[rollback] prod :8787 intacte, canary stoppe"
