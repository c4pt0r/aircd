use anyhow::{anyhow, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::tokens;

#[derive(Clone, Debug)]
pub struct Principal {
    pub id: String,
    pub nick: String,
    pub team: Option<String>,
}

#[derive(Clone)]
pub struct AuthStore {
    db: Arc<Mutex<Connection>>,
    legacy_hits: Arc<AtomicU64>,
}

impl AuthStore {
    pub fn new(db: Arc<Mutex<Connection>>) -> Self {
        Self {
            db,
            legacy_hits: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Authenticate an incoming token. Tries the new `principal_tokens` path
    /// first when the token has the `airc_` prefix, falls back to the legacy
    /// `principals.token` column so existing deployments keep working during
    /// the migration window. PR C will drop the legacy column once all clients
    /// have been rotated.
    pub fn authenticate(&self, token: &str) -> Result<Principal> {
        if let Some((token_id, secret)) = tokens::parse(token) {
            if let Some(principal) = self.authenticate_new(&token_id, &secret)? {
                return Ok(principal);
            }
        }

        let principal = self.authenticate_legacy(token)?;
        let total = self.legacy_hits.fetch_add(1, Ordering::Relaxed) + 1;
        eprintln!(
            "auth: legacy plaintext token accepted for principal {} (legacy_hits={})",
            principal.nick, total,
        );
        Ok(principal)
    }

    /// Total number of successful legacy-path authentications since startup.
    /// Used to gauge when the legacy column in `principals` can be dropped
    /// (PR C). Exposed for future CLI/status endpoints.
    #[allow(dead_code)]
    pub fn legacy_hit_count(&self) -> u64 {
        self.legacy_hits.load(Ordering::Relaxed)
    }

    fn authenticate_new(&self, token_id: &str, secret: &str) -> Result<Option<Principal>> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let row: Option<(String, String, String, Option<String>)> = db
            .query_row(
                "SELECT p.id, p.nick, t.token_hash, p.team
                 FROM principal_tokens t
                 JOIN principals p ON p.id = t.principal_id
                 WHERE t.token_id = ?1 AND t.revoked_at IS NULL",
                params![token_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                },
            )
            .optional()
            .context("principal_tokens lookup")?;

        let Some((id, nick, token_hash, team)) = row else {
            return Ok(None);
        };

        if tokens::verify(secret, &token_hash)? {
            Ok(Some(Principal { id, nick, team }))
        } else {
            Ok(None)
        }
    }

    fn authenticate_legacy(&self, token: &str) -> Result<Principal> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let mut statement = db
            .prepare("SELECT id, nick, team FROM principals WHERE token = ?1")
            .context("prepare principal lookup")?;

        let principal = statement
            .query_row(params![token], |row| {
                Ok(Principal {
                    id: row.get(0)?,
                    nick: row.get(1)?,
                    team: row.get(2)?,
                })
            })
            .map_err(|_| anyhow!("invalid token"))?;

        Ok(principal)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store;
    use rusqlite::params;

    #[test]
    fn legacy_plaintext_token_authenticates() -> Result<()> {
        let db = store::open(":memory:")?;
        let auth = AuthStore::new(db);

        let principal = auth.authenticate("human-token")?;
        assert_eq!(principal.id, "human");
        assert_eq!(principal.nick, "human");
        Ok(())
    }

    #[test]
    fn new_format_token_authenticates() -> Result<()> {
        let db = store::open(":memory:")?;
        let auth = AuthStore::new(db.clone());

        let (full, token_id_hex, secret) = tokens::generate();
        let token_hash = tokens::hash(&secret)?;
        {
            let conn = db.lock().expect("sqlite mutex poisoned");
            conn.execute(
                "INSERT INTO principal_tokens (principal_id, token_id, token_hash, label, created_at)
                 VALUES ('human', ?1, ?2, 'test', unixepoch())",
                params![token_id_hex, token_hash],
            )?;
        }

        let principal = auth.authenticate(&full)?;
        assert_eq!(principal.id, "human");
        assert_eq!(principal.nick, "human");
        Ok(())
    }

    #[test]
    fn revoked_new_token_is_rejected_but_legacy_still_works() -> Result<()> {
        let db = store::open(":memory:")?;
        let auth = AuthStore::new(db.clone());

        let (full, token_id_hex, secret) = tokens::generate();
        let token_hash = tokens::hash(&secret)?;
        {
            let conn = db.lock().expect("sqlite mutex poisoned");
            conn.execute(
                "INSERT INTO principal_tokens (principal_id, token_id, token_hash, label, created_at, revoked_at)
                 VALUES ('human', ?1, ?2, 'test', unixepoch(), unixepoch())",
                params![token_id_hex, token_hash],
            )?;
        }

        assert!(auth.authenticate(&full).is_err());
        // Legacy path still works independently.
        assert!(auth.authenticate("human-token").is_ok());
        Ok(())
    }

    #[test]
    fn invalid_token_errors() {
        let db = store::open(":memory:").expect("open db");
        let auth = AuthStore::new(db);
        assert!(auth.authenticate("does-not-exist").is_err());
        assert!(auth.authenticate("airc_deadbeef_nope").is_err());
    }

    #[test]
    fn legacy_hit_counter_tracks_successful_legacy_auth() -> Result<()> {
        let db = store::open(":memory:")?;
        let auth = AuthStore::new(db);
        assert_eq!(auth.legacy_hit_count(), 0);

        auth.authenticate("human-token")?;
        assert_eq!(auth.legacy_hit_count(), 1);

        auth.authenticate("agent-a-token")?;
        assert_eq!(auth.legacy_hit_count(), 2);

        // Invalid tokens must not bump the counter.
        let _ = auth.authenticate("does-not-exist");
        assert_eq!(auth.legacy_hit_count(), 2);
        Ok(())
    }
}
