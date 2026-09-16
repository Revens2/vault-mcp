//! Bibliotheque partagee `vault-mcp-rs` (adaptateur transparent + assemblage).
//!
//! Miroir de `vault_mcp/server.py` (38 outils, metier Python fige conserve :
//! recherche semantique, graphe, ecriture a consentement, convia, wiki).
//! L'upstream reste l'enforceur de la politique (les outils Python refusent
//! eux-memes sans portee ecriture, en texte `ERREUR: ...`) : la facade relaie
//! donc `tools/list` VERBATIM (aucun filtrage) et ne refuse en local que les
//! noms d'outils inconnus (fail-closed).
//!
//! Point de conception (documente, volontaire) : le `Authorization` client est
//! retransmis TEL QUEL vers l'upstream. Justification : upstream first-party
//! en boucle locale, MEME emetteur et MEME magasin de credentials que la
//! facade — l'upstream applique exactement la meme validation qu'en direct
//! (statique partage + portees + ecriture). Sans cela, la facade devrait
//! reclassifier les 38 outils en lecture/ecriture et risquerait une
//! regression d'autorisation (ex. `move_note`). Les autres facades ne
//! retransmettent jamais `authorization` car leurs upstreams exigent un
//! credential DIFFERENT (PAT) ou aucun.
//!
//! Differences assumees (contrat outils intact) :
//! * 401 au format framework (URL PRM exacte presente) ;
//! * PRM/AS au format framework (`resource_name` present, scopes lecture+
//!   ecriture annonces, `none` seul, sans `bearer_methods_supported`) — le
//!   Python annonce PRM lecture seule + `header` et AS `[post, basic]` ;
//!   les flux OAuth (DCR valide les deux portees des deux cotes) sont
//!   fonctionnellement paritaires ;
//! * rewrite `mcp-protocol-version` 2026-07-28 -> 2025-11-25 ;
//! * ajouts `/health` + `/ready` (le Python repond 404).
//! * pont stateless (voir [`stateless`]) : les requetes AVEC `id`, SANS
//!   `mcp-session-id`, autres que `initialize` (clients sans session, ex.
//!   connecteur ChatGPT), sont servies via une session upstream poolée par
//!   credential, reponse SANS `mcp-session-id` (stateless vu du client,
//!   comme github/orch qui n'en renvoient pas). Tout le reste (session
//!   presente, `initialize`, notifications) : relais direct inchange.
//!
//! OAuth JWT/Python vs opaque/Rust : canary via Bearer statique dedie
//! (meme famille que les lots precedents, bascule gated).

pub mod stateless;

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{middleware, routing::get, Router};

use mcp_auth::bearer::{bearer_middleware, json_response, BearerState, StaticBearer, TokenScopes};
use mcp_auth::filestore::{ChainedResolver, FileStore};
use mcp_auth::oauth::{
    auth_router, protected_resource_router, MemoryStore, OAuthConfig, OAuthState,
};
use mcp_auth::policy::{decide_body, CallDecision, TablePolicy, ToolClass, ToolPolicy};
use mcp_core::error::{self, codes};

/// Les 38 outils du contrat Python (`server.py`). Tous relaies (l'upstream
/// gate l'ecriture lui-meme) ; l'inconnu est refuse en local.
pub const VAULT_TOOLS: &[&str] = &[
    "list_notes",
    "read_note",
    "read_note_versioned",
    "search_notes",
    "search_vault",
    "get_graph_context",
    "create_note",
    "create_folder",
    "update_note",
    "append_note",
    "patch_note",
    "set_frontmatter",
    "delete_note",
    "move_note",
    "rename_note",
    "fix_links",
    "write_status",
    "reindex_vault",
    "reindex_note",
    "sync_now",
    "vault_status",
    "convia_status",
    "convia_list_pending_analysis",
    "convia_read_for_analysis",
    "convia_write_analysis",
    "convia_mark_blocked",
    "convia_requeue_blocked",
    "convia_list_blocked",
    "convia_scan",
    "wiki_ingest_status",
    "wiki_ingest_start",
    "wiki_ingest_claim",
    "wiki_ingest_read",
    "wiki_ingest_contract",
    "wiki_ingest_submit",
    "wiki_ingest_release",
    "wiki_ingest_merge_pending",
    "wiki_ingest_sync",
];

