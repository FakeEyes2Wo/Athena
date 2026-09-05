use athena_types::ValidationError;

/// Lifecycle state of a thread, mirrored to a shared atomic for handle reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThreadState {
    Idle,
    Running,
    Closing,
    Closed,
}

impl ThreadState {
    pub(crate) fn as_u8(self) -> u8 {
        match self {
            ThreadState::Idle => 0,
            ThreadState::Running => 1,
            ThreadState::Closing => 2,
            ThreadState::Closed => 3,
        }
    }

    pub(crate) fn from_u8(v: u8) -> ThreadState {
        match v {
            1 => ThreadState::Running,
            2 => ThreadState::Closing,
            3 => ThreadState::Closed,
            _ => ThreadState::Idle,
        }
    }
}

/// Error from a thread or manager operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuntimeError {
    /// A turn is already running; concurrent starts are rejected.
    AlreadyRunning,
    /// No active turn matches the given id.
    NoActiveTurn(String),
    /// Fork requested a snapshot that does not exist.
    UnknownSnapshot(String),
    /// The thread or manager is closed.
    Closed,
    /// An unknown thread id.
    UnknownThread(String),
    /// An input reference failed validation.
    Invalid(String),
}

impl std::fmt::Display for RuntimeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RuntimeError::AlreadyRunning => write!(f, "thread already has an active turn"),
            RuntimeError::NoActiveTurn(id) => write!(f, "no active turn: {id}"),
            RuntimeError::UnknownSnapshot(id) => write!(f, "no completed snapshot: {id}"),
            RuntimeError::Closed => write!(f, "thread is closed"),
            RuntimeError::UnknownThread(id) => write!(f, "unknown thread: {id}"),
            RuntimeError::Invalid(m) => write!(f, "invalid input: {m}"),
        }
    }
}

impl std::error::Error for RuntimeError {}

impl From<ValidationError> for RuntimeError {
    fn from(e: ValidationError) -> Self {
        RuntimeError::Invalid(e.to_string())
    }
}

/// Terminal signal sent by a runner task back to its thread actor.
pub(crate) enum RunnerSignal {
    Succeeded {
        turn_id: String,
        next_context_ref: athena_types::ArtifactRef,
    },
    Failed {
        turn_id: String,
        error: String,
    },
    Cancelled {
        turn_id: String,
    },
}
