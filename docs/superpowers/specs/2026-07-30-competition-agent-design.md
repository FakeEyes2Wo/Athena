# Competition Agent (TaskUnderstandAgent) Design

**Date:** 2026-07-30
**Author:** YeBai
**Status:** Approved — ready for implementation plan

## 1. Problem Statement

Build a Competition Agent within the Athena AI4S system that accepts a user prompt (competition name or URL) and autonomously completes the competition pipeline: researching competition information → downloading and augmenting datasets → building and executing a baseline solution → producing a submission file.

**Scope:** Kaggle-style competition platforms for the initial implementation.

**Execution mode:** Fully automatic by default, interruptible by the user at any point. State is preserved via `next_context_ref` so work can resume from the breakpoint.

## 2. Architecture

### 2.1 Module ownership boundary

The author is responsible for:

- `src/athena/agents/competition/task_understand_agent.py` — new, the Competition Agent entry point
- `src/athena/tools/kaggle_search.py` — new, Kaggle MCP wrapper
- `src/athena/tools/hf_dataset.py` — new, HuggingFace dataset search + download
- `src/athena/tools/hf_model.py` — new, HuggingFace model search + download
- `src/athena/tools/data_prepare.py` — fill existing stubs, data analysis + cleaning code generation
- `src/athena/tools/baseline_builder.py` — new, solution design + project code generation + submission build

The author does NOT touch:

- `src/athena/agents/search/research_agent.py` — reserved for later iterative improvement work
- `src/athena/agents/control/` — Scheduler, owned by others
- `src/athena/agents/policy/` — Supervisor, owned by others

The following existing files are filled (their docstrings are implemented):
- `workflows/prepare/task_parser.py` — called by `kaggle_competition_search` to produce `TaskMetaData` from scraped competition info
- `workflows/prepare/dataset_service.py` — called by `kaggle_dataset_download` and `hf_dataset_download` to create immutable data copies, content fingerprints, and `DataCard` artifacts.

`agents/prepare/data_agent.py` is deprecated — its responsibilities are absorbed into the tool layer under TaskUnderstandAgent.

### 2.2 High-level architecture

```
User prompt (competition name/URL)
        │
        ▼
┌─────────────────────────────────────────┐
│         TaskUnderstandAgent             │
│   (extends BaseAgent, ReAct loop)       │
│                                         │
│   system_prompt: "You are a Kaggle      │
│   competition agent. Understand user    │
│   intent, decide tool call order        │
│   autonomously, produce artifacts       │
│   at each stage."                       │
│                                         │
│   Supports cancel via AgentContext.cancel│
└──────────┬──────────────────────────────┘
           │ holds ToolRegistry
           ▼
┌──────────────────────────────────────────┐
│               Tool Layer                  │
│                                          │
│   Search & Understand                    │
│   ├─ kaggle_competition_search   (new)   │
│   ├─ kaggle_discussion_search    (new)   │
│   └─ web_research_search       (existing)│
│                                          │
│   Data Acquisition                       │
│   ├─ kaggle_dataset_download     (new)   │
│   ├─ hf_dataset_search           (new)   │
│   ├─ hf_dataset_download         (new)   │
│   ├─ hf_model_search             (new)   │
│   └─ hf_model_download           (new)   │
│                                          │
│   Data Preparation (fill stubs)          │
│   ├─ data_analyze                      │
│   └─ data_clean_code_gen               │
│                                          │
│   Modeling & Submission                  │
│   ├─ solution_design                    │
│   ├─ project_code_gen                   │
│   ├─ code_execute                       │
│   └─ submission_build                   │
└──────────────────────────────────────────┘
```

**Key design decision:** TaskUnderstandAgent is the single agent entry. It uses tools as its capability surface. It is NOT a multi-agent system. The ReAct loop naturally handles the "run code → fail → fix → re-run" cycle without an external orchestrator.

## 3. Tool Interface Contracts

### 3.1 Search & Understand

| Tool | Input | Output |
|------|-------|--------|
| `kaggle_competition_search` | `competition_url: str` | `TaskMetaData` (task_type, data_type, target_vars, primary_metric, constraints) + full competition description artifact |
| `kaggle_discussion_search` | `competition_url: str, top_k: int = 10` | Discussion summary list artifact: title, author, approach summary, score, link per entry |
| `web_research_search` | existing tool, reused as-is | External blog/paper summaries |

### 3.2 Data Acquisition

| Tool | Input | Output |
|------|-------|--------|
| `kaggle_dataset_download` | `competition_ref: str, output_dir: str` | `DataCard` (dataset_ref, fingerprint, schema_ref) |
| `hf_dataset_search` | `task_keywords: str, modality: str, n_results: int` | Matching dataset list: name, description, size, license, download_count per entry |
| `hf_dataset_download` | `hf_dataset_id: str, output_dir: str` | `DataCard` |
| `hf_model_search` | `task_type: str, modality: str, architecture_hint: str, n_results: int` | Matching model list: model_id, params, framework, downloads, last_modified per entry |
| `hf_model_download` | `hf_model_id: str, output_dir: str` | `model_path: ArtifactRef` |

### 3.3 Data Preparation

| Tool | Input | Output |
|------|-------|--------|
| `data_analyze` | `DataCard` refs (competition + augmentation data) | EDA report artifact: distributions, missing values, outliers, feature correlations, class balance |
| `data_clean_code_gen` | EDA report + DataCard refs | Cleaning script artifact + cleaned data DataCard (post-execution) |

### 3.4 Modeling & Submission