pub const READ_SCOPE: &str = "mcp:lecture";
pub const WRITE_SCOPE: &str = "mcp:ecriture";

/// URLs publiques a l'identique du Python (tunnel ngrok, issuer RACINE).
pub const ISSUER_DEFAULT: &str = "https://impeach-ransack-broadside.ngrok-free.dev/";
pub const RESOURCE_URL: &str = "https://impeach-ransack-broadside.ngrok-free.dev/mcp";
/// Nom de ressource cosmétique (le Python n'en annonce aucun ; champ
/// framework uniquement — divergence documentee).
pub const RESOURCE_NAME: &str = "Vault MCP";
pub const PRM_ALIAS: &str = "/.well-known/oauth-protected-resource/mcp";
/// URL PRM exacte servie par le Python (challenge 401 + document).
pub const PRM_URL: &str =
    "https://impeach-ransack-broadside.ngrok-free.dev/.well-known/oauth-protected-resource/mcp";

/// Politique edge : les 38 outils connus passent (upstream enforceur),
/// l'inconnu est refuse. `visibles` = 38 en lecture (aucun filtrage relay).
pub fn policy() -> TablePolicy {
    let entries: Vec<(&str, ToolClass)> =
        VAULT_TOOLS.iter().map(|t| (*t, ToolClass::Read)).collect();
    TablePolicy::new(READ_SCOPE, WRITE_SCOPE, &entries)
}

/// Parse un fichier secret Bearer : nu (jeton seul) ou format env
/// (`mcp.env` prod partage, assignation `VAULT_MCP_TOKEN`). Echecs = messages
/// statiques (jamais de valeur, jamais de chemin).
pub fn parse_token_file(raw: &str) -> Result<String, &'static str> {
    let est_env = raw.lines().any(|l| {
        let t = l.trim().strip_prefix("export ").unwrap_or(l.trim()).trim();
        t.starts_with("VAULT_MCP_TOKEN=")
    });
    if est_env {
        for ligne in raw.lines() {
            let l = ligne.trim();
            if l.is_empty() || l.starts_with('#') {
                continue;
            }
            let l = l.strip_prefix("export ").unwrap_or(l);
            if let Some((k, v)) = l.split_once('=') {
                if k.trim() == "VAULT_MCP_TOKEN" {
                    let tok = v.trim().trim_matches(|c| c == '"' || c == '\'').to_string();
                    if tok.len() < 32 || tok.contains('\n') {
                        return Err("token Bearer invalide (fichier)");
                    }
                    return Ok(tok);
                }
            }
        }
        return Err("token Bearer introuvable (fichier)");
    }
    let tok = raw.trim().to_string();
    if tok.len() < 32 {
        return Err("token Bearer trop court (<32 car.), refuse de demarrer");
    }
    Ok(tok)
}

/// Configuration d'assemblage (resolue par le binaire).
pub struct ServiceConfig {
    pub upstream: String,
    pub static_token: String,
    pub static_token_scopes: Vec<String>,
    pub oauth: OAuthConfig,
    pub max_body_bytes: usize,
}

#[derive(Clone)]
struct AppState {
    client: reqwest::Client,
    upstream: String,
    policy: Arc<TablePolicy>,
    bridge: Arc<stateless::StatelessBridge>,
}

/// Assemble le routeur complet : sante + OAuth + PRM/AS transcrits + `/mcp` + 404.
/// Sans pont fichier (comportement v0.2).
pub fn build_router(cfg: ServiceConfig) -> Result<Router, mcp_core::error::Error> {
    build_router_full(cfg, None)
}

/// Assemble le routeur avec pont fichier optionnel (sessions OAuth Python
/// existantes acceptées sans re-consentement, `resource` exigée parité
/// vault ; `None` = comme [`build_router`]).
pub fn build_router_with_filestore(
    cfg: ServiceConfig,
    mount: Option<mcp_gateway::router::FileStoreMount>,
) -> Result<Router, mcp_core::error::Error> {
    build_router_full(cfg, mount)
}

