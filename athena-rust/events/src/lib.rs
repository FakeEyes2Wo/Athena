use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::{mpsc, Notify, RwLock};

// ── Event ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub thread_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    pub sequence: u64,
    pub kind: String,
    #[serde(default)]
    pub event_ref: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

// ── EventJournal ──

/// Per-thread append-only event journal. Notify replaces asyncio.Condition.
pub struct EventJournal {
    thread_id: String,
    records: RwLock<Vec<Event>>,
    next_sequence: RwLock<u64>,
    notify: Notify,
}

impl EventJournal {
    pub fn new(thread_id: String) -> Self {
        Self {
            thread_id,
            records: RwLock::new(Vec::new()),
            next_sequence: RwLock::new(1),
            notify: Notify::new(),
        }
    }

    pub fn thread_id(&self) -> &str {
        &self.thread_id
    }

    pub async fn next_sequence(&self) -> u64 {
        *self.next_sequence.read().await
    }

    /// Append an event and notify all waiters.
    pub async fn append(&self, event: Event) {
        let mut records = self.records.write().await;
        let mut seq = self.next_sequence.write().await;
        records.push(event);
        *seq = records.last().map(|e| e.sequence + 1).unwrap_or(1);
        self.notify.notify_waiters();
    }

    /// Len of records.
    pub async fn len(&self) -> usize {
        self.records.read().await.len()
    }

    /// Block until a new event arrives after the given index, then return it.
    pub async fn wait_for_next(&self, after_index: usize) -> Event {
        loop {
            {
                let records = self.records.read().await;
                if after_index < records.len() {
                    return records[after_index].clone();
                }
            }
            self.notify.notified().await;
        }
    }

    /// Snapshot all records (for subscription pump).
    pub async fn snapshot(&self) -> Vec<Event> {
        self.records.read().await.clone()
    }

    /// Get events since a given index.
    pub async fn events_since(&self, after_index: usize) -> Vec<Event> {
        let records = self.records.read().await;
        if after_index >= records.len() {
            return vec![];
        }
        records[after_index..].to_vec()
    }

    /// Returns true if the journal has no events.
    pub async fn is_empty(&self) -> bool {
        self.records.read().await.is_empty()
    }
}

// ── Subscription ──

pub struct Subscription {
    pub subscription_id: String,
    pub thread_id: String,
    pub cursor: RwLock<u64>,
    /// FairMux reads events from this receiver in round-robin fashion.
    /// The paired Sender is kept by the pump (external code).
    pub rx: std::sync::Mutex<mpsc::Receiver<Event>>,
    pub has_data: Notify,
    pub active: AtomicBool,
    pub pump_handle: RwLock<Option<tokio::task::JoinHandle<()>>>,
}

impl Subscription {
    pub fn new(
        subscription_id: String,
        thread_id: String,
        cursor: u64,
        rx: mpsc::Receiver<Event>,
    ) -> Self {
        Self {
            subscription_id,
            thread_id,
            cursor: RwLock::new(cursor),
            rx: std::sync::Mutex::new(rx),
            has_data: Notify::new(),
            active: AtomicBool::new(true),
            pump_handle: RwLock::new(None),
        }
    }
}

// ── FairMux ──

/// Round-robin multiplexer — prevents starvation across subscriptions.
pub struct FairMux {
    subs: Arc<RwLock<HashMap<String, Arc<Subscription>>>>,
    outgoing: mpsc::Sender<protocol::EventNotification>,
}

impl FairMux {
    pub fn new(outgoing: mpsc::Sender<protocol::EventNotification>) -> Self {
        Self {
            subs: Arc::new(RwLock::new(HashMap::new())),
            outgoing,
        }
    }

    pub async fn add(&self, sub: Arc<Subscription>) {
        let mut subs = self.subs.write().await;
        subs.insert(sub.subscription_id.clone(), sub);
    }

