use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

#[derive(Clone, Debug)]
pub struct StoredMessage {
    pub seq: i64,
    pub msg_id: String,
    pub channel: String,
    pub sender_id: String,
    pub body: String,
    pub created_at: i64,
}

#[derive(Clone)]
pub struct HistoryStore {
    db: Arc<Mutex<Connection>>,
}

impl HistoryStore {
    pub fn new(db: Arc<Mutex<Connection>>) -> Self {
        Self { db }
    }

    pub fn append(&self, channel: &str, sender_id: &str, body: &str) -> Result<StoredMessage> {
        let msg_id = format!("msg_{}", Uuid::new_v4().simple());
        let created_at = unix_timestamp();
        let db = self.db.lock().expect("sqlite mutex poisoned");

        db.execute(
            "INSERT INTO messages (msg_id, channel, sender_id, body, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![msg_id, channel, sender_id, body, created_at],
        )
        .context("insert message")?;

        let seq = db.last_insert_rowid();

        Ok(StoredMessage {
            seq,
            msg_id,
            channel: channel.to_string(),
            sender_id: sender_id.to_string(),
            body: body.to_string(),
            created_at,
        })
    }

    pub fn after(&self, channel: &str, after_seq: i64, limit: i64) -> Result<Vec<StoredMessage>> {
        let capped_limit = limit.clamp(1, 500);
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let mut statement = db
            .prepare(
                "SELECT seq, msg_id, channel, sender_id, body, created_at
                 FROM messages
                 WHERE channel = ?1 AND seq > ?2
                 ORDER BY seq ASC
                 LIMIT ?3",
            )
            .context("prepare history query")?;

        let messages = statement
            .query_map(params![channel, after_seq, capped_limit], |row| {
                Ok(StoredMessage {
                    seq: row.get(0)?,
                    msg_id: row.get(1)?,
                    channel: row.get(2)?,
                    sender_id: row.get(3)?,
                    body: row.get(4)?,
                    created_at: row.get(5)?,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;

        Ok(messages)
    }
}

pub fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before Unix epoch")
        .as_secs() as i64
}