fn build_router_full(
    cfg: ServiceConfig,
    mount: Option<mcp_gateway::router::FileStoreMount>,
) -> Result<Router, mcp_core::error::Error> {
    let store = Arc::new(MemoryStore::default());
    let file = mount.filter(|m| !m.etat_path.trim().is_empty()).map(|m| {
        let fs = FileStore::new(&m.etat_path);
        // Parité observée : le Python ne contrôle jamais `resource` en
        // pratique (`AuthSettings.validate_token_resource` non posé →
        // `resource_server_url=None` côté `BearerAuthBackend`). Un
        // `expected_resource` explicite reste un opt-in strict.
        match m.expected_resource {
            Some(r) if !r.trim().is_empty() => Arc::new(fs.with_expected_resource(r)),
            _ => Arc::new(fs),
        }
    });
    if let Some(f) = &file {
        // Fail-closed par requête si illisible (le magasin naît à la première
        // émission Python) ; le Bearer statique reste disponible.
        if std::fs::metadata(f.etat_path()).is_ok() {
            tracing::info!("pont fichier OAuth (read-only) monte");
        } else {
            tracing::warn!("pont fichier OAuth illisible au boot (fail-closed par requete)");
        }
    }
    let chained = Arc::new(ChainedResolver::new(Arc::clone(&store), file));
    let oauth_state = OAuthState {
        config: Arc::new(cfg.oauth.clone()),
        store,
    };
    let bearer_state = BearerState::new(
        StaticBearer::new(
            &cfg.static_token,
            "vault-mcp-cli-statique",
            &cfg.static_token_scopes,
        ),
        chained,
        vec![READ_SCOPE.to_string()],
        PRM_URL.to_string(),
    );
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| mcp_core::error::Error::Upstream(e.to_string()))?;
    let app_state = AppState {
        client,
        upstream: cfg.upstream.trim_end_matches('/').to_string(),
        policy: Arc::new(policy()),
        bridge: Arc::new(stateless::StatelessBridge::new()),
    };

    let mcp_route = Router::new()
        .route(
            "/mcp",
            get(mcp_handler)
                .post(mcp_handler)
                .delete(mcp_handler)
                .route_layer(middleware::from_fn_with_state(
                    bearer_state,
                    bearer_middleware::<ChainedResolver>,
                )),
        )
        .with_state(app_state);

    let discovery_alias = PRM_ALIAS;

    let app = Router::new()
        .merge(mcp_http::health::router("vault-mcp-rs", "/mcp"))
        .merge(auth_router(oauth_state.clone()))
        .merge(protected_resource_router(oauth_state, &[discovery_alias]))
        .merge(mcp_route);

    Ok(mcp_http::hardening::harden(app, cfg.max_body_bytes))
}

