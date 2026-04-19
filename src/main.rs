mod auth;
mod history;
mod protocol;
mod server;
mod store;
mod tasks;
mod tokens;

use anyhow::{Context, Result};
use server::Server;
use std::sync::Arc;

fn load_tls_config(cert_path: &str, key_path: &str) -> Result<tokio_rustls::TlsAcceptor> {
    let cert_data =
        std::fs::read(cert_path).with_context(|| format!("read TLS cert: {cert_path}"))?;
    let key_data = std::fs::read(key_path).with_context(|| format!("read TLS key: {key_path}"))?;

    let certs: Vec<_> = rustls_pemfile::certs(&mut cert_data.as_slice())
        .collect::<std::result::Result<Vec<_>, _>>()
        .context("parse TLS certificates")?;
    if certs.is_empty() {
        anyhow::bail!("no certificates found in {cert_path}");
    }

    let key = rustls_pemfile::private_key(&mut key_data.as_slice())
        .context("parse TLS private key")?
        .ok_or_else(|| anyhow::anyhow!("no private key found in {key_path}"))?;

    let config = tokio_rustls::rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .context("build TLS server config")?;

    Ok(tokio_rustls::TlsAcceptor::from(Arc::new(config)))
}

#[tokio::main]
async fn main() -> Result<()> {
    let bind_addr = std::env::var("AIRCD_BIND").unwrap_or_else(|_| "127.0.0.1:6667".to_string());
    let db_path = std::env::var("AIRCD_DB").unwrap_or_else(|_| "aircd.sqlite3".to_string());

    let tls_cert = std::env::var("AIRCD_TLS_CERT").ok();
    let tls_key = std::env::var("AIRCD_TLS_KEY").ok();

    let db = store::open(db_path)?;
    let server = Server::new(db);

    match (tls_cert, tls_key) {
        (Some(cert), Some(key)) => {
            let acceptor = load_tls_config(&cert, &key)?;
            let tls_bind =
                std::env::var("AIRCD_TLS_BIND").unwrap_or_else(|_| "127.0.0.1:6697".to_string());
            // Run both plaintext and TLS listeners
            let plain_server = server.clone();
            let tls_server = server;
            tokio::try_join!(
                plain_server.listen(&bind_addr),
                tls_server.listen_tls(&tls_bind, acceptor),
            )?;
        }
        (Some(_), None) | (None, Some(_)) => {
            anyhow::bail!("both AIRCD_TLS_CERT and AIRCD_TLS_KEY must be set to enable TLS");
        }
        (None, None) => {
            server.listen(&bind_addr).await?;
        }
    }

    Ok(())
}
