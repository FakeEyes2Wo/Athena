use std::collections::HashMap;
use std::sync::Arc;

use crate::spec::ToolSpec;
use crate::tool::Tool;

// ── ToolRegistryBuilder ──

/// Builder for a read-only [`ToolRegistry`].
///
/// Once `build()` is called the builder is consumed and the registry is
/// immutable — no interior mutability, no `RwLock`, zero-cost reads.
pub struct ToolRegistryBuilder {
    tools: HashMap<String, Arc<dyn Tool>>,
}

impl ToolRegistryBuilder {
    pub fn new() -> Self {
        Self {
            tools: HashMap::new(),
        }
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

    /// Consume the builder and produce an immutable [`ToolRegistry`].
    pub fn build(self) -> ToolRegistry {
        let mut sorted_names: Vec<String> = self.tools.keys().cloned().collect();
        sorted_names.sort();

        ToolRegistry {
            tools: self.tools,
            sorted_names,
        }
    }
}

impl Default for ToolRegistryBuilder {
    fn default() -> Self {
        Self::new()
    }
}

// ── ToolRegistry ──

/// A read-only registry of tools, built via [`ToolRegistryBuilder`].
///
/// After construction the registry is immutable.  All query methods take
/// `&self` — no async, no locks.
pub struct ToolRegistry {
    tools: HashMap<String, Arc<dyn Tool>>,
    sorted_names: Vec<String>,
}

impl ToolRegistry {
    /// Look up a tool by name.
    pub fn resolve(&self, name: &str) -> Option<&Arc<dyn Tool>> {
        self.tools.get(name)
    }

    /// Return the specs of all registered tools, sorted alphabetically by
    /// name for stable prompt-cache ordering.
    pub fn specs(&self) -> Vec<&ToolSpec> {
        self.sorted_names
            .iter()
            .filter_map(|name| self.tools.get(name))
            .map(|tool| tool.spec())
            .collect()
    }

    /// Search tools by name or description (case-insensitive substring match).
    pub fn search(&self, query: &str) -> Vec<&ToolSpec> {
        let query_lower = query.to_lowercase();

        self.sorted_names
            .iter()
            .filter_map(|name| self.tools.get(name))
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
