use crate::event::{EventDraft, EventJournal};
use crate::runner::{EmitError, EventSink, TurnInput, TurnRunner};
use crate::submission::{RunnerSignal, RuntimeError, ThreadState};
use athena_types::{
    ArtifactRef, AthenaThread, AthenaTurn, SessionId, ThreadId, ThreadStatus, TurnId, TurnStatus,
};
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

/// A command sent to a thread actor. Each carries a one-shot reply that resolves
/// when the command is *admitted or completed* — never when the whole turn ends.
pub(crate) enum Command {
    Start {
        turn_id: String,
        request_ref: ArtifactRef,
        reply: oneshot::Sender<Result<AthenaTurn, RuntimeError>>,
    },
    Interrupt {
        turn_id: String,
        reply: oneshot::Sender<Result<(), RuntimeError>>,
    },
    ForkSnapshot {
        after_turn_id: Option<String>,
        reply: oneshot::Sender<Result<ArtifactRef, RuntimeError>>,
    },
    Shutdown {
        reply: oneshot::Sender<()>,
    },
}

struct ActiveTurn {
    turn_id: String,
    cancel: CancellationToken,
    handle: JoinHandle<()>,
}

/// The single owner of one thread's state. Nothing else mutates it; terminal
/// events are appended only here.
pub(crate) struct ThreadActor {
    thread_id: ThreadId,
    session_id: SessionId,
    context_ref: ArtifactRef,
    runner: Arc<dyn TurnRunner>,
    journal: Arc<EventJournal>,
    state: Arc<AtomicU8>,
    /// Generation of the currently-emittable turn; `0` means none.
    active_gen: Arc<AtomicU64>,
    next_gen: u64,
    active: Option<ActiveTurn>,
    completed_contexts: HashMap<String, ArtifactRef>,
    cmd_rx: mpsc::Receiver<Command>,
    sig_tx: mpsc::Sender<RunnerSignal>,
    sig_rx: mpsc::Receiver<RunnerSignal>,
}

