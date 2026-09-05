//! `athena-agent` — a streaming, tool-calling agent that implements the runtime
//! [`athena_runtime::TurnRunner`] contract.
//!
//! One turn injects a system prompt and user input, then samples the provider in
//! a loop: text deltas are emitted, tool calls are executed (concurrency-safe
//! tools in parallel, non-safe tools as strict serial barriers), and results are
//! written back to the context until the model returns plain text.

mod agent;
mod input;
mod openai_provider;
mod provider;
mod runner;
mod subagent;

pub use agent::{Agent, AgentContext, AgentError};
pub use input::{InputResolver, PlainInputResolver, RestrictedFileInputResolver};
pub use openai_provider::OpenAiProvider;
pub use provider::{AgentConfig, LlmProvider, ProviderEvent, to_api};
pub use runner::AgentRunner;
pub use subagent::{AgentControl, AgentEvent, AgentHandle, AgentResult};
