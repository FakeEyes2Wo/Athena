use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct EventNotification {
    #[serde(default)]
    pub subscription_id: Option<String>,
    pub kind: String,
    pub data: Value,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn deserializes_event_without_subscription_id() {
        // Python 侧 emit_event 只发 {"kind","data"}，没有 subscription_id。
        let val = json!({"kind": "output", "data": {"text": "hello", "source": "agent"}});
        let evt: EventNotification = serde_json::from_value(val).expect("deserialize event");
        assert_eq!(evt.kind, "output");
        assert!(evt.subscription_id.is_none());
        assert_eq!(evt.data["text"], "hello");
    }
}
