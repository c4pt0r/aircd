use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::tokens;

pub type SharedConnection = Arc<Mutex<Connection>>;

/// Label used to mark `principal_tokens` rows that were backfilled from the
/// legacy `principals.token` column. The uniqueness constraint below prevents
/// double backfills even if `init` runs repeatedly.
const LEGACY_BACKFILL_LABEL: &str = "legacy-backfill";

/// Label used for rows created alongside seeded demo principals.
const SEED_LABEL: &str = "seed";

pub fn open(path: impl AsRef<Path>) -> Result<SharedConnection> {
    let connection = Connection::open(path).context("open sqlite database")?;
    init(&connection)?;
    Ok(Arc::new(Mutex::new(connection)))
}

fn init(connection: &Connection) -> Result<()> {
    connection
        .execute_batch(
            "
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS principals (
              id TEXT PRIMARY KEY,
              nick TEXT NOT NULL,
              token TEXT UNIQUE NOT NULL,
              team TEXT,
              last_seen_seq INTEGER NOT NULL DEFAULT 0,
              connected_at INTEGER,
              disconnected_at INTEGER,
              created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_memberships (
              principal_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              joined_at INTEGER NOT NULL,
              last_seen_seq INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (principal_id, channel)
            );

            CREATE TABLE IF NOT EXISTS messages (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              msg_id TEXT UNIQUE NOT NULL,
              channel TEXT NOT NULL,
              sender_id TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              channel TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              claimed_by TEXT,
              lease_expires_at INTEGER,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS principal_tokens (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
              token_id TEXT UNIQUE NOT NULL,
              token_hash TEXT NOT NULL,
              label TEXT,
              created_at INTEGER NOT NULL,
              revoked_at INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_principal_tokens_principal
              ON principal_tokens (principal_id);

            -- Guarantees at most one backfill row per principal, even if init
            -- runs repeatedly against an already-populated database.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_principal_tokens_principal_label
              ON principal_tokens (principal_id, label);
            ",
        )
        .context("initialize sqlite schema")?;

    migrate_principals(connection)?;
    migrate_channel_memberships(connection)?;

    seed_principal(connection, "human", "human", "human-token", "demo")?;
    seed_principal(connection, "agent-a", "agent-a", "agent-a-token", "demo")?;
    seed_principal(connection, "agent-b", "agent-b", "agent-b-token", "demo")?;
    seed_principal(connection, "agent-c", "agent-c", "agent-c-token", "demo")?;
    seed_principal(connection, "agent-1", "agent-1", "test-token-1", "test")?;
    seed_principal(connection, "agent-2", "agent-2", "test-token-2", "test")?;
    seed_principal(connection, "agent-3", "agent-3", "test-token-3", "test")?;

    backfill_principal_tokens(connection)?;

    Ok(())
}

fn migrate_channel_memberships(connection: &Connection) -> Result<()> {
    let exists: i64 = connection.query_row(
        "SELECT COUNT(*) FROM pragma_table_info('channel_memberships') WHERE name = 'last_seen_seq'",
        [],
        |row| row.get(0),
    )?;

    if exists == 0 {
        connection.execute(
            "ALTER TABLE channel_memberships ADD COLUMN last_seen_seq INTEGER NOT NULL DEFAULT 0",
            [],
        )?;
    }

    Ok(())
}

fn seed_principal(
    connection: &Connection,
    id: &str,
    nick: &str,
    token: &str,
    team: &str,
) -> Result<()> {
    connection
        .execute(
            "INSERT OR IGNORE INTO principals (id, nick, token, team, created_at)
             VALUES (?1, ?2, ?3, ?4, unixepoch())",
            params![id, nick, token, team],
        )
        .context("seed principal")?;

    // Mirror the seeded token into `principal_tokens`. The SEED_LABEL paired
    // with the UNIQUE (principal_id, label) index keeps this idempotent.
    let existing: Option<i64> = connection
        .query_row(
            "SELECT id FROM principal_tokens WHERE principal_id = ?1 AND label = ?2",
            params![id, SEED_LABEL],
            |row| row.get(0),
        )
        .optional()
        .context("check existing seed principal_tokens row")?;

    if existing.is_none() {
        let (_token_id, token_id_hex, _) = tokens::generate();
        let token_hash = tokens::hash(token).context("hash seed token")?;
        connection
            .execute(
                "INSERT INTO principal_tokens (principal_id, token_id, token_hash, label, created_at)
                 VALUES (?1, ?2, ?3, ?4, unixepoch())",
                params![id, token_id_hex, token_hash, SEED_LABEL],
            )
            .context("seed principal_tokens row")?;
    }

    Ok(())
}

fn migrate_principals(connection: &Connection) -> Result<()> {
    let columns = [
        ("last_seen_seq", "INTEGER NOT NULL DEFAULT 0"),
        ("connected_at", "INTEGER"),
        ("disconnected_at", "INTEGER"),
    ];

    for (column, definition) in columns {
        let exists: i64 = connection.query_row(
            "SELECT COUNT(*) FROM pragma_table_info('principals') WHERE name = ?1",
            params![column],
            |row| row.get(0),
        )?;

        if exists == 0 {
            connection.execute(
                &format!("ALTER TABLE principals ADD COLUMN {column} {definition}"),
                [],
            )?;
        }
    }

    Ok(())
}

/// Mirror every legacy `principals.token` row into `principal_tokens` as a
/// `legacy-backfill` entry, keyed by a fresh random `token_id` with the
/// existing plaintext token hashed as `token_hash`. Principals that already
/// have ANY `principal_tokens` row are skipped — this covers both restart
/// idempotency and freshly seeded demo principals whose `seed` row is inserted
/// by [`seed_principal`] before this function runs. The UNIQUE
/// `(principal_id, label)` index provides a second line of defence.
fn backfill_principal_tokens(connection: &Connection) -> Result<()> {
    let mut select = connection
        .prepare(
            "SELECT p.id, p.token
             FROM principals p
             LEFT JOIN principal_tokens t ON t.principal_id = p.id
             WHERE p.token IS NOT NULL AND t.id IS NULL",
        )
        .context("prepare principal_tokens backfill query")?;

    let rows = select
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()
        .context("collect principals needing backfill")?;

    for (principal_id, plaintext) in rows {
        let (_, token_id_hex, _) = tokens::generate();
        let token_hash = tokens::hash(&plaintext).context("hash legacy token for backfill")?;
        connection
            .execute(
                "INSERT INTO principal_tokens (principal_id, token_id, token_hash, label, created_at)
                 VALUES (?1, ?2, ?3, ?4, unixepoch())",
                params![principal_id, token_id_hex, token_hash, LEGACY_BACKFILL_LABEL],
            )
            .context("insert legacy-backfill principal_tokens row")?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_creates_principal_tokens_table_and_seeds_mirrors() -> Result<()> {
        let connection = Connection::open_in_memory()?;
        init(&connection)?;

        let seed_count: i64 = connection.query_row(
            "SELECT COUNT(*) FROM principal_tokens WHERE label = ?1",
            params![SEED_LABEL],
            |row| row.get(0),
        )?;
        assert_eq!(seed_count, 7, "every seeded principal should have a row");

        let unique_ids: i64 = connection.query_row(
            "SELECT COUNT(DISTINCT token_id) FROM principal_tokens",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(unique_ids, seed_count);
        Ok(())
    }

    #[test]
    fn backfill_is_idempotent_across_reinit() -> Result<()> {
        let connection = Connection::open_in_memory()?;
        init(&connection)?;
        let first: i64 =
            connection.query_row("SELECT COUNT(*) FROM principal_tokens", [], |row| {
                row.get(0)
            })?;

        // Simulate a legacy principal inserted before principal_tokens existed.
        connection.execute(
            "INSERT INTO principals (id, nick, token, team, created_at)
             VALUES ('legacy', 'legacy', 'legacy-plaintext', 'ops', unixepoch())",
            [],
        )?;

        init(&connection)?;
        let second: i64 =
            connection.query_row("SELECT COUNT(*) FROM principal_tokens", [], |row| {
                row.get(0)
            })?;
        assert_eq!(second, first + 1, "one legacy row should be backfilled");

        init(&connection)?;
        let third: i64 =
            connection.query_row("SELECT COUNT(*) FROM principal_tokens", [], |row| {
                row.get(0)
            })?;
        assert_eq!(third, second, "backfill must not duplicate rows");

        let (label, hash): (String, String) = connection.query_row(
            "SELECT label, token_hash FROM principal_tokens WHERE principal_id = 'legacy'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        assert_eq!(label, LEGACY_BACKFILL_LABEL);
        assert!(tokens::verify("legacy-plaintext", &hash)?);
        Ok(())
    }
}
