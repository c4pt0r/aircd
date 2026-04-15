use anyhow::{anyhow, Context, Result};
use rusqlite::params;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc::{self, UnboundedSender};

use crate::auth::{AuthStore, Principal};
use crate::history::{unix_timestamp, HistoryStore};
use crate::protocol::{parse_command, Command};
use crate::store::SharedConnection;
use crate::tasks::{Task, TaskStore};

const SERVER_NAME: &str = "aircd";

#[derive(Clone)]
pub struct Server {
    db: SharedConnection,
    auth: AuthStore,
    history: HistoryStore,
    tasks: TaskStore,
    inner: Arc<Mutex<ServerInner>>,
}

#[derive(Default)]
struct ServerInner {
    clients: HashMap<String, ClientHandle>,
    channels: HashMap<String, HashSet<String>>,
}

#[derive(Clone)]
struct ClientHandle {
    session_id: String,
    tx: UnboundedSender<String>,
    shutdown_tx: UnboundedSender<String>,
}

#[derive(Clone, Debug)]
struct Membership {
    channel: String,
    last_seen_seq: i64,
}

struct Session {
    session_id: String,
    principal: Option<Principal>,
    nick: Option<String>,
    pass_token: Option<String>,
    registered: bool,
    tx: UnboundedSender<String>,
    shutdown_tx: UnboundedSender<String>,
}

impl Server {
    pub fn new(db: SharedConnection) -> Self {
        let lease_seconds = std::env::var("AIRCD_TASK_LEASE_SECONDS")
            .ok()
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|value| *value > 0)
            .unwrap_or(300);

