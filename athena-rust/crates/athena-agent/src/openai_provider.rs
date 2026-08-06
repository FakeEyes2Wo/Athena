use crate::provider::{AgentConfig, LlmProvider, ProviderEvent, to_api};
use athena_memory::ModelMessage;
use futures::stream::{BoxStream, StreamExt};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use tokio::sync::watch;

/// OpenAI-compatible Chat Completions streaming provider.
///
/// This performs real network I/O and is therefore not exercised by the crate's
/// tests (which use a fake provider); it mirrors the Python `ResponsesProvider`.
pub struct OpenAiProvider {
    client: reqwest::Client,
    base_url: String,
    api_key: String,
}

impl OpenAiProvider {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>) -> Self {
        Self {
            client: reqwest::Client::new(),
            base_url: base_url.into(),
            api_key: api_key.into(),
        }
    }
}

#[async_trait::async_trait]
impl LlmProvider for OpenAiProvider {
    async fn stream(
        &self,
        config: &AgentConfig,
        tool_specs: Vec<Value>,
        messages: Vec<ModelMessage>,
        cancel: watch::Receiver<bool>,
    ) -> BoxStream<'static, ProviderEvent> {
        let (tx, rx) = futures::channel::mpsc::unbounded();
        let client = self.client.clone();
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let key = self.api_key.clone();
        let body = build_body(config, &tool_specs, &messages);

        tokio::spawn(async move {
            run_stream(client, url, key, body, cancel, tx).await;
        });
        rx.boxed()
    }
}

fn build_body(config: &AgentConfig, tool_specs: &[Value], messages: &[ModelMessage]) -> Value {
    let mut api_msgs = to_api(messages);
    let mut system = config.system_prompt.clone();
    if api_msgs.first().and_then(|m| m.get("role")) == Some(&json!("system")) {
        if let Some(content) = api_msgs[0].get("content").and_then(|c| c.as_str()) {
            system = content.to_string();
        }
        api_msgs.remove(0);
    }
    let mut full = vec![json!({"role": "system", "content": system})];
    full.extend(api_msgs);

    let mut body = json!({
        "model": config.model,
        "messages": full,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "stream": true,
    });
    if !tool_specs.is_empty() {
        body["tools"] = Value::Array(tool_specs.to_vec());
        body["tool_choice"] = json!("auto");
    }
    body
}

type EventTx = futures::channel::mpsc::UnboundedSender<ProviderEvent>;

async fn run_stream(
    client: reqwest::Client,
    url: String,
    key: String,
    body: Value,
    cancel: watch::Receiver<bool>,
    tx: EventTx,
) {
    let resp = match client.post(&url).bearer_auth(&key).json(&body).send().await {
        Ok(r) => r,
        Err(e) => {
            let _ = tx.unbounded_send(ProviderEvent::Error {
                message: format!("request failed: {e}"),
            });
            return;
        }
    };

    let mut stream = resp.bytes_stream();
    let mut buffer = String::new();
    let mut text = String::new();
    let mut finish = String::new();
    let mut calls: BTreeMap<u64, (String, String, String)> = BTreeMap::new();

    while let Some(chunk) = stream.next().await {
        if *cancel.borrow() {
            let _ = tx.unbounded_send(ProviderEvent::Error {
                message: "cancelled".into(),
            });
            return;
        }
        let bytes = match chunk {
            Ok(b) => b,
            Err(e) => {
                let _ = tx.unbounded_send(ProviderEvent::Error {
                    message: format!("stream read failed: {e}"),
                });
                return;
            }
        };
        buffer.push_str(&String::from_utf8_lossy(&bytes));

        while let Some(pos) = buffer.find('\n') {
            let line = buffer[..pos].trim().to_string();
            buffer.drain(..=pos);
            let Some(data) = line.strip_prefix("data:") else {
                continue;
            };
            let data = data.trim();
            if data == "[DONE]" {
                emit_calls(&finish, &mut calls, &tx);
                let _ = tx.unbounded_send(ProviderEvent::ResponseCompleted {
                    finish_reason: if finish.is_empty() {
                        "stop".into()
                    } else {
                        finish.clone()
                    },
                    accumulated_text: text.clone(),
                });
                return;
            }
            let Ok(value) = serde_json::from_str::<Value>(data) else {
                continue;
            };
            for choice in value
                .get("choices")
                .and_then(|c| c.as_array())
                .into_iter()
                .flatten()
            {
                if let Some(reason) = choice.get("finish_reason").and_then(|r| r.as_str()) {
                    finish = reason.to_string();
                }
                let delta = choice.get("delta").cloned().unwrap_or(Value::Null);
                if let Some(content) = delta.get("content").and_then(|c| c.as_str())
                    && !content.is_empty()
                {
                    text.push_str(content);
                    let _ = tx.unbounded_send(ProviderEvent::TextDelta {
                        delta: content.to_string(),
                        accumulated: text.clone(),
                    });
                }
                for call in delta
                    .get("tool_calls")
                    .and_then(|t| t.as_array())
                    .into_iter()
                    .flatten()
                {
                    let idx = call.get("index").and_then(|i| i.as_u64()).unwrap_or(0);
                    let entry = calls.entry(idx).or_default();
                    if let Some(id) = call.get("id").and_then(|i| i.as_str()) {
                        entry.0 = id.to_string();
                    }
                    if let Some(func) = call.get("function") {
                        if let Some(name) = func.get("name").and_then(|n| n.as_str()) {
                            entry.1 = name.to_string();
                        }
                        if let Some(args) = func.get("arguments").and_then(|a| a.as_str()) {
                            entry.2.push_str(args);
                        }
                    }
                }
            }
            if finish == "tool_calls" {
                emit_calls(&finish, &mut calls, &tx);
                finish.clear();
            }
        }
    }

    emit_calls(&finish, &mut calls, &tx);
    let _ = tx.unbounded_send(ProviderEvent::ResponseCompleted {
        finish_reason: if finish.is_empty() {
            "stop".into()
        } else {
            finish
        },
        accumulated_text: text,
    });
}

fn emit_calls(_finish: &str, calls: &mut BTreeMap<u64, (String, String, String)>, tx: &EventTx) {
    for (_, (id, name, args)) in std::mem::take(calls) {
        if id.is_empty() || name.is_empty() {
            continue;
        }
        let arguments = if args.is_empty() {
            json!({})
        } else {
            serde_json::from_str(&args).unwrap_or_else(|_| json!({}))
        };
        let _ = tx.unbounded_send(ProviderEvent::ToolCall {
            call_id: id,
            name,
            arguments,
        });
    }
}
