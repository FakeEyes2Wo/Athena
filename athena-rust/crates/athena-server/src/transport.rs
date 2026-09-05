use athena_protocol::{ClientMessage, EventNotification, ResponseEnvelope, ServerControlMessage};
use thiserror::Error;
use tokio::sync::{mpsc, watch};

const DEFAULT_CONTROL_CAPACITY: usize = 64;
const DEFAULT_EVENT_CAPACITY: usize = 256;

#[derive(Debug, Error)]
pub enum TransportError {
    #[error("channel closed")]
    Closed,
}

/// Build the independent client-to-server, control, and event lanes.
pub fn transport() -> (TransportClientHalf, TransportServerHalf) {
    transport_with_capacity(DEFAULT_CONTROL_CAPACITY, DEFAULT_EVENT_CAPACITY)
}

fn transport_with_capacity(
    control_capacity: usize,
    event_capacity: usize,
) -> (TransportClientHalf, TransportServerHalf) {
    let (c2s_tx, c2s_rx) = mpsc::channel(control_capacity.max(1));
    let (control_tx, control_rx) = mpsc::channel(control_capacity.max(1));
    let (event_tx, event_rx) = mpsc::channel(event_capacity.max(1));
    let (ready_tx, ready_rx) = watch::channel(false);
    (
        TransportClientHalf {
            c2s_tx,
            control_rx,
            event_rx,
            ready_rx,
        },
        TransportServerHalf {
            c2s_rx,
            control_tx,
            event_tx,
            ready_tx,
        },
    )
}

/// Channels owned by the in-process client.
pub struct TransportClientHalf {
    pub(crate) c2s_tx: mpsc::Sender<ClientMessage>,
    pub(crate) control_rx: mpsc::Receiver<ServerControlMessage>,
    pub(crate) event_rx: mpsc::Receiver<EventNotification>,
    pub(crate) ready_rx: watch::Receiver<bool>,
}

impl TransportClientHalf {
    pub async fn send(&self, message: ClientMessage) -> Result<(), TransportError> {
        self.c2s_tx
            .send(message)
            .await
            .map_err(|_| TransportError::Closed)
    }

    pub async fn recv_control(&mut self) -> Option<ServerControlMessage> {
        self.control_rx.recv().await
    }
}

/// Cloneable response sender for concurrent request handlers.
#[derive(Clone)]
pub struct ServerResponder {
    control_tx: mpsc::Sender<ServerControlMessage>,
}

impl ServerResponder {
    pub async fn send_response(&self, message: ResponseEnvelope) -> Result<(), TransportError> {
        self.control_tx
            .send(ServerControlMessage::Response(message))
            .await
            .map_err(|_| TransportError::Closed)
    }
}

/// Channels owned by the in-process server.
pub struct TransportServerHalf {
    c2s_rx: mpsc::Receiver<ClientMessage>,
    control_tx: mpsc::Sender<ServerControlMessage>,
    event_tx: mpsc::Sender<EventNotification>,
    ready_tx: watch::Sender<bool>,
}

impl TransportServerHalf {
    pub async fn recv(&mut self) -> Option<ClientMessage> {
        self.c2s_rx.recv().await
    }

    pub fn responder(&self) -> ServerResponder {
        ServerResponder {
            control_tx: self.control_tx.clone(),
        }
    }

    pub fn event_sender(&self) -> mpsc::Sender<EventNotification> {
        self.event_tx.clone()
    }

    pub fn set_ready(&self) {
        let _ = self.ready_tx.send(true);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use athena_protocol::RequestEnvelope;

    #[tokio::test]
    async fn test_request_response_roundtrip() {
        let (client, mut server) = transport_with_capacity(4, 4);
        client
            .c2s_tx
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1,
                method: "test".into(),
                params: None,
            }))
            .await
            .unwrap();

        let ClientMessage::Request(request) = server.recv().await.unwrap() else {
            panic!("expected request");
        };
        server
            .responder()
            .send_response(ResponseEnvelope {
                request_id: request.request_id,
                result: Some(serde_json::json!({"status": "ok"})),
                error: None,
            })
            .await
            .unwrap();

        let mut control_rx = client.control_rx;
        let Some(ServerControlMessage::Response(response)) = control_rx.recv().await else {
            panic!("expected response");
        };
        assert_eq!(response.result.unwrap()["status"], "ok");
    }

    #[tokio::test]
    async fn test_events_independent_of_control() {
        let (client, server) = transport_with_capacity(2, 2);
        let responder = server.responder();
        for request_id in 0..2 {
            responder
                .send_response(ResponseEnvelope {
                    request_id,
                    result: Some(serde_json::json!({})),
                    error: None,
                })
                .await
                .unwrap();
        }
        server
            .event_sender()
            .send(EventNotification {
                subscription_id: "s1".into(),
                thread_id: "t1".into(),
                turn_id: None,
                sequence: 1,
                kind: "test".into(),
                event_ref: "ev://1".into(),
                data: None,
            })
            .await
            .unwrap();

        let mut event_rx = client.event_rx;
        assert_eq!(event_rx.recv().await.unwrap().kind, "test");
    }

    #[tokio::test]
    async fn test_ready_barrier() {
        let (client, server) = transport_with_capacity(2, 2);
        let mut ready = client.ready_rx;
        assert!(!*ready.borrow());
        server.set_ready();
        ready.changed().await.unwrap();
        assert!(*ready.borrow());
    }
}
