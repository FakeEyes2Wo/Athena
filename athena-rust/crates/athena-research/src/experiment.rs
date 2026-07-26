use athena_types::{Hypothesis, HypothesisStatus};
use serde::{Deserialize, Serialize};

/// An experiment result: a number or free text (e.g. `"N/A"`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ExperimentResult {
    Number(f64),
    Text(String),
}

impl std::fmt::Display for ExperimentResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExperimentResult::Number(v) => write!(f, "{v}"),
            ExperimentResult::Text(s) => write!(f, "{s}"),
        }
    }
}

/// One executed experiment: the commit it produced, the hypothesis it tested,
/// its plan, and the resulting metric.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Experiment {
    pub commit: athena_types::CommitHash,
    pub hypothesis: Hypothesis,
    pub plan: athena_types::ExperimentPlan,
    /// Which metric `result` reports, e.g. `AUC` or `logloss`.
    pub metric_type: String,
    pub result: ExperimentResult,
    pub gitwork: athena_workspace::GitWorkBranch,
}

impl Experiment {
    /// Return the result as a finite float, or `None` (with a warning) when it
    /// is non-numeric or non-finite.
    pub fn result_as_float(&self) -> Option<f64> {
        let value = match &self.result {
            ExperimentResult::Number(v) => *v,
            ExperimentResult::Text(s) => match s.trim().parse::<f64>() {
                Ok(v) => v,
                Err(_) => {
                    tracing::warn!(result = %s, "experiment result is not numeric");
                    return None;
                }
            },
        };
        if !value.is_finite() {
            tracing::warn!(result = %self.result, "experiment result is not finite");
            return None;
        }
        Some(value)
    }

    /// Render this experiment for inclusion in a prompt.
    pub fn to_prompt(&self) -> String {
        format!(
            "\n{}\n\n最后得到的指标{}为: {}\n        ",
            hypothesis_prompt(&self.hypothesis),
            self.metric_type,
            self.result,
        )
    }
}

/// The SCREAMING_SNAKE_CASE wire form of a hypothesis status.
fn status_code(status: HypothesisStatus) -> &'static str {
    match status {
        HypothesisStatus::Proposed => "PROPOSED",
        HypothesisStatus::Supported => "SUPPORTED",
        HypothesisStatus::Refuted => "REFUTED",
        HypothesisStatus::Rejected => "REJECTED",
    }
}

/// Render a hypothesis for a prompt, matching `Hypothesis.to_prompt` in Python.
pub fn hypothesis_prompt(h: &Hypothesis) -> String {
    let status_text = match h.status {
        HypothesisStatus::Proposed => "该假设尚未验证，需要后续实验进行评估。",
        HypothesisStatus::Supported => "实验结果支持该假设，该假设较大概率成立。",
        HypothesisStatus::Refuted => "实验结果不支持该假设，但仍可在调整后继续验证。",
        HypothesisStatus::Rejected => "该假设已被彻底拒绝，不应继续沿此方向实验。",
    };
    format!(
        "假设：{}\n实验改动：{}\n预期效果：{}\n验证状态：{}\n状态结论：{}",
        h.statement,
        h.intervention,
        h.expected_effect,
        status_code(h.status),
        status_text,
    )
}
