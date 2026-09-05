#![allow(clippy::unwrap_used)]

use std::sync::Arc;

use serde_json::{Value, json};

use athena_tools::*;

// ── Mock tool ──

struct MockRegistryTool {
    spec: ToolSpec,
}

#[async_trait::async_trait]
impl Tool for MockRegistryTool {
    fn spec(&self) -> &ToolSpec {
        &self.spec
    }

    async fn execute(&self, _input: Value, _ctx: &ToolContext) -> Result<Value, ToolError> {
        Ok(Value::Null)
    }
}

fn make_tool(name: &str, description: &str) -> Arc<dyn Tool> {
    Arc::new(MockRegistryTool {
        spec: ToolSpec {
            name: name.into(),
            description: description.into(),
            input_schema: json!({"type": "object"}),
            concurrency_safe: true,
            max_result_chars: 50_000,
        },
    })
}

// ── Tests ──

#[test]
fn test_duplicate_registration_errors() {
    let registry = ToolRegistry::new()
        .register(make_tool("alpha", "first tool"))
        .unwrap();

    let err = match registry.register(make_tool("alpha", "duplicate")) {
        Ok(_) => panic!("duplicate registration should fail"),
        Err(err) => err,
    };
    assert!(
        err.contains("already registered"),
        "error message should mention duplicate: {}",
        err
    );
}

#[test]
fn test_registration_consumes_and_registry_is_read_only() {
    let registry = ToolRegistry::new()
        .register(make_tool("a", "tool a"))
        .unwrap()
        .register(make_tool("b", "tool b"))
        .unwrap();

    // Registry only exposes &self methods — no way to modify it
    assert_eq!(registry.len(), 2);
    assert!(registry.resolve("a").is_some());
    assert!(registry.resolve("b").is_some());
    assert!(registry.resolve("c").is_none());
}

#[test]
fn test_specs_sorted_order() {
    let registry = ToolRegistry::new()
        .register(make_tool("zebra", "striped animal"))
        .unwrap()
        .register(make_tool("alpha", "first letter"))
        .unwrap()
        .register(make_tool("beta", "second letter"))
        .unwrap();

    let specs: Vec<&str> = registry.specs().iter().map(|s| s.name.as_str()).collect();
    assert_eq!(specs, vec!["alpha", "beta", "zebra"]);
}

#[test]
fn test_search_by_name() {
    let registry = ToolRegistry::new()
        .register(make_tool("web_search", "Search the web"))
        .unwrap()
        .register(make_tool("file_read", "Read a file from disk"))
        .unwrap()
        .register(make_tool("bash_run", "Execute a bash command"))
        .unwrap();

    // Search by exact name
    let results = registry.search("web_search");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].name, "web_search");

    // Search by partial name (case-insensitive)
    let results = registry.search("SEARCH");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].name, "web_search");
}

#[test]
fn test_search_by_description() {
    let registry = ToolRegistry::new()
        .register(make_tool("web", "Search the web for information"))
        .unwrap()
        .register(make_tool("file", "Read and write files on disk"))
        .unwrap()
        .register(make_tool("bash", "Execute shell commands"))
        .unwrap();

    // Search by description substring
    let results = registry.search("disk");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].name, "file");

    // Search by partial description (case-insensitive)
    let results = registry.search("SHELL");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].name, "bash");
}

#[test]
fn test_search_empty_query() {
    let registry = ToolRegistry::new()
        .register(make_tool("a", "alpha tool"))
        .unwrap()
        .register(make_tool("b", "beta tool"))
        .unwrap();

    // Empty string matches everything (via substring check)
    let results = registry.search("");
    assert_eq!(results.len(), 2);
}

#[test]
fn test_search_no_match() {
    let registry = ToolRegistry::new()
        .register(make_tool("present", "this tool exists"))
        .unwrap();

    let results = registry.search("nonexistent");
    assert!(results.is_empty());
}

#[test]
fn test_empty_registry() {
    let registry = ToolRegistry::new();
    assert_eq!(registry.len(), 0);
    assert!(registry.is_empty());
    assert!(registry.specs().is_empty());
    assert!(registry.resolve("anything").is_none());
}
