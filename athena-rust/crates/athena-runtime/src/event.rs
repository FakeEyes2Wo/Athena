use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock, RwLockReadGuard, RwLockWriteGuard};
use tokio::sync::watch;

// ── Event ──

/// A single authoritative, ordered event on one thread's journal.
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

/// An event before the journal assigns its sequence number.
#[derive(Debug, Clone)]
pub struct EventDraft {
    pub turn_id: Option<String>,
    pub kind: String,
    pub event_ref: String,
    pub data: Option<Value>,
}

impl EventDraft {
    pub fn new(kind: impl Into<String>, event_ref: impl Into<String>) -> Self {
        Self {
            turn_id: None,
            kind: kind.into(),
            event_ref: event_ref.into(),
            data: None,
        }
    }

    pub fn turn(mut self, turn_id: impl Into<String>) -> Self {
        self.turn_id = Some(turn_id.into());
        self
    }

    pub fn data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
}

// ── EventJournal ──

/// Per-thread append-only journal. The journal itself assigns each sequence
/// number (callers never read-then-write it), and a `watch` tail guarantees a
/// waiter is woken for every append even if notifications coalesce.
pub struct EventJournal {
    thread_id: String,
    records: RwLock<Vec<Event>>,
    tail: watch::Sender<u64>,
}

fn read_guard<T>(lock: &RwLock<T>) -> RwLockReadGuard<'_, T> {
    lock.read().unwrap_or_else(|p| p.into_inner())
}

fn write_guard<T>(lock: &RwLock<T>) -> RwLockWriteGuard<'_, T> {
    lock.write().unwrap_or_else(|p| p.into_inner())
}

impl EventJournal {
    pub fn new(thread_id: impl Into<String>) -> Self {
        let (tail, _) = watch::channel(0);
        Self {
            thread_id: thread_id.into(),
            records: RwLock::new(Vec::new()),
            tail,
        }
    }

    /// Append a drafted event, assigning the next sequence internally.
    pub fn append(&self, draft: EventDraft) -> Event {
        let mut records = write_guard(&self.records);
        let sequence = records.len() as u64 + 1;
        let event = Event {
            thread_id: self.thread_id.clone(),
            turn_id: draft.turn_id,
            sequence,
            kind: draft.kind,
            event_ref: draft.event_ref,
            data: draft.data,
        };
        records.push(event.clone());
        // `send_replace` updates the tail value and notifies even when no
        // receiver currently exists (subscribers may come and go).
        self.tail.send_replace(sequence);
        event
    }

    pub fn snapshot(&self) -> Vec<Event> {
        read_guard(&self.records).clone()
    }

    /// Block until an event exists at `after_index`, then return it.
    pub async fn wait_for_next(&self, after_index: usize) -> Event {
        let mut tail = self.tail.subscribe();
        loop {
            {
                let records = read_guard(&self.records);
                if after_index < records.len() {
                    return records[after_index].clone();
                }
            }
            // Sender lives as long as `self`, so this never errors.
            if tail.changed().await.is_err() {
                tail = self.tail.subscribe();
            }
        }
    }
}

// ── Subscription & FairMux ──

/// A single event subscription; the paired `Sender` is held by its pump.
pub struct Subscription {
    pub subscription_id: String,
    pub rx: std::sync::Mutex<tokio::sync::mpsc::Receiver<Event>>,
    pub active: AtomicBool,
}

impl Subscription {
    pub fn new(subscription_id: String, rx: tokio::sync::mpsc::Receiver<Event>) -> Self {
        Self {
            subscription_id,
            rx: std::sync::Mutex::new(rx),
            active: AtomicBool::new(true),
        }
    }
}

/// Round-robin multiplexer — prevents starvation across subscriptions.
pub struct FairMux {
    subs: RwLock<HashMap<String, Arc<Subscription>>>,
    outgoing: tokio::sync::mpsc::Sender<athena_protocol::EventNotification>,
}

fn lock_recover<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

impl FairMux {
    pub fn new(outgoing: tokio::sync::mpsc::Sender<athena_protocol::EventNotification>) -> Self {
        Self {
            subs: RwLock::new(HashMap::new()),
            outgoing,
        }
    }

    pub fn add(&self, sub: Arc<Subscription>) {
        write_guard(&self.subs).insert(sub.subscription_id.clone(), sub);
    }

    pub fn remove(&self, subscription_id: &str) {
        if let Some(sub) = write_guard(&self.subs).remove(subscription_id) {
            sub.active.store(false, Ordering::SeqCst);
        }
    }

