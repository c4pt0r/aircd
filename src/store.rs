use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::path::Path;
use std::sync::{Arc, Mutex};

pub type SharedConnection = Arc<Mutex<Connection>>;

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
