"""Network-free tests for the paper_source fetch pipeline."""

import asyncio
import io
import tarfile
import tempfile
import unittest
import zipfile

from athena.core.tool import ToolRegistry
from athena.core.tool_types import TOOL_BEGIN, TOOL_END, ToolContext
from athena.research.paper_markdown.schemas import PaperConversionRequest
from athena.research.paper_source.arxiv import parse_atom_feed, parse_raw_record
from athena.research.paper_source.fetcher import (
    LocatorCache,
    PaperSourceFetcher,
    sniff_payload,
    title_similarity,
)
from athena.research.paper_source.http import (
    DEFAULT_BUCKET_INTERVALS,
    HostRateLimiter,
    HttpResponse,
    rate_limit_bucket,
)
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRequest,
    SourceHint,
    normalize_arxiv_id,
    normalize_doi,
)
from athena.research.paper_source.tool import PaperFetchTool
from athena.storage import LocalArtifactStore

ZERO_INTERVALS = {bucket: 0.0 for bucket in DEFAULT_BUCKET_INTERVALS}
QUERY_URL = "https://export.arxiv.org/api/query"
OAI_URL = "https://oaipmh.arxiv.org/oai"
SRC_URL = "https://arxiv.org/src/2501.10120v2"
PDF_URL = "https://arxiv.org/pdf/2501.10120v2"

ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2501.10120v2</id>
    <published>2025-01-17T09:00:00Z</published>
    <updated>2025-02-06T12:00:00Z</updated>
    <title>PaSa: An LLM Agent for Comprehensive
      Academic Paper Search</title>
    <summary>We introduce PaSa, an advanced paper search agent.</summary>
    <author><name>Yichen He</name></author>
    <author><name>Guanhua Huang</name></author>
    <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.1145/3654777</arxiv:doi>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.IR" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""

OAI_RECORD = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <GetRecord>
    <record>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2501.10120</id>
          <title>PaSa: An LLM Agent for Comprehensive Academic Paper Search</title>
          <authors>He, Yichen; Huang, Guanhua</authors>
          <categories>cs.CL cs.IR</categories>
          <license>http://creativecommons.org/licenses/by/4.0/</license>
          <version version="v1"><date>Fri, 17 Jan 2025 09:00:00 GMT</date></version>
          <version version="v2"><date>Thu, 6 Feb 2025 12:00:00 GMT</date></version>
        </arXivRaw>
      </metadata>
    </record>
  </GetRecord>
