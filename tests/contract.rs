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

/// Pont fichier parité observée : le Python n'exige jamais `resource`
/// (`validate_token_resource` non posé) — session existante acceptée,
/// resource absente ou incohérente acceptée comme en prod.
#[tokio::test]
async fn pont_fichier_parite_observee_sans_controle_resource() {
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;
    use vault_mcp_rs::{build_router_with_filestore, ServiceConfig};

    let dir = std::env::temp_dir().join(format!(
        "vault-pont-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let etat = dir.join("etat.json");
    let tok_none = "synthetique-vault-pont-none-00000001";
    let tok_autre = "synthetique-vault-pont-autre-0000001";
    let doc = serde_json::json!({
        "demandes": {}, "codes": {},
        "acces": {
            tok_none: {
                "jeton": tok_none, "client_id": "client-synth",
                "scopes": ["mcp:lecture"], "resource": serde_json::Value::Null,
                "expire_a": 9_999_999_999i64 },
            tok_autre: {
                "jeton": tok_autre, "client_id": "client-synth",
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
                expected_resource: None,
            }),
        )
        .expect("gateway de test")
    };
    let body = r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"outil-x"}}"#;
    // `resource` nulle puis incohérente : auth OK → refus local -32000 (pas 401).
    for tok in [tok_none, tok_autre] {
        let res = app_of()
            .oneshot(
                Request::post("/mcp")
                    .header("authorization", format!("Bearer {tok}"))
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK, "{tok}");
        let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"]["code"], -32000, "{tok}");
    }
    // Opaque inconnu : toujours 401.
    let res = app_of()
        .oneshot(
            Request::post("/mcp")
                .header("authorization", "Bearer inconnu-synth-0123456789abcdef")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Pont stateless : un upstream stateful strict (400 sans session, comme le
/// Python) devient stateless vu du client (200 sans session, comme
/// github/orch), sans toucher aux flux avec session ni a `initialize`.
///
/// Le mock mime le manager Python : `initialize` sans session → 200 + session ;
/// autre methode sans session → 400 + session (comme `_create_error_response`
/// qui inclut toujours la session du transport) ; session inconnue → 404 ;
/// session connue → 200 par methode.
#[tokio::test]
async fn pont_stateless_client_sans_session() {
    use axum::body::Body;
    use axum::extract::Request;
    use axum::http::{HeaderMap, HeaderValue, StatusCode};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use tower::ServiceExt;

    const SESSION: &str = "mock-stateful-session-0001";
    let initializes = Arc::new(AtomicUsize::new(0));
    let compteur = Arc::clone(&initializes);
    let mock = axum::Router::new().route(
        "/mcp",
        axum::routing::post(move |req: Request| {
            let compteur = Arc::clone(&compteur);
            async move {
                let (parts, body) = req.into_parts();
                if parts.headers.get("authorization").and_then(|v| v.to_str().ok())
                    != Some(format!("Bearer {}", "x".repeat(32)).as_str())
                {
                    return (StatusCode::UNAUTHORIZED, HeaderMap::new(), Body::empty());
                }
                let bytes = axum::body::to_bytes(body, 65536).await.unwrap_or_default();
                let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_default();
                let method = v.get("method").and_then(|m| m.as_str()).unwrap_or("");
                let id = v.get("id").cloned().unwrap_or(serde_json::Value::Null);
                let session = parts
                    .headers
                    .get("mcp-session-id")
                    .and_then(|hv| hv.to_str().ok())
                    .unwrap_or("");
                let mut headers = HeaderMap::new();
                headers.insert("content-type", HeaderValue::from_static("application/json"));
                // Comme le Python : toute erreur porte la session du transport.
                headers.insert("mcp-session-id", HeaderValue::from_static(SESSION));
                if method == "initialize" && session.is_empty() {
                    compteur.fetch_add(1, Ordering::SeqCst);
                    let corps = serde_json::json!({"jsonrpc": "2.0", "id": id,
                        "result": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "serverInfo": {"name": "mock", "version": "0"}}});
                    return (StatusCode::OK, headers, Body::from(corps.to_string()));
                }
                if session != SESSION {
                    if session.is_empty() {
                        let corps = serde_json::json!({"jsonrpc": "2.0", "id": serde_json::Value::Null,
                            "error": {"code": -32600, "message": "Bad Request: Missing session ID"}});
                        return (StatusCode::BAD_REQUEST, headers, Body::from(corps.to_string()));
                    }
                    let corps = serde_json::json!({"jsonrpc": "2.0", "id": serde_json::Value::Null,
                        "error": {"code": -32600, "message": "Session not found"}});
                    return (StatusCode::NOT_FOUND, HeaderMap::new(), Body::from(corps.to_string()));
                }
                let corps = match method {
                    "tools/list" => serde_json::json!({"jsonrpc": "2.0", "id": id,
                        "result": {"tools": [{"name": "vault_status"}]}}),
                    _ => serde_json::json!({"jsonrpc": "2.0", "id": id,
                        "result": {"content": [{"type": "text", "text": "ok"}]}}),
                };
                (StatusCode::OK, headers, Body::from(corps.to_string()))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });

    use vault_mcp_rs::{build_router, ServiceConfig};
    let app_of = || {
        build_router(ServiceConfig {
            upstream: format!("http://127.0.0.1:{}", addr.port()),
            static_token: "x".repeat(32),
            static_token_scopes: vec![READ_SCOPE.to_string(), WRITE_SCOPE.to_string()],
            oauth: oauth_cfg(),
            max_body_bytes: 1024 * 1024,
        })
        .unwrap()
    };
    let auth = format!("Bearer {}", "x".repeat(32));
    let envoi = |app: axum::Router, corps: &str, session: Option<&str>| {
        let auth = auth.clone();
        let corps = corps.to_string();
        let session = session.map(str::to_string);
        async move {
            let mut b = Request::post("/mcp")
                .header("authorization", auth)
                .header("content-type", "application/json")
                .header("accept", "application/json, text/event-stream");
            if let Some(s) = session {
                b = b.header("mcp-session-id", s);
            }
            app.oneshot(b.body(Body::from(corps)).unwrap())
                .await
                .unwrap()
        }
    };

    // 1) tools/list SANS session → 200 ponte, reponse SANS session (stateless).
    // Une seule facade pour les cas pontes : le cache doit eviter tout nouvel
    // `initialize` upstream entre deux appels du meme credential.
    let app = app_of();
    let res = envoi(
        app.clone(),
        r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
        None,
    )
    .await;
    assert_eq!(res.status(), StatusCode::OK);
    assert!(
        res.headers().get("mcp-session-id").is_none(),
        "le client stateless ne recoit pas de session"
    );
    let bytes = axum::body::to_bytes(res.into_body(), 4096).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["result"]["tools"][0]["name"], "vault_status");
    assert_eq!(initializes.load(Ordering::SeqCst), 1);

    // 2) Meme appel : 200 via le cache, PAS de nouvel initialize upstream.
    let res = envoi(
        app.clone(),
        r#"{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}"#,
        None,
    )
    .await;
    assert_eq!(res.status(), StatusCode::OK);
    assert_eq!(
        initializes.load(Ordering::SeqCst),
        1,
        "session poolée reutilisee, pas de nouvel initialize"
    );

    // 3) initialize SANS session : relais direct, session conservee.
    let res = envoi(
        app_of(),
        r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}"#,
        None,
    )
    .await;
    assert_eq!(res.status(), StatusCode::OK);
    assert_eq!(
        res.headers()
            .get("mcp-session-id")
            .and_then(|v| v.to_str().ok()),
        Some(SESSION)
    );

    // 4) tools/list AVEC session inconnue : relais direct 404 verbatim.
    let res = envoi(
        app_of(),
        r#"{"jsonrpc":"2.0","id":5,"method":"tools/list","params":{}}"#,
        Some("session-inconnue-0000000000000000"),
    )
    .await;
    assert_eq!(res.status(), StatusCode::NOT_FOUND);

    // 5) tools/call connu SANS session → 200 ponte (meme facade, cache).
    let res = envoi(
        app,
        r#"{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"vault_status","arguments":{}}}"#,
        None,
    )
    .await;
    assert_eq!(res.status(), StatusCode::OK);
    assert_eq!(
        initializes.load(Ordering::SeqCst),
        2,
        "pas de 3e initialize : 1 pont (cas 1-2-5) + 1 direct (cas 3)"
    );

    // 6) Notification sans session : relais direct (le mock repond 400 + session).
    let res = envoi(
        app_of(),
        r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
        None,
    )
    .await;
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
}
