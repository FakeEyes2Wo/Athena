use crate::input::InputResolver;
use crate::provider::{AgentConfig, LlmProvider, ProviderEvent};
use athena_memory::{ContextManager, MessagePart, MessageRole, ModelMessage};
use athena_runtime::{EventDraft, EventSink};
use athena_tools::{Tool, ToolContext, ToolError, ToolExecutor, ToolRegistry, ToolResult};
use athena_types::{ArtifactRef, AthenaThread, AthenaTurn};
use futures::StreamExt;
use serde_json::{Value, json};
use std::sync::Arc;
use tokio::sync::{Mutex, mpsc, watch};

/// Per-turn context. Memory is shared (a sub-agent's `send_message` may append
/// concurrently), mirroring the Python `AgentContext`.
pub struct AgentContext {
    pub thread: AthenaThread,
    pub turn: AthenaTurn,
    pub emit: Arc<dyn EventSink>,
    pub cancel: watch::Receiver<bool>,
    pub memory: Arc<Mutex<ContextManager>>,
}

/// The committed references of a finished agent run.
#[derive(Debug, Clone)]
pub struct AgentOutcome {
    pub result_ref: ArtifactRef,
    pub next_context_ref: ArtifactRef,
}

/// An error from an agent run.
#[derive(Debug, Clone)]
pub enum AgentError {
    Provider(String),
    Cancelled,
    Invalid(String),
}

impl std::fmt::Display for AgentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AgentError::Provider(m) => write!(f, "provider stream failed: {m}"),
            AgentError::Cancelled => write!(f, "agent cancelled"),
            AgentError::Invalid(m) => write!(f, "invalid: {m}"),
        }
    }
}

impl std::error::Error for AgentError {}

#[derive(Clone)]
struct ToolCall {
    call_id: String,
    name: String,
    arguments: Value,
}

enum Step {
    Done,
    Continue,
    Error(String),
}

/// A streaming, tool-calling agent. One `run` executes a ReAct-style loop:
/// inject system prompt + user input, then sample until the model returns plain
/// text. Concurrency-safe tools run in parallel; a non-safe tool is a strict
/// serial barrier.
pub struct Agent {
    config: AgentConfig,
    provider: Arc<dyn LlmProvider>,
    tools: Arc<ToolRegistry>,
    input: Arc<dyn InputResolver>,
}

impl Agent {
    pub fn new(
        config: AgentConfig,
        provider: Arc<dyn LlmProvider>,
        tools: Arc<ToolRegistry>,
        input: Arc<dyn InputResolver>,
    ) -> Self {
        Self {
            config,
            provider,
            tools,
            input,
        }
    }

    pub fn tools(&self) -> &Arc<ToolRegistry> {
        &self.tools
    }

    pub async fn run(&self, ctx: AgentContext) -> Result<AgentOutcome, AgentError> {
        // Bridge synchronous tool lifecycle events to the async event sink.
        let (tool_tx, mut tool_rx) = mpsc::unbounded_channel::<(String, String, Option<Value>)>();
        let emit = ctx.emit.clone();
        let forwarder = tokio::spawn(async move {
            while let Some((kind, call_id, data)) = tool_rx.recv().await {
                let mut draft = EventDraft::new(kind, format!("event:{call_id}"));
                draft.data = data;
                let _ = emit.emit(draft).await;
            }
        });
        let executor = ToolExecutor::new(Arc::new(ChannelSink { tx: tool_tx }));

        let outcome = self.run_loop(&ctx, &executor).await;

        drop(executor); // closes the bridge channel
        let _ = forwarder.await;
        outcome
    }

    async fn run_loop(
        &self,
        ctx: &AgentContext,
        executor: &ToolExecutor,
    ) -> Result<AgentOutcome, AgentError> {
        let user = self.input.resolve(ctx.turn.request_ref.as_str()).await;
        {
            let mut mem = ctx.memory.lock().await;
            if !has_system(&mem) {
                mem.append(ModelMessage {
                    role: MessageRole::System,
                    parts: vec![MessagePart::SystemPrompt {
                        content: self.config.system_prompt.clone(),
                    }],
                });
            }
            if !user.is_empty() {
                mem.append(ModelMessage {
                    role: MessageRole::User,
                    parts: vec![MessagePart::UserPrompt { content: user }],
                });
            }
        }

        for _ in 0..self.config.max_turns {
            if *ctx.cancel.borrow() {
                break;
            }
            match self.sampling_step(ctx, executor).await? {
                Step::Done => return Ok(self.outcome(ctx)),
                Step::Error(msg) => return Err(AgentError::Provider(msg)),
                Step::Continue => {}
            }
        }
        Ok(self.outcome(ctx))
    }

    fn outcome(&self, ctx: &AgentContext) -> AgentOutcome {
        let tid = ctx.turn.turn_id.as_str();
        AgentOutcome {
            result_ref: ArtifactRef::new(format!("result://{tid}"))
                .unwrap_or_else(|_| ctx.thread.context_ref.clone()),
            next_context_ref: ArtifactRef::new(format!("context://{tid}/next"))
                .unwrap_or_else(|_| ctx.thread.context_ref.clone()),
        }
    }

