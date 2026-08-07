use crate::experiment::Experiment;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

const INTERVAL_MIDDLE: &str = "===============";

/// Error for research-tree lookups and traversal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResearchError {
    /// A referenced node id is not present in the tree.
    UnknownNode(String),
    /// The parent→child links form a cycle.
    Cycle(String),
}

impl std::fmt::Display for ResearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ResearchError::UnknownNode(id) => write!(f, "node not found in research tree: {id}"),
            ResearchError::Cycle(id) => write!(f, "cycle detected in research tree at node: {id}"),
        }
    }
}

impl std::error::Error for ResearchError {}

/// A single node in the research tree, wrapping one experiment.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResearchTreeNode {
    pub exp: Experiment,
    pub id: String,
    #[serde(default)]
    pub parent_id: Option<String>,
    #[serde(default)]
    pub children_ids: Vec<String>,
}

impl ResearchTreeNode {
    /// Render this node's experiment prompt.
    pub fn to_prompt(&self) -> String {
        format!("\n==> Research Node\n{}\n", self.exp.to_prompt())
    }
}

/// The research tree: alternating experiment nodes forming a causal chain.
///
/// Only current behaviour is modelled — no persistence, ranking, or proximity.
#[derive(Debug, Default)]
pub struct ResearchTree {
    nodes: HashMap<String, ResearchTreeNode>,
}

impl ResearchTree {
    pub fn new() -> Self {
        Self::default()
    }

    /// Create a node, register it, and link it to its parent (if any).
    pub fn create_node(
        &mut self,
        exp: Experiment,
        parent_id: Option<&str>,
    ) -> Result<ResearchTreeNode, ResearchError> {
        if let Some(pid) = parent_id
            && !self.nodes.contains_key(pid)
        {
            return Err(ResearchError::UnknownNode(pid.to_string()));
        }

        let mut node_id = Uuid::new_v4().simple().to_string();
        while self.nodes.contains_key(&node_id) {
            node_id = Uuid::new_v4().simple().to_string();
        }

        let node = ResearchTreeNode {
            exp,
            id: node_id.clone(),
            parent_id: parent_id.map(str::to_string),
            children_ids: Vec::new(),
        };
        self.nodes.insert(node_id.clone(), node.clone());
        if let Some(pid) = parent_id
            && let Some(parent) = self.nodes.get_mut(pid)
        {
            parent.children_ids.push(node_id);
        }
        Ok(node)
    }

    /// Look up a node by id.
    pub fn get_node_by_id(&self, node_id: &str) -> Result<&ResearchTreeNode, ResearchError> {
        self.nodes
            .get(node_id)
            .ok_or_else(|| ResearchError::UnknownNode(node_id.to_string()))
    }

    /// Build the leaf→root experiment path prompt. At the first fork encountered,
    /// the sibling experiments of that fork are injected as diverging branches to
    /// steer the next hypothesis away from them.
    pub fn get_prompt(&self, node_id: &str) -> Result<String, ResearchError> {
        let mut visited: HashSet<String> = HashSet::new();
        let mut parts: Vec<String> = Vec::new();
        let mut branch_handled = false;
        let mut current_id = node_id.to_string();

        loop {
            if !visited.insert(current_id.clone()) {
                return Err(ResearchError::Cycle(current_id));
            }
            let current = self.get_node_by_id(&current_id)?;
            parts.push(format!("{INTERVAL_MIDDLE}{}", current.to_prompt()));

            let parent_id = match &current.parent_id {
                None => break,
                Some(p) => p.clone(),
            };
            let parent = self.get_node_by_id(&parent_id)?;

            if !branch_handled && parent.children_ids.len() > 1 {
                branch_handled = true;
                parts.push(format!(
                    "{INTERVAL_MIDDLE}对于当前实验，我们做出了如下大分支："
                ));
                for sibling_id in &parent.children_ids {
                    if *sibling_id == current_id {
                        continue;
                    }
                    parts.push(self.get_node_by_id(sibling_id)?.to_prompt());
                }
                parts.push(format!(
                    "{INTERVAL_MIDDLE}要求你的下一步假设生成和修改生成必须极大的规避上述大分支"
                ));
            }

            current_id = parent_id;
        }

        Ok(parts.join("\n"))
    }
}