| Tool | Input | Output |
|------|-------|--------|
| `solution_design` | TaskMetaData + EDA report + research summaries + available model list | Solution plan artifact: model selection, pipeline structure, training strategy, loss/metric choices |
| `project_code_gen` | Solution plan + DataCard + model_path + submission format spec | Complete project code artifact: model.py, dataset.py, train.py, infer.py, config.yaml |
| `code_execute` | Code artifact + entry_point (`train` / `infer`) | Execution log + metrics + checkpoint path (train) or predictions path (infer). Uses existing `athena/execution/sandbox_runtime.py` for isolation |
| `submission_build` | Predictions path + submission format spec | `submission.csv` artifact |

### 3.5 Data flow

```
User prompt
 │  TaskUnderstandAgent ReAct loop
 │
 ├─→ kaggle_competition_search  ──→ TaskMetaData
 │   kaggle_discussion_search   ──→ discussion_notes (artifact)
 │   web_research_search        ──→ external_refs (artifact)
 │
 ├─→ kaggle_dataset_download    ──→ raw_data_card (DataCard)
 │   hf_dataset_search          ──→ hf_candidates (artifact)
 │   hf_dataset_download        ──→ aug_data_card (DataCard)
 │
 ├─→ data_analyze               ──→ eda_report (artifact)
 │   data_clean_code_gen        ──→ clean_data_card (DataCard)
 │
 ├─→ hf_model_search            ──→ model_candidates (artifact)
 │   hf_model_download           ──→ model_weights_path (ArtifactRef)
 │
 ├─→ solution_design             ──→ solution_plan (artifact)
 │   project_code_gen            ──→ code_artifact (artifact)
 │   code_execute (train)        ──→ train_log + checkpoint (artifact)
 │   code_execute (infer)        ──→ predictions (artifact)
 │
 └─→ submission_build            ──→ submission.csv (artifact)
```

All tool outputs pass through artifact references in `AgentContext`. Tools are decoupled — each tool only depends on artifact refs, not on other tools directly. TaskUnderstandAgent maintains the "what stage are we at, what next" context.

## 4. Error Handling & Recovery

### 4.1 Per-layer failure modes

| Layer | Typical failure | Recovery strategy |
|-------|----------------|-------------------|
| Kaggle search | URL unreachable, page structure changed | Retry ×2 → fallback to `web_research_search` generic search |
| HF search | No matching datasets/models found | Return empty list, suggest LLM adjust search terms via tool response |
| HF download | Network interruption, disk full, rate limiting | Resume partial downloads, retry ×3, report error to LLM on exhaustion |
| Data cleaning | Cleaning script execution error | `code_execute` captures stderr → feedback to LLM → fix → re-run (max 3 rounds) |
| Train/Inference | OOM, CUDA error, logic bug | Same pattern: capture → feedback to LLM → fix → re-run (max 3 rounds) |
| Submission build | Format mismatch with spec | Feed format spec + current output to LLM for correction |

### 4.2 LLM decision quality

- **Rubric-bound scoring** — `solution_design` output includes a self-check rubric. `code_execute` failure info is fed back against the rubric for targeted fixes.
- **Structured outputs** — Every tool return is a Pydantic model or explicit schema, never loose text.
- **Evidence chain** — Every decision (model choice, skip data augmentation, etc.) carries a reasoning trace written to an artifact for later stages to reference.

### 4.3 User interruption

- `AgentContext.cancel` Event is controlled by the outer `ThreadRuntime`.
- User sends interrupt → `cancel.set()` → Agent loop finishes current iteration → `break` → saves state to `next_context_ref`.
- Resume: restore context from `next_context_ref`, Agent continues from breakpoint.

## 5. Testing Strategy

```
test/
├── unit/
│   └── tools/
│       ├── test_kaggle_search.py       # Mock Kaggle MCP responses
│       ├── test_hf_dataset.py          # Mock HF CLI output
│       ├── test_hf_model.py            # Mock HF CLI output
│       ├── test_data_prepare.py        # Fixed dataset input, validate EDA/cleaning outputs
│       ├── test_baseline_builder.py    # Mock LLM → validate code structure completeness
│       └── test_code_execute.py        # Sandbox execution validation
├── fixtures/
│   ├── mock_competition_page.html      # Simulated Kaggle competition page
│   ├── mock_discussion_kernel.ipynb    # Simulated Discussion notebook
│   └── sample_dataset/                 # Small sample dataset for integration tests
└── integration/
    └── test_task_understand_agent.py   # End-to-end: given prompt, validate full pipeline output
```

- **Unit tests (tools):** Mock external dependencies (MCP, HF CLI, LLM API), verify input→output contract per tool.
- **Integration tests (Agent):** Real DeepSeek API calls, small competition prompt, validate end-to-end pipeline completion.
- **No E2E UI tests:** Athena has no frontend.

## 6. Implementation Notes

- All prompts are in English. Pydantic `description` fields are in English.
- All scoring/ranking/selection decisions must be bound to a versioned rubric with per-item evidence.
- Occam's razor: use deterministic services before agents; use agents only when independent context, tool permissions, multi-turn reasoning, or concurrent execution is needed.
- State objects hold only necessary fields; large objects are stored as artifact references.
- The Agent uses the existing Athena `BaseAgent` + `ToolRegistry` + `AgentContext` infrastructure. No new framework code.
- Kaggle interactions go through Kaggle MCP (Model Context Protocol server).
- HuggingFace interactions go through `huggingface-cli` (installed via uv dependency).

## 7. Dependencies

Added to `pyproject.toml`:

- `huggingface_hub` — HF CLI backend, dataset/model search and download
- Kaggle MCP server — configured as an MCP server in `.claude/mcp.json` or equivalent, accessed via the tool layer
