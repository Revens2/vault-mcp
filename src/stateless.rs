//! Pont stateless → stateful (compatibilite clients sans session, ex. ChatGPT).
//!
//! Contexte : l'upstream Python (`vault_mcp/server.py`, `MCPServer` stateful)
//! exige `mcp-session-id` pour toute methode autre que `initialize` (400
//! `Missing session ID` sinon). Les autres MCP de la flotte (github, orch :
//! upstreams sans session) repondent 200 sans session, et certains clients
//! (connecteur ChatGPT `openai-mcp`) appellent `tools/*` directement, sans
//! `initialize` prealable — constate le 2026-09-16 sur `/vault/mcp`
//! (telemetrie : `tools/call search_vault` → 400, une seule tentative).
//!
//! Ce pont rend la facade aussi tolerante que github/orch, SANS toucher a
//! l'upstream (fuge) ni au protocole client :
//! * requete AVEC session, ou `initialize`, ou notification (sans `id`) :
//!   relais direct inchange (parite) ;
//! * requete AVEC `id`, SANS session, methode autre que `initialize`
//!   (`tools/list`, `tools/call`, `ping`, `resources/list`, ...) : la facade
//!   etablit (ou reutilise) une session upstream liee a ce credential, rejoue
//!   la requete avec, et renvoie la reponse SANS `mcp-session-id` (stateless vu
//!   du client, comme github/orch qui n'en renvoient pas).
//!
//! Contraintes de securite (fail-closed, zero secret) :
//! * le cache est indexe par l'empreinte SHA-256 du credential (jamais le
//!   credential lui-meme, jamais journalise) ; les jetons ne sont utilises
//!   que depuis la requete en cours, jamais stockes ;
//! * une session poolée ne sert que son credential d'origine (meme emetteur,
//!   meme magasin : l'upstream rejetterait de toute facon un melange en 404,
//!   auquel cas le pont re-etablit une fois puis relaie tel quel) ;
//! * TTL 10 min (< idle 30 min Python), capacite bornee (64), eviction du plus
//!   ancien ; echec d'etablissement = repli vers le relais direct (400
//!   parite), jamais d'erreur inventee ni de contournement d'authentification.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use tokio::sync::Mutex;

/// Duree de vie d'une session poolée (< `DEFAULT_SESSION_IDLE_TIMEOUT` 30 min
/// du SDK Python, marge large contre les 404 d'expiration).
pub const BRIDGE_TTL: Duration = Duration::from_secs(600);
/// Borne du cache (un credential actif = une entree ; volume ChatGPT faible).
pub const BRIDGE_CAP: usize = 64;
/// Version annoncee a l'upstream pour les sessions du pont (ere legacy 2025,
/// comme l'upstream fige ; le proxy reecrit deja 2026-07-28 vers celle-ci).
pub const BRIDGE_PROTOCOL_VERSION: &str = "2025-11-25";
/// Delai maximal d'etablissement d'une session upstream (boucle locale).
pub const BRIDGE_ESTABLISH_TIMEOUT: Duration = Duration::from_secs(10);

struct BridgeEntry {
    session: String,
    updated: Instant,
}

/// Cache des sessions upstream poolées, indexe par empreinte du credential.
pub struct StatelessBridge {
    inner: Mutex<HashMap<String, BridgeEntry>>,
}

impl StatelessBridge {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(HashMap::new()),
        }
    }

    /// Session poolée encore valide pour ce credential, le cas echeant.
    pub async fn get(&self, key: &str) -> Option<String> {
        let map = self.inner.lock().await;
        map.get(key).and_then(|e| {
            if e.updated.elapsed() < BRIDGE_TTL {
                Some(e.session.clone())
            } else {
                None
            }
        })
    }

    /// Memorise une session pour ce credential (evince les expirees puis la
    /// plus ancienne si plein). `session` vide = purge de l'entree.
    pub async fn put(&self, key: String, session: String) {
        let mut map = self.inner.lock().await;
        if session.is_empty() {
            map.remove(&key);
            return;
        }
        map.retain(|_, e| e.updated.elapsed() < BRIDGE_TTL);
        if map.len() >= BRIDGE_CAP && !map.contains_key(&key) {
            if let Some(victime) = map
                .iter()
                .min_by_key(|(_, e)| e.updated)
                .map(|(k, _)| k.clone())
            {
                map.remove(&victime);
            }
        }
        map.insert(
            key,
            BridgeEntry {
                session,
                updated: Instant::now(),
            },
        );
    }

    /// Evince une entree (apres 404 upstream, avant re-etablissement).
    pub async fn evict(&self, key: &str) {
        self.inner.lock().await.remove(key);
    }
}

impl Default for StatelessBridge {
    fn default() -> Self {
        Self::new()
    }
}

/// Empreinte hexadécimale SHA-256 du credential (index opaque, non reversible).
pub fn credential_key(authorization: &str) -> String {
    let mut h = Sha256::new();
    h.update(authorization.as_bytes());
    hex_encode(h.finalize())
}

