use crate::client::AthenaClient;
use crate::execution::ExecutionAdapter;
use crate::processor::MessageProcessor;
use crate::subscription::SubscriptionRegistry;
use crate::transport::transport;
use athena_runtime::{FairMux, RuntimeThreadManager};
use std::sync::Arc;
use std::time::Duration;
use tokio::task::JoinHandle;

use crate::client::RpcException;

/// Top-level in-process AppServer: wires transport, mux, subscriptions, the
/// message processor, and a connected client through the initialize handshake.
pub struct AppServer {
    pub client: AthenaClient,
    processor: MessageProcessor,
    manager: Arc<RuntimeThreadManager>,
    subscriptions: Arc<SubscriptionRegistry>,
    mux: JoinHandle<()>,
}

impl AppServer {
    /// Create and fully initialize the server and its client.
    pub async fn create(manager: Arc<RuntimeThreadManager>) -> Result<Self, RpcException> {
        let (client_half, server_half) = transport();

        let mux = Arc::new(FairMux::new(server_half.event_sender()));
        let mux_handle = tokio::spawn(mux.clone().run());

        let subscriptions = Arc::new(SubscriptionRegistry::new(mux.clone(), manager.clone()));
        let executor = Arc::new(ExecutionAdapter::new(
            manager.clone(),
            subscriptions.clone(),
        ));
        let processor = MessageProcessor::start(server_half, executor);

        let client = AthenaClient::start(client_half).await?;

        Ok(Self {
            client,
            processor,
            manager,
            subscriptions,
            mux: mux_handle,
        })
    }

    /// Ordered shutdown: admission → subscriptions → threads → transport.
    pub async fn shutdown(&self) {
        self.client.shutdown(Duration::from_secs(2)).await;
        self.processor.shutdown(Duration::from_secs(2)).await;
        self.subscriptions.remove_all().await;
        self.mux.abort();
        self.manager.close().await;
    }
}
