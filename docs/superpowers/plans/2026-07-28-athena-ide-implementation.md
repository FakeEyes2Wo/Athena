# Athena IDE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Tauri desktop GUI for the Athena AI4ML pipeline — chat-driven interaction, real-time experiment dashboard, file diff viewer, ResearchTree visualization, and metric charts.

**Architecture:** Tauri (Rust) spawns Python backend as a child process, communicates via WebSocket over localhost. Python side adds `src/athena/ide/` with a WebSocket server reusing the existing `app_server` protocol layer — zero modifications to pipeline code. Frontend is React + TypeScript with Monaco, ReactFlow, and Recharts.

**Tech Stack:** Tauri 2.x, Rust, React 18, TypeScript, Python 3.12+, websockets, Monaco Editor, ReactFlow, Recharts

## Global Constraints

- Zero modifications to existing pipeline code (`src/athena/core/`, `workflows/`, `app_server/`)
- Python 3.12+; Pydantic v2; all prompts in English
- Tauri 2.x (latest stable); Rust edition 2024
- WebSocket transport: reuse existing `RequestEnvelope`/`ResponseEnvelope`/`EventNotification` protocol
- Port negotiation: Python prints port to stdout line 1; Tauri reads it
- Tauri app directory: `athena-ide/` at repo root (sibling to `src/`)

---

## File Structure

```
athena-ide/                          # NEW: Tauri project root
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html
├── src/                             # React frontend
│   ├── main.tsx
│   ├── App.tsx
│   ├── App.css
│   ├── components/
│   │   ├── ChatPanel.tsx
│   │   ├── ExperimentDashboard.tsx
│   │   ├── FileTree.tsx
│   │   ├── DiffViewer.tsx
│   │   ├── ResearchTreeViz.tsx
│   │   └── MetricChart.tsx
│   ├── hooks/
│   │   ├── useEvents.ts
│   │   └── usePipeline.ts
│   └── lib/
│       └── tauri-bridge.ts
├── src-tauri/
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── build.rs
│   ├── icons/
│   └── src/
│       ├── main.rs
│       ├── python/
│       │   ├── mod.rs
│       │   ├── bridge.rs
│       │   └── types.rs
│       ├── commands/
│       │   ├── mod.rs
│       │   ├── chat.rs
│       │   ├── search.rs
│       │   └── validate.rs
│       └── events.rs

src/athena/ide/                      # NEW: Python IDE backend
├── __init__.py
├── __main__.py
├── transport.py
├── handler.py
└── stream.py
```

---

### Task 1: Python IDE Backend — WebSocket Server + Transport

**Files:**
- Create: `src/athena/ide/__init__.py`
- Create: `src/athena/ide/__main__.py`
- Create: `src/athena/ide/transport.py`
- Test: `tests/test_ide_transport.py`

**Interfaces:**
- Produces: `WebSocketTransport(host, port)` — wraps `websockets.serve()` with existing protocol; `main()` — entry point that prints port to stdout
- Consumes: `app_server.protocol` (RequestEnvelope, ResponseEnvelope, EventNotification), `app_server.server` (MessageProcessor)

- [ ] **Step 1: Write failing test**

```python
# tests/test_ide_transport.py
import asyncio, json
import websockets
from athena.ide.__main__ import start_server

async def test_server_starts_and_prints_port():
    """Server starts, prints port to stdout, accepts WS connection."""
    server, port = await start_server(test_mode=True)
    assert port > 0
    # Connect and send a ping
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"request_id": 1, "method": "ping", "params": {}}))
        resp = json.loads(await ws.recv())
        assert resp["request_id"] == 1
        assert "result" in resp
    server.close()
    await server.wait_closed()
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_ide_transport.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write __init__.py + __main__.py + transport.py**

```python
# src/athena/ide/__init__.py
"""Athena IDE — WebSocket backend for the Tauri desktop GUI."""

# src/athena/ide/transport.py
import asyncio, json
import websockets
from websockets.asyncio.server import ServerConnection
from athena.app_server.protocol import RequestEnvelope, ResponseEnvelope

class WebSocketTransport:
    """Bridges WebSocket messages to the existing app_server protocol layer."""

    def __init__(self, handler: "IDEHandler"):
        self._handler = handler

    async def handle(self, ws: ServerConnection):
        async for raw in ws:
            try:
                req = RequestEnvelope.model_validate(json.loads(raw))
                result = await self._handler.dispatch(req.method, req.params)
                resp = ResponseEnvelope(
                    request_id=req.request_id,
                    result=result,
                    error=None,
                )
                await ws.send(resp.model_dump_json())
            except Exception as e:
                err = ResponseEnvelope(request_id=0, result=None, error={"code": -1, "message": str(e)})
                await ws.send(err.model_dump_json())

    async def serve(self, host: str = "127.0.0.1", port: int = 0):
        return await websockets.serve(self.handle, host, port)


