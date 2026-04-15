mod auth;
mod history;
mod protocol;
mod server;
mod store;
mod tasks;

use anyhow::Result;
use server::Server;

#[tokio::main]
async fn main() -> Result<()> {
    let bind_addr = std::env::var("AIRCD_BIND").unwrap_or_else(|_| "127.0.0.1:6667".to_string());
    let db_path = std::env::var("AIRCD_DB").unwrap_or_else(|_| "aircd.sqlite3".to_string());

    let db = store::open(db_path)?;
    let server = Server::new(db);
    server.listen(&bind_addr).await
}
