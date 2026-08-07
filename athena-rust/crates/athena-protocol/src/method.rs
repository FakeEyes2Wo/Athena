// ── Method name constants matching Python Method class ──

pub const INITIALIZE: &str = "initialize";
pub const INITIALIZED: &str = "initialized";
pub const THREAD_START: &str = "thread/start";
pub const THREAD_FORK: &str = "thread/fork";
pub const TURN_START: &str = "turn/start";
pub const TURN_INTERRUPT: &str = "turn/interrupt";
pub const THREAD_SUBSCRIBE: &str = "thread/subscribe";
pub const THREAD_UNSUBSCRIBE: &str = "thread/unsubscribe";
pub const SERVER_SHUTDOWN: &str = "server/shutdown";
pub const ITEM_APPROVAL_REQUEST: &str = "item/approval/request";
pub const ITEM_USER_INPUT_REQUEST: &str = "item/userInput/request";
pub const TOOL_CALL_REQUEST: &str = "tool/call/request";

/// Returns `true` if the method is a control-plane method.
pub fn is_control_method(m: &str) -> bool {
    matches!(m, INITIALIZE | SERVER_SHUTDOWN)
}