# src/athena/ide/__main__.py
import asyncio, sys
from athena.ide.transport import WebSocketTransport
from athena.ide.handler import IDEHandler

async def start_server(test_mode: bool = False):
    handler = IDEHandler()
    transport = WebSocketTransport(handler)
    server = await transport.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    if not test_mode:
        print(port, flush=True)
    return server, port

async def main():
    server, port = await start_server()
    await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Write stub IDEHandler so import works**

```python
# src/athena/ide/handler.py (stub for now)
class IDEHandler:
    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "ping":
            return {"pong": True}
        return {"error": f"unknown method: {method}"}
```

- [ ] **Step 5: Run test**

Run: `uv run pytest tests/test_ide_transport.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/athena/ide/ tests/test_ide_transport.py
git commit -m "feat(ide): add WebSocket server and transport layer"
```

---

### Task 2: Python IDE Backend — IDEHandler Methods

**Files:**
- Modify: `src/athena/ide/handler.py`
- Create: `tests/test_ide_handler.py`

**Interfaces:**
- Produces: `IDEHandler.dispatch(method, params) -> dict` with methods: PARSE_INTENT, SEARCH_START, SEARCH_PAUSE, SEARCH_RESUME, SEARCH_STOP, VALIDATE_START, REPORT_GENERATE
- Consumes: `workflows.search.search_loop` (SearchLoop), `workflows.prepare.evaluator_factory` (EvaluatorFactory), `core.schemas` (TaskMetaData)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ide_handler.py
import pytest
from athena.ide.handler import IDEHandler

@pytest.mark.asyncio
async def test_handler_ping():
    h = IDEHandler()
    result = await h.dispatch("ping", {})
    assert result == {"pong": True}

@pytest.mark.asyncio
async def test_handler_parse_intent():
    h = IDEHandler()
    result = await h.dispatch("PARSE_INTENT", {
        "message": "classify this tabular dataset, target is 'label', optimize f1"
    })
    assert "task_type" in result
    assert result["task_type"] == "classification"
    assert "primary_metric" in result
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_ide_handler.py::test_handler_parse_intent -v`
Expected: FAIL (unknown method or returns error)

- [ ] **Step 3: Implement IDEHandler with PARSE_INTENT**

```python
# src/athena/ide/handler.py
from athena.core.schemas import TaskMetaData, MetricSpec, MetricDef
from athena.core.budget import BudgetSnapshot

class IDEHandler:
    def __init__(self):
        self._active_loop = None
        self._active_task = None
        self._budget = BudgetSnapshot()

    async def dispatch(self, method: str, params: dict) -> dict:
        handlers = {
            "ping": self._ping,
            "PARSE_INTENT": self._parse_intent,
            "TASK_CONFIGURE": self._task_configure,
            "SEARCH_START": self._search_start,
            "SEARCH_PAUSE": self._search_pause,
            "SEARCH_RESUME": self._search_resume,
            "SEARCH_STOP": self._search_stop,
            "VALIDATE_START": self._validate_start,
            "REPORT_GENERATE": self._report_generate,
        }
        h = handlers.get(method)
        if h is None:
            return {"error": f"unknown method: {method}"}
        return await h(params)

    async def _ping(self, _):
        return {"pong": True}

    async def _parse_intent(self, params: dict) -> dict:
        """Use LLM to parse natural language into TaskMetaData preview.
        MVP: keyword-based parsing, no LLM call."""
        msg = params.get("message", "").lower()
        task_type = "classification"
        if "regression" in msg or "regress" in msg:
            task_type = "regression"
        if "cluster" in msg:
            task_type = "clustering"
        data_type = "tabular"
        if "image" in msg:
            data_type = "image"
        if "text" in msg or "nlp" in msg:
            data_type = "text"
        primary = "f1_macro"
        if "rmse" in msg or "mae" in msg:
            primary = "rmse"
        if "accuracy" in msg or "acc" in msg:
            primary = "accuracy"
        if "auc" in msg or "roc" in msg:
            primary = "roc_auc"
        return {
            "task_type": task_type,
            "data_type": data_type,
            "target_vars": [],
            "primary_metric": primary,
            "direction": "minimize" if primary in ("rmse", "mae", "mse") else "maximize",
            "needs_configuration": True,
        }

    async def _task_configure(self, params: dict) -> dict:
        self._active_task = TaskMetaData(
            task_type=params.get("task_type", "classification"),
            data_type=params.get("data_type", "tabular"),
            target_vars=params.get("target_vars", []),
            primary_metric=MetricSpec(
                name=params.get("primary_metric", "f1_macro"),
                direction=params.get("direction", "maximize"),
            ),
        )
        return {"configured": True, "task": self._active_task.model_dump()}

    async def _search_start(self, params: dict) -> dict:
        self._budget = BudgetSnapshot(
            remaining=params.get("max_experiments", 10),
            max_no_improve=params.get("max_no_improve", 3),
        )
        return {"phase": "SEARCH", "status": "started", "budget": self._budget.model_dump()}

    async def _search_pause(self, _):
        return {"paused": True}

    async def _search_resume(self, _):
        return {"resumed": True}

    async def _search_stop(self, _):
        return {"stopped": True}

    async def _validate_start(self, _):
        return {"phase": "VALIDATE", "status": "started"}

    async def _report_generate(self, _):
        return {"report_ref": "artifact://reports/placeholder"}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ide_handler.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/athena/ide/handler.py tests/test_ide_handler.py
