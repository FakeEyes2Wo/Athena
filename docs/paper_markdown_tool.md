# Paper Markdown Tool

This document is the collaboration contract for the `paper_markdown` tool. It is intended
for tool developers, upstream source providers, model-adapter authors, RAG ingestion
components, and human reviewers. Paper-specific defects and regression history live in the
separate [RAG quality record](paper_markdown_rag_quality_ch.md).

## Purpose and principles

Paper Markdown Tool converts an upstream-provided TeX source or PDF into durable,
traceable Markdown and retrieval units. The goal is to recover paper semantics for RAG,
not to reproduce publisher page design.

The tool follows these principles:

- **TeX first:** TeX is authoritative when supplied; PDF is used only when TeX is absent.
- **Deterministic first:** reliable parser work does not call a model.
- **Evidence separate from retrieval:** source-order evidence and retrieval-oriented text
  are persisted independently.
- **Content addressed:** requests, text, visuals, diagnostics, and results cross boundaries
  through `ArtifactStore` references.
- **Explicit degradation:** uncertain structure or missing visual semantics produces a
  diagnostic instead of guessed content.
- **RAG oriented:** sections, reference edges, visual relationships, and metadata take
  precedence over page-layout fidelity.

The registered tool name is:

```text
paper_markdown
```

The `request_ref` argument points to a `PaperConversionRequest` artifact. The returned
`ToolResult.data.paper_content_ref` points to the persisted `PaperContent` artifact.

## Ownership boundaries

| Component | Responsibility |
| --- | --- |
| AcademicSurvey / Web Search | Find papers, resolve identifiers, download TeX/PDF, and store source artifacts |
| Athena runtime | Create the shared store, register the tool, inject model adapters, and schedule calls |
| Paper Markdown Tool | Select the source, recover structure and visuals, build chunks, run quality gates, and persist results |
| `VisualInterpreter` provider | Produce faithful searchable explanations from visual evidence and context |
| `StructureRefiner` provider | Repair only parser-identified low-confidence Markdown structure |
| RAG ingestion | Gate outputs, load retrieval units, and build embedding, lexical, and relationship indexes |
| Higher-level agents | Consume retrieval results and evidence refs without parsing large TeX/PDF payloads |

The tool does not own:

- web search, arXiv/DOI resolution, or downloads;
- model clients, credentials, quotas, retries, or serving infrastructure;
- vector databases, BM25 indexes, or rerankers;
- Athena sessions, scheduling, or agent workflows;
- publication-quality PDF rendering from Markdown.

## Data flow

```text
upstream TeX/PDF artifacts
        │
        ▼
PaperConversionRequest artifact
        │
        ▼
ToolRegistry → PaperMarkdownTool → PaperProcessor
        │
        ├─ TeX source ─→ TeX parser
        └─ PDF only   ─→ PyMuPDF parser
                         │
                         ▼
                ParsedPaper + visuals
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
     deterministic structure   injected model assistance
             └───────────┬───────────┘
                         ▼
             Markdown + chunks + diagnostics
                         │
                         ▼
                PaperContent artifact
                         │
                         ▼
              load_retrieval_units()
                         │
                         ▼
                   RAG ingestion
```

Models receive only the evidence needed for a bounded task. They do not choose sources,
advance workflows, or rewrite normal paper prose.

## Input contract

The main `PaperConversionRequest` fields are:

| Field | Meaning |
| --- | --- |
| `tex_source_ref` | TeX file or source-package artifact; authoritative when present |
| `tex_source_format` | `auto`, `tar`, `tar.gz`, `zip`, `gzip`, or `plain` |
| `tex_entrypoint` | Optional relative root TeX path; otherwise selected deterministically |
| `pdf_ref` | Upstream PDF artifact; used as the body source only when TeX is absent |
| `paper_id` | Recommended canonical arXiv, DOI, or corpus identifier |
| `metadata` | Small upstream metadata such as title, venue, and year |
| `visual_policy` | `required` or `best_effort`; defaults to `required` |
| `chunking` | Target characters and complete-element overlap count |

At least one source ref is required. Callers pass artifact refs, not local paths or large
inline payloads.

