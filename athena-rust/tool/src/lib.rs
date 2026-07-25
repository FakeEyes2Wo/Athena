use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{watch, RwLock};

// ── Types ──

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

fn default_true() -> bool {
    true
}
fn default_max_chars() -> usize {
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    pub data: Option<Value>,
    #[serde(default = "default_true")]
    pub success: bool,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub artifacts: Vec<String>,
}

impl ToolResult {
    pub fn ok(data: Value) -> Self {
        Self {
            data: Some(data),
            success: true,
            error: None,
            truncated: false,
            artifacts: vec![],
        }
    }

    pub fn empty_ok() -> Self {
        Self {
            data: None,
            success: true,
            error: None,
            truncated: false,
            artifacts: vec![],
        }
    }

    pub fn err(error: impl Into<String>) -> Self {
        Self {
            data: None,
            success: false,
            error: Some(error.into()),
            truncated: false,
            artifacts: vec![],
        }
    }
}

/// Per-invocation context.
pub struct ToolContext {
    pub tool_name: String,
    pub call_id: String,
    pub cancel: watch::Receiver<bool>,
}

/// The tool trait -- implementors provide spec + async execute.
#[async_trait]
pub trait Tool: Send + Sync {
    fn spec(&self) -> &ToolSpec;
    async fn execute(&self, input: Value, ctx: &ToolContext) -> ToolResult;
}

// ── Registry ──

pub struct ToolRegistry {
    tools: RwLock<HashMap<String, Arc<dyn Tool>>>,
    sorted_names: RwLock<Vec<String>>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self {
            tools: RwLock::new(HashMap::new()),
            sorted_names: RwLock::new(Vec::new()),
        }
    }

    pub async fn register(&self, tool: Arc<dyn Tool>) -> Result<(), String> {
        let name = tool.spec().name.clone();
        let mut tools = self.tools.write().await;
        if tools.contains_key(&name) {
            return Err(format!("tool '{}' already registered", name));
        }
        tools.insert(name.clone(), tool);
        let mut sorted = self.sorted_names.write().await;
        sorted.push(name);
        sorted.sort();
        Ok(())
    }

    pub async fn resolve(&self, name: &str) -> Option<Arc<dyn Tool>> {
        self.tools.read().await.get(name).cloned()
    }

    pub async fn specs(&self) -> Vec<ToolSpec> {
        let tools = self.tools.read().await;
        let sorted = self.sorted_names.read().await;
        sorted
            .iter()
            .filter_map(|n| tools.get(n).map(|t| t.spec().clone()))
            .collect()
    }

    pub async fn len(&self) -> usize {
        self.tools.read().await.len()
    }
}