git commit -m "feat(ide): add IDEHandler with PARSE_INTENT and SEARCH methods"
```

---

### Task 3: Python IDE Backend — StreamEmitter

**Files:**
- Create: `src/athena/ide/stream.py`
- Test: `tests/test_ide_stream.py`

**Interfaces:**
- Produces: `StreamEmitter(emit_callback: Callable)` with typed emit methods for each event kind
- Consumes: nothing external

- [ ] **Step 1: Write failing test**

```python
# tests/test_ide_stream.py
import pytest
from athena.ide.stream import StreamEmitter

@pytest.mark.asyncio
async def test_stream_emit_chat_token():
    events = []
    emitter = StreamEmitter(emit=lambda kind, data: events.append((kind, data)))
    await emitter.emit_chat_token("Hello")
    assert len(events) == 1
    assert events[0][0] == "chat/token"
    assert events[0][1]["text"] == "Hello"

@pytest.mark.asyncio
async def test_stream_emit_experiment_result():
    events = []
    emitter = StreamEmitter(emit=lambda kind, data: events.append((kind, data)))
    await emitter.emit_experiment_result("exp1", 0.78, "candidate", 0.01)
    assert len(events) == 1
    assert events[0][0] == "experiment/completed"
    assert events[0][1]["primary"] == 0.78
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_ide_stream.py -v`
Expected: FAIL

- [ ] **Step 3: Implement stream.py**

```python
# src/athena/ide/stream.py
from typing import Callable, Awaitable

EmitFn = Callable[[str, dict], Awaitable[None]]

class StreamEmitter:
    """Typed streaming event emitter. Each method maps to a spec-defined event kind."""

    def __init__(self, emit: EmitFn):
        self._emit = emit

    async def emit_chat_token(self, text: str):
        await self._emit("chat/token", {"text": text})

    async def emit_intent_parsed(self, task_preview: dict):
        await self._emit("chat/intent_parsed", task_preview)

    async def emit_experiment_started(self, experiment_id: str, hypothesis: str):
        await self._emit("experiment/started", {
            "experiment_id": experiment_id,
            "hypothesis": hypothesis,
        })

    async def emit_experiment_result(self, experiment_id: str, primary: float, winner: str, p_value: float):
        await self._emit("experiment/completed", {
            "experiment_id": experiment_id,
            "primary": primary,
            "winner": winner,
            "p_value": p_value,
        })

    async def emit_experiment_failed(self, experiment_id: str, error: str):
        await self._emit("experiment/failed", {
            "experiment_id": experiment_id,
            "error": error,
        })

    async def emit_phase_change(self, phase: str):
        await self._emit("phase/change", {"phase": phase})

    async def emit_budget_update(self, remaining: int, no_improve_streak: int, is_exhausted: bool):
        await self._emit("budget/update", {
            "remaining": remaining,
            "no_improve_streak": no_improve_streak,
            "is_exhausted": is_exhausted,
        })

    async def emit_tree_update(self, node_id: str, status: str, parent_id: str | None):
        await self._emit("tree/update", {
            "node_id": node_id,
            "status": status,
            "parent_id": parent_id,
        })
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ide_stream.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/athena/ide/stream.py tests/test_ide_stream.py
git commit -m "feat(ide): add StreamEmitter with typed event methods"
```

---

### Task 4: Scaffold Tauri Project

**Files:**
- Create: `athena-ide/` (entire Tauri project via `npm create tauri-app`)

**Note:** This task requires Tauri CLI. Run `cargo install tauri-cli --version "^2"` first if not installed.

- [ ] **Step 1: Create Tauri project**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
npm create tauri-app@latest athena-ide -- --template react-ts --manager npm
```

