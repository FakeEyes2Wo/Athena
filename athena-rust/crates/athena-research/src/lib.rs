//! `athena-research` — the research tree: experiment nodes forming a causal
//! chain, node lookup, and leaf→root prompt aggregation with sibling-branch
//! injection at the first fork.
//!
//! Scope is the current Python behaviour of `research_tree.py`: no persistence,
//! ranking, or proximity.

mod experiment;
mod tree;

pub use experiment::{Experiment, ExperimentResult, hypothesis_prompt};
pub use tree::{ResearchError, ResearchTree, ResearchTreeNode};

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use athena_types::{ExperimentPlan, Hypothesis, HypothesisStatus};
    use athena_workspace::GitWorkBranch;

    fn make_experiment(name: &str, change: &str, result: f64, commit: &str) -> Experiment {
        Experiment {
            commit: athena_types::CommitHash::new(commit).unwrap(),
            hypothesis: Hypothesis {
                statement: format!("{change} 可能提升验证集 AUC"),
                intervention: change.to_string(),
                expected_effect: "验证集 AUC 提升".into(),
                status: HypothesisStatus::Supported,
                evidence_refs: vec![],
                patience_grant: 0,
                patience_evidence_ref: None,
            },
            plan: ExperimentPlan {
                kind: "ablation".into(),
                change: change.to_string(),
                rubrics: vec![],
                run_config_ref: athena_types::ArtifactRef::new(format!("artifact://runs/{name}"))
                    .unwrap(),
                budget: serde_json::json!({"epochs": 10}),
                acceptance_rule: "AUC 不低于父实验".into(),
            },
            metric_type: "AUC".into(),
            result: ExperimentResult::Number(result),
            gitwork: GitWorkBranch {
                path: format!("C:/athena-demo/{name}"),
                branch: format!("demo/{name}"),
                base_commit: athena_types::CommitHash::new(commit).unwrap(),
            },
        }
    }

    #[test]
    fn create_node_links_parent_and_child() {
        let mut tree = ResearchTree::new();
        let root = tree
            .create_node(
                make_experiment("baseline", "训练基线模型", 0.72, &"0".repeat(40)),
                None,
            )
            .unwrap();
        let leaf = tree
            .create_node(
                make_experiment("standardize", "标准化数值特征", 0.75, &"1".repeat(40)),
                Some(&root.id),
            )
            .unwrap();

        assert_eq!(
            tree.get_node_by_id(&leaf.id).unwrap().parent_id.as_deref(),
            Some(root.id.as_str())
        );
        assert_eq!(
            tree.get_node_by_id(&root.id).unwrap().children_ids,
            vec![leaf.id.clone()]
        );
    }

    #[test]
    fn create_node_rejects_unknown_parent() {
        let mut tree = ResearchTree::new();
        let err = tree
            .create_node(
                make_experiment("x", "c", 0.1, &"0".repeat(40)),
                Some("missing"),
            )
            .unwrap_err();
        assert_eq!(err, ResearchError::UnknownNode("missing".into()));
    }

    #[test]
    fn get_node_by_id_reports_unknown() {
        let tree = ResearchTree::new();
        assert!(matches!(
            tree.get_node_by_id("nope"),
            Err(ResearchError::UnknownNode(_))
        ));
    }

    #[test]
    fn get_prompt_walks_leaf_to_root_and_injects_first_fork() {
        let mut tree = ResearchTree::new();
        let root = tree
            .create_node(
                make_experiment("baseline", "训练基线模型", 0.72, &"0".repeat(40)),
                None,
            )
            .unwrap();
        let leaf = tree
            .create_node(
                make_experiment("standardize", "标准化数值特征", 0.75, &"1".repeat(40)),
                Some(&root.id),
            )
            .unwrap();
        let sibling = tree
            .create_node(
                make_experiment("bucketize", "分箱数值特征", 0.74, &"2".repeat(40)),
                Some(&root.id),
            )
            .unwrap();

        let prompt = tree.get_prompt(&leaf.id).unwrap();
        // Leaf appears before root (leaf→root order).
        let leaf_pos = prompt.find("标准化数值特征").unwrap();
        let root_pos = prompt.find("训练基线模型").unwrap();
        assert!(leaf_pos < root_pos);
        // The fork injects the sibling and the avoidance instruction.
        assert!(prompt.contains("对于当前实验，我们做出了如下大分支："));
        assert!(prompt.contains("分箱数值特征"));
        assert!(prompt.contains("要求你的下一步假设生成和修改生成必须极大的规避上述大分支"));
        let _ = sibling;
    }

    #[test]
    fn get_prompt_single_chain_has_no_fork_injection() {
        let mut tree = ResearchTree::new();
        let root = tree
            .create_node(
                make_experiment("baseline", "训练基线模型", 0.72, &"0".repeat(40)),
                None,
            )
            .unwrap();
        let leaf = tree
            .create_node(
                make_experiment("standardize", "标准化数值特征", 0.75, &"1".repeat(40)),
                Some(&root.id),
            )
            .unwrap();
        let prompt = tree.get_prompt(&leaf.id).unwrap();
        assert!(!prompt.contains("大分支"));
    }

    #[test]
    fn result_as_float_handles_numeric_and_text() {
        let mut exp = make_experiment("x", "c", 0.75, &"0".repeat(40));
        assert_eq!(exp.result_as_float(), Some(0.75));

        exp.result = ExperimentResult::Text("N/A".into());
        assert_eq!(exp.result_as_float(), None);

        exp.result = ExperimentResult::Text("inf".into());
        assert_eq!(exp.result_as_float(), None);

        exp.result = ExperimentResult::Number(f64::NAN);
        assert_eq!(exp.result_as_float(), None);
    }
}
