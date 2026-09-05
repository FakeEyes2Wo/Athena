use crate::python::{PythonBridge, RpcError, RpcResult};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Arc;
use tauri::State;

/// Typed client reply: exactly one of choice / text / skip.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum HumanReply {
    Choice { value: String },
    Text { text: String },
    Skip,
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
        (Some(_), Some(_)) => Err(RpcError::transport(
            "human_reply accepts exactly one of `reply` or `answer`",
        )),
        (None, None) => Err(RpcError::transport(
            "human_reply requires either `reply` or `answer`",
        )),
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

    const HUMAN_REPLY_VALID: &str =
        include_str!("../../../../test/fixtures/clarification/human_reply_valid.json");

    #[test]
    fn human_contract_fixtures_deserialize_all_valid_replies() {
        let replies: Vec<HumanReply> = serde_json::from_str(HUMAN_REPLY_VALID).unwrap();
        assert_eq!(replies.len(), 3);
        assert!(matches!(replies[0], HumanReply::Choice { .. }));
        assert!(matches!(replies[1], HumanReply::Text { .. }));
        assert!(matches!(replies[2], HumanReply::Skip));
    }
}