Expected: Creates `athena-ide/` with React + TypeScript + Tauri 2 template

- [ ] **Step 2: Verify it builds**

```bash
cd athena-ide
npm install
npm run tauri dev  # quick smoke test, then Ctrl+C
```

Expected: Tauri window opens with default React template. Kill the process after confirming.

- [ ] **Step 3: Add WebSocket dependency to Cargo.toml**

Edit `athena-ide/src-tauri/Cargo.toml`, add under `[dependencies]`:

```toml
tokio-tungstenite = { version = "0.24", features = ["native-tls"] }
futures-util = "0.3"
serde_json = "1"
```

- [ ] **Step 4: Run cargo check**

```bash
cd athena-ide/src-tauri
cargo check
```

Expected: Compiles without errors.

- [ ] **Step 5: Add frontend dependencies**

```bash
cd athena-ide
npm install @tauri-apps/api @tauri-apps/plugin-shell
npm install reactflow recharts monaco-editor @monaco-editor/react
```

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add athena-ide/
git commit -m "feat(ide): scaffold Tauri project with React + TypeScript"
```

---

### Task 5: Tauri Rust — PythonBridge

**Files:**
- Create: `athena-ide/src-tauri/src/python/mod.rs`
- Create: `athena-ide/src-tauri/src/python/bridge.rs`
- Create: `athena-ide/src-tauri/src/python/types.rs`
- Modify: `athena-ide/src-tauri/src/main.rs`

**Interfaces:**
- Produces: `PythonBridge::start() -> Self`, `PythonBridge::call(method, params) -> Value`, `PythonBridge::subscribe() -> Receiver<EventNotification>`
- Consumes: std::process, tokio-tungstenite, serde_json

- [ ] **Step 1: Write types.rs**

```rust
// athena-ide/src-tauri/src/python/types.rs
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RequestEnvelope {
    pub request_id: u64,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ResponseEnvelope {
    pub request_id: u64,
    pub result: Option<Value>,
    pub error: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct EventNotification {
    pub subscription_id: Option<String>,
    pub kind: String,
    pub data: Value,
}
```

- [ ] **Step 2: Write bridge.rs**

```rust
// athena-ide/src-tauri/src/python/bridge.rs
use std::collections::HashMap;
use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::{broadcast, oneshot, Mutex};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use futures_util::StreamExt;
use serde_json::Value;
use crate::python::types::*;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

pub struct PythonBridge {
    child: Mutex<Child>,
    ws_send: Mutex<futures_util::stream::SplitSink<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>
        >,
        Message,
    >>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    events: broadcast::Sender<EventNotification>,
}

impl PythonBridge {
    pub async fn start() -> Result<Self, String> {
        // 1. Spawn Python process
        let mut child = Command::new("uv")
            .args(["run", "python", "-m", "athena.ide"])
            .current_dir(env!("CARGO_MANIFEST_DIR").replace("\\src-tauri", "").replace("\\athena-ide", ""))
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("Failed to spawn Python: {}", e))?;

        // 2. Read port from stdout line 1
        let stdout = child.stdout.take().ok_or("No stdout")?;
        let mut reader = std::io::BufReader::new(stdout);
        let mut port_line = String::new();
        reader.read_line(&mut port_line).map_err(|e| format!("Failed to read port: {}", e))?;
        let port: u16 = port_line.trim().parse().map_err(|e| format!("Invalid port: {}", e))?;

        // Spawn a task to keep reading stdout (subsequent lines are logs)
        tokio::spawn(async move {
            let mut line = String::new();
            loop {
                line.clear();
                // reader is moved here — in production, use async read
                let _ = std::io::BufRead::read_line(&mut reader, &mut line);
            }
        });

        // 3. Connect WebSocket
        let url = format!("ws://127.0.0.1:{}", port);
        let (ws, _) = connect_async(&url).await.map_err(|e| format!("WS connect failed: {}", e))?;
        let (ws_send, mut ws_recv) = ws.split();
        let ws_send = Mutex::new(ws_send);

        let (events_tx, _) = broadcast::channel(256);
        let events_clone = events_tx.clone();

        // 4. Spawn receive loop
        let pending: Mutex<HashMap<u64, oneshot::Sender<Value>>> = Mutex::new(HashMap::new());
        tokio::spawn(async move {
            while let Some(msg) = ws_recv.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(val) = serde_json::from_str::<Value>(&text) {
                            if let Some(rid) = val.get("request_id").and_then(|v| v.as_u64()) {
                                // Response to pending request
                                let tx = {
                                    let mut p = pending.lock().await;
                                    p.remove(&rid)
                                };
                                if let Some(tx) = tx {
                                    let _ = tx.send(val.get("result").cloned().unwrap_or(Value::Null));
                                }
                            } else if val.get("kind").is_some() {
                                // Event notification
                                if let Ok(evt) = serde_json::from_value::<EventNotification>(val) {
                                    let _ = events_clone.send(evt);
                                }
                            }
                        }
                    }
                    Ok(Message::Close(_)) => break,
                    Err(_) => break,
                    _ => {}
                }
            }
        });

        Ok(Self {
            child: Mutex::new(child),
            ws_send,
            pending,
            events: events_tx,
        })
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let id = NEXT_ID.fetch_add(1, Ordering::SeqCst);
        let req = serde_json::json!({
            "request_id": id,
            "method": method,
            "params": params,
        });
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        self.ws_send.lock().await
            .send(Message::Text(req.to_string()))
            .await
            .map_err(|e| format!("WS send failed: {}", e))?;
        rx.await.map_err(|_| "Request cancelled".to_string())
    }

    pub fn subscribe(&self) -> broadcast::Receiver<EventNotification> {
        self.events.subscribe()
    }
}
```

- [ ] **Step 3: Write mod.rs**

```rust
// athena-ide/src-tauri/src/python/mod.rs
pub mod types;
pub mod bridge;
```

- [ ] **Step 4: Update main.rs**

```rust
// athena-ide/src-tauri/src/main.rs
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod python;
mod commands;
mod events;

