//! Contrat vault-mcp-rs : adaptateur transparent, 38 outils connus.
//!
//! Preuves exigibles du lot vault (adaptateur) :
//! * les 38 outils du contrat Python sont connus et relayes (aucun filtrage
//!   tools/list) ;
//! * l'inconnu est refuse en local AVANT tout envoi ;
//! * la gateway exige un Bearer sur `/mcp` (401 + `www-authenticate`), sert
//!   `/health` au format framework et la PRM a l'URL exacte ;
//! * le relais retransmet l'`Authorization` client (meme emetteur, loopback)
//!   et relaie les reponses verbatim (l'upstream gate l'ecriture).

use std::collections::HashSet;

use mcp_auth::policy::{ToolClass, ToolPolicy};
use vault_mcp_rs::{policy, READ_SCOPE, VAULT_TOOLS, WRITE_SCOPE};

fn scopes(s: &[&str]) -> HashSet<String> {
    s.iter().map(|x| x.to_string()).collect()
}

#[test]
fn trente_huit_outils_connus() {
    assert_eq!(VAULT_TOOLS.len(), 38, "regression table outils");
    let p = policy();
    for tool in VAULT_TOOLS {
        assert_eq!(p.classify(tool), ToolClass::Read, "outil {tool}");
        assert!(p.autoriser_call(tool, &scopes(&[READ_SCOPE])).is_none());
    }
    assert!(p.visibles(&scopes(&[READ_SCOPE])).len() >= 38);
}

#[test]
fn parse_token_nu_et_env() {
    use vault_mcp_rs::parse_token_file;
    assert_eq!(parse_token_file(&"y".repeat(40)).unwrap(), "y".repeat(40));
    let env = "# commentaire\nAUTRE=1\nVAULT_MCP_TOKEN=zyxwvu-tsrqponm-lkjihgfe-dcba9876543210\n";
    assert_eq!(
        parse_token_file(env).unwrap(),
        "zyxwvu-tsrqponm-lkjihgfe-dcba9876543210"
    );
    assert!(parse_token_file("trop-court").is_err());
    assert!(parse_token_file("AUTRE=1\n").is_err());
}

#[test]
fn inconnu_refuse_fail_closed() {
    let p = policy();
    assert_eq!(p.classify("drop_database"), ToolClass::Unknown);
    assert!(p
        .autoriser_call("drop_database", &scopes(&[READ_SCOPE, WRITE_SCOPE]))
        .is_some());
}

fn oauth_cfg() -> mcp_auth::oauth::OAuthConfig {
    mcp_auth::oauth::OAuthConfig {
        issuer: vault_mcp_rs::ISSUER_DEFAULT.to_string(),
        resource_url: vault_mcp_rs::RESOURCE_URL.to_string(),
        resource_name: vault_mcp_rs::RESOURCE_NAME.to_string(),
        default_scope: READ_SCOPE.to_string(),
        valid_scopes: vec![READ_SCOPE.to_string(), WRITE_SCOPE.to_string()],
        extra_submit_scopes: vec![WRITE_SCOPE.to_string()],
        consent_hash: String::new(),
        static_client_id: "vault-mcp-cli-statique".to_string(),
    }
}

fn gateway_test() -> axum::Router {
    use vault_mcp_rs::{build_router, ServiceConfig};

    build_router(ServiceConfig {
        upstream: "http://127.0.0.1:9".to_string(),
        static_token: "x".repeat(32),
        static_token_scopes: vec![READ_SCOPE.to_string(), WRITE_SCOPE.to_string()],
        oauth: oauth_cfg(),
        max_body_bytes: 1024 * 1024,
    })
    .expect("gateway de test")
}

#[tokio::test]
async fn mcp_sans_bearer_401_avec_prm() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let app = gateway_test();
    let res = app
        .oneshot(Request::post("/mcp").body(Body::from("{}")).unwrap())
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let challenge = res.headers()["www-authenticate"]
        .to_str()
        .unwrap()
        .to_string();
    assert!(
        challenge.contains("oauth-protected-resource/mcp"),
        "{challenge}"
    );
}

#[tokio::test]
async fn health_et_prm_alias() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let app = gateway_test();
    let res = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
    assert_eq!(v["service"], "vault-mcp-rs");

    let app = gateway_test();
    let res = app
        .oneshot(
            Request::get(vault_mcp_rs::PRM_ALIAS)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["resource"], vault_mcp_rs::RESOURCE_URL);
}

