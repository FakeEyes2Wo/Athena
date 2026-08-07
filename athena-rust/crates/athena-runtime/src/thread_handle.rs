use crate::event::EventJournal;
use crate::submission::{RuntimeError, ThreadState};
use crate::thread_actor::Command;
use athena_types::{ArtifactRef, AthenaTurn, SessionId, ThreadId};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use tokio::sync::{mpsc, oneshot};

/// The public boundary to a thread actor — no queues, tasks, or locks leak out.
#[derive(Clone)]
pub struct ThreadHandle {
    thread_id: ThreadId,
    session_id: SessionId,
    cmd_tx: mpsc::Sender<Command>,
    journal: Arc<EventJournal>,
    state: Arc<AtomicU8>,
}

impl ThreadHandle {
    pub(crate) fn new(
        thread_id: ThreadId,
        session_id: SessionId,
        cmd_tx: mpsc::Sender<Command>,
        journal: Arc<EventJournal>,
        state: Arc<AtomicU8>,
    ) -> Self {
        Self {
            thread_id,
            session_id,
            cmd_tx,
            journal,
            state,
        }
    }

    pub fn thread_id(&self) -> &ThreadId {
        &self.thread_id
    }

    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    pub fn journal(&self) -> &Arc<EventJournal> {
        &self.journal
    }

    pub fn state(&self) -> ThreadState {
        ThreadState::from_u8(self.state.load(Ordering::SeqCst))
    }

    /// Start a turn, returning once the turn is admitted (not once it finishes).
    pub async fn start_turn(
        &self,
        turn_id: String,
        request_ref: ArtifactRef,
    ) -> Result<AthenaTurn, RuntimeError> {
        self.request(|reply| Command::Start {
            turn_id,
            request_ref,
            reply,
        })
        .await?
    }

    /// Interrupt the given turn, committing a single interrupted terminal.
    pub async fn interrupt(&self, turn_id: String, _reason: String) -> Result<(), RuntimeError> {
        self.request(|reply| Command::Interrupt { turn_id, reply })
            .await?
    }

    /// Read the fork snapshot for a completed turn (or current context if `None`).
    pub async fn fork_snapshot(
        &self,
        after_turn_id: Option<String>,
    ) -> Result<ArtifactRef, RuntimeError> {
        self.request(|reply| Command::ForkSnapshot {
            after_turn_id,
            reply,
        })
        .await?
    }

    /// Gracefully shut down the thread and wait for the actor to finish.
    pub async fn shutdown(&self, _reason: &str) {
        if matches!(self.state(), ThreadState::Closed) {
            return;
        }
        let (reply, rx) = oneshot::channel();
        if self.cmd_tx.send(Command::Shutdown { reply }).await.is_err() {
            return;
        }
        let _ = rx.await;
    }

    /// Send a command and await its admission/completion reply, mapping a closed
    /// actor to `RuntimeError::Closed`.
    async fn request<T>(
        &self,
        make: impl FnOnce(oneshot::Sender<T>) -> Command,
    ) -> Result<T, RuntimeError> {
        if matches!(self.state(), ThreadState::Closing | ThreadState::Closed) {
            return Err(RuntimeError::Closed);
        }
        let (reply, rx) = oneshot::channel();
        if self.cmd_tx.send(make(reply)).await.is_err() {
            return Err(RuntimeError::Closed);
        }
        rx.await.map_err(|_| RuntimeError::Closed)
    }
}