use python::bridge::PythonBridge;
use std::sync::Arc;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let bridge = tauri::async_runtime::block_on(async {
                PythonBridge::start().await.expect("Failed to start Python backend")
            });
            app.manage(Arc::new(bridge));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::chat::send_message,
            commands::search::start_search,
            commands::search::pause_search,
            commands::search::resume_search,
            commands::search::stop_search,
            commands::validate::start_validation,
            commands::validate::generate_report,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

- [ ] **Step 5: Write stub command modules**

```rust
// athena-ide/src-tauri/src/commands/mod.rs
pub mod chat;
pub mod search;
pub mod validate;

// athena-ide/src-tauri/src/commands/chat.rs
use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn send_message(state: State<'_, Arc<PythonBridge>>, message: String) -> Result<serde_json::Value, String> {
    state.call("PARSE_INTENT", json!({"message": message})).await
}

// athena-ide/src-tauri/src/commands/search.rs
use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn start_search(state: State<'_, Arc<PythonBridge>>, config: serde_json::Value) -> Result<serde_json::Value, String> {
    state.call("SEARCH_START", config).await
}
#[tauri::command]
pub async fn pause_search(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("SEARCH_PAUSE", json!({})).await
}
#[tauri::command]
pub async fn resume_search(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("SEARCH_RESUME", json!({})).await
}
#[tauri::command]
pub async fn stop_search(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("SEARCH_STOP", json!({})).await
}

// athena-ide/src-tauri/src/commands/validate.rs
use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn start_validation(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("VALIDATE_START", json!({})).await
}
#[tauri::command]
pub async fn generate_report(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("REPORT_GENERATE", json!({})).await
}
```

- [ ] **Step 6: Write events.rs**

```rust
// athena-ide/src-tauri/src/events.rs
use tauri::{AppHandle, Emitter};
use std::sync::Arc;
use crate::python::bridge::PythonBridge;

pub fn start_event_relay(app: AppHandle, bridge: Arc<PythonBridge>) {
    let mut rx = bridge.subscribe();
    tauri::async_runtime::spawn(async move {
        while let Ok(evt) = rx.recv().await {
            let _ = app.emit(&evt.kind, serde_json::json!({
                "kind": evt.kind,
                "data": evt.data,
            }));
        }
    });
}
```