    async fn sampling_step(
        &self,
        ctx: &AgentContext,
        executor: &ToolExecutor,
    ) -> Result<Step, AgentError> {
        let messages = ctx.memory.lock().await.items();
        let tool_specs: Vec<Value> = self
            .tools
            .specs()
            .iter()
            .map(|s| s.to_openai_tool())
            .collect();

        let mut stream = self
            .provider
            .stream(&self.config, tool_specs, messages, ctx.cancel.clone())
            .await;

        let mut text = String::new();
        let mut calls: Vec<ToolCall> = Vec::new();
        let mut had_calls = false;

        while let Some(ev) = stream.next().await {
            match ev {
                ProviderEvent::TextDelta { delta, accumulated } => {
                    text = accumulated;
                    let _ = ctx
                        .emit
                        .emit(
                            EventDraft::new(
                                "agent/text_delta",
                                format!("event:{}", ctx.turn.turn_id.as_str()),
                            )
                            .data(json!({"delta": delta, "accumulated": text})),
                        )
                        .await;
                }
                ProviderEvent::ToolCall {
                    call_id,
                    name,
                    arguments,
                } => {
                    had_calls = true;
                    calls.push(ToolCall {
                        call_id,
                        name,
                        arguments,
                    });
                }
                ProviderEvent::ResponseCompleted { .. } => break,
                ProviderEvent::Error { message } => return Ok(Step::Error(message)),
            }
        }

        let results = self.run_tools(&calls, ctx, executor).await?;

        let mut mem = ctx.memory.lock().await;
        if had_calls && !calls.is_empty() {
            let mut parts = Vec::new();
            if !text.is_empty() {
                parts.push(MessagePart::Text {
                    content: text.clone(),
                });
            }
            for tc in &calls {
                parts.push(MessagePart::ToolCall {
                    tool_call_id: tc.call_id.clone(),
                    tool_name: tc.name.clone(),
                    arguments: serde_json::to_string(&tc.arguments).unwrap_or_else(|_| "{}".into()),
                });
            }
            mem.append(ModelMessage {
                role: MessageRole::Assistant,
                parts,
            });
        }
        for (tc, content) in calls.iter().zip(results.iter()) {
            mem.append(ModelMessage {
                role: MessageRole::Tool,
                parts: vec![MessagePart::ToolReturn {
                    tool_call_id: tc.call_id.clone(),
                    tool_name: tc.name.clone(),
                    content: content.clone(),
                }],
            });
        }

        if !had_calls && !text.is_empty() {
            mem.append(ModelMessage {
                role: MessageRole::Assistant,
                parts: vec![MessagePart::Text { content: text }],
            });
            return Ok(Step::Done);
        }
        if had_calls {
            return Ok(Step::Continue);
        }
        Ok(Step::Done)
    }

    /// Execute tool calls in order: consecutive concurrency-safe calls run as one
    /// parallel batch; each non-safe call is its own serial segment. This is the
    /// observable equivalent of the Python serial-barrier scheduling.
    async fn run_tools(
        &self,
        calls: &[ToolCall],
        ctx: &AgentContext,
        executor: &ToolExecutor,
    ) -> Result<Vec<String>, AgentError> {
        let mut results: Vec<String> = Vec::with_capacity(calls.len());
        let mut i = 0;
        while i < calls.len() {
            let non_safe = self
                .tools
                .resolve(&calls[i].name)
                .map(|t| !t.spec().concurrency_safe)
                .unwrap_or(false);

            if non_safe {
                let tool = self.resolve(&calls[i].name)?;
                results.push(self.invoke_one(executor, &tool, &calls[i], ctx).await?);
                i += 1;
            } else {
                let mut batch = Vec::new();
                while i < calls.len()
                    && self
                        .tools
                        .resolve(&calls[i].name)
                        .map(|t| t.spec().concurrency_safe)
                        .unwrap_or(true)
                {
                    batch.push(&calls[i]);
                    i += 1;
                }
                let futs = batch.iter().map(|tc| async move {
                    let tool = self.resolve(&tc.name)?;
                    self.invoke_one(executor, &tool, tc, ctx).await
                });
                for r in futures::future::join_all(futs).await {
                    results.push(r?);
                }
            }
        }
        Ok(results)
    }

    fn resolve(&self, name: &str) -> Result<Arc<dyn Tool>, AgentError> {
        self.tools
            .resolve(name)
            .cloned()
            .ok_or_else(|| AgentError::Provider(format!("unknown tool: {name}")))
    }

    async fn invoke_one(
        &self,
        executor: &ToolExecutor,
        tool: &Arc<dyn Tool>,
        tc: &ToolCall,
        ctx: &AgentContext,
    ) -> Result<String, AgentError> {
        let tool_ctx = ToolContext {
            tool_name: tc.name.clone(),
            call_id: format!("{}:{}", ctx.turn.turn_id.as_str(), tc.name),
            cancel: ctx.cancel.clone(),
        };
        match executor
            .invoke(tool.as_ref(), tc.arguments.clone(), &tool_ctx)
            .await
        {
            Ok(result) => Ok(tool_result_to_string(&result)),
            Err(ToolError::Cancelled) => Err(AgentError::Cancelled),
            Err(e) => Ok(format!("[ERROR] {e}")),
        }
    }
}

fn has_system(mem: &ContextManager) -> bool {
    mem.items().iter().any(|m| {
        m.parts
            .iter()
            .any(|p| matches!(p, MessagePart::SystemPrompt { .. }))
    })
}

fn tool_result_to_string(r: &ToolResult) -> String {
    if !r.success {
        return match &r.error {
            Some(e) => format!("[ERROR] {e}"),
            None => "[ERROR]".into(),
        };
    }
    match &r.data {
        None => "[OK]".into(),
        Some(Value::Object(_)) | Some(Value::Array(_)) => {
            serde_json::to_string(r.data.as_ref().unwrap_or(&Value::Null)).unwrap_or_default()
        }
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// Bridges the synchronous tool `EventSink` to the async runtime event sink.
struct ChannelSink {
    tx: mpsc::UnboundedSender<(String, String, Option<Value>)>,
}

impl athena_tools::EventSink for ChannelSink {
    fn emit(&self, kind: &str, call_id: &str, data: Option<Value>) {
        let _ = self.tx.send((kind.to_string(), call_id.to_string(), data));
    }
}
