use serde::{Deserialize, Serialize};
use serde_json::Value;

// ── ToolSpec ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
    #[serde(default = "default_true")]
    pub concurrency_safe: bool,
    #[serde(default = "default_max_chars")]
    pub max_result_chars: usize,
}

const fn default_true() -> bool {
    true
}

const fn default_max_chars() -> usize {
    50_000
}

impl ToolSpec {
    pub fn to_openai_tool(&self) -> Value {
        serde_json::json!({
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            }
        })
    }
}

// ── ToolResult ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolResult {
    pub data: Option<Value>,
    #[serde(default = "default_true")]
    pub success: bool,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub truncated: bool,
}
