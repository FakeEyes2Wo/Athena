use async_trait::async_trait;
use std::path::PathBuf;

/// Resolves a turn's `request_ref` into the user input text.
#[async_trait]
pub trait InputResolver: Send + Sync {
    async fn resolve(&self, request_ref: &str) -> String;
}

/// Returns the reference verbatim.
pub struct PlainInputResolver;

#[async_trait]
impl InputResolver for PlainInputResolver {
    async fn resolve(&self, request_ref: &str) -> String {
        request_ref.to_string()
    }
}

/// Reads `artifact://<path>` inputs, restricted to a canonicalized root. Any
/// failure or attempt to escape the root falls back to the raw reference.
pub struct RestrictedFileInputResolver {
    root: PathBuf,
}

impl RestrictedFileInputResolver {
    pub fn new(root: PathBuf) -> Self {
        let root = root.canonicalize().unwrap_or(root);
        Self { root }
    }
}

#[async_trait]
impl InputResolver for RestrictedFileInputResolver {
    async fn resolve(&self, request_ref: &str) -> String {
        if let Some(rest) = request_ref.strip_prefix("artifact://") {
            let candidate = self.root.join(rest);
            if let Ok(canon) = candidate.canonicalize()
                && canon.starts_with(&self.root)
                && let Ok(text) = tokio::fs::read_to_string(&canon).await
            {
                return text;
            }
        }
        request_ref.to_string()
    }
}
