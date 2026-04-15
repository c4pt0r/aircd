use anyhow::{anyhow, Context, Result};
use rusqlite::{params, Connection};
use std::sync::{Arc, Mutex};

#[derive(Clone, Debug)]
pub struct Principal {
    pub id: String,
    pub nick: String,
    pub team: Option<String>,
}

#[derive(Clone)]
pub struct AuthStore {
    db: Arc<Mutex<Connection>>,
}

impl AuthStore {
    pub fn new(db: Arc<Mutex<Connection>>) -> Self {
        Self { db }
    }

    pub fn authenticate(&self, token: &str) -> Result<Principal> {
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
