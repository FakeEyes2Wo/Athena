use crate::subscription::SubscriptionRegistry;
use athena_protocol::{
    ErrorCode, RpcError, ThreadForkParams, ThreadStartParams, ThreadSubscribeParams,
    ThreadUnsubscribeParams, TurnInterruptParams, TurnStartParams, method,
};
use athena_runtime::{RuntimeError, RuntimeThreadManager};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::sync::Arc;

/// Map a runtime error to a sanitized protocol error.
pub fn runtime_error_to_rpc(e: RuntimeError) -> RpcError {
    match e {
        RuntimeError::UnknownThread(id) => {
            RpcError::new(ErrorCode::NotFound, format!("unknown thread: {id}"))
        }
        RuntimeError::UnknownSnapshot(id) => {
            RpcError::new(ErrorCode::NotFound, format!("no completed snapshot: {id}"))
        }
        RuntimeError::AlreadyRunning => RpcError::new(
            ErrorCode::FailedPrecondition,
            "thread already has an active turn",
        ),
        RuntimeError::NoActiveTurn(id) => RpcError::new(
            ErrorCode::FailedPrecondition,
            format!("no active turn: {id}"),
        ),
        RuntimeError::Closed => RpcError::new(ErrorCode::Closed, "thread is closed"),
        RuntimeError::Invalid(m) => RpcError::new(ErrorCode::InvalidArgument, m),
    }
}

/// Executes a protocol method against backing services.
#[async_trait::async_trait]
pub trait Executor: Send + Sync {
    async fn execute(&self, method: &str, params: Value) -> Result<Value, RpcError>;
}

/// Maps protocol methods to `RuntimeThreadManager` and subscription calls.
pub struct ExecutionAdapter {
    manager: Arc<RuntimeThreadManager>,
    subscriptions: Arc<SubscriptionRegistry>,
}

impl ExecutionAdapter {
    pub fn new(
        manager: Arc<RuntimeThreadManager>,
        subscriptions: Arc<SubscriptionRegistry>,
    ) -> Self {
        Self {
            manager,
            subscriptions,
        }
    }
}

#[async_trait::async_trait]
impl Executor for ExecutionAdapter {
    async fn execute(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        match method {
            method::THREAD_START => {
                let p: ThreadStartParams = parse(params)?;
                let thread = self
                    .manager
                    .start(&p.session_id, p.context_ref.as_str())
                    .await
                    .map_err(runtime_error_to_rpc)?;
                Ok(json!({ "thread_id": thread.thread_id.as_str() }))
            }
            method::TURN_START => {
                let p: TurnStartParams = parse(params)?;
                let turn = self
                    .manager
                    .submit(p.thread_id.as_str(), p.request_ref.as_str())
                    .await
                    .map_err(runtime_error_to_rpc)?;
                Ok(json!({ "turn_id": turn.turn_id.as_str() }))
            }
            method::TURN_INTERRUPT => {
                let p: TurnInterruptParams = parse(params)?;
                self.manager
                    .interrupt(p.thread_id.as_str(), p.turn_id.as_str(), &p.reason)
                    .await
                    .map_err(runtime_error_to_rpc)?;
                Ok(json!({ "turn_id": p.turn_id.as_str(), "status": "interrupted" }))
            }
            method::THREAD_FORK => {
                let p: ThreadForkParams = parse(params)?;
                let child = self
                    .manager
                    .fork(
                        p.thread_id.as_str(),
                        p.after_turn_id.as_ref().map(|t| t.as_str()),
                    )
                    .await
                    .map_err(runtime_error_to_rpc)?;
                Ok(json!({ "thread_id": child.thread_id.as_str() }))
            }
            method::THREAD_SUBSCRIBE => {
                let p: ThreadSubscribeParams = parse(params)?;
                let sub_id = self
                    .subscriptions
                    .create(p.thread_id.as_str(), p.after_sequence)
                    .await
                    .map_err(runtime_error_to_rpc)?;
                Ok(json!({ "subscription_id": sub_id }))
            }
            method::THREAD_UNSUBSCRIBE => {
                let p: ThreadUnsubscribeParams = parse(params)?;
                self.subscriptions.remove(&p.subscription_id).await;
                Ok(json!({ "status": "unsubscribed" }))
            }
            other => Err(RpcError::new(
                ErrorCode::NotFound,
                format!("unknown method: {other}"),
            )),
        }
    }
}

fn parse<T: DeserializeOwned>(value: Value) -> Result<T, RpcError> {
    serde_json::from_value(value)
        .map_err(|e| RpcError::new(ErrorCode::InvalidArgument, e.to_string()))
}
