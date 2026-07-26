//! `athena-runtime` — per-thread actor runtime.
//!
//! Each thread is owned by a single actor task that serially processes commands
//! (start/interrupt/fork/shutdown) and runner completion signals. Terminal
//! events are appended only by the actor, so a completing turn and an interrupt
//! can race freely yet produce exactly one terminal. The runtime defines the
//! [`TurnRunner`] contract; `athena-agent` implements it.

pub mod event;
mod runner;
mod submission;
mod thread_actor;
mod thread_handle;
mod thread_manager;

pub use event::{Event, EventDraft, EventJournal, FairMux, Subscription};
pub use runner::{EmitError, EventSink, RunnerError, TurnInput, TurnOutput, TurnRunner};
pub use submission::{RuntimeError, ThreadState};
pub use thread_handle::ThreadHandle;
pub use thread_manager::RuntimeThreadManager;
