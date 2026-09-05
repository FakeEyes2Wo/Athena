//! # athena-tools
//!
//! Tool system for the Athena agent framework.  Separates **business logic**
//! (`Tool` trait) from **lifecycle events** (`ToolExecutor`) and provides a
//! immutable, deterministically ordered registry.

pub mod executor;
pub mod registry;
pub mod spec;
pub mod tool;

// Re-exports ────────────────────────────────────────────────────────────

pub use executor::ToolExecutor;
pub use registry::ToolRegistry;
pub use spec::{ToolResult, ToolSpec};
pub use tool::{EventSink, TOOL_BEGIN, TOOL_END, TOOL_ERROR, Tool, ToolContext, ToolError};