impl ThreadActor {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        thread_id: ThreadId,
        session_id: SessionId,
        context_ref: ArtifactRef,
        runner: Arc<dyn TurnRunner>,
        journal: Arc<EventJournal>,
        state: Arc<AtomicU8>,
        active_gen: Arc<AtomicU64>,
        cmd_rx: mpsc::Receiver<Command>,
    ) -> Self {
        let (sig_tx, sig_rx) = mpsc::channel(16);
        Self {
            thread_id,
            session_id,
            context_ref,
            runner,
            journal,
            state,
            active_gen,
            next_gen: 0,
            active: None,
            completed_contexts: HashMap::new(),
            cmd_rx,
            sig_tx,
            sig_rx,
        }
    }

    pub(crate) async fn run(mut self) {
        self.set_state(ThreadState::Idle);
        loop {
            tokio::select! {
                Some(sig) = self.sig_rx.recv() => self.handle_signal(sig),
                cmd = self.cmd_rx.recv() => match cmd {
                    Some(cmd) => {
                        if self.handle_command(cmd).await {
                            break;
                        }
                    }
                    None => break, // every handle dropped
                }
            }
        }
        self.stop_runner().await;
        self.set_state(ThreadState::Closed);
        self.active_gen.store(0, Ordering::SeqCst);
    }

    async fn handle_command(&mut self, cmd: Command) -> bool {
        match cmd {
            Command::Start {
                turn_id,
                request_ref,
                reply,
            } => {
                let result = self.start_turn(turn_id, request_ref);
                let _ = reply.send(result);
                false
            }
            Command::Interrupt { turn_id, reply } => {
                let matches = self.active.as_ref().is_some_and(|a| a.turn_id == turn_id);
                if matches {
                    self.commit_interrupted(&turn_id).await;
                    let _ = reply.send(Ok(()));
                } else {
                    let _ = reply.send(Err(RuntimeError::NoActiveTurn(turn_id)));
                }
                false
            }
            Command::ForkSnapshot {
                after_turn_id,
                reply,
            } => {
                let _ = reply.send(self.completed_snapshot(after_turn_id));
                false
            }
            Command::Shutdown { reply } => {
                if let Some(turn_id) = self.active.as_ref().map(|a| a.turn_id.clone()) {
                    self.commit_interrupted(&turn_id).await;
                }
                let _ = reply.send(());
                true
            }
        }
    }

    fn start_turn(
        &mut self,
        turn_id: String,
        request_ref: ArtifactRef,
    ) -> Result<AthenaTurn, RuntimeError> {
        if self.active.is_some() {
            return Err(RuntimeError::AlreadyRunning);
        }
        let turn_id_typed = TurnId::new(turn_id.clone())?;
        let turn = AthenaTurn {
            turn_id: turn_id_typed,
            thread_id: self.thread_id.clone(),
            request_ref: request_ref.clone(),
            status: TurnStatus::Running,
            result_ref: None,
        };
        self.append_lifecycle(&turn_id, "turn_started");

        // Allocate this turn's generation and publish it for the emit sink.
        self.next_gen += 1;
        let generation = self.next_gen;
        self.active_gen.store(generation, Ordering::SeqCst);

        let cancel = CancellationToken::new();
        let sink: Arc<dyn EventSink> = Arc::new(ActorEventSink {
            journal: self.journal.clone(),
            active_gen: self.active_gen.clone(),
            generation,
            turn_id: turn_id.clone(),
        });
        let thread = AthenaThread {
            thread_id: self.thread_id.clone(),
            session_id: self.session_id.clone(),
            status: ThreadStatus::Running,
            context_ref: self.context_ref.clone(),
        };
        let input = TurnInput {
            thread,
            turn: turn.clone(),
            emit: sink,
            cancel: cancel.clone(),
        };

        let runner = self.runner.clone();
        let sig_tx = self.sig_tx.clone();
        let cancel_probe = cancel.clone();
        let signal_turn = turn_id.clone();
        let handle = tokio::spawn(async move {
            let signal = match runner.run(input).await {
                Ok(out) => RunnerSignal::Succeeded {
                    turn_id: signal_turn,
                    next_context_ref: out.next_context_ref,
                },
                Err(_) if cancel_probe.is_cancelled() => RunnerSignal::Cancelled {
                    turn_id: signal_turn,
                },
                Err(e) => RunnerSignal::Failed {
                    turn_id: signal_turn,
                    error: e.0,
                },
            };
            let _ = sig_tx.send(signal).await;
        });

        self.active = Some(ActiveTurn {
            turn_id,
            cancel,
            handle,
        });
        self.set_state(ThreadState::Running);
        Ok(turn)
    }

    fn handle_signal(&mut self, sig: RunnerSignal) {
        match sig {
            RunnerSignal::Succeeded {
                turn_id,
                next_context_ref,
            } => {
                if self.is_active(&turn_id) {
                    self.active = None;
                    self.append_lifecycle(&turn_id, "turn_completed");
                    self.context_ref = next_context_ref.clone();
                    self.completed_contexts
                        .insert(turn_id.clone(), next_context_ref);
                    self.finish_turn();
                }
            }
            RunnerSignal::Failed { turn_id, error } => {
                if self.is_active(&turn_id) {
                    self.active = None;
                    tracing::debug!(turn = %turn_id, error, "turn failed");
                    self.append_lifecycle(&turn_id, "turn_failed");
                    self.finish_turn();
                }
            }
            RunnerSignal::Cancelled { turn_id } => {
                if self.is_active(&turn_id) {
                    self.active = None;
                    self.append_lifecycle(&turn_id, "turn_interrupted");
                    self.finish_turn();
                }
            }
        }
    }

    /// Commit the single interrupted terminal, then cancel and await the runner.
    /// The runner's later signal is ignored because no turn is active.
    async fn commit_interrupted(&mut self, turn_id: &str) {
        if let Some(active) = self.active.take() {
            self.append_lifecycle(turn_id, "turn_interrupted");
            self.finish_turn();
            active.cancel.cancel();
            let _ = active.handle.await;
        }
    }

    async fn stop_runner(&mut self) {
        if let Some(active) = self.active.take() {
            active.cancel.cancel();
            let _ = active.handle.await;
        }
        self.active_gen.store(0, Ordering::SeqCst);
    }

    fn finish_turn(&mut self) {
        self.active_gen.store(0, Ordering::SeqCst);
        if ThreadState::from_u8(self.state.load(Ordering::SeqCst)) == ThreadState::Running {
            self.set_state(ThreadState::Idle);
        }
    }

    fn completed_snapshot(
        &self,
        after_turn_id: Option<String>,
    ) -> Result<ArtifactRef, RuntimeError> {
        match after_turn_id {
            None => Ok(self.context_ref.clone()),
            Some(id) => self
                .completed_contexts
                .get(&id)
                .cloned()
                .ok_or(RuntimeError::UnknownSnapshot(id)),
        }
    }

    fn is_active(&self, turn_id: &str) -> bool {
        self.active.as_ref().is_some_and(|a| a.turn_id == turn_id)
    }

    fn append_lifecycle(&self, turn_id: &str, kind: &str) {
        self.journal.append(
            EventDraft::new(kind, format!("athena-event:{}", Uuid::new_v4().simple()))
                .turn(turn_id),
        );
    }

    fn set_state(&self, state: ThreadState) {
        self.state.store(state.as_u8(), Ordering::SeqCst);
    }
}

/// Per-turn event sink. Emits succeed only while the actor still regards this
/// turn's generation as active.
struct ActorEventSink {
    journal: Arc<EventJournal>,
    active_gen: Arc<AtomicU64>,
    generation: u64,
    turn_id: String,
}

#[async_trait::async_trait]
impl EventSink for ActorEventSink {
    async fn emit(&self, draft: EventDraft) -> Result<(), EmitError> {
        if self.active_gen.load(Ordering::SeqCst) != self.generation {
            return Err(EmitError);
        }
        let draft = EventDraft {
            turn_id: Some(self.turn_id.clone()),
            ..draft
        };
        self.journal.append(draft);
        Ok(())
    }
}
