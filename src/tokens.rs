//! Shared token management primitives.
//!
//! A token has the textual form `airc_<token_id_hex>_<secret>` where:
//! - `token_id_hex` is a 16-byte random identifier hex-encoded (32 chars).
//!   It is a public, non-sequential lookup key stored in `principal_tokens.token_id`.
//! - `secret` is a 32-byte random secret hex-encoded (64 chars). Only its
//!   argon2id hash (`principal_tokens.token_hash`) is persisted.
//!
//! The `tokens` module is intentionally self-contained so it can be reused by
//! the upcoming CLI (PR B).

use anyhow::{anyhow, Result};
use argon2::password_hash::rand_core::OsRng as PasswordHashOsRng;
use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::Argon2;
use rand::rngs::OsRng;
use rand::RngCore;

/// Prefix used to distinguish new-format tokens from the legacy plaintext ones.
pub const TOKEN_PREFIX: &str = "airc_";

/// Number of random bytes in the `token_id` field (public lookup key).
const TOKEN_ID_BYTES: usize = 16;

/// Number of random bytes in the `secret` portion (hashed at rest).
const SECRET_BYTES: usize = 32;

/// Generate a fresh token.
///
/// Returns `(full_token, token_id_hex, secret)`, where `full_token` is the
/// user-visible `airc_<id>_<secret>` string. The caller is expected to hash
/// `secret` via [`hash`] before persisting it.
pub fn generate() -> (String, String, String) {
    let mut rng = OsRng;

    let mut token_id_bytes = [0u8; TOKEN_ID_BYTES];
    rng.fill_bytes(&mut token_id_bytes);
    let token_id_hex = hex_encode(&token_id_bytes);

    let mut secret_bytes = [0u8; SECRET_BYTES];
    rng.fill_bytes(&mut secret_bytes);
    let secret_hex = hex_encode(&secret_bytes);

    let full = format!("{TOKEN_PREFIX}{token_id_hex}_{secret_hex}");
    (full, token_id_hex, secret_hex)
}

/// Hash a token secret with argon2id and return a PHC-encoded string.
pub fn hash(secret: &str) -> Result<String> {
    let salt = SaltString::generate(&mut PasswordHashOsRng);
    let argon2 = Argon2::default();
    let phc = argon2
        .hash_password(secret.as_bytes(), &salt)
        .map_err(|error| anyhow!("argon2 hash failed: {error}"))?
        .to_string();
    Ok(phc)
}

/// Verify a secret against a PHC-encoded argon2 hash.
pub fn verify(secret: &str, phc: &str) -> Result<bool> {
    let parsed =
        PasswordHash::new(phc).map_err(|error| anyhow!("parse argon2 PHC string: {error}"))?;
    match Argon2::default().verify_password(secret.as_bytes(), &parsed) {
        Ok(()) => Ok(true),
        Err(argon2::password_hash::Error::Password) => Ok(false),
        Err(error) => Err(anyhow!("argon2 verify failed: {error}")),
    }
}

/// Parse a `airc_<id>_<secret>` token into `(token_id_hex, secret)`.
///
/// Returns `None` if the token does not start with `airc_` or otherwise does
/// not match the expected shape, so callers can fall back to the legacy path.
pub fn parse(token: &str) -> Option<(String, String)> {
    let body = token.strip_prefix(TOKEN_PREFIX)?;
    let (token_id, secret) = body.split_once('_')?;
    if token_id.is_empty() || secret.is_empty() {
        return None;
    }
    Some((token_id.to_string(), secret.to_string()))
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_produces_well_formed_token() {
        let (full, token_id, secret) = generate();
        assert!(full.starts_with(TOKEN_PREFIX));
        assert_eq!(token_id.len(), TOKEN_ID_BYTES * 2);
        assert_eq!(secret.len(), SECRET_BYTES * 2);
        assert_eq!(full, format!("{TOKEN_PREFIX}{token_id}_{secret}"));
    }

    #[test]
    fn generate_produces_unique_ids() {
        let (_, id_a, secret_a) = generate();
        let (_, id_b, secret_b) = generate();
        assert_ne!(id_a, id_b);
        assert_ne!(secret_a, secret_b);
    }

    #[test]
    fn parse_roundtrips_generated_token() {
        let (full, token_id, secret) = generate();
        let parsed = parse(&full).expect("parse succeeds");
        assert_eq!(parsed.0, token_id);
        assert_eq!(parsed.1, secret);
    }

    #[test]
    fn parse_rejects_non_airc_prefix() {
        assert!(parse("human-token").is_none());
        assert!(parse("bearer_abc_def").is_none());
        assert!(parse("").is_none());
    }

    #[test]
    fn parse_rejects_missing_separator() {
        assert!(parse("airc_nosecret").is_none());
    }

    #[test]
    fn parse_rejects_empty_halves() {
        assert!(parse("airc__secret").is_none());
        assert!(parse("airc_id_").is_none());
    }

    #[test]
    fn hash_and_verify_roundtrip() -> Result<()> {
        let (_, _, secret) = generate();
        let phc = hash(&secret)?;
        assert!(phc.starts_with("$argon2"));
        assert!(verify(&secret, &phc)?);
        assert!(!verify("wrong-secret", &phc)?);
        Ok(())
    }

    #[test]
    fn hash_produces_distinct_outputs_for_same_input() -> Result<()> {
        let secret = "same-secret";
        let a = hash(secret)?;
        let b = hash(secret)?;
        assert_ne!(a, b, "salt should randomize the PHC string");
        assert!(verify(secret, &a)?);
        assert!(verify(secret, &b)?);
        Ok(())
    }
}
