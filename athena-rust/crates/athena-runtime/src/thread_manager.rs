use crate::event::EventJournal;
use crate::runner::TurnRunner;
use crate::submission::{RuntimeError, ThreadState};
use crate::thread_actor::ThreadActor;
use crate::thread_handle::ThreadHandle;
use athena_types::{ArtifactRef, AthenaThread, SessionId, ThreadId, ThreadStatus};
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, AtomicU64};
use tokio::sync::{Mutex, mpsc};
use uuid::Uuid;

#[derive(PartialEq, Eq, Clone, Copy)]
enum ManagerState {
    Alive,
    Closing,
    Closed,
}

struct Inner {
    handles: HashMap<String, ThreadHandle>,
    state: ManagerState,
}

/// Process-level thread registry and `ThreadActor` factory.
pub struct RuntimeThreadManager {
    runner: Arc<dyn TurnRunner>,
    inner: Mutex<Inner>,
}

impl RuntimeThreadManager {
    pub fn new(runner: Arc<dyn TurnRunner>) -> Self {
        Self {
            runner,
            inner: Mutex::new(Inner {
                handles: HashMap::new(),
                state: ManagerState::Alive,
            }),
        }
    }

    /// Start a new thread with its own journal and actor.
    pub async fn start(
        &self,
        session_id: &str,
        context_ref: &str,
    ) -> Result<AthenaThread, RuntimeError> {
        let session = SessionId::new(session_id)?;
        let ctx = ArtifactRef::new(context_ref)?;
        {
            let inner = self.inner.lock().await;
            if inner.state != ManagerState::Alive {
                return Err(RuntimeError::Closed);
            }
        }

        let thread_id = ThreadId::new(Uuid::new_v4().simple().to_string())?;
        let handle = spawn_thread(
            thread_id.clone(),
            session.clone(),
            ctx.clone(),
            self.runner.clone(),
        );

        let mut inner = self.inner.lock().await;
        if inner.state != ManagerState::Alive {
            drop(inner);
            handle.shutdown().await;
            return Err(RuntimeError::Closed);
        }
        inner.handles.insert(thread_id.as_str().to_string(), handle);
        Ok(AthenaThread {
            thread_id,
            session_id: session,
            status: ThreadStatus::Idle,
            context_ref: ctx,
        })
    }

    /// Submit a request as a new turn on `thread_id`.
    pub async fn submit(
        &self,
        thread_id: &str,
        request_ref: &str,
    ) -> Result<athena_types::AthenaTurn, RuntimeError> {
        let req = ArtifactRef::new(request_ref)?;
        let handle = self.get(thread_id).await?;
        let turn_id = Uuid::new_v4().simple().to_string();
        handle.start_turn(turn_id, req).await
    }

    /// Fork a child thread from a completed turn's snapshot.
    pub async fn fork(
        &self,
        thread_id: &str,
        after_turn_id: Option<&str>,
    ) -> Result<AthenaThread, RuntimeError> {
        let parent = {
            let inner = self.inner.lock().await;
            if inner.state != ManagerState::Alive {
                return Err(RuntimeError::Closed);
            }
            inner
                .handles
                .get(thread_id)
                .cloned()
                .ok_or_else(|| RuntimeError::UnknownThread(thread_id.to_string()))?
        };

        let context_ref = parent
            .fork_snapshot(after_turn_id.map(str::to_string))
            .await?;
        let session = parent.session_id().clone();
        let child_id = ThreadId::new(Uuid::new_v4().simple().to_string())?;
        let handle = spawn_thread(
            child_id.clone(),
            session.clone(),
            context_ref.clone(),
            self.runner.clone(),
        );

        let mut inner = self.inner.lock().await;
        if inner.state != ManagerState::Alive {
            drop(inner);
            handle.shutdown().await;
            return Err(RuntimeError::Closed);
        }
        inner.handles.insert(child_id.as_str().to_string(), handle);
        Ok(AthenaThread {
            thread_id: child_id,
            session_id: session,
            status: ThreadStatus::Idle,
            context_ref,
        })
    }

    /// Interrupt a turn on `thread_id`.
    pub async fn interrupt(&self, thread_id: &str, turn_id: &str) -> Result<(), RuntimeError> {
        let handle = self.get(thread_id).await?;
        handle.interrupt(turn_id.to_string()).await
    }

    /// Look up a live thread handle.
    pub async fn get(&self, thread_id: &str) -> Result<ThreadHandle, RuntimeError> {
        let inner = self.inner.lock().await;
        inner
            .handles
            .get(thread_id)
            .cloned()
            .ok_or_else(|| RuntimeError::UnknownThread(thread_id.to_string()))
    }

    /// Shut down every thread. Idempotent; does not hold the lock while waiting.
    pub async fn close(&self) {
        let handles = {
            let mut inner = self.inner.lock().await;
            if inner.state != ManagerState::Alive {
                return;
            }
            inner.state = ManagerState::Closing;
            inner.handles.values().cloned().collect::<Vec<_>>()
        };

        futures::future::join_all(handles.iter().map(ThreadHandle::shutdown)).await;

        let mut inner = self.inner.lock().await;
        inner.state = ManagerState::Closed;
        inner.handles.clear();
    }
}

/// Build a thread actor + its handle, spawning the actor task.
fn spawn_thread(
    thread_id: ThreadId,
    session_id: SessionId,
    context_ref: ArtifactRef,
    runner: Arc<dyn TurnRunner>,
) -> ThreadHandle {
    let journal = Arc::new(EventJournal::new(thread_id.as_str()));
    let state = Arc::new(AtomicU8::new(ThreadState::Idle.as_u8()));
    let active_gen = Arc::new(AtomicU64::new(0));
    let (cmd_tx, cmd_rx) = mpsc::channel(32);

    let actor = ThreadActor::new(
        thread_id.clone(),
        session_id.clone(),
        context_ref,
        runner,
        journal.clone(),
        state.clone(),
        active_gen,
        cmd_rx,
    );
    tokio::spawn(actor.run());

    ThreadHandle::new(session_id, cmd_tx, journal, state)
}
