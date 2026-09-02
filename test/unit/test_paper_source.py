"""Network-free tests for the paper_source fetch pipeline."""

import asyncio
import io
import tarfile
import tempfile
import unittest
import zipfile

from athena.core.tool import ToolRegistry
from athena.core.tool_types import TOOL_BEGIN, TOOL_END, ToolContext
from athena.research.literature.paper_markdown.schemas import PaperConversionRequest
from athena.research.literature.paper_source.arxiv import (
    parse_atom_feed,
    parse_raw_record,
)
from athena.research.literature.paper_source.fetcher import (
    DEFAULT_FETCH_CONCURRENCY,
    LocatorCache,
    PaperSourceFetcher,
    sniff_payload,
    title_similarity,
)
from athena.research.literature.paper_source.http import (
    DEFAULT_BUCKET_INTERVALS,
    HostRateLimiter,
    HttpResponse,
    HttpTransportError,
    rate_limit_bucket,
)
from athena.research.literature.paper_source.openalex import parse_work
from athena.research.literature.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRequest,
    SourceHint,
    normalize_arxiv_id,
    normalize_doi,
)
from athena.research.literature.paper_source.tool import PaperFetchTool
from athena.core.artifact_store import LocalArtifactStore

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
        """Pop the next queued response for a prefix, keeping the last one sticky.

        A route may also be an exception instance, which is raised instead of
        returned; that is how transport-layer failures (DNS, TLS, timeout) are
        simulated, since those never produce a response at all.
        """
        route = self.routes[prefix]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
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
    def test_openalex_work_retains_citation_count(self) -> None:
        work = parse_work(
            {
                "id": "https://openalex.org/W123",
                "display_name": "A Baseline Paper",
                "publication_year": 2020,
                "cited_by_count": 137,
            }
        )

        self.assertEqual("W123", work.openalex_id)
        self.assertEqual(137, work.cited_by_count)

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

    async def test_retries_a_transport_failure_just_like_a_429(self) -> None:
        """超时与 429 一样是瞬时故障，必须重试。

        真机命中：``export.arxiv.org`` 的批量版本解析是整批一次请求，它一超时，这一批
        所有 arXiv 论文都会因 ``version_unresolved`` 被跳过——13 篇里当场丢掉 9 篇。
        """
        transport = FakeTransport(
            {
                "https://arxiv.org/src/": [
                    HttpTransportError("timed out"),
                    HttpTransportError("timed out"),
                    ok(b"%PDF-1.5"),
                ]
            }
        )
        sleeps: list[float] = []

        response = await limiter(transport, sleeps).get(SRC_URL)

        self.assertEqual(200, response.status)
        self.assertEqual(3, transport.count("https://arxiv.org/src/"))
        self.assertEqual(2, len(sleeps))

    async def test_a_transport_failure_still_surfaces_once_retries_run_out(
        self,
    ) -> None:
        """持续不可达要如实上抛，由调用方按段决定降级——不能重试到永远也不能吞掉。"""
        transport = FakeTransport(
            {"https://arxiv.org/src/": HttpTransportError("timed out")}
        )
        rate_limited = limiter(transport, [])

        with self.assertRaises(HttpTransportError):
            await rate_limited.get(SRC_URL)

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
        self, routes: dict[str, object], concurrency: int = DEFAULT_FETCH_CONCURRENCY
    ) -> tuple[PaperSourceFetcher, FakeTransport]:
        """Build a fetcher wired to a fake transport and an in-memory cache."""
        transport = FakeTransport(routes)
        fetcher = PaperSourceFetcher(
            self.artifacts,
            http=limiter(transport, []),
            cache=LocatorCache(),
            concurrency=concurrency,
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

    async def test_an_unreachable_hint_host_does_not_abort_the_batch(self) -> None:
        """真机命中：一条 doi.org 线索 TLS 握手超时，异常一路逃到 run_survey 打断全程。

        线索 URL 来自检索后端，域名完全不可控。取源是唯一按篇计费的阶段，跑到一半崩掉
        等于前面下载的都白花，因此传输层失败只能降级成诊断。
        """
        fetcher, transport = self.build(
            {
                QUERY_URL: ok(ATOM_FEED),
                SRC_URL: ok(self.source),
                "https://dead.example": HttpTransportError("handshake timed out"),
            }
        )
        request = PaperSourceRequest(
            papers=[
                PaperRef(
                    identity=PaperIdentity(doi="10.18845/tm.v37i7.7295"),
                    hints=[SourceHint(url="https://dead.example/x.pdf", kind="oa_pdf")],
                ),
                PaperRef(identity=PaperIdentity(arxiv_id="arXiv:2501.10120")),
            ],
            policy=PaperSourcePolicy(),
        )

        result = await fetcher.fetch(request)

        self.assertEqual("failed", result.records[0].status)
        self.assertEqual("fetched", result.records[1].status)
        self.assertIn(
            "paper_source.transport_failed",
            {item.code for item in result.records[0].diagnostics},
        )

    async def test_an_unreachable_metadata_endpoint_still_lets_papers_through(
        self,
    ) -> None:
        """版本解析在逐篇取源之前，异常逃出去等于整批一篇都拿不到。"""
        fetcher, _ = self.build(
            {
                QUERY_URL: HttpTransportError("arxiv.org unreachable"),
                # 版本没解析出来，下载走的是不带 v2 的定位符
                "https://arxiv.org/src/2501.10120": ok(self.source),
            }
        )

        result = await fetcher.fetch(self.pasa_request(allow_unpinned_version=True))

        self.assertEqual("fetched", result.records[0].status)
        self.assertFalse(result.records[0].version_pinned)
        self.assertIn(
            "paper_source.transport_failed",
            {item.code for item in result.diagnostics},
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

    async def test_fetching_stops_once_the_success_target_is_met(self) -> None:
        """按"要几篇成功的"下单，够数就不再下载后面的候选。

        成功率按通道差一倍（实测 arXiv 91%、期刊 47%），而候选的通道构成每轮都不同，
        任何固定的超额系数都会随构成失准——真机上失准过一次，13 篇候选里 5 篇取不到。
        """
        fetcher, transport = self.build(
            {QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)}
        )
        request = self.pasa_request(max_papers=5, stop_after_fetched=1)
        for extra in ("1706.03762", "1512.03385", "2009.02040"):
            request.papers.append(PaperRef(identity=PaperIdentity(arxiv_id=extra)))

        result = await fetcher.fetch(request)

        self.assertEqual(4, result.stats.accepted)
        self.assertEqual(1, result.stats.attempted)
        self.assertEqual(1, result.stats.fetched)
        self.assertEqual(1, len(result.records))
        self.assertIn(
            "paper_source.stopped_after_target",
            [item.code for item in result.diagnostics],
        )

    async def test_failures_do_not_count_towards_the_success_target(self) -> None:
        """失败的候选要继续往下试，否则"够数即停"就退化成"试够几次即停"。"""
        fetcher, _ = self.build(
            {
                QUERY_URL: ok(ATOM_FEED),
                "https://arxiv.org/src/2501.10120": HttpResponse(
                    status=404, url="", body=b""
                ),
                "https://arxiv.org/src/": ok(self.source),
            }
        )
        request = self.pasa_request(
            max_papers=5, stop_after_fetched=1, allow_unpinned_version=True
        )
        request.papers.append(PaperRef(identity=PaperIdentity(arxiv_id="1706.03762")))

        result = await fetcher.fetch(request)

        self.assertEqual(2, result.stats.attempted)
        self.assertEqual(1, result.stats.fetched)
        self.assertEqual(1, result.stats.failed)

    async def test_no_target_attempts_every_accepted_paper(self) -> None:
        """默认 0 保持原行为：接受几篇就试几篇。"""
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})
        request = self.pasa_request(max_papers=5)
        request.papers.append(PaperRef(identity=PaperIdentity(arxiv_id="1706.03762")))

        result = await fetcher.fetch(request)

        self.assertEqual(2, result.stats.accepted)
        self.assertEqual(2, result.stats.attempted)
        self.assertNotIn(
            "paper_source.stopped_after_target",
            [item.code for item in result.diagnostics],
        )

    async def test_cancellation_stops_the_batch(self) -> None:
        fetcher, _ = self.build({QUERY_URL: ok(ATOM_FEED), SRC_URL: ok(self.source)})
        cancel = asyncio.Event()
        cancel.set()

        with self.assertRaises(asyncio.CancelledError):
            await fetcher.fetch(self.pasa_request(), cancel)


class FetchConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """并发取源必须与串行版给出**逐字相同**的 records，只是更快。

    这是接受这次改动的全部前提：``stop_after_fetched`` 决定交付集合，而它原本依赖
    "挨个试、够数即停"。分波并发之后仍要按名次收、够数即停，超出停止点的那几条丢掉。
    """

    async def asyncSetUp(self) -> None:
        self.artifacts = LocalArtifactStore(tempfile.mkdtemp(prefix="fetch_conc_"))

    def _fetcher(self, routes, concurrency):
        return PaperSourceFetcher(
            self.artifacts,
            http=limiter(FakeTransport(routes), []),
            cache=LocatorCache(),
            concurrency=concurrency,
        )

    def _request(self, ids: list[str], target: int) -> PaperSourceRequest:
        return PaperSourceRequest(
            papers=[PaperRef(identity=PaperIdentity(arxiv_id=item)) for item in ids],
            policy=PaperSourcePolicy(
                max_papers=len(ids),
                stop_after_fetched=target,
                allow_unpinned_version=True,
            ),
        )

    def _routes(self, source: bytes, failing: set[str]) -> dict:
        routes = {QUERY_URL: ok(ATOM_FEED), "https://arxiv.org/src/": ok(source)}
        for item in failing:
            routes[f"https://arxiv.org/src/{item}"] = HttpResponse(
                status=404, url="", body=b""
            )
        return routes

    async def _records(self, ids, failing, target, concurrency):
        source = tar_gz_bytes(
            {
                "main.tex": rb"\documentclass{article}"
                rb"\begin{document}PaSa\end{document}"
            }
        )
        fetcher = self._fetcher(self._routes(source, failing), concurrency)
        result = await fetcher.fetch(self._request(ids, target))
        return [(item.paper_key, item.status) for item in result.records]

    async def test_waves_match_the_serial_order_exactly(self) -> None:
        """交错的成功与失败是最容易出错的形态：够数的那一刻正落在某一波中间。"""
        ids = ["2501.10120", "1706.03762", "1512.03385", "2009.02040", "1412.6980"]
        failing = {"1706.03762", "2009.02040"}

        serial = await self._records(ids, failing, 2, 1)
        parallel = await self._records(ids, failing, 2, 4)

        self.assertEqual(serial, parallel)

    async def test_a_target_reached_mid_wave_discards_the_rest_of_it(self) -> None:
        """目标在一波中间达成时，本波剩下的结果必须丢掉，否则 records 会比串行版长。"""
        ids = ["2501.10120", "1706.03762", "1512.03385", "2009.02040"]

        serial = await self._records(ids, set(), 1, 1)
        parallel = await self._records(ids, set(), 1, 4)

        self.assertEqual(1, len(serial))
        self.assertEqual(serial, parallel)

    async def test_no_target_attempts_every_candidate_either_way(self) -> None:
        """没有停止目标时两者都要把候选试完，一篇不少。"""
        ids = ["2501.10120", "1706.03762", "1512.03385"]

        serial = await self._records(ids, {"1706.03762"}, 0, 1)
        parallel = await self._records(ids, {"1706.03762"}, 0, 4)

        self.assertEqual(3, len(serial))
        self.assertEqual(serial, parallel)

    async def test_all_failing_candidates_are_all_attempted(self) -> None:
        """全失败时不能提前收手——够数即停不是"试够几次即停"。"""
        ids = ["2501.10120", "1706.03762", "1512.03385"]
        failing = set(ids)

        serial = await self._records(ids, failing, 2, 1)
        parallel = await self._records(ids, failing, 2, 4)

        self.assertEqual(3, len(serial))
        self.assertEqual(serial, parallel)


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