/// Le relais retransmet l'`Authorization` client et relaie verbatim : le mock
/// exige le Bearer client puis repond `tools/list` non filtree.
#[tokio::test]
async fn relais_transparent_authorization_client() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let mock = axum::Router::new().route(
        "/mcp",
        axum::routing::post(|req: axum::extract::Request| async move {
            let auth = req
                .headers()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string();
            assert_eq!(auth, format!("Bearer {}", "x".repeat(32)));
            (
                StatusCode::OK,
                [("content-type", "application/json")],
                r#"{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"vault_status","description":"d","inputSchema":{"type":"object"}}]}}"#,
            )
        }),
    );
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });

    use vault_mcp_rs::{build_router, ServiceConfig};
    let app = build_router(ServiceConfig {
        upstream: format!("http://127.0.0.1:{}", addr.port()),
        static_token: "x".repeat(32),
        static_token_scopes: vec![READ_SCOPE.to_string(), WRITE_SCOPE.to_string()],
        oauth: oauth_cfg(),
        max_body_bytes: 1024 * 1024,
    })
    .unwrap();

    let body = r#"{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}"#;
    let res = app
        .oneshot(
            Request::post("/mcp")
                .header("authorization", format!("Bearer {}", "x".repeat(32)))
                .header("content-type", "application/json")
                .header("accept", "application/json, text/event-stream")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    // Aucun filtrage : reponse upstream verbatim.
    assert_eq!(v["result"]["tools"][0]["name"], "vault_status");
}

#[tokio::test]
async fn appel_outil_inconnu_refuse_avant_upstream() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    let app = gateway_test();
    let body =
        r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"drop_database"}}"#;
    let res = app
        .oneshot(
            Request::post("/mcp")
                .header("authorization", format!("Bearer {}", "x".repeat(32)))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["id"], 7);
    assert_eq!(v["error"]["code"], -32000);
}

/// Pont fichier parité vault : `resource` égale exigée (comme
/// `AuthSettings.resource_server_url`) ; session existante acceptée.
#[tokio::test]
async fn pont_fichier_exige_resource_canonique() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;
    use vault_mcp_rs::{build_router_with_filestore, ServiceConfig, RESOURCE_URL};

    let dir = std::env::temp_dir().join(format!(
        "vault-pont-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let etat = dir.join("etat.json");
    let tok_ok = "synthetique-vault-pont-ok-0000000001";
    let tok_bad = "synthetique-vault-pont-ko-000000001";
    let doc = serde_json::json!({
        "demandes": {}, "codes": {},
        "acces": {
            tok_ok: {
                "jeton": tok_ok, "client_id": "client-synth",
                "scopes": ["mcp:lecture"], "resource": RESOURCE_URL,
                "expire_a": 9_999_999_999i64 },
            tok_bad: {
                "jeton": tok_bad, "client_id": "client-synth",
                "scopes": ["mcp:lecture"], "resource": "https://autre.example/mcp",
                "expire_a": 9_999_999_999i64 } },
        "rafraichissements": {},
    });
    std::fs::write(&etat, doc.to_string()).unwrap();
    let app_of = || {
        build_router_with_filestore(
            ServiceConfig {
                upstream: "http://127.0.0.1:9".to_string(),
                static_token: "x".repeat(32),
                static_token_scopes: vec![READ_SCOPE.to_string()],
                oauth: oauth_cfg(),
                max_body_bytes: 1024 * 1024,
            },
            Some(mcp_gateway::router::FileStoreMount {
                etat_path: etat.to_string_lossy().to_string(),
                expected_resource: None, // le pont vault impose le canonique
            }),
        )
        .expect("gateway de test")
    };
    let body = r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"outil-x"}}"#;
    // `resource` canonique : auth OK → refus local -32000 (pas 401).
    let res = app_of()
        .oneshot(
            Request::post("/mcp")
                .header("authorization", format!("Bearer {tok_ok}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["error"]["code"], -32000);
    // `resource` incohérente : 401 (parité Python).
    let res = app_of()
        .oneshot(
            Request::post("/mcp")
                .header("authorization", format!("Bearer {tok_bad}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let _ = std::fs::remove_dir_all(&dir);
}