### Source selection

1. If `tex_source_ref` exists, the TeX path is always selected.
2. PDF is selected only when TeX is absent.
3. If both exist, PDF remains in provenance but does not supply body text.
4. Invalid supplied TeX fails explicitly; it never silently falls back to PDF.

This keeps upstream defects visible and makes source selection independent of the runtime
environment.

### TeX packages

Tar, compressed tar, zip, gzip, and plain TeX are supported. Automatic entrypoint
selection considers `documentclass`, the `document` environment, and common root names.

Archives are expanded in memory. The loader rejects:

- absolute paths and `..` traversal;
- symbolic links, hard links, and device entries;
- excessive file counts or expanded sizes;
- recursive `input/include/subfile` chains;
- missing or unresolved entrypoints.

Expanded includes retain original relative filenames and line mappings.

## Processing semantics

### TeX path

The `pylatexenc`-based parser recovers:

- title, authors, abstract, and section hierarchy;
- paragraphs, lists, footnotes, theorem-like prose, and code;
- inline and displayed math;
- citation keys, local reference targets, and labels;
- independent bibliography entries;
- PDF, SVG, and raster assets referenced by `includegraphics`;
- regular `tabular` content that can be represented safely as Markdown.

Complex tables that cannot be flattened reliably retain their evidence for model
interpretation instead of losing cells or inventing structure. Each element retains TeX
file and line provenance.

The declaration location of a TeX float can differ from the section that discusses it.
The tool preserves the source heading path and may infer a semantic heading path from an
explicit same-section reference. This changes retrieval context only; Markdown order and
source locators remain unchanged.

### PDF path

When TeX is absent, the PyMuPDF path uses text blocks, fonts, and geometry to recover:

- repeated header/footer filtering;
- full-width and multi-column reading order;
- headings, paragraph joining, and dehyphenation;
- page and bounding-box provenance;
- deterministically recognizable Markdown tables;
- embedded images and captions;
- vector-figure regions and previews for low-text pages.

The PDF path does not invent TeX for uncertain equations. It preserves extracted equation
text, renders a cropped equation asset, and sends it as an `equation` task to the optional
`VisualInterpreter`. An optional `StructureRefiner` may repair parser-identified structure
defects, but it must not summarize or alter claims.

## Model interfaces

### VisualInterpreter

`VisualInterpreter` may be backed by a dedicated VLM or Athena base model. Each request
represents one figure, table, equation, or low-text page:

| Field | Purpose |
| --- | --- |
| `visual_id` / `kind` | Stable identity and visual type |
| `asset_ref` / `media_type` | Normalized image or original visual asset |
| `structured_text_ref` | Deterministically extracted table/source text |
| `context_ref` | Caption, label, and nearby discussion |
| `locator` | Original TeX line or PDF page/bbox |

`VisualInterpretation` returns:

- a faithful summary;
- standalone `searchable_text`;
- optional axes, trends, nodes, relationships, or table fields in `structured_data`;
- a revision-pinned model identifier;
- optional confidence.

An adapter that cannot interpret the evidence reliably should raise an error. It should
not disguise a copied caption as successful visual understanding.

### StructureRefiner

`StructureRefiner` is called only for elements with explicit `repair_issue_codes`. Its
request includes the original fragment ref, issue codes, locators, and a restrictive
instruction. Results must preserve every fact, number, citation, and equation. Normal
elements are never sent to this model.

### Visual policies

| Policy | Missing or failed model behavior | Typical use |
| --- | --- | --- |
| `required` | Fail without producing an incomplete result | Production multimodal ingestion |
| `best_effort` | Keep deterministic caption/table/page evidence and emit a warning | No-model environments, fault recovery, structural tests |

An unavailable best-effort visual does not produce an independent visual retrieval unit.
Only actual model interpretations become `visual:*` units.

## Chunk and retrieval semantics

Chunk boundaries preserve complete structural elements. Equations, tables, figures, and
bibliography entries are not split. Each table, figure, and bibliography entry remains
independent. Overlap reuses only paragraph, list, or abstract elements within one section.

Each `PaperChunk` has two text roles:

- `content_ref`: source-order chunk Markdown for evidence and character-span replay;
- `retrieval_text_ref`: text used for embedding, BM25, and reranking.

When source and semantic paths differ, retrieval text uses the semantic section and drops
the adjacent stale source heading. Source evidence remains unchanged. Older schemas without
`retrieval_text_ref` fall back to `content_ref`.

Chunks also persist:

- source and semantic heading paths;
- full-Markdown character interval and approximate token count;
- TeX/PDF locators;
- citation keys;
- local `reference_keys` and labels defined by the chunk;
- linked `visual_ids`.

Consumers can use these fields for section filtering, citation retrieval, and relationship
expansion to referenced tables, equations, or appendices.

## Persistent model

The current `PaperContent` schema is `1.3`. `PaperContent` is a lightweight index; it does
not inline large bodies or images.

| Data | Persistence |
| --- | --- |
| Complete Markdown | `markdown_ref` |
| Abstract / bibliography | `abstract_ref` / `bibliography_ref` |
| Chunk evidence and retrieval text | Two artifact refs on each `PaperChunk` |
| Original or cropped visual | `asset_ref`, with optional normalized `preview_ref` |
| Structured table text | `structured_text_ref` |
| Model response and visual search text | `interpretation_ref` / `search_text_ref` |
| Processing diagnostics | `diagnostics_ref` |
| Source and converter facts | `PaperProvenance` |

`PaperVisual` connects a visual to its parsed element, source and semantic headings, parent
chunks, model revision, interpretation status, and original locator.

Schemas 1.0--1.2 remain readable through compatibility defaults. Any new persisted field
or semantic change requires a schema-version increment and a legacy read strategy.

## RetrievalUnit contract

Call:

```python
units = await content.load_retrieval_units(store)
```

to obtain provider-neutral body and visual retrieval units. Consumers should use this
method instead of assembling an index from every artifact manually.

Each unit carries:

- a global `unit_id` in `paper_id:local_id` form, falling back to source fingerprint;
- `kind` and the actual text to index;
- semantic `heading_path`;
- source locators;
- paper identity, title, authors, source, and quality metadata;
- local chunk or visual identity and relationship metadata.

Body metadata additionally carries citation keys, reference keys, labels, visual IDs, and
source heading path. Visual metadata carries label, caption, element ID, parent chunks,
model, and interpretation status.

RAG ingestion should:

1. inspect `quality_status` and `quality_codes` first;
2. use `unit_id` as the cross-paper primary key;
3. index `RetrievalUnit.text`, not raw `content_ref` text;
4. retain metadata for filtering, expansion, and evidence replay;
5. use `chunk_ids` / `visual_ids` to group or deduplicate multi-granularity hits.

## Quality and failure behavior

| Status | Meaning | Ingestion guidance |
| --- | --- | --- |
| `pass` | No warning/error diagnostics | Eligible for normal ingestion |
| `degraded` | At least one warning/error | Accept, isolate, or rerun according to stable codes |
| `unknown` | Legacy output lacks a complete quality conclusion | Handle conservatively |

Explicit failures include:

- a request with no source;
- unsafe, damaged, or unresolved TeX input;
- invalid PDF or no recoverable content;
- unavailable interpretation under `required` policy;
- missing or digest-invalid artifacts.

Quality gates cover element identity, character spans, headings, math Markdown, rectangular
tables, label/reference graphs, and retrieval-section context. A visual that references a
missing parent element or body placeholder fails explicitly. Warning codes are stable
machine-facing inputs for ingestion policy; detailed evidence is stored in the diagnostics
artifact.

## ToolRegistry integration

The runtime must construct and register the tool explicitly with a shared `ArtifactStore`. The tool
does not depend on automatic discovery.

