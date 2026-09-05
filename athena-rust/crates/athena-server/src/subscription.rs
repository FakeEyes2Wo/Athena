use athena_runtime::{Event, FairMux, RuntimeError, RuntimeThreadManager, Subscription};
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use tokio::sync::{Mutex, mpsc};
use tokio::task::JoinHandle;
use uuid::Uuid;

struct Entry {
    sub: Arc<Subscription>,
    pump: JoinHandle<()>,
}

/// Creates and tears down event subscriptions, pumping a thread's journal into
/// the subscription channel that `FairMux` drains onto the event lane.
pub struct SubscriptionRegistry {
    mux: Arc<FairMux>,
    manager: Arc<RuntimeThreadManager>,
    subs: Mutex<HashMap<String, Entry>>,
}

impl SubscriptionRegistry {
    pub fn new(mux: Arc<FairMux>, manager: Arc<RuntimeThreadManager>) -> Self {
        Self {
            mux,
            manager,
            subs: Mutex::new(HashMap::new()),
        }
    }

    /// Create a subscription after validating the thread exists.
    pub async fn create(
        &self,
        thread_id: &str,
        after_sequence: u64,
    ) -> Result<String, RuntimeError> {
        let handle = self.manager.get(thread_id).await?;
        let sub_id = format!("sub:{}", &Uuid::new_v4().simple().to_string()[..12]);
        let (tx, rx) = mpsc::channel::<Event>(256);
        let sub = Arc::new(Subscription::new(
            sub_id.clone(),
            thread_id.to_string(),
            after_sequence,
            rx,
        ));
        self.mux.add(sub.clone());

        let journal = handle.journal().clone();
        let pump_sub = sub.clone();
        let pump = tokio::spawn(async move {
            let mut index = after_sequence as usize;
            while pump_sub.active.load(Ordering::SeqCst) {
                let event = journal.wait_for_next(index).await;
                if !pump_sub.active.load(Ordering::SeqCst) || tx.send(event).await.is_err() {
                    break;
                }
                index += 1;
            }
        });

        self.subs
            .lock()
            .await
            .insert(sub_id.clone(), Entry { sub, pump });
        Ok(sub_id)
    }

    /// Remove a subscription: mark inactive, detach from the mux, stop the pump.
    pub async fn remove(&self, subscription_id: &str) {
        if let Some(entry) = self.subs.lock().await.remove(subscription_id) {
            entry.sub.active.store(false, Ordering::SeqCst);
            self.mux.remove(subscription_id);
            entry.pump.abort();
        }
    }

    pub async fn remove_all(&self) {
        let entries = std::mem::take(&mut *self.subs.lock().await);
        for (id, entry) in entries {
            entry.sub.active.store(false, Ordering::SeqCst);
            self.mux.remove(&id);
            entry.pump.abort();
        }
    }
}
