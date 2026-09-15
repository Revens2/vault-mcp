#!/bin/bash
# Verification canary vault : sante + fail-closed, prod intacte. Lecture seule.
set -u
export HOME=/home/juliann
echo "=== canary :18987 ==="
curl -s --max-time 5 http://127.0.0.1:18987/health; echo
curl -s --max-time 5 http://127.0.0.1:18987/ready; echo
curl -s -o /dev/null -w "POST /mcp sans auth -> %{http_code}\n" --max-time 5 \
  -X POST http://127.0.0.1:18987/mcp -H "content-type: application/json" -d '{}' || true
systemctl is-active vault-mcp-rs.service
echo "=== prod :8787 intacte ==="
curl -s -o /dev/null -w "POST /mcp sans auth -> %{http_code}\n" --max-time 5 \
  -X POST http://127.0.0.1:8787/mcp -H "content-type: application/json" -d '{}' || true
systemctl is-active vault-mcp.service vault-context-mcp.service
