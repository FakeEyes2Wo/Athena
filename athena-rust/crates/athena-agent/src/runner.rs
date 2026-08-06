use crate::agent::{Agent, AgentContext, AgentError};
use athena_memory::ContextManager;
use athena_runtime::{RunnerError, TurnInput, TurnOutput, TurnRunner};
use std::sync::Arc;
use tokio::sync::{Mutex, watch};

/// Adapts an [`Agent`] to the runtime [`TurnRunner`] contract, giving each turn
/// its own context and bridging the cancellation token to a watch flag.
pub struct AgentRunner {
    agent: Arc<Agent>,
    context_limit: usize,
}

impl AgentRunner {
    pub fn new(agent: Arc<Agent>, context_limit: usize) -> Self {
        Self {
            agent,
            context_limit,
        }
    }
}

#[async_trait::async_trait]
impl TurnRunner for AgentRunner {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError> {
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let token = input.cancel.clone();
        tokio::spawn(async move {
            token.cancelled().await;
            let _ = cancel_tx.send(true);
        });

        let ctx = AgentContext {
            thread: input.thread,
            turn: input.turn,
            emit: input.emit,
            cancel: cancel_rx,
            memory: Arc::new(Mutex::new(ContextManager::new(self.context_limit))),
        };

        match self.agent.run(ctx).await {
            Ok(o) => Ok(TurnOutput {
                result_ref: o.result_ref,
                next_context_ref: o.next_context_ref,
            }),
            Err(AgentError::Cancelled) => Err(RunnerError("cancelled".into())),
            Err(e) => Err(RunnerError(e.to_string())),
        }
    }
}