    /// Run the multiplexer loop, delivering at most one event per ready
    /// subscription per round.
    pub async fn run(self: Arc<Self>) {
        let mut index = 0usize;
        loop {
            let ready: Vec<Arc<Subscription>> = {
                let subs = read_guard(&self.subs);
                subs.values()
                    .filter(|s| s.active.load(Ordering::SeqCst) && !lock_recover(&s.rx).is_closed())
                    .cloned()
                    .collect()
            };

            if ready.is_empty() {
                tokio::time::sleep(tokio::time::Duration::from_millis(1)).await;
                continue;
            }

            index %= ready.len();
            let sub = &ready[index];
            index += 1;

            let recv_result = lock_recover(&sub.rx).try_recv();
            let notification = match recv_result {
                Ok(event) => Some(athena_protocol::EventNotification {
                    subscription_id: sub.subscription_id.clone(),
                    thread_id: event.thread_id,
                    turn_id: event.turn_id,
                    sequence: event.sequence,
                    kind: event.kind,
                    event_ref: event.event_ref,
                    data: event.data,
                }),
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {
                    // Yield the worker while idle without delaying newly queued events
                    // enough to make subscription delivery feel stalled.
                    tokio::time::sleep(tokio::time::Duration::from_millis(1)).await;
                    None
                }
                Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) => None,
            };

            if let Some(n) = notification
                && self.outgoing.send(n).await.is_err()
            {
                break;
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn journal_assigns_sequences_and_replays() {
        let journal = EventJournal::new("t1");

        let e1 = journal.append(EventDraft::new("started", "ev:1"));
        assert_eq!(e1.sequence, 1);
        let e2 = journal.append(EventDraft::new("delta", "ev:2").turn("turn-1"));
        assert_eq!(e2.sequence, 2);

        let events = journal.snapshot();
        assert_eq!(events.len(), 2);
        assert_eq!(events[1].kind, "delta");
    }

    #[tokio::test]
    async fn wait_for_next_wakes_on_append() {
        let journal = Arc::new(EventJournal::new("t1"));
        let j = journal.clone();
        let waiter = tokio::spawn(async move { j.wait_for_next(0).await });
        tokio::time::sleep(Duration::from_millis(20)).await;
        journal.append(EventDraft::new("first", "ev:1"));
        let event = tokio::time::timeout(Duration::from_secs(5), waiter)
            .await
            .expect("no timeout")
            .expect("joined");
        assert_eq!(event.sequence, 1);
    }

    #[tokio::test]
    async fn fair_mux_delivers_queued_subscriptions_round_robin() {
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel(8);
        let mux = Arc::new(FairMux::new(out_tx));
        let (a_tx, a_rx) = tokio::sync::mpsc::channel(4);
        let (b_tx, b_rx) = tokio::sync::mpsc::channel(4);
        mux.add(Arc::new(Subscription::new("a".into(), a_rx)));
        mux.add(Arc::new(Subscription::new("b".into(), b_rx)));
        for sequence in 1..=3 {
            let event = Event {
                thread_id: "thread".into(),
                turn_id: None,
                sequence,
                kind: "delta".into(),
                event_ref: format!("event:{sequence}"),
                data: None,
            };
            a_tx.send(event.clone()).await.expect("send a");
            b_tx.send(event).await.expect("send b");
        }
        a_tx.send(Event {
            thread_id: "thread".into(),
            turn_id: None,
            sequence: 4,
            kind: "delta".into(),
            event_ref: "event:4".into(),
            data: None,
        })
        .await
        .expect("queue event to observe closed outgoing channel");

        let mux_task = tokio::spawn(mux.clone().run());
        let mut delivered = Vec::new();
        for _ in 0..6 {
            delivered.push(
                tokio::time::timeout(Duration::from_millis(100), out_rx.recv())
                    .await
                    .expect("mux delivery timed out")
                    .expect("notification channel closed")
                    .subscription_id,
            );
        }
        assert_eq!(delivered.iter().filter(|id| *id == "a").count(), 3);
        assert_eq!(delivered.iter().filter(|id| *id == "b").count(), 3);
        assert_ne!(delivered[0], delivered[1]);
        assert_ne!(delivered[1], delivered[2]);

        mux_task.abort();
        let _ = mux_task.await;
    }

    #[tokio::test]
    async fn fair_mux_wakes_quickly_when_subscription_is_added() {
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel(2);
        let mux = Arc::new(FairMux::new(out_tx));
        let mux_task = tokio::spawn(mux.clone().run());
        tokio::time::sleep(Duration::from_millis(5)).await;

        let (event_tx, event_rx) = tokio::sync::mpsc::channel(1);
        mux.add(Arc::new(Subscription::new("late".into(), event_rx)));
        event_tx
            .send(Event {
                thread_id: "thread".into(),
                turn_id: None,
                sequence: 1,
                kind: "delta".into(),
                event_ref: "event:1".into(),
                data: None,
            })
            .await
            .expect("send event");
        event_tx
            .send(Event {
                thread_id: "thread".into(),
                turn_id: None,
                sequence: 2,
                kind: "delta".into(),
                event_ref: "event:2".into(),
                data: None,
            })
            .await
            .expect("queue event to observe closed outgoing channel");

        let notification = tokio::time::timeout(Duration::from_millis(100), out_rx.recv())
            .await
            .expect("late subscription was not polled promptly")
            .expect("notification channel closed");
        assert_eq!(notification.subscription_id, "late");
        mux_task.abort();
        let _ = mux_task.await;
    }

    #[tokio::test]
    async fn fair_mux_stops_when_outgoing_receiver_closes() {
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel(1);
        let mux = Arc::new(FairMux::new(out_tx));
        let (event_tx, event_rx) = tokio::sync::mpsc::channel(8);
        mux.add(Arc::new(Subscription::new("sub".into(), event_rx)));
        for sequence in 1..=8 {
            event_tx
                .send(Event {
                    thread_id: "thread".into(),
                    turn_id: None,
                    sequence,
                    kind: "delta".into(),
                    event_ref: format!("event:{sequence}"),
                    data: None,
                })
                .await
                .expect("queue event");
        }

        let mux_task = tokio::spawn(mux.run());
        tokio::time::timeout(Duration::from_millis(100), out_rx.recv())
            .await
            .expect("mux did not send first notification")
            .expect("notification channel closed too early");
        tokio::time::sleep(Duration::from_millis(5)).await;
        out_rx.close();
        tokio::time::timeout(Duration::from_millis(100), mux_task)
            .await
            .expect("mux did not stop after outgoing close")
            .expect("mux task panicked");
    }
}