    pub async fn remove(&self, subscription_id: &str) {
        let mut subs = self.subs.write().await;
        if let Some(sub) = subs.remove(subscription_id) {
            sub.active.store(false, Ordering::SeqCst);
        }
    }

    /// Start the multiplexer loop.
    pub async fn run(self: Arc<Self>) {
        let mut index = 0usize;
        loop {
            let ready: Vec<Arc<Subscription>> = {
                let subs = self.subs.read().await;
                subs.values()
                    .filter(|s| {
                        s.active.load(Ordering::SeqCst)
                            && !s.rx.lock().expect("poisoned").is_closed()
                    })
                    .cloned()
                    .collect()
            };

            if ready.is_empty() {
                // Wait for a subscription to be added (poll)
                tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
                continue;
            }

            index = index % ready.len();
            let sub = &ready[index];
            index += 1;

            // Try to receive from this subscription's channel.
            // Scope the MutexGuard so it is dropped before any .await below.
            let recv_result = sub.rx.lock().expect("poisoned").try_recv();
            let notification = match recv_result {
                Ok(event) => Some(protocol::EventNotification {
                    subscription_id: sub.subscription_id.clone(),
                    thread_id: event.thread_id.clone(),
                    turn_id: event.turn_id.clone(),
                    sequence: event.sequence,
                    kind: event.kind.clone(),
                    event_ref: event.event_ref.clone(),
                    data: event.data.clone(),
                }),
                Err(mpsc::error::TryRecvError::Empty) => {
                    sub.has_data.notify_one();
                    tokio::task::yield_now().await;
                    None
                }
                Err(mpsc::error::TryRecvError::Disconnected) => None,
            };

            if let Some(n) = notification {
                if self.outgoing.send(n).await.is_err() {
                    break;
                }
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn make_event(thread_id: &str, sequence: u64, kind: &str) -> Event {
        Event {
            thread_id: thread_id.to_string(),
            turn_id: None,
            sequence,
            kind: kind.to_string(),
            event_ref: String::new(),
            data: None,
        }
    }

    // ── journal append/read ──

    #[tokio::test]
    async fn test_journal_append_read() {
        let journal = EventJournal::new("t1".to_string());

        assert_eq!(journal.len().await, 0);
        assert!(journal.is_empty().await);
        assert_eq!(journal.next_sequence().await, 1);

        let e1 = make_event("t1", 1, "test_event");
        journal.append(e1).await;
        assert_eq!(journal.len().await, 1);
        assert_eq!(journal.next_sequence().await, 2);

        // snapshot
        let snap = journal.snapshot().await;
        assert_eq!(snap.len(), 1);
        assert_eq!(snap[0].kind, "test_event");

        // events_since
        let since = journal.events_since(0).await;
        assert_eq!(since.len(), 1);
        let since = journal.events_since(1).await;
        assert!(since.is_empty());

        // append second event
        let e2 = make_event("t1", 2, "second");
        journal.append(e2).await;
        assert_eq!(journal.len().await, 2);

        // events_since after index 0 returns only newer events
        let since = journal.events_since(1).await;
        assert_eq!(since.len(), 1);
        assert_eq!(since[0].sequence, 2);
        assert_eq!(since[0].kind, "second");
    }

    // ── multi-subscriber (concurrent wait_for_next) ──

    #[tokio::test]
    async fn test_multi_subscriber() {
        let journal = Arc::new(EventJournal::new("t1".to_string()));

        // Spawn three waiters, each waiting for a different index
        let mut handles = Vec::new();
        for i in 0..3 {
            let j = journal.clone();
            handles.push(tokio::spawn(async move {
                let event = j.wait_for_next(i).await;
                assert_eq!(event.sequence, (i + 1) as u64);
                event
            }));
        }

        // Give waiters a moment to start
        tokio::time::sleep(Duration::from_millis(50)).await;

        // Append events one at a time — each notify_waiters() wakes all waiters
        for i in 0..3 {
            let ev = make_event("t1", (i + 1) as u64, &format!("ev{}", i + 1));
            journal.append(ev).await;
        }

        for (i, handle) in handles.into_iter().enumerate() {
            let event = tokio::time::timeout(Duration::from_secs(5), handle)
                .await
                .expect("waiter timed out")
                .expect("waiter panicked");
            assert_eq!(event.sequence, (i + 1) as u64);
        }
    }

    // ── FairMux round-robin ──

    #[tokio::test]
    async fn test_fairmux_round_robin() {
        let (outgoing_tx, mut outgoing_rx) = mpsc::channel(256);
        let mux = Arc::new(FairMux::new(outgoing_tx));

        // Create 3 subscriptions with independent channels
        let (tx1, rx1) = mpsc::channel(256);
        let sub1 = Arc::new(Subscription::new(
            "sub:1".into(), "t1".into(), 0, rx1,
        ));
        let (tx2, rx2) = mpsc::channel(256);
        let sub2 = Arc::new(Subscription::new(
            "sub:2".into(), "t1".into(), 0, rx2,
        ));
        let (tx3, rx3) = mpsc::channel(256);
        let sub3 = Arc::new(Subscription::new(
            "sub:3".into(), "t1".into(), 0, rx3,
        ));

        // Register subscriptions
        mux.add(sub1).await;
        mux.add(sub2).await;
        mux.add(sub3).await;

        // Start the mux loop in background
        let mux_clone = mux.clone();
        let _mux_handle = tokio::spawn(async move {
            mux_clone.run().await;
        });

        // Let the mux settle and register all subs
        tokio::time::sleep(Duration::from_millis(100)).await;

        // Send one event to each subscription's sender
        tx1.send(make_event("t1", 1, "ev1"))
            .await
            .expect("tx1 send");
        tx2.send(make_event("t1", 2, "ev2"))
            .await
            .expect("tx2 send");
        tx3.send(make_event("t1", 3, "ev3"))
            .await
            .expect("tx3 send");

        // Collect 3 notifications from outgoing — round-robin order depends on
        // HashMap iteration, but each subscription must deliver exactly once.
        let mut seen: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for _ in 0..3 {
            let notif = tokio::time::timeout(Duration::from_secs(5), outgoing_rx.recv())
                .await
                .expect("timeout waiting for notification")
                .expect("outgoing channel closed unexpectedly");

            assert!(
                seen.insert(notif.subscription_id.clone()),
                "duplicate subscription_id: {}",
                notif.subscription_id
            );
            assert!(notif.sequence >= 1 && notif.sequence <= 3);
        }

        // All three distinct subs were served
        assert_eq!(seen.len(), 3);
    }

    // ── Event serialization round-trip ──

    #[test]
    fn test_event_serialize_roundtrip() {
        let event = Event {
            thread_id: "t1".to_string(),
            turn_id: Some("turn-1".to_string()),
            sequence: 42,
            kind: "ping".to_string(),
            event_ref: "ref:abc".to_string(),
            data: Some(serde_json::json!({"key": "value"})),
        };

        let json = serde_json::to_string(&event).expect("serialize");
        let deserialized: Event =
            serde_json::from_str(&json).expect("deserialize");

        assert_eq!(deserialized.thread_id, "t1");
        assert_eq!(deserialized.turn_id, Some("turn-1".to_string()));
        assert_eq!(deserialized.sequence, 42);
        assert_eq!(deserialized.kind, "ping");
        assert_eq!(deserialized.event_ref, "ref:abc");
        assert_eq!(
            deserialized.data,
            Some(serde_json::json!({"key": "value"}))
        );
    }

    #[test]
    fn test_event_deny_unknown_fields() {
        let json = r#"{"thread_id":"t1","sequence":1,"kind":"x","event_ref":"","unknown":"should_fail"}"#;
        let result: Result<Event, _> = serde_json::from_str(json);
        assert!(result.is_err(), "unknown fields should be denied");
    }
}
