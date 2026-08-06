use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock, RwLockReadGuard, RwLockWriteGuard};
use tokio::sync::{Notify, watch};

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

    pub fn thread_id(&self) -> &str {
        &self.thread_id
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

    pub fn len(&self) -> usize {
        read_guard(&self.records).len()
    }

    pub fn is_empty(&self) -> bool {
        read_guard(&self.records).is_empty()
    }

    pub fn last_sequence(&self) -> u64 {
        *self.tail.borrow()
    }

    pub fn snapshot(&self) -> Vec<Event> {
        read_guard(&self.records).clone()
    }

    /// Events strictly after `after_index` (0-based position in the log).
    pub fn events_since(&self, after_index: usize) -> Vec<Event> {
        let records = read_guard(&self.records);
        if after_index >= records.len() {
            Vec::new()
        } else {
            records[after_index..].to_vec()
        }
    }

    /// A receiver for tail-sequence updates, for cursor-driven replay.
    pub fn subscribe_tail(&self) -> watch::Receiver<u64> {
        self.tail.subscribe()
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
    pub thread_id: String,
    pub cursor: RwLock<u64>,
    pub rx: std::sync::Mutex<tokio::sync::mpsc::Receiver<Event>>,
    pub has_data: Notify,
    pub active: AtomicBool,
    pub pump_handle: RwLock<Option<tokio::task::JoinHandle<()>>>,
}

impl Subscription {
    pub fn new(
        subscription_id: String,
        thread_id: String,
        cursor: u64,
        rx: tokio::sync::mpsc::Receiver<Event>,
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

/// Round-robin multiplexer — prevents starvation across subscriptions.
pub struct FairMux {
    subs: Arc<RwLock<HashMap<String, Arc<Subscription>>>>,
    outgoing: tokio::sync::mpsc::Sender<athena_protocol::EventNotification>,
}

fn lock_recover<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

impl FairMux {
    pub fn new(outgoing: tokio::sync::mpsc::Sender<athena_protocol::EventNotification>) -> Self {
        Self {
            subs: Arc::new(RwLock::new(HashMap::new())),
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
                tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
                continue;
            }

            index %= ready.len();
            let sub = &ready[index];
            index += 1;

            let recv_result = lock_recover(&sub.rx).try_recv();
            let notification = match recv_result {
                Ok(event) => Some(athena_protocol::EventNotification {
                    subscription_id: sub.subscription_id.clone(),
                    thread_id: event.thread_id.clone(),
                    turn_id: event.turn_id.clone(),
                    sequence: event.sequence,
                    kind: event.kind.clone(),
                    event_ref: event.event_ref.clone(),
                    data: event.data.clone(),
                }),
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {
                    sub.has_data.notify_one();
                    tokio::task::yield_now().await;
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
        assert!(journal.is_empty());
        assert_eq!(journal.last_sequence(), 0);

        let e1 = journal.append(EventDraft::new("started", "ev:1"));
        assert_eq!(e1.sequence, 1);
        let e2 = journal.append(EventDraft::new("delta", "ev:2").turn("turn-1"));
        assert_eq!(e2.sequence, 2);

        assert_eq!(journal.len(), 2);
        assert_eq!(journal.last_sequence(), 2);
        assert_eq!(journal.events_since(1).len(), 1);
        assert_eq!(journal.events_since(1)[0].kind, "delta");
        assert!(journal.events_since(2).is_empty());
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
}
