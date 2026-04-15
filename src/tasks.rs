use anyhow::{anyhow, Context, Result};
use rusqlite::{params, Connection};
use std::sync::{Arc, Mutex};
use uuid::Uuid;

use crate::history::unix_timestamp;

#[derive(Clone, Debug)]
pub struct Task {
    pub id: String,
    pub channel: String,
    pub title: String,
    pub status: String,
    pub claimed_by: Option<String>,
    pub lease_expires_at: Option<i64>,
}

#[derive(Clone)]
pub struct TaskStore {
    db: Arc<Mutex<Connection>>,
    lease_seconds: i64,
}

impl TaskStore {
    pub fn new(db: Arc<Mutex<Connection>>, lease_seconds: i64) -> Self {
        Self { db, lease_seconds }
    }

    pub fn create(&self, channel: &str, title: &str) -> Result<Task> {
        let now = unix_timestamp();
        let task = Task {
            id: format!("task_{}", Uuid::new_v4().simple()),
            channel: channel.to_string(),
            title: title.to_string(),
            status: "open".to_string(),
            claimed_by: None,
            lease_expires_at: None,
        };

        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "INSERT INTO tasks (id, channel, title, status, claimed_by, lease_expires_at, created_at, updated_at)
             VALUES (?1, ?2, ?3, 'open', NULL, NULL, ?4, ?4)",
            params![task.id, task.channel, task.title, now],
        )
        .context("insert task")?;

        Ok(task)
    }

    pub fn claim(&self, task_id: &str, principal_id: &str) -> Result<Task> {
        let now = unix_timestamp();
        let lease_expires_at = now + self.lease_seconds;
        let db = self.db.lock().expect("sqlite mutex poisoned");

        let changed = db
            .execute(
                "UPDATE tasks
                 SET status = 'claimed', claimed_by = ?1, lease_expires_at = ?2, updated_at = ?3
                 WHERE id = ?4
                   AND status != 'done'
                   AND (
                     status = 'open'
                     OR lease_expires_at IS NULL
                     OR lease_expires_at < ?3
                   )",
                params![principal_id, lease_expires_at, now, task_id],
            )
            .context("claim task")?;

        if changed != 1 {
            return Err(anyhow!("task is already claimed or done"));
        }

        Self::get_locked(&db, task_id)
    }

    pub fn done(&self, task_id: &str, principal_id: &str) -> Result<Task> {
        let now = unix_timestamp();
        let db = self.db.lock().expect("sqlite mutex poisoned");

        let changed = db
            .execute(
                "UPDATE tasks
                 SET status = 'done', updated_at = ?1
                 WHERE id = ?2 AND claimed_by = ?3 AND status = 'claimed'",
                params![now, task_id, principal_id],
            )
            .context("mark task done")?;

        if changed != 1 {
            return Err(anyhow!("task is not claimed by this principal"));
        }

        Self::get_locked(&db, task_id)
    }

    pub fn release(&self, task_id: &str, principal_id: &str) -> Result<Task> {
        let now = unix_timestamp();
        let db = self.db.lock().expect("sqlite mutex poisoned");

        let changed = db
            .execute(
                "UPDATE tasks
                 SET status = 'open', claimed_by = NULL, lease_expires_at = NULL, updated_at = ?1
                 WHERE id = ?2 AND claimed_by = ?3 AND status = 'claimed'",
                params![now, task_id, principal_id],
            )
            .context("release task")?;

        if changed != 1 {
            return Err(anyhow!("task is not claimed by this principal"));
        }

        Self::get_locked(&db, task_id)
    }

    pub fn list(&self, channel: &str) -> Result<Vec<Task>> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let mut statement = db
            .prepare(
                "SELECT id, channel, title, status, claimed_by, lease_expires_at
                 FROM tasks
                 WHERE channel = ?1 AND status != 'done'
                 ORDER BY created_at ASC",
            )
            .context("prepare task list")?;

        let tasks = statement
            .query_map(params![channel], |row| Self::from_row(row))?
            .collect::<std::result::Result<Vec<_>, _>>()?;

        Ok(tasks)
    }

    fn get_locked(db: &Connection, task_id: &str) -> Result<Task> {
        db.query_row(
            "SELECT id, channel, title, status, claimed_by, lease_expires_at
             FROM tasks WHERE id = ?1",
            params![task_id],
            Self::from_row,
        )
        .context("load task")
    }

    fn from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Task> {
        Ok(Task {
            id: row.get(0)?,
            channel: row.get(1)?,
            title: row.get(2)?,
            status: row.get(3)?,
            claimed_by: row.get(4)?,
            lease_expires_at: row.get(5)?,
        })
    }
}
