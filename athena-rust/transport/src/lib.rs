use protocol::{ClientMessage, EventNotification, ServerControlMessage};
use thiserror::Error;
use tokio::sync::mpsc;

const DEFAULT_CONTROL_CAPACITY: usize = 64;
const DEFAULT_EVENT_CAPACITY: usize = 256;

#[derive(Debug, Error)]
pub enum TransportError {
    #[error("channel closed")]
    Closed,
    #[error("channel full")]
    Full,
}

/// In-process dual-channel transport.
///
/// Three logical channels over two physical mpsc queues:
/// - c2s (client→server): requests, notifications, server-request-replies
/// - s2c_control (server→client): responses, ServerRequests
/// - s2c_event (server→client): EventNotifications (independent, never blocked by control)
pub struct Transport {
    c2s_tx: mpsc::Sender<ClientMessage>,
    c2s_rx: mpsc::Receiver<ClientMessage>,
    s2c_control_tx: mpsc::Sender<ServerControlMessage>,
    s2c_control_rx: mpsc::Receiver<ServerControlMessage>,
    s2c_event_tx: mpsc::Sender<EventNotification>,
    s2c_event_rx: mpsc::Receiver<EventNotification>,
}

impl Transport {
    pub fn new(control_capacity: usize, event_capacity: usize) -> Self {
        let cap_c = control_capacity.max(1);
        let cap_e = event_capacity.max(1);
        let (c2s_tx, c2s_rx) = mpsc::channel(cap_c);
        let (s2c_control_tx, s2c_control_rx) = mpsc::channel(cap_c);
        let (s2c_event_tx, s2c_event_rx) = mpsc::channel(cap_e);
        Self { c2s_tx, c2s_rx, s2c_control_tx, s2c_control_rx, s2c_event_tx, s2c_event_rx }
    }

    pub fn with_defaults() -> Self {
        Self::new(DEFAULT_CONTROL_CAPACITY, DEFAULT_EVENT_CAPACITY)
    }

    /// Split into client and server halves.
    pub fn split(self) -> (TransportClientHalf, TransportServerHalf) {
        let client = TransportClientHalf {
            c2s_tx: self.c2s_tx,
            s2c_control_rx: self.s2c_control_rx,
            s2c_event_rx: self.s2c_event_rx,
        };
        let server = TransportServerHalf {
            c2s_rx: self.c2s_rx,
            s2c_control_tx: self.s2c_control_tx,
            s2c_event_tx: self.s2c_event_tx,
        };
        (client, server)
    }
}

// ── Client half ──

pub struct TransportClientHalf {
    c2s_tx: mpsc::Sender<ClientMessage>,
    s2c_control_rx: mpsc::Receiver<ServerControlMessage>,
    s2c_event_rx: mpsc::Receiver<EventNotification>,
}

impl TransportClientHalf {
    /// Send a request to the server with timeout. Returns WouldBlock-style error on full.
    pub async fn send_request(&self, msg: protocol::RequestEnvelope) -> Result<(), TransportError> {
        self.c2s_tx
            .send(ClientMessage::Request(msg))
            .await
            .map_err(|_| TransportError::Closed)
    }

    /// Send a notification to the server (fire-and-forget; drops on full).
    pub fn send_notification(&self, msg: protocol::ClientNotification) {
        let _ = self.c2s_tx.try_send(ClientMessage::Notification(msg));
    }

    /// Send a reply to a server-initiated request.
    pub async fn send_server_request_reply(
        &self,
        msg: protocol::ServerRequestReply,
    ) -> Result<(), TransportError> {
        self.c2s_tx
            .send(ClientMessage::ServerRequestReply(msg))
            .await
            .map_err(|_| TransportError::Closed)
    }

    /// Receive a response or server request.
    pub async fn recv_control(&mut self) -> Option<ServerControlMessage> {
        self.s2c_control_rx.recv().await
    }

    /// Receive an event notification.
    pub async fn recv_event(&mut self) -> Option<EventNotification> {
        self.s2c_event_rx.recv().await
    }
}

// ── Server half ──

pub struct TransportServerHalf {
    c2s_rx: mpsc::Receiver<ClientMessage>,
    s2c_control_tx: mpsc::Sender<ServerControlMessage>,
    s2c_event_tx: mpsc::Sender<EventNotification>,
}

impl TransportServerHalf {
    /// Receive next client message.
    pub async fn recv(&mut self) -> Option<ClientMessage> {
        self.c2s_rx.recv().await
    }

    /// Send a response to the client.
    pub async fn send_response(&self, msg: protocol::ResponseEnvelope) -> Result<(), TransportError> {
        self.s2c_control_tx
            .send(ServerControlMessage::Response(msg))
            .await
            .map_err(|_| TransportError::Closed)
    }

    /// Send a server-initiated request to the client.
    pub async fn send_server_request(
        &self,
        msg: protocol::ServerRequest,
    ) -> Result<(), TransportError> {
        self.s2c_control_tx
            .send(ServerControlMessage::ServerRequest(msg))
            .await
            .map_err(|_| TransportError::Closed)
    }

    /// Send an event to the client (fire-and-forget; drops on full).
    pub fn send_event(&self, msg: EventNotification) {
        let _ = self.s2c_event_tx.try_send(msg);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_request_response_roundtrip() {
        let transport = Transport::new(4, 4);
        let (mut client, server) = transport.split();

        // Server task
        let server_handle = tokio::spawn(async move {
            let mut s = server;
            if let Some(ClientMessage::Request(req)) = s.recv().await {
                assert_eq!(req.request_id, 1);
                let resp = protocol::ResponseEnvelope {
                    request_id: req.request_id,
                    result: Some(serde_json::json!({"status": "ok"})),
                    error: None,
                };
                s.send_response(resp).await.unwrap();
            }
        });

        // Client sends
        let req = protocol::RequestEnvelope {
            request_id: 1,
            method: "test".into(),
            params: None,
        };
        client.send_request(req).await.unwrap();
        if let Some(ServerControlMessage::Response(resp)) = client.recv_control().await {
            assert_eq!(resp.request_id, 1);
            assert_eq!(resp.result.unwrap()["status"], "ok");
        }

        server_handle.await.unwrap();
    }

    #[tokio::test]
    async fn test_events_independent_of_control() {
        let transport = Transport::new(2, 2);
        let (mut client, server) = transport.split();

        // Fill control channel first, then send event — event must not be blocked
        server.send_response(protocol::ResponseEnvelope {
            request_id: 0, result: Some(serde_json::json!({})), error: None,
        }).await.unwrap();
        server.send_response(protocol::ResponseEnvelope {
            request_id: 1, result: Some(serde_json::json!({})), error: None,
        }).await.unwrap();

        // Event should still go through (separate channel)
        server.send_event(EventNotification {
            subscription_id: "s1".into(),
            thread_id: "t1".into(),
            turn_id: None,
            sequence: 1,
            kind: "test".into(),
            event_ref: "ev://1".into(),
            data: None,
        });

        let ev = client.recv_event().await.unwrap();
        assert_eq!(ev.kind, "test");
    }

    #[tokio::test]
    async fn test_notification_is_fire_and_forget() {
        let transport = Transport::new(2, 2);
        let (client, mut server) = transport.split();

        client.send_notification(protocol::ClientNotification {
            method: "initialized".into(),
            params: None,
        });

        if let Some(ClientMessage::Notification(n)) = server.recv().await {
            assert_eq!(n.method, "initialized");
        } else {
            panic!("expected notification");
        }
    }
}
