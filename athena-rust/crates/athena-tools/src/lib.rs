//! # athena-tools
//!
//! Tool system for the Athena agent framework.  Separates **business logic**
//! (`Tool` trait) from **lifecycle events** (`ToolExecutor`) and provides a
//! **read-only registry** built via the builder pattern.

pub mod context;
pub mod executor;
pub mod registry;
pub mod spec;
pub mod tool;

// Re-exports ────────────────────────────────────────────────────────────

pub use context::ToolContext;
pub use executor::ToolExecutor;
pub use registry::{ToolRegistry, ToolRegistryBuilder};
pub use spec::{ToolResult, ToolSpec};
pub use tool::{EventSink, TOOL_BEGIN, TOOL_END, TOOL_ERROR, Tool, ToolError};