/// Handler `/mcp` : Bearer edge, refus locaux (parse/batch/sans-nom/inconnu),
/// puis relais transparent avec `Authorization` client conserve.
async fn mcp_handler(State(state): State<AppState>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let method = parts.method.clone();
    let path = parts.uri.path().to_string();
    let query = parts.uri.query().map(str::to_string);
    let scopes: HashSet<String> = parts
        .extensions
        .get::<TokenScopes>()
        .map(|s| s.0.clone())
        .unwrap_or_default();
    // Retransmission edge->upstream (meme emetteur, meme magasin, loopback).
    let authorization = parts
        .headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    let mut body_bytes: Option<Vec<u8>> = None;
    if matches!(method, Method::POST | Method::PUT | Method::PATCH) {
        let bytes =
            match axum::body::to_bytes(body, mcp_http::hardening::DEFAULT_MAX_BODY_BYTES).await {
                Ok(b) => b,
                Err(_) => {
                    return json_response(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        error::payload_too_large_body(),
                    );
                }
            };
        if !bytes.is_empty() {
            match decide_body(&bytes) {
                CallDecision::ParseError => {
                    return json_response(
                        StatusCode::OK,
                        error::jsonrpc_error(
                            None,
                            codes::PARSE,
                            "corps JSON-RPC illisible (fail-closed)",
                        ),
                    );
                }
                CallDecision::BatchRejected => {
                    return json_response(
                        StatusCode::OK,
                        error::jsonrpc_error(
                            None,
                            codes::BATCH,
                            "requetes par lot non prises en charge",
                        ),
                    );
                }
                CallDecision::Nameless { id } => {
                    return json_response(
                        StatusCode::OK,
                        error::jsonrpc_error(
                            id.as_ref(),
                            codes::APP,
                            "tools/call sans nom d'outil (fail-closed)",
                        ),
                    );
                }
                CallDecision::Call { id, name } => {
                    if state.policy.classify(&name) == ToolClass::Unknown {
                        return json_response(
                            StatusCode::OK,
                            error::jsonrpc_error(
                                id.as_ref(),
                                codes::APP,
                                "outil inconnu ou non classe : appel refuse (fail-closed)",
                            ),
                        );
                    }
                    // Outil connu : l'upstream gate lui-meme (portees + ERREUR).
                    let _ = scopes;
                }
                CallDecision::Passthrough => {}
            }
            body_bytes = Some(bytes.to_vec());
        }
    }

    // Pont stateless (compat clients sans session, ex. ChatGPT) : POST avec
    // `id`, sans `mcp-session-id`, autre que `initialize`. Tout le reste
    // (session presente, `initialize`, notifications, GET/DELETE) : direct.
    let pontable = method == Method::POST
        && !parts.headers.contains_key("mcp-session-id")
        && body_bytes
            .as_deref()
            .is_some_and(|b| stateless::classify_body(b) == stateless::BridgeKind::Request);
    if pontable {
        return bridge_forward(
            &state,
            &path,
            query.as_deref(),
            &parts.headers,
            body_bytes,
            authorization,
        )
        .await;
    }

    forward(
        &state,
        &method,
        &path,
        query.as_deref(),
        &parts.headers,
        body_bytes,
        authorization,
    )
    .await
}

/// Pont stateless → stateful : sert une requete sans session via une session
/// upstream poolée par credential (meme `Authorization` client, jamais stocke :
/// seule son empreinte indexe le cache). Reponse SANS `mcp-session-id`.
/// Tout echec d'etablissement = repli vers le relais direct (400 parite),
/// jamais d'erreur inventee ni de contournement d'authentification.
async fn bridge_forward(
    state: &AppState,
    path: &str,
    query: Option<&str>,
    headers: &axum::http::HeaderMap,
    body: Option<Vec<u8>>,
    authorization: Option<String>,
) -> Response {
    let Some(auth) = authorization else {
        return forward_direct(state, path, query, headers, body, None).await;
    };
    let key = stateless::credential_key(&auth);
    let accept = headers
        .get("accept")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let version = headers
        .get("mcp-protocol-version")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    // 1) Session poolée encore valide : rejouer direct, re-etablir sur 404.
    if let Some(sess) = state.bridge.get(&key).await {
        if let Some(resp) = forward_as(
            state,
            path,
            query,
            headers,
            body.clone(),
            auth.clone(),
            &sess,
        )
        .await
        {
            if resp.status() != StatusCode::NOT_FOUND {
                return strip_session(resp);
            }
            state.bridge.evict(&key).await;
        } else {
            state.bridge.evict(&key).await;
        }
    }

    // 2) Etablir une session upstream avec le credential en cours, puis rejouer.
    // Un seul essai : echec = repli direct (400 parite, fail-closed).
    if let Some(sess) = stateless::establish_session(
        &state.client,
        &state.upstream,
        &auth,
        accept.as_deref(),
        version.as_deref(),
    )
    .await
    {
        state.bridge.put(key.clone(), sess.clone()).await;
        if let Some(resp) = forward_as(
            state,
            path,
            query,
            headers,
            body.clone(),
            auth.clone(),
            &sess,
        )
        .await
        {
            if resp.status() == StatusCode::NOT_FOUND {
                state.bridge.evict(&key).await;
            }
            return strip_session(resp);
        }
        state.bridge.evict(&key).await;
    } else {
        tracing::warn!("pont stateless : session amont indisponible (repli parite)");
    }
    forward_direct(state, path, query, headers, body, Some(auth)).await
}