</OAI-PMH>
"""


def tar_gz_bytes(files: dict[str, bytes]) -> bytes:
    """Build an in-memory gzipped tar mimicking an arXiv source package."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    """Build an in-memory zip source package."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class FakeTransport:
    """Serve canned responses by URL prefix and record every call."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """Return the route matching the longest URL prefix, else HTTP 404."""
        self.calls.append(url)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                return self._take(prefix, url)
        return HttpResponse(status=404, url=url, body=b"not found")

    def _take(self, prefix: str, url: str) -> HttpResponse:
        """Pop the next queued response for a prefix, keeping the last one sticky."""
        route = self.routes[prefix]
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route

    def count(self, prefix: str) -> int:
        """Count recorded calls whose URL starts with the prefix."""
        return sum(1 for url in self.calls if url.startswith(prefix))


def limiter(
    transport: FakeTransport, sleeps: list[float] | None = None
) -> HostRateLimiter:
    """Build a rate limiter with zero intervals and a recording sleeper."""

    async def sleeper(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    return HostRateLimiter(
        transport,
        default_interval=0.0,
        bucket_intervals=ZERO_INTERVALS,
        sleeper=sleeper,
    )


def ok(body: bytes) -> HttpResponse:
    """Build a 200 response carrying the given bytes."""
    return HttpResponse(status=200, url="", body=body, headers={})


async def noop_emit(kind: str, ref: str, data: dict | None = None) -> None:
    """Discard tool lifecycle events during tests."""


class IdentityTest(unittest.TestCase):
    def test_normalize_doi_strips_url_and_lowercases(self) -> None:
        self.assertEqual(
            "10.1145/3654777", normalize_doi("https://doi.org/10.1145/3654777")
        )
        self.assertEqual("10.1145/x", normalize_doi("doi:10.1145/X"))
        self.assertEqual("", normalize_doi("not-a-doi"))

    def test_normalize_arxiv_id_accepts_every_upstream_spelling(self) -> None:
        cases = {
            "arXiv:2501.10120v2": ("2501.10120", 2),
            "http://arxiv.org/abs/2501.10120v2": ("2501.10120", 2),
            "https://arxiv.org/pdf/2501.10120.pdf": ("2501.10120", None),
            "oai:arXiv.org:2501.10120": ("2501.10120", None),
            "hep-th/9901001v3": ("hep-th/9901001", 3),
            "math.AG/0601001": ("math.AG/0601001", None),
            "10.1145/3654777": ("", None),
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, normalize_arxiv_id(raw), raw)

    def test_arxiv_doi_derives_bare_id_but_never_wins_paper_key(self) -> None:
        identity = PaperIdentity(doi="https://doi.org/10.48550/arXiv.2501.10120")

        self.assertEqual("2501.10120", identity.arxiv_id)
        self.assertEqual("arxiv:2501.10120", identity.paper_key())

    def test_publisher_doi_wins_paper_key_over_arxiv_id(self) -> None:
        identity = PaperIdentity(arxiv_id="2501.10120v2", doi="10.1145/3654777")

        self.assertEqual("2501.10120", identity.arxiv_id)
        self.assertEqual("doi:10.1145/3654777", identity.paper_key())

    def test_title_only_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PaperIdentity(title="PaSa: An LLM Agent for Academic Paper Search")


class ParsingTest(unittest.TestCase):
    def test_atom_feed_yields_latest_version_and_collapsed_title(self) -> None:
        resolved = parse_atom_feed(ATOM_FEED)
        metadata = resolved["2501.10120"]

        self.assertEqual(2, metadata.latest_version)
        self.assertEqual(
            "PaSa: An LLM Agent for Comprehensive Academic Paper Search", metadata.title
        )
        self.assertEqual(["Yichen He", "Guanhua Huang"], metadata.authors)
        self.assertEqual("10.1145/3654777", metadata.doi)
        self.assertEqual(["cs.CL", "cs.IR"], metadata.categories)

    def test_atom_feed_skips_error_entries(self) -> None:
        feed = ATOM_FEED.replace(
            b"http://arxiv.org/abs/2501.10120v2",
            b"http://arxiv.org/api/errors#incorrect_id_format",
        )

        self.assertEqual({}, parse_atom_feed(feed))

    def test_oai_record_exposes_license_and_version_history(self) -> None:
        metadata = parse_raw_record(OAI_RECORD)

        self.assertIsNotNone(metadata)
        self.assertEqual(["v1", "v2"], metadata.versions)
        self.assertEqual(2, metadata.latest_version)
        self.assertEqual(
            "http://creativecommons.org/licenses/by/4.0/", metadata.license
        )


class SniffTest(unittest.TestCase):
    def test_detects_arxiv_source_package_formats(self) -> None:
        self.assertEqual(
            ("tex", "tar.gz"),
            sniff_payload(tar_gz_bytes({"main.tex": b"\\section{A}"})),
        )
        self.assertEqual(
            ("tex", "zip"), sniff_payload(zip_bytes({"main.tex": b"\\section{A}"}))
        )
        self.assertEqual(
            ("tex", "plain"), sniff_payload(b"\\documentclass{article}\n\\section{A}")
        )

    def test_pdf_bytes_from_source_endpoint_are_not_mistaken_for_tex(self) -> None:
        self.assertEqual(("pdf", None), sniff_payload(b"%PDF-1.5\n%rest of the file"))

    def test_html_error_page_and_empty_body_are_unknown(self) -> None:
        self.assertEqual(
            ("unknown", None), sniff_payload(b"<html><body>503</body></html>")
        )
        self.assertEqual(("unknown", None), sniff_payload(b""))

    def test_title_similarity_flags_a_different_paper(self) -> None:
        self.assertGreater(
            title_similarity("PaSa: an LLM agent", "PaSa: An LLM Agent!"), 0.9
        )
        self.assertLess(title_similarity("Attention Is All You Need", "PaSa"), 0.5)


class RateLimiterTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_429_and_honours_retry_after(self) -> None:
        transport = FakeTransport(
            {
                "https://arxiv.org/src/": [
                    HttpResponse(
                        status=429, url="", body=b"", headers={"retry-after": "7"}
                    ),
                    ok(b"%PDF-1.5"),
                ]
            }
        )
        sleeps: list[float] = []

        response = await limiter(transport, sleeps).get(SRC_URL)

        self.assertEqual(200, response.status)
        self.assertEqual(2, transport.count("https://arxiv.org/src/"))
        self.assertEqual([7.0], sleeps)

    async def test_gives_up_after_max_retries_and_returns_last_response(self) -> None:
        transport = FakeTransport(
            {"https://arxiv.org/src/": HttpResponse(status=503, url="", body=b"")}
        )
        rate_limited = limiter(transport, [])

        response = await rate_limited.get(SRC_URL)

        self.assertEqual(503, response.status)
        self.assertEqual(4, rate_limited.request_count)

    async def test_serializes_concurrent_requests_per_bucket(self) -> None:
        transport = FakeTransport({"https://arxiv.org/": ok(b"%PDF-1.5")})
        rate_limited = limiter(transport, [])

        await asyncio.gather(
            rate_limited.get(SRC_URL),
            rate_limited.get(PDF_URL),
            rate_limited.get(SRC_URL),
        )

        self.assertEqual(3, rate_limited.request_count)

    def test_arxiv_subdomains_share_one_rate_limit_bucket(self) -> None:
        hosts = ["arxiv.org", "export.arxiv.org", "oaipmh.arxiv.org", "ARXIV.ORG:443"]

        self.assertEqual({"arxiv.org"}, {rate_limit_bucket(host) for host in hosts})
        self.assertEqual("openalex.org", rate_limit_bucket("api.openalex.org"))
        self.assertEqual("evil-arxiv.org", rate_limit_bucket("evil-arxiv.org"))

    async def test_arxiv_subdomains_do_not_get_independent_budgets(self) -> None:
        transport = FakeTransport({"https://": ok(b"%PDF-1.5")})
        sleeps: list[float] = []

        async def sleeper(delay: float) -> None:
            sleeps.append(delay)

        rate_limited = HostRateLimiter(
            transport, bucket_intervals={"arxiv.org": 3.0}, sleeper=sleeper
        )

        await rate_limited.get(QUERY_URL)
        await rate_limited.get("https://oaipmh.arxiv.org/oai")
        await rate_limited.get(SRC_URL)

        # 假 sleeper 不推进时钟，因此共用桶的预留槽位累计成 3s 与 6s；
        # 修复前三个子域各有一份预算，这里会一次都不等待。
        self.assertEqual(3, rate_limited.request_count)
        self.assertEqual([3, 6], [round(delay) for delay in sleeps])


class FetcherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.artifacts = LocalArtifactStore(self.tmp.name)
        self.source = tar_gz_bytes(
            {
                "main.tex": b"\\documentclass{article}\\begin{document}PaSa\\end{document}"
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(
        self, routes: dict[str, object]
    ) -> tuple[PaperSourceFetcher, FakeTransport]:
        """Build a fetcher wired to a fake transport and an in-memory cache."""
        transport = FakeTransport(routes)
        fetcher = PaperSourceFetcher(
            self.artifacts, http=limiter(transport, []), cache=LocatorCache()
        )
        return fetcher, transport

    def pasa_request(self, **policy: object) -> PaperSourceRequest:
        """Build a one-paper request for arXiv 2501.10120."""
        return PaperSourceRequest(
            papers=[
                PaperRef(
                    identity=PaperIdentity(arxiv_id="arXiv:2501.10120"),
                    upstream_metadata={
                        "title": "PaSa: An LLM Agent for Comprehensive Academic Paper Search",
                        "year": "2025",
                    },
                    retrieval_channels=["arxiv"],
                )
            ],
            policy=PaperSourcePolicy(**policy),
        )

    async def test_pins_resolved_version_and_emits_conversion_request(self) -> None:
        fetcher, transport = self.build(
            {QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)}
        )

        result = await fetcher.fetch(self.pasa_request())
        record = result.records[0]

        self.assertEqual("fetched", record.status)
        self.assertEqual("arxiv:2501.10120", record.paper_key)
        self.assertEqual("v2", record.version)
        self.assertTrue(record.version_pinned)
        self.assertEqual("arxiv:2501.10120v2/src", record.source_locator)
        self.assertEqual("tar.gz", record.tex_source_format)
        self.assertIsNone(record.pdf_ref)
        self.assertEqual(2, result.stats.http_requests)
        self.assertEqual(1, result.stats.tex_sources)
        self.assertEqual(1, transport.count(SRC_URL))

        payload = await self.artifacts.get_text(record.conversion_request_ref)
        request = PaperConversionRequest.model_validate_json(payload)
        self.assertEqual("arxiv:2501.10120", request.paper_id)
        self.assertEqual("tar.gz", request.tex_source_format)
        self.assertEqual(record.tex_source_ref, request.tex_source_ref)
        self.assertEqual(
            self.source, await self.artifacts.get_bytes(request.tex_source_ref)
        )

    async def test_resolved_doi_enriches_identity_without_moving_paper_key(
        self,
    ) -> None:
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})

        record = (await fetcher.fetch(self.pasa_request())).records[0]

        self.assertEqual("10.1145/3654777", record.identity.doi)
        self.assertEqual("arxiv:2501.10120", record.paper_key)

    async def test_pdf_only_submission_is_routed_to_pdf_ref(self) -> None:
        fetcher, _ = self.build(
            {QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(b"%PDF-1.5\ntrailer")}
        )

        record = (await fetcher.fetch(self.pasa_request())).records[0]
        codes = [item.code for item in record.diagnostics]

        self.assertEqual("fetched", record.status)
        self.assertIsNone(record.tex_source_ref)
        self.assertIsNotNone(record.pdf_ref)
        self.assertIn("paper_source.pdf_only_submission", codes)

    async def test_missing_source_falls_back_to_the_pdf_endpoint(self) -> None:
        fetcher, transport = self.build(
            {
                QUERY_URL: ok(ATOM_FEED),
                SRC_URL: HttpResponse(status=404, url="", body=b""),
                PDF_URL: ok(b"%PDF-1.5\ntrailer"),
            }
        )

        record = (await fetcher.fetch(self.pasa_request())).records[0]

        self.assertEqual("fetched", record.status)
        self.assertEqual("arxiv_pdf", record.channel)
        self.assertEqual(1, transport.count(PDF_URL))

    async def test_unresolved_version_is_skipped_unless_policy_allows_it(self) -> None:
        fetcher, transport = self.build(
            {
                QUERY_URL: HttpResponse(status=503, url="", body=b""),
                SRC_URL: ok(self.source),
            }
        )

        result = await fetcher.fetch(self.pasa_request(allow_non_arxiv_channels=False))
        codes = [item.code for item in result.records[0].diagnostics]

        self.assertEqual("skipped", result.records[0].status)
        self.assertIn("paper_source.version_unresolved", codes)
        self.assertEqual(0, transport.count(SRC_URL))
        self.assertIn(
            "paper_source.arxiv_lookup_failed",
            [item.code for item in result.diagnostics],
        )

    async def test_oai_record_pins_the_version_when_the_batch_query_is_rate_limited(
        self,
    ) -> None:
        fetcher, transport = self.build(
            {
                QUERY_URL: HttpResponse(status=429, url="", body=b"Rate exceeded."),
                OAI_URL: ok(OAI_RECORD),
                SRC_URL: ok(self.source),
            }
        )

        result = await fetcher.fetch(self.pasa_request(fetch_license=True))
        record = result.records[0]
        codes = [item.code for item in record.diagnostics]

        self.assertEqual("fetched", record.status)
        self.assertEqual("v2", record.version)
        self.assertTrue(record.version_pinned)
        self.assertEqual("arxiv:2501.10120v2/src", record.source_locator)
        self.assertEqual("http://creativecommons.org/licenses/by/4.0/", record.license)
        self.assertNotIn("paper_source.version_unresolved", codes)
        self.assertEqual(1, transport.count(SRC_URL))

    async def test_unpinned_download_is_allowed_with_a_warning(self) -> None:
        fetcher, transport = self.build(
            {
                QUERY_URL: HttpResponse(status=503, url="", body=b""),
                "https://arxiv.org/src/2501.10120": ok(self.source),
            }
        )

        record = (
            await fetcher.fetch(self.pasa_request(allow_unpinned_version=True))
        ).records[0]

        self.assertEqual("fetched", record.status)
        self.assertFalse(record.version_pinned)
        self.assertEqual("arxiv:2501.10120/src", record.source_locator)
        self.assertIn(
            "paper_source.unpinned_version", [item.code for item in record.diagnostics]
        )
        self.assertEqual(1, transport.count("https://arxiv.org/src/2501.10120"))

    async def test_title_mismatch_is_reported(self) -> None:
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})
        request = self.pasa_request()
        request.papers[0].identity.title = "Attention Is All You Need"

        record = (await fetcher.fetch(request)).records[0]

        self.assertIn(
            "paper_source.title_mismatch", [item.code for item in record.diagnostics]
        )

    async def test_locator_cache_makes_a_rerun_skip_the_download(self) -> None:
        fetcher, transport = self.build(
            {QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)}
        )

        first = await fetcher.fetch(self.pasa_request())
        second = await fetcher.fetch(self.pasa_request())

        self.assertEqual(1, transport.count(SRC_URL))
        self.assertFalse(first.records[0].cache_hit)
        self.assertTrue(second.records[0].cache_hit)
        self.assertEqual(1, second.stats.http_requests)
        self.assertEqual(
            first.records[0].tex_source_ref, second.records[0].tex_source_ref
        )

    async def test_unrecognized_payload_fails_instead_of_becoming_garbage_tex(
        self,
    ) -> None:
        fetcher, _ = self.build(
            {QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(b"<html>maintenance</html>")}
        )

        record = (
            await fetcher.fetch(self.pasa_request(allow_non_arxiv_channels=False))
        ).records[0]

        self.assertEqual("failed", record.status)
        self.assertIsNone(record.conversion_request_ref)
        self.assertIn(
            "paper_source.payload_not_recognized",
            [item.code for item in record.diagnostics],
        )

    async def test_non_http_download_hint_is_refused(self) -> None:
        fetcher, transport = self.build({})
        request = PaperSourceRequest(
            papers=[
                PaperRef(
                    identity=PaperIdentity(doi="10.1145/3654777"),
                    hints=[SourceHint(url="file:///etc/passwd", kind="oa_pdf")],
                )
            ]
        )

        record = (await fetcher.fetch(request)).records[0]
        codes = [item.code for item in record.diagnostics]

        self.assertEqual("skipped", record.status)
        self.assertIn("paper_source.url_scheme_rejected", codes)
        self.assertNotIn("file:///etc/passwd", transport.calls)

    async def test_open_access_hint_is_downloaded_for_a_non_arxiv_paper(self) -> None:
        hint = "https://dl.acm.org/doi/pdf/10.1145/3654777"
        fetcher, transport = self.build({hint: ok(b"%PDF-1.7\nbody")})
        request = PaperSourceRequest(
            papers=[
                PaperRef(
                    identity=PaperIdentity(doi="10.1145/3654777"),
                    hints=[SourceHint(url=hint, kind="publisher_pdf")],
                )
            ]
        )

        record = (await fetcher.fetch(request)).records[0]

        self.assertEqual("fetched", record.status)
        self.assertEqual("hint_pdf", record.channel)
        self.assertIsNotNone(record.pdf_ref)
        self.assertEqual(1, transport.count(hint))

    async def test_max_papers_cap_is_reported_at_batch_level(self) -> None:
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})
        request = self.pasa_request(max_papers=1)
        request.papers.append(PaperRef(identity=PaperIdentity(arxiv_id="1706.03762")))

        result = await fetcher.fetch(request)

        self.assertEqual(2, result.stats.requested)
        self.assertEqual(1, result.stats.accepted)
        self.assertIn(
            "paper_source.max_papers_truncated",
            [item.code for item in result.diagnostics],
        )

    async def test_cancellation_stops_the_batch(self) -> None:
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})
        cancel = asyncio.Event()
        cancel.set()

        with self.assertRaises(asyncio.CancelledError):
            await fetcher.fetch(self.pasa_request(), cancel)


class PaperFetchToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_returns_conversion_refs_ready_for_paper_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            artifacts = LocalArtifactStore(root)
            source = tar_gz_bytes({"main.tex": b"\\documentclass{article}\\section{A}"})
            transport = FakeTransport({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(source)})
            tool = PaperFetchTool(artifacts, http=limiter(transport, []))
            request = PaperSourceRequest(
                papers=[PaperRef(identity=PaperIdentity(arxiv_id="2501.10120"))]
            )
            request_ref = await artifacts.put_text(request.model_dump_json())
            ctx = ToolContext("paper_fetch", "call-1", noop_emit, asyncio.Event())

            result = await tool.ainvoke(ctx, request_ref=request_ref)

            self.assertTrue(result.success, result.error)
            self.assertEqual(1, result.data["fetched"])
            refs = result.data["conversion_request_refs"]
            self.assertEqual(1, len(refs))
            self.assertIn(result.data["result_ref"], result.artifacts)

            payload = await artifacts.get_text(refs[0])
            conversion = PaperConversionRequest.model_validate_json(payload)
            self.assertEqual("arxiv:2501.10120", conversion.paper_id)
            self.assertEqual("required", conversion.visual_policy)

    async def test_tool_registers_and_emits_the_standard_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            artifacts = LocalArtifactStore(root)
            source = tar_gz_bytes({"main.tex": b"\\documentclass{article}"})
            transport = FakeTransport({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(source)})
            tool = PaperFetchTool(artifacts, http=limiter(transport, []))
            registry = ToolRegistry()
            registry.register(tool)
            events: list[str] = []

            async def emit(kind: str, _ref: str, _data: dict | None = None) -> None:
                events.append(kind)

            request = PaperSourceRequest(
                papers=[PaperRef(identity=PaperIdentity(arxiv_id="2501.10120"))]
            )
            request_ref = await artifacts.put_text(request.model_dump_json())
            ctx = ToolContext(tool.spec.name, "call-3", emit, asyncio.Event())

            result = await registry.resolve("paper_fetch").ainvoke(
                ctx, request_ref=request_ref
            )

            self.assertTrue(result.success, result.error)
            self.assertEqual(["paper_fetch"], [spec.name for spec in registry.specs])
            self.assertEqual([TOOL_BEGIN, TOOL_END], events)

    async def test_tool_rejects_a_blank_request_ref(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            tool = PaperFetchTool(LocalArtifactStore(root))
            ctx = ToolContext("paper_fetch", "call-2", noop_emit, asyncio.Event())

            result = await tool.ainvoke(ctx, request_ref="  ")

            self.assertFalse(result.success)
            self.assertIn("non-empty artifact reference", result.error)


if __name__ == "__main__":
    unittest.main()