```python
import asyncio

from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.research.literature.paper_markdown import (
    PaperContent,
    PaperConversionRequest,
    PaperMarkdownTool,
)

request = PaperConversionRequest(
    paper_id="arxiv:2501.10120",
    tex_source_ref=tex_ref,
    pdf_ref=pdf_ref,
    visual_policy="required",
)
request_ref = await store.put_text(request.model_dump_json())

tool = PaperMarkdownTool(store, visual_interpreter, structure_refiner=None)
tools = ToolRegistry()
tools.register(tool)

async def emit(_kind, _ref, _data=None):
    pass

context = ToolContext(tool.spec.name, "manual-paper-call", emit, asyncio.Event())
result = await tools.resolve("paper_markdown").ainvoke(
    context,
    request_ref=request_ref,
)
if not result.success:
    raise RuntimeError(result.error)
result_ref = result.data["paper_content_ref"]
content = PaperContent.model_validate_json(await store.get_text(result_ref))
units = await content.load_retrieval_units(store)
```

The upstream writer, tool, and result consumer must share the same logical artifact
store. A content ref without the corresponding bytes is not an executable input.

## Reproducibility and concurrency

The same deterministic source, request, tool version, and model outputs produce the same
`PaperContent` result ref. `LocalArtifactStore` writes equal content idempotently and
verifies SHA-256 on reads.

Live model calls can break byte-level reproducibility. A reproducible model adapter should
pin model revision, prompt version, decoding parameters, and preprocessing version, then
cache responses by evidence and configuration.

The tool holds no cross-request business state. Because the injected visual and structure
model protocols do not promise concurrency safety, its `ToolSpec` serializes top-level
calls. Shared-store and adapter lifecycles remain runtime responsibilities.

## Module collaboration map

| Module | Concern |
| --- | --- |
| `schemas.py` | Request, persistence, and retrieval contracts |
| `interfaces.py` | Provider-neutral visual and structure-model protocols |
| `tex_source.py` | Safe source-package loading, entrypoint selection, include expansion |
| `tex_parser.py` | TeX AST to structured paper elements |
| `pdf_parser.py` | PDF layout, table, and visual-evidence recovery |
| `chunking.py` | Full Markdown, evidence chunks, and retrieval text |
| `visuals.py` | Previews, deterministic fallback, and body visual representation |
| `quality.py` | Deterministic RAG quality gates |
| `processor.py` | End-to-end orchestration and artifact persistence |
| `tool.py` | `paper_markdown` `BaseTool` and `ToolRegistry` boundary |

A new source parser should normalize into the shared `ParsedPaper` form and reuse visual,
chunking, quality, and persistence stages. A new model provider should implement a protocol
instead of adding provider-specific branches to the processor.

## Testing and acceptance

Baseline verification:

```powershell
$env:PYTHONPATH='src'
python -m pytest -q
python -m black --check src test
python -m compileall -q src test
git diff --check
```

A complete acceptance run should cover:

- TeX priority and PDF fallback;
- package safety and include provenance;
- headings, math, tables, bibliography, and reference graphs;
- chunk boundaries, semantic retrieval sections, and cross-paper IDs;
- `required` and `best_effort` visual policies;
- model success, failure, and adapter rejection of invalid results;
- artifact integrity and deterministic repeated execution;
- lexical/vector retrieval probes on a real paper.

The PaSa tool-level regression entrypoint is:

```text
../papers/PaSa_2501.10120_paper_markdown_tool_v3/reproduce.py
```

The PDF fallback regression for the same path is:

```text
../papers/PaSa_2501.10120_paper_markdown_tool_pdf/reproduce_pdf.py
```

Tool-level regressions must execute through
`ToolRegistry → PaperMarkdownTool → PaperProcessor`. Exported files are inspection copies
and do not participate in conversion. Without an injected visual interpreter, PDF output
explicitly remains `best_effort/degraded` and does not fabricate visual retrieval units.

## Known limitations

- Complex custom TeX macros and unusual tables may be only partially expanded
  deterministically.
- PDF reading order, math, and borderless tables are less reliable than TeX source.
- Values, trends, and relationships inside images require an injected visual model.
- A best-effort result may be suitable for text RAG without passing multimodal quality.
- The complete Athena runtime still owns tool registration, upstream source handoff, and
  downstream RAG ingestion.

New reproducible quality defects, impact, decisions, and acceptance criteria belong in the
[RAG quality record](paper_markdown_rag_quality_ch.md); this document maintains the current
stable contract.