- [ ] **Step 7: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add athena-ide/src-tauri/
git commit -m "feat(ide): add PythonBridge and Tauri commands"
```

---

### Task 6: React Frontend — tauri-bridge + useEvents + usePipeline

**Files:**
- Create: `athena-ide/src/lib/tauri-bridge.ts`
- Create: `athena-ide/src/hooks/useEvents.ts`
- Create: `athena-ide/src/hooks/usePipeline.ts`

- [ ] **Step 1: Write tauri-bridge.ts**

```typescript
// athena-ide/src/lib/tauri-bridge.ts
import { invoke } from "@tauri-apps/api/core";
import { listen, UnlistenFn } from "@tauri-apps/api/event";

export interface TaskPreview {
  task_type: string;
  data_type: string;
  target_vars: string[];
  primary_metric: string;
  direction: string;
  needs_configuration: boolean;
}

export interface BudgetState {
  remaining: number;
  no_improve_streak: number;
  is_exhausted: boolean;
}

export interface ExperimentEvent {
  kind: string;
  data: {
    experiment_id?: string;
    hypothesis?: string;
    primary?: number;
    winner?: string;
    p_value?: number;
    phase?: string;
    remaining?: number;
  };
}

export async function sendMessage(msg: string): Promise<TaskPreview> {
  return invoke("send_message", { message: msg });
}

export async function startSearch(config: Record<string, unknown>): Promise<unknown> {
  return invoke("start_search", { config });
}

export async function pauseSearch(): Promise<unknown> {
  return invoke("pause_search");
}

export async function resumeSearch(): Promise<unknown> {
  return invoke("resume_search");
}

export async function stopSearch(): Promise<unknown> {
  return invoke("stop_search");
}

export async function startValidation(): Promise<unknown> {
  return invoke("start_validation");
}

export async function generateReport(): Promise<unknown> {
  return invite("generate_report");
}

export function onEvent(handler: (evt: ExperimentEvent) => void): Promise<UnlistenFn> {
  return listen<ExperimentEvent>("experiment/completed", (e) => handler(e.payload));
}
```

- [ ] **Step 2: Write useEvents.ts + usePipeline.ts**

```typescript
// athena-ide/src/hooks/useEvents.ts
import { useEffect, useState } from "react";
import { onEvent, ExperimentEvent } from "../lib/tauri-bridge";

export function useEvents() {
  const [events, setEvents] = useState<ExperimentEvent[]>([]);
  useEffect(() => {
    const unlisten = onEvent((evt) => {
      setEvents((prev) => [...prev.slice(-100), evt]);
    });
    return () => { unlisten.then((u) => u()); };
  }, []);
  return events;
}

// athena-ide/src/hooks/usePipeline.ts
import { useState, useCallback } from "react";
import { sendMessage, startSearch, pauseSearch, TaskPreview, BudgetState } from "../lib/tauri-bridge";

export function usePipeline() {
  const [phase, setPhase] = useState<string>("idle");
  const [budget, setBudget] = useState<BudgetState>({ remaining: 0, no_improve_streak: 0, is_exhausted: false });
  const [sota, setSota] = useState<number>(0);

  const handleSend = useCallback(async (msg: string): Promise<TaskPreview> => {
    return sendMessage(msg);
  }, []);

  const handleStartSearch = useCallback(async (config: Record<string, unknown>) => {
    setPhase("SEARCH");
    await startSearch(config);
  }, []);

  const handlePause = useCallback(async () => {
    await pauseSearch();
  }, []);

  return { phase, budget, sota, handleSend, handleStartSearch, handlePause };
}
```

- [ ] **Step 3: Verify TypeScript compilation**

```bash
cd athena-ide
npx tsc --noEmit
```

Expected: No type errors.

- [ ] **Step 4: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add athena-ide/src/lib/ athena-ide/src/hooks/
git commit -m "feat(ide): add tauri-bridge, useEvents, usePipeline hooks"
```

---

### Task 7: React Frontend — App Shell + ChatPanel

**Files:**
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/App.css`
- Create: `athena-ide/src/components/ChatPanel.tsx`

- [ ] **Step 1: Write App.tsx shell with layout**

```tsx
// athena-ide/src/App.tsx
import { useState } from "react";
import ChatPanel from "./components/ChatPanel";
import ExperimentDashboard from "./components/ExperimentDashboard";
import FileTree from "./components/FileTree";
import DiffViewer from "./components/DiffViewer";
import ResearchTreeViz from "./components/ResearchTreeViz";
import MetricChart from "./components/MetricChart";
import { usePipeline, useEvents } from "./hooks/usePipeline";
import "./App.css";

