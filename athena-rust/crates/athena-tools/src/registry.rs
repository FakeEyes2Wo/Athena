use std::collections::BTreeMap;
use std::sync::Arc;

use crate::spec::ToolSpec;
use crate::tool::Tool;

/// Immutable registry assembled through consuming registrations.
#[derive(Default)]
pub struct ToolRegistry {
    tools: BTreeMap<String, Arc<dyn Tool>>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a tool.
    ///
    /// Returns an error if a tool with the same `name` is already registered.
    pub fn register(mut self, tool: Arc<dyn Tool>) -> Result<Self, String> {
        let name = tool.spec().name.clone();
        if self.tools.contains_key(&name) {
            return Err(format!("tool '{}' already registered", name));
        }
        self.tools.insert(name, tool);
        Ok(self)
    }
    /// Look up a tool by name.
    pub fn resolve(&self, name: &str) -> Option<&Arc<dyn Tool>> {
        self.tools.get(name)
    }

    /// Return the specs of all registered tools, sorted alphabetically by
    /// name for stable prompt-cache ordering.
    pub fn specs(&self) -> Vec<&ToolSpec> {
        self.tools.values().map(|tool| tool.spec()).collect()
    }

    /// Search tools by name or description (case-insensitive substring match).
    pub fn search(&self, query: &str) -> Vec<&ToolSpec> {
        let query_lower = query.to_lowercase();

        self.tools
            .values()
            .filter(|tool| {
                let spec = tool.spec();
                spec.name.to_lowercase().contains(&query_lower)
                    || spec.description.to_lowercase().contains(&query_lower)
            })
            .map(|tool| tool.spec())
            .collect()
    }

    /// Number of registered tools.
    pub fn len(&self) -> usize {
        self.tools.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tools.is_empty()
    }
}
