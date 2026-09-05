//! `athena-server` — the protocol server: transport, message-processor state
//! machine, execution adapter, event subscriptions over `FairMux`, and the
//! `AppServer` lifecycle that wires them to a connected client.

mod client;
mod execution;
mod lifecycle;
mod processor;
mod subscription;
pub mod transport;

pub use client::{AthenaClient, RpcException, Sequencer, ServerEvent};
pub use execution::{ExecutionAdapter, Executor, runtime_error_to_rpc};
pub use lifecycle::AppServer;
pub use processor::{DRAINING, INITIALIZING, MessageProcessor, READY, TERMINATED};
pub use subscription::SubscriptionRegistry;
pub use transport::{
    ServerResponder, Transport, TransportClientHalf, TransportError, TransportServerHalf,
};
