use anyhow::{anyhow, Context, Result};
use rusqlite::params;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio_rustls::TlsAcceptor;

use crate::auth::{AuthStore, Principal};
use crate::history::{unix_timestamp, HistoryStore};
use crate::protocol::{parse_command, Command};
use crate::store::SharedConnection;
use crate::tasks::{Task, TaskStore};

const SERVER_NAME: &str = "aircd";
const METADATA_CAPABILITIES: &[&str] = &["message-tags"];

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

    pub async fn listen_tls(self, bind_addr: &str, acceptor: TlsAcceptor) -> Result<()> {
        let listener = TcpListener::bind(bind_addr)
            .await
            .with_context(|| format!("bind {bind_addr}"))?;
        println!("aircd listening on {bind_addr} (TLS)");

        loop {
            let (stream, peer_addr) = listener.accept().await?;
            let acceptor = acceptor.clone();
            let server = self.clone();
            tokio::spawn(async move {
                let tls_stream = match acceptor.accept(stream).await {
                    Ok(stream) => stream,
                    Err(error) => {
                        eprintln!("TLS handshake failed from {peer_addr}: {error:#}");
                        return;
                    }
                };
                if let Err(error) = server.handle_stream(tls_stream).await {
                    eprintln!("connection {peer_addr} closed: {error:#}");
                }
            });
        }
    }

    async fn handle_stream<S>(&self, stream: S) -> Result<()>
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let (reader, mut writer) = tokio::io::split(stream);
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
            Command::Cap {
                subcommand,
                capabilities,
            } => {
                self.cap(session, subcommand, capabilities)?;
            }
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

    fn cap(
        &self,
        session: &Session,
        subcommand: String,
        requested_capabilities: Vec<String>,
    ) -> Result<()> {
        let nick = session.nick.as_deref().unwrap_or("*");
        let advertised = METADATA_CAPABILITIES.join(" ");

        match subcommand.as_str() {
            "LS" => {
                session.send(format!(":{SERVER_NAME} CAP {nick} LS :{advertised}"));
            }
            "LIST" => {
                session.send(format!(":{SERVER_NAME} CAP {nick} LIST :{advertised}"));
            }
            "REQ" => {
                let requested = requested_capabilities.join(" ");
                let all_supported = requested_capabilities
                    .iter()
                    .all(|capability| METADATA_CAPABILITIES.contains(&capability.as_str()));
                let status = if all_supported { "ACK" } else { "NAK" };
                session.send(format!(":{SERVER_NAME} CAP {nick} {status} :{requested}"));
            }
            "END" => {}
            _ => {
                session.send(format!(
                    ":{SERVER_NAME} 410 {nick} {} :Invalid CAP command",
                    subcommand
                ));
            }
        }

        Ok(())
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
        let tags = metadata_tags(message.seq, &message.msg_id, message.created_at, false);
        self.broadcast_message(
            &channel,
            format!("{tags} :{} PRIVMSG {} :{}", principal.nick, channel, body),
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
                let tags = metadata_tags(message.seq, &message.msg_id, message.created_at, true);
                session.send(format!(
                    "{tags} :{} PRIVMSG {} :{}",
                    sender, message.channel, message.body
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
                "{} :{SERVER_NAME} NOTICE {} :TASK CREATE failed: title is required",
                task_failure_tags("create", "", &principal.nick, "title is required"),
                principal.nick
            ));
            return Ok(());
        }
        let task = self.tasks.create(&channel, title.trim())?;
        self.broadcast_task_event(
            &task,
            "create",
            "success",
            &principal.nick,
            format!("created by {}", principal.nick),
        )?;
        Ok(())
    }

    fn task_claim(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.claim(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    "{} :{SERVER_NAME} NOTICE {} :TASK CLAIM {} failed: {}",
                    task_failure_tags("claim", &task_id, &principal.nick, &error.to_string()),
                    principal.nick,
                    task_id,
                    error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(
            &task,
            "claim",
            "success",
            &principal.nick,
            format!("claimed by {}", principal.nick),
        )?;
        Ok(())
    }

    fn task_done(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.done(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    "{} :{SERVER_NAME} NOTICE {} :TASK DONE {} failed: {}",
                    task_failure_tags("done", &task_id, &principal.nick, &error.to_string()),
                    principal.nick,
                    task_id,
                    error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(
            &task,
            "done",
            "success",
            &principal.nick,
            format!("completed by {}", principal.nick),
        )?;
        Ok(())
    }

    fn task_release(&self, session: &Session, task_id: String) -> Result<()> {
        let principal = session.principal()?;
        let task = match self.tasks.release(&task_id, &principal.id) {
            Ok(task) => task,
            Err(error) => {
                session.send(format!(
                    "{} :{SERVER_NAME} NOTICE {} :TASK RELEASE {} failed: {}",
                    task_failure_tags("release", &task_id, &principal.nick, &error.to_string()),
                    principal.nick,
                    task_id,
                    error
                ));
                return Ok(());
            }
        };
        self.broadcast_task_event(
            &task,
            "release",
            "success",
            &principal.nick,
            format!("released by {}", principal.nick),
        )?;
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

    fn broadcast_task_event(
        &self,
        task: &Task,
        action: &str,
        result: &str,
        actor: &str,
        suffix: String,
    ) -> Result<()> {
        let body = format!("TASK {} {}: {}", task.id, suffix, task.title);
        let message = self.history.append(&task.channel, SERVER_NAME, &body)?;
        let tags = task_metadata_tags(
            message.seq,
            &message.msg_id,
            message.created_at,
            false,
            task,
            action,
            result,
            actor,
        );
        self.broadcast_message(
            &task.channel,
            format!("{tags} :{SERVER_NAME} NOTICE {} :{body}", task.channel),
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

        let should_mark_disconnected = {
            let mut inner = self.inner.lock().expect("server mutex poisoned");
            let should_remove = inner
                .clients
                .get(&principal.id)
                .map(|client| client.tx.same_channel(&session.tx))
                .unwrap_or(false);
            if should_remove {
                inner.clients.remove(&principal.id);
            }
            should_remove
        };

        if should_mark_disconnected {
            let db = self.db.lock().expect("sqlite mutex poisoned");
            db.execute(
                "UPDATE principals SET disconnected_at = ?1 WHERE id = ?2",
                params![unix_timestamp(), principal.id],
            )?;
        }

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
        let current_channel_seq: i64 = db.query_row(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE channel = ?1",
            params![channel],
            |row| row.get(0),
        )?;
        db.execute(
            "INSERT OR IGNORE INTO channel_memberships (principal_id, channel, joined_at, last_seen_seq)
             VALUES (?1, ?2, ?3, ?4)",
            params![principal_id, channel, unix_timestamp(), current_channel_seq],
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

#[cfg(test)]
mod tests {
    use super::{
        escape_tag_value, metadata_tags, task_failure_tags, task_metadata_tags, Server, Session,
        Task,
    };
    use anyhow::Result;
    use tokio::sync::mpsc;

    fn test_server() -> Result<Server> {
        let db = crate::store::open(":memory:")?;
        Ok(Server::new(db))
    }

    fn test_session(token: &str, nick: &str) -> Session {
        let (tx, _rx) = mpsc::unbounded_channel();
        let (shutdown_tx, _shutdown_rx) = mpsc::unbounded_channel();
        Session {
            session_id: format!("sess_{}", uuid::Uuid::new_v4().simple()),
            principal: None,
            nick: Some(nick.to_string()),
            pass_token: Some(token.to_string()),
            registered: false,
            tx,
            shutdown_tx,
        }
    }

    #[test]
    fn replaced_session_disconnect_does_not_mark_active_principal_disconnected() -> Result<()> {
        let server = test_server()?;
        let mut old_session = test_session("agent-a-token", "agent-a");
        let mut new_session = test_session("agent-a-token", "agent-a");

        server.try_register(&mut old_session)?;
        server.try_register(&mut new_session)?;
        server.disconnect(&old_session)?;

        let disconnected_at: Option<i64> =
            server.db.lock().expect("sqlite mutex poisoned").query_row(
                "SELECT disconnected_at FROM principals WHERE id = 'agent-a'",
                [],
                |row| row.get(0),
            )?;
        assert_eq!(disconnected_at, None);

        server.disconnect(&new_session)?;
        let disconnected_at: Option<i64> =
            server.db.lock().expect("sqlite mutex poisoned").query_row(
                "SELECT disconnected_at FROM principals WHERE id = 'agent-a'",
                [],
                |row| row.get(0),
            )?;
        assert!(disconnected_at.is_some());

        Ok(())
    }

    #[test]
    fn new_channel_membership_starts_at_current_channel_max_seq() -> Result<()> {
        let server = test_server()?;
        let before_join = server
            .history
            .append("#existing", "agent-a", "before join")?;
        let mut session = test_session("agent-b-token", "agent-b");

        server.try_register(&mut session)?;
        server.join(&session, "#existing".to_string())?;

        let last_seen_seq: i64 = server.db.lock().expect("sqlite mutex poisoned").query_row(
            "SELECT last_seen_seq FROM channel_memberships WHERE principal_id = 'agent-b' AND channel = '#existing'",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(last_seen_seq, before_join.seq);

        Ok(())
    }

    #[test]
    fn metadata_tags_use_ircv3_escaping() {
        assert_eq!(
            escape_tag_value("hello world;ok\\done\r\n"),
            "hello\\sworld\\:ok\\\\done\\r\\n"
        );
    }

    #[test]
    fn metadata_tags_include_replay_flag_when_requested() {
        assert_eq!(
            metadata_tags(42, "msg_abc", 1_776_250_000, true),
            "@seq=42;msg-id=msg_abc;time=1776250000;replay=1"
        );
    }

    #[test]
    fn task_metadata_tags_include_machine_readable_fields() {
        let task = Task {
            id: "task_1".to_string(),
            channel: "#work".to_string(),
            title: "fix it".to_string(),
            status: "claimed".to_string(),
            claimed_by: Some("agent-a".to_string()),
            lease_expires_at: Some(123),
        };

        assert_eq!(
            task_metadata_tags(
                42,
                "msg_abc",
                1_776_250_000,
                false,
                &task,
                "claim",
                "success",
                "agent a"
            ),
            "@seq=42;msg-id=msg_abc;time=1776250000;type=task;task-id=task_1;task-channel=#work;task-action=claim;task-result=success;task-status=claimed;task-actor=agent\\sa"
        );
    }

    #[test]
    fn task_failure_tags_escape_error_metadata() {
        assert_eq!(
            task_failure_tags("claim", "task_1", "agent-a", "bad; state"),
            "@type=task;task-id=task_1;task-action=claim;task-result=error;task-actor=agent-a;task-error=bad\\:\\sstate"
        );
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

fn metadata_tags(seq: i64, msg_id: &str, created_at: i64, replay: bool) -> String {
    let mut tags = vec![
        ("seq", seq.to_string()),
        ("msg-id", msg_id.to_string()),
        ("time", created_at.to_string()),
    ];
    if replay {
        tags.push(("replay", "1".to_string()));
    }
    format_message_tags(&tags)
}

fn task_metadata_tags(
    seq: i64,
    msg_id: &str,
    created_at: i64,
    replay: bool,
    task: &Task,
    action: &str,
    result: &str,
    actor: &str,
) -> String {
    let mut tags = vec![
        ("seq", seq.to_string()),
        ("msg-id", msg_id.to_string()),
        ("time", created_at.to_string()),
    ];
    if replay {
        tags.push(("replay", "1".to_string()));
    }
    tags.extend([
        ("type", "task".to_string()),
        ("task-id", task.id.clone()),
        ("task-channel", task.channel.clone()),
        ("task-action", action.to_string()),
        ("task-result", result.to_string()),
        ("task-status", task.status.clone()),
        ("task-actor", actor.to_string()),
    ]);
    format_message_tags(&tags)
}

fn task_failure_tags(action: &str, task_id: &str, actor: &str, error: &str) -> String {
    let tags = vec![
        ("type", "task".to_string()),
        ("task-id", task_id.to_string()),
        ("task-action", action.to_string()),
        ("task-result", "error".to_string()),
        ("task-actor", actor.to_string()),
        ("task-error", error.to_string()),
    ];
    format_message_tags(&tags)
}

fn format_message_tags(tags: &[(&str, String)]) -> String {
    format!(
        "@{}",
        tags.iter()
            .map(|(key, value)| format!("{key}={}", escape_tag_value(value)))
            .collect::<Vec<_>>()
            .join(";")
    )
}

fn escape_tag_value(value: &str) -> String {
    let mut escaped = String::new();
    for character in value.chars() {
        match character {
            ';' => escaped.push_str("\\:"),
            ' ' => escaped.push_str("\\s"),
            '\\' => escaped.push_str("\\\\"),
            '\r' => escaped.push_str("\\r"),
            '\n' => escaped.push_str("\\n"),
            character => escaped.push(character),
        }
    }
    escaped
}
