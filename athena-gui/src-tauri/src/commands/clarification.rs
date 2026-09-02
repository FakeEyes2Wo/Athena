use crate::python::bridge::PythonBridge;
use crate::python::types::{RpcError, RpcResult};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::HashSet;
use std::sync::Arc;
use tauri::State;

/// One choice offered to the human.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HumanChoice {
    pub label: String,
    pub value: String,
}

/// Typed client reply: exactly one of choice / text / skip.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum HumanReply {
    Choice { value: String },
    Text { text: String },
    Skip,
}

/// Shared human request contract across Python, Rust, and TypeScript.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HumanRequest {
    pub request_id: String,
    pub session_id: String,
    pub scope_id: String,
    pub scope_kind: String,
    pub prompt: String,
    pub choices: Vec<HumanChoice>,
    pub allow_custom: bool,
    pub allow_skip: bool,
    pub created_at: String,
    pub expires_at: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawHumanRequest {
    request_id: String,
    session_id: String,
    scope_id: String,
    scope_kind: String,
    prompt: String,
    choices: Vec<HumanChoice>,
    allow_custom: bool,
    allow_skip: bool,
    created_at: String,
    expires_at: String,
}

impl<'de> Deserialize<'de> for HumanRequest {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = RawHumanRequest::deserialize(deserializer)?;
        validate_human_request(&raw).map_err(serde::de::Error::custom)?;
        Ok(HumanRequest {
            request_id: raw.request_id,
            session_id: raw.session_id,
            scope_id: raw.scope_id,
            scope_kind: raw.scope_kind,
            prompt: raw.prompt,
            choices: raw.choices,
            allow_custom: raw.allow_custom,
            allow_skip: raw.allow_skip,
            created_at: raw.created_at,
            expires_at: raw.expires_at,
        })
    }
}

fn ensure_len(field: &str, value: &str, max: usize) -> Result<(), String> {
    if value.is_empty() || value.len() > max {
        return Err(format!("{field} must be 1..{max} characters"));
    }
    Ok(())
}

fn validate_choice(
    choice: &HumanChoice,
    labels: &mut HashSet<String>,
    values: &mut HashSet<String>,
) -> Result<(), String> {
    ensure_len("choice label", &choice.label, 120)?;
    ensure_len("choice value", &choice.value, 256)?;
    if !labels.insert(choice.label.clone()) {
        return Err("duplicate_choice_labels".to_string());
    }
    if !values.insert(choice.value.clone()) {
        return Err("duplicate_choice_values".to_string());
    }
    Ok(())
}

fn validate_human_request(raw: &RawHumanRequest) -> Result<(), String> {
    ensure_len("request_id", &raw.request_id, 128)?;
    ensure_len("session_id", &raw.session_id, 128)?;
    ensure_len("scope_id", &raw.scope_id, 128)?;
    if raw.scope_kind.is_empty() {
        return Err("scope_kind must be non-empty".to_string());
    }
    ensure_len("prompt", &raw.prompt, 4000)?;

    let has_response_mode = raw.allow_custom || raw.allow_skip || !raw.choices.is_empty();
    if !has_response_mode {
        return Err("no_response_mode".to_string());
    }
    if !raw.choices.is_empty() && !(2..=3).contains(&raw.choices.len()) {
        return Err("choices must contain exactly 2 or 3 items".to_string());
    }

    let mut labels = HashSet::new();
    let mut values = HashSet::new();
    for choice in &raw.choices {
        validate_choice(choice, &mut labels, &mut values)?;
    }
    Ok(())
}

#[tauri::command]
pub async fn task_clarification_start(
    task: String,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    state
        .call_rpc("task_clarification_start", json!({ "task": task }))
        .await
}

#[tauri::command]
pub async fn task_clarification_get(
    draft_id: String,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    state
        .call_rpc("task_clarification_get", json!({ "draft_id": draft_id }))
        .await
}

#[tauri::command]
pub async fn task_clarification_retry(
    draft_id: String,
    revision: u64,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    state
        .call_rpc(
            "task_clarification_retry",
            json!({ "draft_id": draft_id, "revision": revision }),
        )
        .await
}

#[tauri::command]
pub async fn task_clarification_revise(
    draft_id: String,
    revision: u64,
    instruction: String,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    state
        .call_rpc(
            "task_clarification_revise",
            json!({
                "draft_id": draft_id,
                "revision": revision,
                "instruction": instruction,
            }),
        )
        .await
}

#[tauri::command]
pub async fn task_clarification_cancel(
    draft_id: String,
    revision: u64,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    state
        .call_rpc(
            "task_clarification_cancel",
            json!({ "draft_id": draft_id, "revision": revision }),
        )
        .await
}

#[tauri::command]
pub async fn human_reply(
    request_id: String,
    reply: Option<HumanReply>,
    answer: Option<String>,
    state: State<'_, Arc<PythonBridge>>,
) -> RpcResult<serde_json::Value> {
    match (reply, answer) {
        (Some(_), Some(_)) => {
            return Err(RpcError::transport(
                "human_reply accepts exactly one of `reply` or `answer`",
            ));
        }
        (None, None) => {
            return Err(RpcError::transport(
                "human_reply requires either `reply` or `answer`",
            ));
        }
        (Some(reply), None) => {
            state
                .call_rpc(
                    "human_reply",
                    json!({ "request_id": request_id, "reply": reply }),
                )
                .await
        }
        (None, Some(answer)) => {
            state
                .call_rpc(
                    "human_reply",
                    json!({ "request_id": request_id, "answer": answer }),
                )
                .await
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HUMAN_REQUEST_VALID: &str =
        include_str!("../../../../test/fixtures/clarification/human_request_valid.json");
    const HUMAN_REPLY_VALID: &str =
        include_str!("../../../../test/fixtures/clarification/human_reply_valid.json");
    const HUMAN_REQUEST_INVALID: &str =
        include_str!("../../../../test/fixtures/clarification/human_request_invalid.json");

    #[test]
    fn human_contract_fixtures_deserialize_valid_requests() {
        let request: HumanRequest = serde_json::from_str(HUMAN_REQUEST_VALID).unwrap();
        assert_eq!(request.request_id, "req-1");
        assert_eq!(request.choices.len(), 2);
    }

    #[test]
    fn human_contract_fixtures_deserialize_all_valid_replies() {
        let replies: Vec<HumanReply> = serde_json::from_str(HUMAN_REPLY_VALID).unwrap();
        assert_eq!(replies.len(), 3);
        assert!(matches!(replies[0], HumanReply::Choice { .. }));
        assert!(matches!(replies[1], HumanReply::Text { .. }));
        assert!(matches!(replies[2], HumanReply::Skip));
    }

    #[test]
    fn human_contract_fixtures_reject_invalid_requests() {
        // The invalid fixture is a list of invalid payloads; each is rejected.
        let invalid: Vec<serde_json::Value> = serde_json::from_str(HUMAN_REQUEST_INVALID).unwrap();
        for case in invalid {
            let payload = &case["payload"];
            assert!(
                serde_json::from_value::<HumanRequest>(payload.clone()).is_err(),
                "expected invalid HumanRequest to deserialize failed: {payload}"
            );
        }
    }
}