        Self {
            auth: AuthStore::new(db.clone()),
            history: HistoryStore::new(db.clone()),
            tasks: TaskStore::new(db.clone(), lease_seconds),
            db,
            inner: Arc::new(Mutex::new(ServerInner::default())),
        }
    }

    pub async fn listen(self, bind_addr: &str) -> Result<()> {
        let listener = TcpListener::bind(bind_addr)
            .await
            .with_context(|| format!("bind {bind_addr}"))?;
        println!("aircd listening on {bind_addr}");

        loop {
            let (stream, peer_addr) = listener.accept().await?;
            let server = self.clone();
            tokio::spawn(async move {
                if let Err(error) = server.handle_stream(stream).await {
                    eprintln!("connection {peer_addr} closed: {error:#}");
                }
            });
        }
    }

    async fn handle_stream(&self, stream: TcpStream) -> Result<()> {
        let (reader, mut writer) = stream.into_split();
        let mut lines = BufReader::new(reader).lines();
        let (tx, mut rx) = mpsc::unbounded_channel::<String>();
        let (shutdown_tx, mut shutdown_rx) = mpsc::unbounded_channel::<String>();

        let writer_task = tokio::spawn(async move {
            while let Some(line) = rx.recv().await {
                if writer.write_all(line.as_bytes()).await.is_err() {
                    break;
                }
                if writer.write_all(b"\r\n").await.is_err() {
                    break;
                }
            }
        });

        let mut session = Session {
            session_id: format!("sess_{}", uuid::Uuid::new_v4().simple()),
            principal: None,
            nick: None,
            pass_token: None,
            registered: false,
            tx,
            shutdown_tx,
        };

        session.send(format!(
            ":{SERVER_NAME} NOTICE * :Welcome to aircd prototype"
        ));

        loop {
            tokio::select! {
                line = lines.next_line() => {
                    let Some(line) = line? else {
                        break;
                    };
                    let command = parse_command(&line);
                    let should_quit = self.handle_command(&mut session, command).await?;
                    if should_quit {
                        break;
                    }
                }
                shutdown_reason = shutdown_rx.recv() => {
                    if let Some(reason) = shutdown_reason {
                        session.send(format!(":{SERVER_NAME} ERROR :{reason}"));
                    }
                    break;
                }
            }
        }

        self.disconnect(&session)?;
        writer_task.abort();
        Ok(())
    }

    async fn handle_command(&self, session: &mut Session, command: Command) -> Result<bool> {
        match command {
            Command::Pass(token) => {
                session.pass_token = Some(token);
                self.try_register(session)?;
            }
            Command::Nick(nick) => {
                session.nick = Some(nick);
                self.try_register(session)?;
            }
            Command::User(_) => {
                self.try_register(session)?;
            }
            Command::Ping(token) => {
                session.send(format!(":{SERVER_NAME} PONG {SERVER_NAME} :{token}"));
            }
            Command::Pong => {}
            Command::Join(channel) => {
                self.ensure_registered(session)?;
                self.join(session, normalize_channel(&channel))?;
            }
            Command::Part(channel) => {
                self.ensure_registered(session)?;
                self.part(session, normalize_channel(&channel))?;
            }
            Command::Privmsg { target, body } => {
                self.ensure_registered(session)?;
                self.privmsg(session, normalize_channel(&target), body)?;
            }
            Command::History {
                channel,
                after_seq,
                limit,
            } => {
                self.ensure_registered(session)?;
                let channels = match channel {
                    Some(channel) => vec![normalize_channel(&channel)],
                    None => self
                        .memberships(session.principal()?.id.as_str())?
                        .into_iter()
                        .map(|membership| membership.channel)
                        .collect(),
                };
                self.replay_history(session, &channels, after_seq, limit)?;
            }
            Command::TaskCreate { channel, title } => {
                self.ensure_registered(session)?;
                self.task_create(session, normalize_channel(&channel), title)?;
            }
            Command::TaskClaim(task_id) => {
                self.ensure_registered(session)?;
                self.task_claim(session, task_id)?;
            }
            Command::TaskDone(task_id) => {
                self.ensure_registered(session)?;
                self.task_done(session, task_id)?;
            }
            Command::TaskRelease(task_id) => {
                self.ensure_registered(session)?;
                self.task_release(session, task_id)?;
            }
            Command::TaskList(channel) => {
                self.ensure_registered(session)?;
                self.task_list(session, normalize_channel(&channel))?;
            }
            Command::Quit => return Ok(true),
            Command::Unknown(name) => {
                session.send(format!(":{SERVER_NAME} 421 * {name} :Unknown command"));
            }
        }

        Ok(false)
    }

    fn try_register(&self, session: &mut Session) -> Result<()> {
        if session.registered {
            return Ok(());
        }

        let Some(token) = session.pass_token.as_deref() else {
            return Ok(());
        };
        if session.nick.is_none() {
            return Ok(());
        }

        let principal = match self.auth.authenticate(token) {
            Ok(principal) => principal,
            Err(_) => {
                session.send(format!(":{SERVER_NAME} 464 * :Password incorrect"));
                return Ok(());
            }
        };

        session.nick = Some(principal.nick.clone());
        session.principal = Some(principal.clone());
        session.registered = true;

        let memberships = self.mark_connected_and_memberships(&principal.id)?;
        let channels = memberships
            .iter()
            .map(|membership| membership.channel.clone())
            .collect::<Vec<_>>();

        {
            let mut inner = self.inner.lock().expect("server mutex poisoned");
            if let Some(old_client) = inner.clients.insert(
                principal.id.clone(),
                ClientHandle {
                    session_id: session.session_id.clone(),
                    tx: session.tx.clone(),
                    shutdown_tx: session.shutdown_tx.clone(),
                },
            ) {
                let _ = old_client
                    .shutdown_tx
                    .send(format!("Replaced by a newer session for {}", principal.id));
            }

            for channel in &channels {
                inner
                    .channels
                    .entry(channel.clone())
                    .or_default()
                    .insert(principal.id.clone());
            }
        }

        session.send(format!(
            ":{SERVER_NAME} 001 {} :Welcome to aircd, {}{}",
            principal.nick,
            principal.nick,
            principal
                .team
                .as_ref()
                .map(|team| format!(" ({team})"))
                .unwrap_or_default()
        ));

        for channel in &channels {
            session.send(format!(":{} JOIN {}", principal.nick, channel));
        }

        if !memberships.is_empty() {
            session.send(format!(
                ":{SERVER_NAME} NOTICE {} :Replaying missed messages for {} channel(s)",
                principal.nick,
                memberships.len()
            ));
            for membership in memberships {
                self.replay_history(
                    session,
                    std::slice::from_ref(&membership.channel),
                    membership.last_seen_seq,
                    200,
                )?;
            }
        }

        Ok(())
    }

    fn join(&self, session: &Session, channel: String) -> Result<()> {
        let principal = session.principal()?;
        self.add_membership(&principal.id, &channel)?;

        {
            let mut inner = self.inner.lock().expect("server mutex poisoned");
            inner
                .channels
                .entry(channel.clone())
                .or_default()
                .insert(principal.id.clone());
        }

        self.broadcast_raw(&channel, format!(":{} JOIN {}", principal.nick, channel))?;
        Ok(())
    }

    fn part(&self, session: &Session, channel: String) -> Result<()> {
        let principal = session.principal()?;
        self.remove_membership(&principal.id, &channel)?;

        {
            let mut inner = self.inner.lock().expect("server mutex poisoned");
            if let Some(members) = inner.channels.get_mut(&channel) {
                members.remove(&principal.id);
            }
        }

        self.broadcast_raw(&channel, format!(":{} PART {}", principal.nick, channel))?;
        Ok(())
    }

    fn privmsg(&self, session: &Session, channel: String, body: String) -> Result<()> {
        let principal = session.principal()?;
        if !self.is_member(&principal.id, &channel)? {
            session.send(format!(
                ":{SERVER_NAME} 442 {} {} :You're not on that channel",
                principal.nick, channel
            ));
            return Ok(());
        }

        let message = self.history.append(&channel, &principal.id, &body)?;
        self.broadcast_message(
            &channel,
            format!(
                "@seq={};msg-id={};time={} :{} PRIVMSG {} :{}",
                message.seq, message.msg_id, message.created_at, principal.nick, channel, body
            ),
            message.seq,
        )?;
        Ok(())
    }

    fn replay_history(
        &self,
        session: &Session,
        channels: &[String],
        after_seq: i64,
        limit: i64,
    ) -> Result<()> {
        let principal = session.principal()?;

        for channel in channels {
            let mut channel_max_seq = after_seq;
            for message in self.history.after(channel, after_seq, limit)? {
                channel_max_seq = channel_max_seq.max(message.seq);
                let sender = self.display_name(&message.sender_id)?;
                session.send(format!(
                    "@seq={};msg-id={};time={};replay=1 :{} PRIVMSG {} :{}",
                    message.seq,
                    message.msg_id,
                    message.created_at,
                    sender,
                    message.channel,
                    message.body
                ));
            }

            if channel_max_seq > after_seq {
                self.mark_seen(&principal.id, channel, channel_max_seq)?;
            }
        }

        Ok(())
    }

    fn task_create(&self, session: &Session, channel: String, title: String) -> Result<()> {
        let principal = session.principal()?;
        if title.trim().is_empty() {
            session.send(format!(
                ":{SERVER_NAME} NOTICE {} :TASK CREATE failed: title is required",
                principal.nick
            ));
            return Ok(());
        }
        let task = self.tasks.create(&channel, title.trim())?;
        self.broadcast_system_event(
            &channel,
            format!(
                "TASK {} created by {}: {}",
                task.id, principal.nick, task.title
            ),
        )?;
        Ok(())
    }

    fn task_claim(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.claim(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    ":{SERVER_NAME} NOTICE {} :TASK CLAIM {} failed: {}",
                    principal.nick, task_id, error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(&task, format!("claimed by {}", principal.nick))?;
        Ok(())
    }

    fn task_done(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.done(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    ":{SERVER_NAME} NOTICE {} :TASK DONE {} failed: {}",
                    principal.nick, task_id, error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(&task, format!("completed by {}", principal.nick))?;
        Ok(())
    }

    fn task_release(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.release(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    ":{SERVER_NAME} NOTICE {} :TASK RELEASE {} failed: {}",
                    principal.nick, task_id, error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(&task, format!("released by {}", principal.nick))?;
        Ok(())
    }

    fn task_list(&self, session: &Session, channel: String) -> Result<()> {
        let principal = session.principal()?;
        let tasks = self.tasks.list(&channel)?;
        if tasks.is_empty() {
            session.send(format!(
                ":{SERVER_NAME} NOTICE {} :No open tasks in {}",
                principal.nick, channel
            ));
            return Ok(());
        }

        for task in tasks {
            session.send(format!(
                ":{SERVER_NAME} NOTICE {} :TASK {} channel={} status={} claimed_by={} lease_expires_at={} title=:{}",
                principal.nick,
                task.id,
                task.channel,
                task.status,
                task.claimed_by.unwrap_or_else(|| "-".to_string()),
                task.lease_expires_at
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "-".to_string()),
                task.title
            ));
        }

        Ok(())
    }

    fn broadcast_task_event(&self, task: &Task, suffix: String) -> Result<()> {
        self.broadcast_system_event(
            &task.channel,
            format!("TASK {} {}: {}", task.id, suffix, task.title),
        )
    }

    fn broadcast_system_event(&self, channel: &str, body: String) -> Result<()> {
        let message = self.history.append(channel, SERVER_NAME, &body)?;
        self.broadcast_message(
            channel,
            format!(
                "@seq={};msg-id={};time={} :{SERVER_NAME} NOTICE {channel} :{body}",
                message.seq, message.msg_id, message.created_at
            ),
            message.seq,
        )
    }

    fn broadcast_raw(&self, channel: &str, line: String) -> Result<()> {
        let recipients = self.recipients(channel);
        for recipient in recipients {
            let _ = recipient.tx.send(line.clone());
        }
        Ok(())
    }

    fn broadcast_message(&self, channel: &str, line: String, seq: i64) -> Result<()> {
        let recipients = self.recipients_with_ids(channel);
        for (principal_id, recipient) in recipients {
            if recipient.tx.send(line.clone()).is_ok() {
                self.mark_seen(&principal_id, channel, seq)?;
            }
        }
        Ok(())
    }

    fn recipients(&self, channel: &str) -> Vec<ClientHandle> {
        self.recipients_with_ids(channel)
            .into_iter()
            .map(|(_, client)| client)
            .collect()
    }

    fn recipients_with_ids(&self, channel: &str) -> Vec<(String, ClientHandle)> {
        let inner = self.inner.lock().expect("server mutex poisoned");
        let Some(members) = inner.channels.get(channel) else {
            return Vec::new();
        };

        members
            .iter()
            .filter_map(|principal_id| {
                inner
                    .clients
                    .get(principal_id)
                    .cloned()
                    .map(|client| (principal_id.clone(), client))
            })
            .collect()
    }

    fn disconnect(&self, session: &Session) -> Result<()> {
        let Some(principal) = session.principal.as_ref() else {
            return Ok(());
        };

        {
            let mut inner = self.inner.lock().expect("server mutex poisoned");
            let should_remove = inner
                .clients
                .get(&principal.id)
                .map(|client| client.tx.same_channel(&session.tx))
                .unwrap_or(false);
            if should_remove {
                inner.clients.remove(&principal.id);
            }
        }

        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "UPDATE principals SET disconnected_at = ?1 WHERE id = ?2",
            params![unix_timestamp(), principal.id],
        )?;

        Ok(())
    }

    fn ensure_registered(&self, session: &Session) -> Result<()> {
        if !session.registered {
            session.send(format!(":{SERVER_NAME} 451 * :You have not registered"));
            return Err(anyhow!("not registered"));
        }

        let principal = session.principal()?;
        let is_active = self
            .inner
            .lock()
            .expect("server mutex poisoned")
            .clients
            .get(&principal.id)
            .is_some_and(|client| client.session_id == session.session_id);

        if !is_active {
            session.send(format!(
                ":{SERVER_NAME} ERROR :Session replaced by a newer connection"
            ));
            return Err(anyhow!("session replaced"));
        }

        Ok(())
    }

    fn mark_connected_and_memberships(&self, principal_id: &str) -> Result<Vec<Membership>> {
        let now = unix_timestamp();
        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "UPDATE principals SET connected_at = ?1, disconnected_at = NULL WHERE id = ?2",
            params![now, principal_id],
        )?;

        let mut statement = db.prepare(
            "SELECT channel, last_seen_seq FROM channel_memberships WHERE principal_id = ?1 ORDER BY channel",
        )?;
        let memberships = statement
            .query_map(params![principal_id], |row| {
                Ok(Membership {
                    channel: row.get(0)?,
                    last_seen_seq: row.get(1)?,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(memberships)
    }

    fn memberships(&self, principal_id: &str) -> Result<Vec<Membership>> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let mut statement = db.prepare(
            "SELECT channel, last_seen_seq FROM channel_memberships WHERE principal_id = ?1 ORDER BY channel",
        )?;
        let memberships = statement
            .query_map(params![principal_id], |row| {
                Ok(Membership {
                    channel: row.get(0)?,
                    last_seen_seq: row.get(1)?,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(memberships)
    }

    fn add_membership(&self, principal_id: &str, channel: &str) -> Result<()> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "INSERT OR IGNORE INTO channel_memberships (principal_id, channel, joined_at)
             VALUES (?1, ?2, ?3)",
            params![principal_id, channel, unix_timestamp()],
        )?;
        Ok(())
    }

    fn remove_membership(&self, principal_id: &str, channel: &str) -> Result<()> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "DELETE FROM channel_memberships WHERE principal_id = ?1 AND channel = ?2",
            params![principal_id, channel],
        )?;
        Ok(())
    }

    fn is_member(&self, principal_id: &str, channel: &str) -> Result<bool> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        let count: i64 = db.query_row(
            "SELECT COUNT(*) FROM channel_memberships WHERE principal_id = ?1 AND channel = ?2",
            params![principal_id, channel],
            |row| row.get(0),
        )?;
        Ok(count > 0)
    }

    fn display_name(&self, principal_id: &str) -> Result<String> {
        if principal_id == SERVER_NAME {
            return Ok(SERVER_NAME.to_string());
        }

        let db = self.db.lock().expect("sqlite mutex poisoned");
        let nick = db.query_row(
            "SELECT nick FROM principals WHERE id = ?1",
            params![principal_id],
            |row| row.get(0),
        );

        Ok(nick.unwrap_or_else(|_| principal_id.to_string()))
    }

    fn mark_seen(&self, principal_id: &str, channel: &str, seq: i64) -> Result<()> {
        let db = self.db.lock().expect("sqlite mutex poisoned");
        db.execute(
            "UPDATE principals
             SET last_seen_seq = MAX(last_seen_seq, ?1)
             WHERE id = ?2",
            params![seq, principal_id],
        )?;
        db.execute(
            "UPDATE channel_memberships
             SET last_seen_seq = MAX(last_seen_seq, ?1)
             WHERE principal_id = ?2 AND channel = ?3",
            params![seq, principal_id, channel],
        )?;
        Ok(())
    }
}

impl Session {
    fn send(&self, line: String) {
        let _ = self.tx.send(line);
    }

    fn principal(&self) -> Result<&Principal> {
        self.principal
            .as_ref()
            .ok_or_else(|| anyhow!("session has no principal"))
    }
}

fn normalize_channel(channel: &str) -> String {
    let channel = channel.trim();
    if channel.starts_with('#') {
        channel.to_string()
    } else if channel.is_empty() {
        "#demo".to_string()
    } else {
        format!("#{channel}")
    }
}
