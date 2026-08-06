use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct EventNotification {
    pub subscription_id: Option<String>,
    pub kind: String,
    pub data: Value,
}
