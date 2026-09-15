#!/bin/bash
# Canary VPS vault-mcp-rs : toolchain + build release + install + demarrage.
# Idempotent. Aucune modification du service prod vault-mcp.service.
set -euo pipefail
export HOME=/home/juliann
export PATH="$HOME/.cargo/bin:$PATH"
BUILD=/home/juliann/build/mcp-rust-migration

cargo --version
rustup component add rustfmt clippy 2>/dev/null || true
cd "$BUILD/vault-mcp-rs"
echo "[canary] fmt/clippy/test…"
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
echo "[canary] build release…"
cargo build --release
echo "[canary] installation /opt/vault-mcp-rs…"
sudo install -d -o juliann-app -g juliann-app -m 0755 /opt/vault-mcp-rs
sudo install -m 0755 target/release/vault-mcp-rs /opt/vault-mcp-rs/vault-mcp-rs
sudo install -m 0644 deploy/vault-mcp-rs.service /etc/systemd/system/vault-mcp-rs.service
sudo systemctl daemon-reload
echo "[canary] demarrage vault-mcp-rs.service (:18987)…"
sudo systemctl enable --now vault-mcp-rs.service
sleep 3
sudo systemctl is-active vault-mcp-rs.service
curl -s http://127.0.0.1:18987/health; echo
curl -s http://127.0.0.1:18987/ready; echo
echo "[canary] OK"