/// Relais direct (sans pont) : factorise le repli fail-closed du pont.
async fn forward_direct(
    state: &AppState,
    path: &str,
    query: Option<&str>,
    headers: &axum::http::HeaderMap,
    body: Option<Vec<u8>>,
    authorization: Option<String>,
) -> Response {
    forward(
        state,
        &Method::POST,
        path,
        query,
        headers,
        body,
        authorization,
    )
    .await
}

/// Retire `mcp-session-id` d'une reponse pointee (stateless vu du client,
/// comme github/orch qui n'en renvoient pas ; evite qu'un client ferme une
/// session poolée partagee via DELETE).
fn strip_session(mut resp: Response) -> Response {
    resp.headers_mut().remove("mcp-session-id");
    resp
}

/// Relais avec une session poolée injectee (le `Authorization` reste celui du
/// client : meme emetteur, meme magasin). `None` (parse impossible, ne devrait
/// pas arriver : identifiants hex) = repli direct.
async fn forward_as(
    state: &AppState,
    path: &str,
    query: Option<&str>,
    headers: &axum::http::HeaderMap,
    body: Option<Vec<u8>>,
    authorization: String,
    session: &str,
) -> Option<Response> {
    let value: axum::http::HeaderValue = session.parse().ok()?;
    let mut h = headers.clone();
    h.insert("mcp-session-id", value);
    Some(
        forward(
            state,
            &Method::POST,
            path,
            query,
            &h,
            body,
            Some(authorization),
        )
        .await,
    )
}

async fn forward(
    state: &AppState,
    method: &Method,
    path: &str,
    query: Option<&str>,
    headers: &axum::http::HeaderMap,
    body: Option<Vec<u8>>,
    authorization: Option<String>,
) -> Response {
    use mcp_http::proxy as hp;

    let outgoing = hp::forward_request_headers(headers);
    let url = match query {
        Some(q) if !q.is_empty() => format!("{}{}?{q}", state.upstream, path),
        _ => format!("{}{}", state.upstream, path),
    };
    let mut builder = state.client.request(method.clone(), url);
    for (name, value) in outgoing.iter() {
        builder = builder.header(name, value);
    }
    // Meme emetteur + meme magasin + loopback : l'upstream revalide a
    // l'identique du direct (portees + ecriture). Jamais journalise.
    if let Some(auth) = authorization {
        builder = builder.header(axum::http::header::AUTHORIZATION, auth);
    }
    if *method == Method::POST {
        builder = builder.timeout(Duration::from_secs(mcp_http::hardening::PROXY_TIMEOUT_SECS));
    }
    if let Some(b) = body {
        builder = builder.body(b);
    }
    let upstream = match builder.send().await {
        Ok(r) => r,
        Err(_) => {
            return json_response(
                StatusCode::BAD_GATEWAY,
                error::bad_gateway_body("HttpError"),
            );
        }
    };
    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let out_headers = hp::forward_response_headers(upstream.headers());

    if *method == Method::GET {
        let stream = upstream.bytes_stream();
        let mut builder = Response::builder().status(status);
        for (name, value) in out_headers.iter() {
            builder = builder.header(name, value);
        }
        return builder
            .body(Body::from_stream(stream))
            .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response());
    }

    let bytes = match upstream.bytes().await {
        Ok(b) => b.to_vec(),
        Err(_) => {
            return json_response(
                StatusCode::BAD_GATEWAY,
                error::bad_gateway_body("HttpError"),
            );
        }
    };
    // Relais verbatim : ni filtrage tools/list (l'upstream annonce), ni
    // reecriture (l'upstream gate). Contrat outils intact par construction.
    let mut builder = Response::builder().status(status);
    for (name, value) in out_headers.iter() {
        builder = builder.header(name, value);
    }
    builder
        .body(Body::from(bytes))
        .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
}
