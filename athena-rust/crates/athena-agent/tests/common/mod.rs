//! Shared fakes for agent tests: a scripted provider, recording sink, and tools.
#![allow(
    dead_code,
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::new_ret_no_self
)]

use athena_agent::{
    Agent, AgentConfig, AgentContext, LlmProvider, PlainInputResolver, ProviderEvent,
};
use athena_memory::ContextManager;
use athena_runtime::{EmitError, EventDraft, EventSink};
use athena_tools::{Tool, ToolContext, ToolError, ToolRegistry, ToolSpec};
use athena_types::{
    ArtifactRef, AthenaThread, AthenaTurn, SessionId, ThreadId, ThreadStatus, TurnId, TurnStatus,
};
use futures::stream::{BoxStream, StreamExt};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{Mutex, watch};

/// A provider that replays a scripted sequence of events per `stream` call.
pub struct FakeProvider {
    scripts: Mutex<VecDeque<Vec<ProviderEvent>>>,
}

impl FakeProvider {
    pub fn new(scripts: Vec<Vec<ProviderEvent>>) -> Arc<Self> {
        Arc::new(Self {
            scripts: Mutex::new(scripts.into()),
        })
    }
}

#[async_trait::async_trait]
impl LlmProvider for FakeProvider {
    async fn stream(
        &self,
        _config: &AgentConfig,
        _tool_specs: Vec<Value>,
        _messages: Vec<athena_memory::ModelMessage>,
        _cancel: watch::Receiver<bool>,
    ) -> BoxStream<'static, ProviderEvent> {
        let script = self.scripts.lock().await.pop_front().unwrap_or_default();
        futures::stream::iter(script).boxed()
    }
}

/// Records emitted event kinds.
pub struct RecordingSink {
    pub kinds: Arc<Mutex<Vec<String>>>,
}

impl RecordingSink {
    pub fn new() -> (Arc<Self>, Arc<Mutex<Vec<String>>>) {
        let kinds = Arc::new(Mutex::new(Vec::new()));
        (
            Arc::new(Self {
                kinds: kinds.clone(),
            }),
            kinds,
        )
    }
}

#[async_trait::async_trait]
impl EventSink for RecordingSink {
    async fn emit(&self, draft: EventDraft) -> Result<(), EmitError> {
        self.kinds.lock().await.push(draft.kind);
        Ok(())
    }
}

fn spec(name: &str, safe: bool) -> ToolSpec {
    ToolSpec {
        name: name.to_string(),
        description: format!("test tool {name}"),
        input_schema: json!({"type": "object"}),
        concurrency_safe: safe,
        max_result_chars: 50_000,
    }
}

/// Echoes its input.
pub struct EchoTool(ToolSpec);

impl EchoTool {
    pub fn new(name: &str, safe: bool) -> Arc<dyn Tool> {
        Arc::new(EchoTool(spec(name, safe)))
    }
}

#[async_trait::async_trait]
impl Tool for EchoTool {
    fn spec(&self) -> &ToolSpec {
        &self.0
    }
    async fn execute(&self, input: Value, _ctx: &ToolContext) -> Result<Value, ToolError> {
        Ok(json!({ "echo": input }))
    }
}

/// Records a [start, end] execution interval, sleeping in between.
pub struct IntervalTool {
    spec: ToolSpec,
    log: Arc<Mutex<Vec<(String, u128, u128)>>>,
    base: Instant,
    sleep_ms: u64,
}

impl IntervalTool {
    pub fn new(
        name: &str,
        safe: bool,
        log: Arc<Mutex<Vec<(String, u128, u128)>>>,
        base: Instant,
        sleep_ms: u64,
    ) -> Arc<dyn Tool> {
        Arc::new(IntervalTool {
            spec: spec(name, safe),
            log,
            base,
            sleep_ms,
        })
    }
}

#[async_trait::async_trait]
impl Tool for IntervalTool {
    fn spec(&self) -> &ToolSpec {
        &self.spec
    }
    async fn execute(&self, _input: Value, _ctx: &ToolContext) -> Result<Value, ToolError> {
        let start = self.base.elapsed().as_millis();
        tokio::time::sleep(std::time::Duration::from_millis(self.sleep_ms)).await;
        let end = self.base.elapsed().as_millis();
        self.log
            .lock()
            .await
            .push((self.spec.name.clone(), start, end));
        Ok(json!("done"))
    }
}

/// Sleeps far longer than any test runs, to keep an agent "in-flight".
pub struct SleeperTool(ToolSpec);

impl SleeperTool {
    pub fn new(name: &str) -> Arc<dyn Tool> {
        Arc::new(SleeperTool(spec(name, true)))
    }
}

#[async_trait::async_trait]
impl Tool for SleeperTool {
    fn spec(&self) -> &ToolSpec {
        &self.0
    }
    async fn execute(&self, _input: Value, _ctx: &ToolContext) -> Result<Value, ToolError> {
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        Ok(json!("done"))
    }
}

/// Build an `Agent` from a provider and a set of tools.
pub fn make_agent(provider: Arc<dyn LlmProvider>, tools: Vec<Arc<dyn Tool>>) -> Arc<Agent> {
    let mut registry = ToolRegistry::new();
    for tool in tools {
        registry = registry.register(tool).expect("unique tool");
    }
    Arc::new(Agent::new(
        AgentConfig::new("test-model", "You are a test agent."),
        provider,
        Arc::new(registry),
        Arc::new(PlainInputResolver),
    ))
}

/// Build an `AgentContext` with a fresh shared memory and a not-cancelled flag.
pub fn make_ctx(
    emit: Arc<dyn EventSink>,
    request: &str,
) -> (AgentContext, Arc<Mutex<ContextManager>>) {
    let (_cancel_tx, cancel_rx) = watch::channel(false);
    let memory = Arc::new(Mutex::new(ContextManager::new(100_000)));
    let ctx = AgentContext {
        thread: AthenaThread {
            thread_id: ThreadId::new("t1").unwrap(),
            session_id: SessionId::new("s1").unwrap(),
            status: ThreadStatus::Running,
            context_ref: ArtifactRef::new("ctx://init").unwrap(),
        },
        turn: AthenaTurn {
            turn_id: TurnId::new("turn-1").unwrap(),
            thread_id: ThreadId::new("t1").unwrap(),
            request_ref: ArtifactRef::new(request).unwrap(),
            status: TurnStatus::Running,
            result_ref: None,
        },
        emit,
        cancel: cancel_rx,
        memory: memory.clone(),
    };
    (ctx, memory)
}

pub fn text_delta(s: &str) -> ProviderEvent {
    ProviderEvent::TextDelta {
        delta: s.to_string(),
        accumulated: s.to_string(),
    }
}

pub fn tool_call(id: &str, name: &str) -> ProviderEvent {
    ProviderEvent::ToolCall {
        call_id: id.to_string(),
        name: name.to_string(),
        arguments: json!({}),
    }
}

pub fn completed() -> ProviderEvent {
    ProviderEvent::ResponseCompleted {
        finish_reason: "stop".to_string(),
        accumulated_text: String::new(),
    }
}
