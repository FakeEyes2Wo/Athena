# Web Tool and HTTP Ownership Design

Status: approved on 2026-09-02 after an explicit reflection-and-auto-approval
instruction.

## Goal

Delete the standalone `athena.retrieval` namespace, make public-web search and fetch
ordinary Agent tools, and move the HTTP transport shared by Web tools, Kaggle, and
literature into a neutral core owner.

This is an ownership simplification. Tool names, schemas, result shapes, ref-id
behavior, HTTP retry/rate-limit behavior, and user-visible errors remain unchanged.

## Current state

- `src/athena/retrieval/web_search.py` contains `WebSearchTool`, `WebFetchTool`,
  `WebSession`, page extraction, and in-page search.
- `src/athena/research/literature/paper_source/http.py` contains the shared HTTP
  response, transport, urllib implementation, and host rate limiter.
- Web tools, Kaggle, Paper Source, Paper Scout, and Survey use that HTTP layer.
- The current dirty worktree has already moved paper-source files under
  `research/literature/`, but several imports still name the deleted
  `athena.research.paper_source.http` path. The implementation must finish the move;
  it must not restore the deleted package as a shim.
- `_WebTool` exists only to forward construction. It stores both `session` and a
  duplicate `http` attribute even though the session already owns the client.

## Considered approaches

### Chosen: neutral HTTP plus Agent tools

Move the HTTP implementation to `athena.core.http`, move the Web implementation to
`athena.core.agent.tools.web`, update every consumer, and delete `athena.retrieval`.
This gives generic facilities neutral owners and removes the core-to-research
dependency.

### Rejected: move only Web tools

Moving `web_search.py` while continuing to import HTTP from Paper Source is smaller,
but it leaves `core.agent.tools` depending on a research feature package. The file
layout would look cleaner while the dependency direction remains wrong.

### Rejected: make Web tools literature-only

Putting Web tools under `research.literature` avoids a core dependency but makes a
generic Agent capability unavailable to non-research Agents and contradicts the
existing `BaseTool`/`ToolRegistry` ownership.

## Target layout

```text
src/athena/core/
├── http.py
└── agent/tools/
    ├── __init__.py
    ├── user_input.py
    └── web.py
```

The following files and directories are deleted after imports are migrated:

```text
src/athena/retrieval/__init__.py
src/athena/retrieval/web_search.py
src/athena/research/literature/paper_source/http.py
test/unit/retrieval/
```

No compatibility module remains at an old path.

## Ownership and interfaces

### `athena.core.http`

Owns the existing standard-library HTTP primitives:

- `HttpTransportError`
- `HttpResponse`
- `HttpTransport`
- `UrllibTransport`
- `HostRateLimiter`
- rate-limit constants and small header/user-agent helpers

The move preserves constructor signatures and retry, timeout, response-size,
rate-limit-bucket, user-agent, and `Retry-After` behavior. This task does not introduce
an HTTP framework, client registry, configuration object, or new dependency.

All production consumers import these names directly from `athena.core.http`.
Paper Source stops re-exporting generic HTTP infrastructure.

### `athena.core.agent.tools.web`

Owns:

- `WebSession`
- `WebSearchTool`
- `WebFetchTool`
- `extract_page_text`
- `find_in_page`
- private DuckDuckGo parsing/query helpers

`WebSession` remains the sole owner of the HTTP client, ref-id mappings, cached pages,
and search/fetch counters.

The forwarding-only `_WebTool` class is deleted. `WebSearchTool` and `WebFetchTool`
each accept only `session: WebSession | None = None`; callers that need an injected
client construct `WebSession(http=fake_or_limiter)`. Tool methods use
`self.session.http`, so the duplicate Tool-level `http` attribute is deleted.

Production code imports the concrete `web` module. `agent.tools.__init__` exports only
names that are deliberately part of the Agent-tool public surface; it does not become
a registry or wildcard barrel.

## Data flow

1. Agent turn setup constructs one `WebSession`.
2. Search and fetch tools receive that same session and are registered in the existing
   `ToolRegistry`.
3. Search assigns ref ids and records URLs in the session.
4. Fetch resolves a URL or search ref id, stores fetched pages in the same session, and
   optionally delegates to `find_in_page` for bounded excerpts.
5. HTTP calls flow through `HostRateLimiter` from `athena.core.http`.

No new persistence or cross-turn global cache is added.

## Error behavior

- Empty queries, oversized query batches, short four-query batches, missing URLs, and
  unknown ref ids keep their current `ValueError` messages.
- Non-success DuckDuckGo/fetch responses keep their current `RuntimeError` behavior.
- DNS, TLS, connection, and timeout failures remain `HttpTransportError` and retain
  existing retry/backoff behavior.
- No fallback import, alias module, or silent exception path is added.

## Migration scope

Production import migration covers:

- Research General-turn tool registration
- Kaggle client
- Literature Paper Source, Paper Scout, and Survey
- any deliberate package exports or type annotations that still use the old path

Test migration covers Web tools, Paper Source, Paper Scout, Survey, Kaggle, Research
runtime wiring, and import/architecture assertions. Tests must import the concrete new
owner rather than old package shims.

Documentation migration covers current developer/user documents only. Archived plans
may retain historical paths.

## Simplification rules

- Delete `_WebTool`, duplicate `.http` attributes, and the two Tool `http` constructor
  parameters.
- Delete old package files after the last live import moves.
- Remove stale HTTP re-exports from Paper Source.
- Do not create adapters, aliases, registries, service locators, or configuration
  dataclasses for this move.
- Preserve parameters that are exercised by real callers or define HTTP policy; delete
  a parameter only after production and test searches show it has no caller.
- Keep one implementation for HTTP and one implementation for Web tools.

## Verification

Before implementation, record collection/test failures caused by the current partial
migration. After each move, run the owning focused tests.

Final evidence must include:

- no `athena.retrieval` import or source directory;
- no `athena.research.paper_source.http` or
  `athena.research.literature.paper_source.http` import or source file;
- Web tool schemas, results, ref ids, open/find behavior, and shared-session tests pass;
- Paper Source, Paper Scout, Survey, Kaggle, Research-turn, CLI, and gateway consumers
  pass;
- `python -m compileall -q src/athena/core src/athena/research src/athena/kaggle`;
- configured code-style check, Black check, and `git diff --check` pass;
- final file/line inventory records the deleted retrieval package and net file change;
- no new failure relative to the recorded baseline.

## Out of scope

- replacing DuckDuckGo or urllib;
- adding POST support to the shared transport;
- redesigning Paper Source, Survey, Kaggle, or Agent tool registration;
- changing network retry/rate-limit policy;
- changing persisted data, RPC contracts, tool schemas, or tool result formats;
- fixing unrelated failures in the already-dirty worktree.
