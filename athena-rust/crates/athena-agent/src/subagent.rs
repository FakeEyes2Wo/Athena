use crate::agent::{Agent, AgentContext, AgentError};
use athena_memory::{ContextManager, MessagePart, MessageRole, ModelMessage};
use athena_runtime::{EmitError, EventDraft, EventSink};
use athena_types::{
    AthenaThread, AthenaTurn, SessionId, ThreadId, ThreadStatus, TurnId, TurnStatus,
};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::sync::{Mutex, Semaphore, mpsc, watch};
use tokio_util::sync::CancellationToken;

const SUB_CONTEXT_LIMIT: usize = 128_000;

/// A sub-agent's structured result.
#[derive(Debug, Clone)]
pub struct AgentResult {
    pub agent_id: String,
    pub result_ref: String,
    pub status: String,
}

/// An event forwarded from a running sub-agent.
#[derive(Debug, Clone)]
pub struct AgentEvent {
    pub kind: String,
    pub event_ref: String,
    pub data: Option<Value>,
}

/// Handle to a spawned sub-agent: its result, event stream, and cancellation.
pub struct AgentHandle {
    pub agent_id: String,
    result: watch::Receiver<Option<AgentResult>>,
    events: Mutex<mpsc::UnboundedReceiver<AgentEvent>>,
    cancel: CancellationToken,
}

impl AgentHandle {
    /// Await the sub-agent's terminal result.
    pub async fn wait(&self) -> AgentResult {
        let mut rx = self.result.clone();
        loop {
            if let Some(result) = rx.borrow().clone() {
                return result;
            }
            if rx.changed().await.is_err() {
                return AgentResult {
                    agent_id: self.agent_id.clone(),
                    result_ref: String::new(),
                    status: "unknown".into(),
                };
            }
        }
    }

    /// Receive the next event, or `None` once the sub-agent finished.
    pub async fn next_event(&self) -> Option<AgentEvent> {
        self.events.lock().await.recv().await
    }

    pub fn cancel(&self) {
        self.cancel.cancel();
    }
}

struct Entry {
    memory: Arc<Mutex<ContextManager>>,
    cancel: CancellationToken,
}

/// Manages sub-agents with a concurrency limit and isolated memory per agent.
pub struct AgentControl {
    sem: Arc<Semaphore>,
    inner: Arc<Mutex<HashMap<String, Entry>>>,
    counter: AtomicUsize,
}

impl AgentControl {
    pub fn new(max_concurrency: usize) -> Self {
        Self {
            sem: Arc::new(Semaphore::new(max_concurrency.max(1))),
            inner: Arc::new(Mutex::new(HashMap::new())),
            counter: AtomicUsize::new(0),
        }
    }

    /// Spawn a sub-agent running `task`, optionally forwarding events to `parent`.
    pub async fn spawn(
        &self,
        agent: Arc<Agent>,
        task: String,
        parent: Option<Arc<dyn EventSink>>,
    ) -> Result<AgentHandle, AgentError> {
        let permit = self
            .sem
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| AgentError::Invalid("agent control closed".into()))?;

        let id = self.counter.fetch_add(1, Ordering::SeqCst) + 1;
        let agent_id = format!("sub-{id}");
        let memory = Arc::new(Mutex::new(ContextManager::new(SUB_CONTEXT_LIMIT)));
        let cancel = CancellationToken::new();

        let thread = AthenaThread {
            thread_id: id_err(ThreadId::new(agent_id.clone()))?,
            session_id: id_err(SessionId::new("sub"))?,
            status: ThreadStatus::Running,
            context_ref: id_err(athena_types::ArtifactRef::new(format!("sub://{agent_id}")))?,
        };
        let request = if task.is_empty() {
            "task".to_string()
        } else {
            task
        };
        let turn = AthenaTurn {
            turn_id: id_err(TurnId::new(format!("{agent_id}-1")))?,
            thread_id: thread.thread_id.clone(),
            request_ref: id_err(athena_types::ArtifactRef::new(request))?,
            status: TurnStatus::Running,
            result_ref: None,
        };

        self.inner.lock().await.insert(
            agent_id.clone(),
            Entry {
                memory: memory.clone(),
                cancel: cancel.clone(),
            },
        );

        let (result_tx, result_rx) = watch::channel(None);
        let (events_tx, events_rx) = mpsc::unbounded_channel();
        let sink: Arc<dyn EventSink> = Arc::new(SubAgentSink {
            events: events_tx,
            parent,
        });
        let inner = self.inner.clone();
        let run_cancel = cancel.clone();
        let run_id = agent_id.clone();

        tokio::spawn(async move {
            let (_ctx_cancel_tx, ctx_cancel_rx) = watch::channel(false);
            let ctx = AgentContext {
                thread,
                turn,
                emit: sink,
                cancel: ctx_cancel_rx,
                memory,
            };
            let result = tokio::select! {
                r = agent.run(ctx) => match r {
                    Ok(o) => AgentResult { agent_id: run_id.clone(), result_ref: o.result_ref.as_str().to_string(), status: "completed".into() },
                    Err(_) => AgentResult { agent_id: run_id.clone(), result_ref: String::new(), status: "failed".into() },
                },
                _ = run_cancel.cancelled() => AgentResult { agent_id: run_id.clone(), result_ref: String::new(), status: "cancelled".into() },
            };
            let _ = result_tx.send(Some(result));
            drop(permit);
            inner.lock().await.remove(&run_id);
        });

        Ok(AgentHandle {
            agent_id,
            result: result_rx,
            events: Mutex::new(events_rx),
            cancel,
        })
    }

    /// Append a message to a running sub-agent's context.
    pub async fn send_message(&self, agent_id: &str, message: &str) -> Result<(), AgentError> {
        let memory = self
            .inner
            .lock()
            .await
            .get(agent_id)
            .map(|e| e.memory.clone());
        match memory {
            Some(mem) => {
                mem.lock().await.append(ModelMessage {
                    role: MessageRole::User,
                    parts: vec![MessagePart::UserPrompt {
                        content: message.to_string(),
                    }],
                });
                Ok(())
            }
            None => Err(AgentError::Invalid(format!("unknown agent: {agent_id}"))),
        }
    }

    pub async fn interrupt(&self, agent_id: &str) {
        if let Some(entry) = self.inner.lock().await.get(agent_id) {
            entry.cancel.cancel();
        }
    }

    pub async fn list_agents(&self) -> Vec<String> {
        self.inner.lock().await.keys().cloned().collect()
    }
}

fn id_err<T>(r: Result<T, athena_types::ValidationError>) -> Result<T, AgentError> {
    r.map_err(|e| AgentError::Invalid(e.to_string()))
}

struct SubAgentSink {
    events: mpsc::UnboundedSender<AgentEvent>,
    parent: Option<Arc<dyn EventSink>>,
}

#[async_trait::async_trait]
impl EventSink for SubAgentSink {
    async fn emit(&self, draft: EventDraft) -> Result<(), EmitError> {
        let _ = self.events.send(AgentEvent {
            kind: draft.kind.clone(),
            event_ref: draft.event_ref.clone(),
            data: draft.data.clone(),
        });
        if let Some(parent) = &self.parent {
            let _ = parent.emit(draft).await;
        }
        Ok(())
    }
}
