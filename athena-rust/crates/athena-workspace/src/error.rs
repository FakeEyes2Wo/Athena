use std::fmt;

/// Error for every Git workspace operation. Messages are internal diagnostics;
/// callers translate them into protocol errors at the boundary.
#[derive(Debug, Clone)]
pub struct GitWorkspaceError(pub String);

impl GitWorkspaceError {
    pub(crate) fn new(msg: impl Into<String>) -> Self {
        Self(msg.into())
    }
}

impl fmt::Display for GitWorkspaceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for GitWorkspaceError {}
