"""External model providers used by the survey pipeline."""

import asyncio
import base64
import json
import re

from openai import AsyncOpenAI, RateLimitError

from athena.core.artifact_store import LocalArtifactStore
from athena.research.literature.paper_markdown.models import (
    VisualInterpretation,
    VisualInterpretationRequest,
)

EMBED_MAX_RETRIES = 5
EMBED_BACKOFF_SECONDS = 2.0
DEFAULT_EMBED_BATCH = 16
DEFAULT_EMBED_CONCURRENCY = 4
DEFAULT_VISION_TIMEOUT = 180.0
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
VISUAL_PROMPT = """You are reading one visual element from a scientific paper. \
Explain it faithfully for a researcher who cannot see it.

Element kind: {kind}
Caption and surrounding discussion:
{context}
{structured}
Return one JSON object and nothing else, with exactly these keys:
- "summary": a faithful explanation of what the element shows.
- "searchable_text": self-contained text for retrieval. Name the quantities, \
methods, datasets and directions of change explicitly; do not write phrases like \
"the figure shows" or refer to the element by number.
- "structured_data": an object with machine-readable facts such as axes, units, \
compared methods, or table fields. Use an empty object when nothing applies.

Report only what the element supports. Never invent numbers you cannot read."""


class OpenAIEmbedder:
    """Batching, bounded-concurrency, and retry policy for embeddings."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        batch_size: int = DEFAULT_EMBED_BATCH,
        concurrency: int = DEFAULT_EMBED_CONCURRENCY,
    ) -> None:
        self.client = client
        self.model = model
        self.batch_size = batch_size
        self.calls = 0
        self.embedded = 0
        self.retries = 0
        self._limit = asyncio.Semaphore(concurrency)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in bounded batches while preserving input order."""
        if not texts:
            return []
        batches = [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]
        results = await asyncio.gather(*(self._embed_batch(item) for item in batches))
        return [vector for batch in results for vector in batch]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        async with self._limit:
            return await self._request(batch)

    async def _request(self, batch: list[str]) -> list[list[float]]:
        for attempt in range(EMBED_MAX_RETRIES):
            self.calls += 1
            try:
                reply = await self.client.embeddings.create(
                    model=self.model, input=batch
                )
            except RateLimitError:
                if attempt == EMBED_MAX_RETRIES - 1:
                    raise
                self.retries += 1
                await asyncio.sleep(EMBED_BACKOFF_SECONDS * 2**attempt)
                continue
            self.embedded += len(batch)
            ordered = sorted(reply.data, key=lambda item: item.index)
            return [list(item.embedding) for item in ordered]
        raise RuntimeError("unreachable: the retry loop either returns or raises")


class VisionInterpreter:
    """Multimodal visual-element interpreter backed by an async client."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        artifacts: LocalArtifactStore,
        *,
        timeout: float = DEFAULT_VISION_TIMEOUT,
    ) -> None:
        self.client = client
        self.model = model
        self.artifacts = artifacts
        self.timeout = timeout
        self.calls = 0
        self.failures = 0

    async def interpret(
        self, request: VisualInterpretationRequest
    ) -> VisualInterpretation:
        """Interpret one visual request and parse its structured response."""
        content = await self._build_content(request)
        self.calls += 1
        try:
            reply = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                timeout=self.timeout,
            )
        except Exception:
            self.failures += 1
            raise
        return self._parse(reply.choices[0].message.content or "")

    async def _build_content(self, request: VisualInterpretationRequest) -> list[dict]:
        context = await self.artifacts.get_text(request.context_ref)
        structured = ""
        if request.structured_text_ref is not None:
            extracted = await self.artifacts.get_text(request.structured_text_ref)
            structured = f"Extracted text of the element:\n{extracted}\n"
        prompt = VISUAL_PROMPT.format(
            kind=request.kind, context=context, structured=structured
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        if request.asset_ref is None:
            return content
        data = await self.artifacts.get_bytes(request.asset_ref)
        media_type = request.media_type or "image/png"
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
        )
        return content

    def _parse(self, text: str) -> VisualInterpretation:
        match = JSON_OBJECT.search(text)
        payload: dict = {}
        if match is not None:
            try:
                loaded = json.loads(match.group(0))
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                payload = loaded
        summary = str(payload.get("summary", "")).strip()
        searchable = str(payload.get("searchable_text", "")).strip()
        structured = payload.get("structured_data")
        fallback = text.strip()
        return VisualInterpretation(
            summary=summary or fallback,
            searchable_text=searchable or summary or fallback,
            structured_data=structured if isinstance(structured, dict) else {},
            model=self.model,
        )


__all__ = ["OpenAIEmbedder", "VisionInterpreter"]
