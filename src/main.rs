//! vault-mcp-rs — adaptateur Rust devant l'upstream Python fige (`:8787`).
//!
//! Metier conserve : recherche semantique, graphe, ecriture a consentement,
//! convia, wiki + OAuth Python. L'upstream reste l'enforceur (portees,
//! ecriture) : la facade relaie `tools/list` verbatim et retransmet le
//! `Authorization` client (meme emetteur, meme magasin, loopback).
//!
//! Environnement (prefixe `VAULT_MCP_RS_*`) :
//! * `VAULT_MCP_RS_ISSUER` (defaut issuer ngrok prod, HTTPS requis),
//! * `VAULT_MCP_RS_UPSTREAM` (defaut `http://127.0.0.1:8787`, loopback requis),
//! * `VAULT_MCP_RS_PORT` (defaut `18987` canary ; `8787` a la bascule),
//! * `VAULT_MCP_RS_TOKEN` (>= 32 car.) OU `VAULT_MCP_RS_TOKEN_FILE`
//!   (defaut `/opt/vault-mcp/mcp.env` prod partage en lecture seule, SANS
//!   copie — nu ou format env `VAULT_MCP_TOKEN=...`) — fail-closed,
//! * `VAULT_MCP_RS_TOKEN_SCOPES` (defaut lecture+ecriture, quoté dans l'unit),
//! * `VAULT_MCP_RS_CONSENT_HASH` (empreinte PBKDF2, vide = consentement refuse).
//! * `VAULT_MCP_RS_OAUTH_ETAT` (defaut `/opt/vault-mcp/oauth/etat.json` :
//!   pont READ-ONLY vers le magasin Python, sessions existantes sans
//!   re-consentement ; vide = pont desactive ; `resource` TOUJOURS exigee
//!   egale a l'URL canonique, parite `AuthSettings`).

use mcp_auth::oauth::OAuthConfig;
use vault_mcp_rs::{
    ISSUER_DEFAULT, PRM_ALIAS, READ_SCOPE, RESOURCE_NAME, RESOURCE_URL, WRITE_SCOPE,
};

/// Charge le Bearer statique : variable directe, sinon fichier. Le fichier est
/// soit nu (jeton seul), soit au format `KEY=valeur` (`mcp.env` prod partage,
/// sans copie : variable `VAULT_MCP_TOKEN`). Echecs = messages statiques.
fn load_static_token() -> Result<String, String> {
    if let Ok(v) = std::env::var("VAULT_MCP_RS_TOKEN") {
        let v = v.trim().to_string();
        if !v.is_empty() {
            if v.len() < 32 {
                return Err("VAULT_MCP_RS_TOKEN trop court (<32 car.)".to_string());
            }
            return Ok(v);
        }
    }
    let path = std::env::var("VAULT_MCP_RS_TOKEN_FILE")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "/opt/vault-mcp/mcp.env".to_string());
    let raw = std::fs::read_to_string(&path)
        .map_err(|_| "token Bearer illisible (fichier)".to_string())?;
    vault_mcp_rs::parse_token_file(&raw).map_err(str::to_string)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    mcp_observe::init("vault-mcp-rs");

    if std::env::var("VAULT_MCP_RS_ISSUER")
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        // `set_var` est `unsafe` sur cette toolchain : init mono-thread.
        unsafe { std::env::set_var("VAULT_MCP_RS_ISSUER", ISSUER_DEFAULT) };
    }
    if std::env::var("VAULT_MCP_RS_UPSTREAM")
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        unsafe { std::env::set_var("VAULT_MCP_RS_UPSTREAM", "http://127.0.0.1:8787") };
    }
    // Lecon lot 2 : sans SCOPES explicites, le defaut install ne donne que la
    // lecture. Le canary parite exige lecture+ecriture.
    if std::env::var("VAULT_MCP_RS_TOKEN_SCOPES")
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        unsafe {
            std::env::set_var(
                "VAULT_MCP_RS_TOKEN_SCOPES",
                format!("{READ_SCOPE} {WRITE_SCOPE}"),
            )
        };
    }
    let token = load_static_token().map_err(|e| {
        tracing::error!("fail-closed: pas de Bearer valide");
        std::io::Error::new(std::io::ErrorKind::PermissionDenied, e)
    })?;
    unsafe { std::env::set_var("VAULT_MCP_RS_TOKEN", &token) };
    // Pont fichier OAuth (transition) : Python = AS/control-plane, Rust =
    // data-plane. Meme utilisateur UNIX que le Python (fichiers 0600).
    if std::env::var("VAULT_MCP_RS_OAUTH_ETAT")
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        unsafe { std::env::set_var("VAULT_MCP_RS_OAUTH_ETAT", "/opt/vault-mcp/oauth/etat.json") };
    }

    let env = mcp_gateway::config::from_prefix(
        "VAULT_MCP_RS",
        18987,
        READ_SCOPE,
        &[READ_SCOPE, WRITE_SCOPE],
    )?;

    let port = env.port;
    let upstream_log = env.upstream.clone();
    let mount = mcp_gateway::config::filestore_mount(&env);
    let app = vault_mcp_rs::build_router_with_filestore(
        vault_mcp_rs::ServiceConfig {
            upstream: env.upstream,
            static_token: env.static_token,
            static_token_scopes: env.token_scopes,
            oauth: OAuthConfig {
                issuer: env.issuer,
                resource_url: RESOURCE_URL.to_string(),
                resource_name: RESOURCE_NAME.to_string(),
                default_scope: READ_SCOPE.to_string(),
                valid_scopes: vec![READ_SCOPE.to_string(), WRITE_SCOPE.to_string()],
                extra_submit_scopes: vec![WRITE_SCOPE.to_string()],
                consent_hash: env.consent_hash,
                static_client_id: "vault-mcp-cli-statique".to_string(),
            },
            max_body_bytes: mcp_http::hardening::DEFAULT_MAX_BODY_BYTES,
        },
        mount,
    )?;
    let _ = PRM_ALIAS;
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    tracing::info!(
        port,
        upstream = %upstream_log,
        "vault-mcp-rs prete (boucle locale uniquement)"
    );
    axum::serve(listener, app)
        .with_graceful_shutdown(mcp_core::lifecycle::shutdown_signal())
        .await?;
    Ok(())
}