fn hex_encode(bytes: impl AsRef<[u8]>) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.as_ref().len() * 2);
    for b in bytes.as_ref() {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Classification d'un corps de requete POST pour le pont.
#[derive(Debug, PartialEq, Eq)]
pub enum BridgeKind {
    /// `initialize` : le client etablit sa session lui-meme (relais direct).
    Initialize,
    /// Requete (avec `id`) autre que `initialize` : pontable si sans session.
    Request,
    /// Notification / reponse / corps non-requete : relais direct.
    Other,
}

/// Classe un corps JSON-RPC (octets deja valides par `decide_body` en amont ;
/// corps illisible = `Other`, relais direct parite).
pub fn classify_body(body: &[u8]) -> BridgeKind {
    let data: serde_json::Value = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(_) => return BridgeKind::Other,
    };
    if data.is_array() {
        return BridgeKind::Other;
    }
    let method = data.get("method").and_then(|m| m.as_str());
    match method {
        Some("initialize") => BridgeKind::Initialize,
        Some(_) => {
            let has_id = data.get("id").is_some_and(|id| !id.is_null());
            if has_id {
                BridgeKind::Request
            } else {
                BridgeKind::Other
            }
        }
        None => BridgeKind::Other,
    }
}

/// Corps d'`initialize` minimal servi a l'upstream pour le pont (ere legacy,
/// identite explicite `bridge` pour la telemetrie amont).
pub fn bridge_initialize_body() -> Vec<u8> {
    serde_json::json!({
        "jsonrpc": "2.0",
        "id": "vault-bridge-init",
        "method": "initialize",
        "params": {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vault-mcp-rs-bridge", "version": "0.2.1"},
        },
    })
    .to_string()
    .into_bytes()
}

/// Etablit une session upstream avec le credential de la requete en cours.
/// Ne lit jamais le corps (headers seuls, connexion refermee aussitot :
/// la session survit, liee au transport gere par le manager Python).
/// `None` = echec (l'appelant replie vers le relais direct, 400 parite).
pub async fn establish_session(
    client: &reqwest::Client,
    upstream: &str,
    authorization: &str,
    accept: Option<&str>,
    protocol_version: Option<&str>,
) -> Option<String> {
    let version = match protocol_version {
        Some(v) if !v.trim().is_empty() => {
            mcp_core::protocol::normalize_upstream_version(v.trim()).to_string()
        }
        _ => BRIDGE_PROTOCOL_VERSION.to_string(),
    };
    let url = format!("{upstream}/mcp");
    let resp = client
        .post(url)
        .header("content-type", "application/json")
        .header(
            "accept",
            accept.unwrap_or("application/json, text/event-stream"),
        )
        .header("mcp-protocol-version", version)
        .header("authorization", authorization)
        .header("user-agent", "vault-mcp-rs-bridge/0.2.1")
        .body(bridge_initialize_body())
        .timeout(BRIDGE_ESTABLISH_TIMEOUT)
        .send()
        .await
        .ok()?;
    if resp.status() != reqwest::StatusCode::OK {
        return None;
    }
    resp.headers()
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classification_pont() {
        assert_eq!(
            classify_body(br#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}"#),
            BridgeKind::Initialize
        );
        assert_eq!(
            classify_body(br#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#),
            BridgeKind::Request
        );
        assert_eq!(
            classify_body(
                br#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"x"}}"#
            ),
            BridgeKind::Request
        );
        assert_eq!(
            classify_body(br#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#),
            BridgeKind::Other
        );
        assert_eq!(
            classify_body(br#"{"jsonrpc":"2.0","id":4,"method":"ping"}"#),
            BridgeKind::Request
        );
        assert_eq!(classify_body(b"[1,2]"), BridgeKind::Other);
        assert_eq!(classify_body(b"pas du json"), BridgeKind::Other);
        assert_eq!(
            classify_body(br#"{"jsonrpc":"2.0","id":null,"method":"tools/list"}"#),
            BridgeKind::Other
        );
    }

    #[test]
    fn empreinte_stable_et_discriminante() {
        let a = credential_key("Bearer x");
        let b = credential_key("Bearer x");
        let c = credential_key("Bearer y");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|ch| ch.is_ascii_hexdigit()));
    }

    #[tokio::test]
    async fn cache_put_get_drop() {
        let bridge = StatelessBridge::new();
        assert!(bridge.get("k").await.is_none());
        bridge.put("k".to_string(), "s1".to_string()).await;
        assert_eq!(bridge.get("k").await.as_deref(), Some("s1"));
        bridge.put("k".to_string(), String::new()).await;
        assert!(bridge.get("k").await.is_none());
        bridge.put("k".to_string(), "s2".to_string()).await;
        bridge.evict("k").await;
        assert!(bridge.get("k").await.is_none());
    }

    #[tokio::test]
    async fn cache_evict_le_plus_ancien_a_capacite() {
        let bridge = StatelessBridge::new();
        for i in 0..(BRIDGE_CAP + 5) {
            bridge.put(format!("k{i:03}"), format!("s{i:03}")).await;
        }
        let map = bridge.inner.lock().await;
        assert!(map.len() <= BRIDGE_CAP, "borne respectee");
    }
}