function App() {
  const pipeline = usePipeline();
  const events = useEvents(); // imported directly, not from usePipeline
  const [activeTab, setActiveTab] = useState<"files" | "diff">("files");

  return (
    <div className="app-container">
      <div className="left-panel">
        <ChatPanel pipeline={pipeline} />
      </div>
      <div className="right-panel">
        <ExperimentDashboard events={events as any} pipeline={pipeline} />
        <div className="bottom-panels">
          <div className="tab-bar">
            <button onClick={() => setActiveTab("files")}>Files</button>
            <button onClick={() => setActiveTab("diff")}>Diff</button>
          </div>
          {activeTab === "files" ? <FileTree /> : <DiffViewer />}
          <ResearchTreeViz />
          <MetricChart />
        </div>
      </div>
    </div>
  );
}

export default App;
```

- [ ] **Step 2: Write App.css layout**

```css
/* athena-ide/src/App.css */
.app-container { display: flex; height: 100vh; font-family: system-ui; }
.left-panel { width: 380px; border-right: 1px solid #333; display: flex; flex-direction: column; }
.right-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.bottom-panels { flex: 1; display: flex; flex-direction: column; }
.tab-bar { display: flex; gap: 8px; padding: 8px; border-bottom: 1px solid #333; }
.tab-bar button { background: #1e1e1e; color: #ccc; border: 1px solid #444; padding: 4px 12px; cursor: pointer; }
```

- [ ] **Step 3: Write ChatPanel.tsx**

```tsx
// athena-ide/src/components/ChatPanel.tsx
import { useState, useRef, useEffect } from "react";

interface ChatPanelProps {
  pipeline: {
    handleSend: (msg: string) => Promise<{ task_type: string; primary_metric: string; needs_configuration: boolean }>;
    handleStartSearch: (config: Record<string, unknown>) => Promise<void>;
    phase: string;
  };
}

interface Message { role: "user" | "athena"; content: string; preview?: Record<string, unknown>; }

export default function ChatPanel({ pipeline }: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const handleSend = async () => {
    if (!input.trim()) return;
    const userMsg: Message = { role: "user", content: input };
    setMessages((m) => [...m, userMsg]);
    setInput("");
    try {
      const preview = await pipeline.handleSend(input);
      const athenaMsg: Message = {
        role: "athena",
        content: `Task: ${preview.task_type} | Primary: ${preview.primary_metric}${preview.needs_configuration ? " — please configure before starting search" : ""}`,
        preview: preview as any,
      };
      setMessages((m) => [...m, athenaMsg]);
    } catch (e: any) {
      setMessages((m) => [...m, { role: "athena", content: `Error: ${e}` }]);
    }
  };

  const handleConfirm = async (preview: Record<string, unknown>) => {
    await pipeline.handleStartSearch({ max_experiments: 10, ...preview });
    setMessages((m) => [...m, { role: "athena", content: "Search started. Monitoring experiments..." }]);
  };

  return (
    <div className="chat-panel">
      <div className="chat-header">Athena IDE</div>
      <div className="chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            <div className="msg-content">{m.content}</div>
            {m.preview?.needs_configuration && (
              <button onClick={() => handleConfirm(m.preview!)}>Confirm & Start</button>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <div className="chat-input">
        <input value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSend()}
          placeholder="Describe your ML task..." />
        <button onClick={handleSend}>Send</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Verify compilation**

```bash
cd athena-ide && npx tsc --noEmit
```

Expected: No errors.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add athena-ide/src/App.tsx athena-ide/src/App.css athena-ide/src/components/ChatPanel.tsx
git commit -m "feat(ide): add App shell layout and ChatPanel"
```

---

### Task 8: React Frontend — Dashboard + FileTree + DiffViewer

**Files:**
- Create: `athena-ide/src/components/ExperimentDashboard.tsx`
- Create: `athena-ide/src/components/FileTree.tsx`
- Create: `athena-ide/src/components/DiffViewer.tsx`

- [ ] **Step 1: Write ExperimentDashboard.tsx**

```tsx
// athena-ide/src/components/ExperimentDashboard.tsx
interface ExpEntry { experiment_id: string; primary: number; winner: string; hypothesis: string; }
interface Props { events: any[]; pipeline: { phase: string; budget: { remaining: number; no_improve_streak: number; is_exhausted: boolean }; sota: number }; }

export default function ExperimentDashboard({ events, pipeline }: Props) {
  const experiments: ExpEntry[] = events
    .filter((e) => e.kind === "experiment/completed")
    .map((e) => ({
      experiment_id: e.data.experiment_id ?? "?",
      primary: e.data.primary ?? 0,
      winner: e.data.winner ?? "?",
      hypothesis: e.data.hypothesis ?? "?",
    }));

  return (
    <div className="dashboard">
      <div className="dash-header">
        Phase: {pipeline.phase} | Budget: {pipeline.budget.remaining} | SOTA: {pipeline.sota.toFixed(4)} | Streak: {pipeline.budget.no_improve_streak}
      </div>
      <div className="experiment-log">
        <h3>Experiment Log</h3>
        {experiments.map((exp, i) => (
          <div key={i} className={`exp-row ${exp.winner}`}>
            #{i + 1} {exp.hypothesis.slice(0, 30)} → {exp.primary.toFixed(4)} {exp.winner === "candidate" ? "↑" : "↓"}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write stub FileTree + DiffViewer**

```tsx
// athena-ide/src/components/FileTree.tsx
export default function FileTree() {
  return <div className="file-tree"><h3>Files</h3><p>Select an experiment to browse files.</p></div>;
}

// athena-ide/src/components/DiffViewer.tsx
export default function DiffViewer() {
  return <div className="diff-viewer"><h3>Diff</h3><p>Select a file to view changes.</p></div>;
}
```

- [ ] **Step 3: Write stub ResearchTreeViz + MetricChart**

```tsx
// athena-ide/src/components/ResearchTreeViz.tsx
export default function ResearchTreeViz() {
  return <div className="tree-viz"><h3>Research Tree</h3><p>DAG visualization coming soon.</p></div>;
}

// athena-ide/src/components/MetricChart.tsx
export default function MetricChart() {
  return <div className="metric-chart"><h3>Metrics</h3><p>Trend chart coming soon.</p></div>;
}
```

- [ ] **Step 4: Verify compilation**

```bash
cd athena-ide && npx tsc --noEmit
```

Expected: No errors.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add athena-ide/src/components/
git commit -m "feat(ide): add ExperimentDashboard, FileTree, DiffViewer, viz stubs"
```

---

### Task 9: Integration — End-to-end Smoke Test

**Files:**
- Create: `tests/test_ide_e2e.py` — Python backend e2e
- Modify: `athena-ide/src-tauri/tests/` — none yet, verify manually

- [ ] **Step 1: Write Python e2e test**

```python
# tests/test_ide_e2e.py
import asyncio, json, pytest, websockets
from athena.ide.__main__ import start_server

@pytest.mark.asyncio
async def test_full_chat_flow():
    """Chat message -> PARSE_INTENT -> TASK_CONFIGURE -> SEARCH_START"""
    server, port = await start_server(test_mode=True)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Chat
        await ws.send(json.dumps({"request_id": 1, "method": "PARSE_INTENT", "params": {"message": "classify tabular data, target label, optimize f1"}}))
        resp = json.loads(await ws.recv())
        assert resp["result"]["task_type"] == "classification"
        assert resp["result"]["primary_metric"] == "f1_macro"

        # Configure
        await ws.send(json.dumps({"request_id": 2, "method": "TASK_CONFIGURE", "params": resp["result"]}))
        resp2 = json.loads(await ws.recv())
        assert resp2["result"]["configured"] == True

        # Start search
        await ws.send(json.dumps({"request_id": 3, "method": "SEARCH_START", "params": {"max_experiments": 3}}))
        resp3 = json.loads(await ws.recv())
        assert resp3["result"]["phase"] == "SEARCH"
        assert resp3["result"]["status"] == "started"

    server.close()
    await server.wait_closed()
```

- [ ] **Step 2: Run e2e test**

```bash
uv run pytest tests/test_ide_e2e.py -v
```

Expected: PASS

- [ ] **Step 3: Run all IDE tests**

```bash
uv run pytest tests/test_ide_*.py -v
```

Expected: All PASS

- [ ] **Step 4: Verify Tauri project compiles**

```bash
cd athena-ide && npx tsc --noEmit
cd src-tauri && cargo check
```

Expected: No errors.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git add tests/test_ide_e2e.py
git commit -m "test(ide): add end-to-end integration test"
```

---

## Execution Order

Tasks must run sequentially: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

- Tasks 1-3: Python IDE backend (no Tauri dependency)
- Task 4: Scaffold Tauri (needs `cargo install tauri-cli`)
- Task 5: Rust PythonBridge (depends on Tasks 1-3 Python backend + Task 4 Tauri project)
- Tasks 6-8: React frontend (depends on Task 5 commands being defined)
- Task 9: Integration test (depends on everything)
