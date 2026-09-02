"""Kaggle API v1 客户端：元数据走 urllib GET，数据下载经 kagglehub，提交走两步 POST。"""

import asyncio
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from athena.kaggle.auth import KaggleCredentials
from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    HttpResponse,
    UrllibTransport,
    lower_headers,
)

KAGGLE_HOST = "www.kaggle.com"
BASE_URL = f"https://{KAGGLE_HOST}/api/v1"
MAX_SUBMIT_BYTES = 256 * 1024 * 1024


def slug_from_ref(ref: str) -> str:
    """新版 API 的 ``ref`` 是完整 URL，取其最后一段作 slug；非 URL 原样返回。"""
    value = (ref or "").strip()
    if not value or "://" not in value:
        return value
    return urllib.parse.urlsplit(value).path.strip("/").rsplit("/", 1)[-1]


def pick_first(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """取 ``keys`` 中第一个非空值（兼容新旧 API 的 snake/camel 键名）。"""
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default


def author_name(author: object) -> str:
    """归一化 notebook 的 ``author`` 字段（字符串或 ``{"name": ...}``）。"""
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("name", ""))
    return ""


class KaggleApiError(RuntimeError):
    def __init__(self, status: int, message: str, url: str) -> None:
        self.status = status
        self.message = message
        self.url = url
        super().__init__(f"Kaggle API {status} for {url}: {message}")


class KaggleAuthError(KaggleApiError):
    """未配置任何凭据。"""


def _error_message(response: HttpResponse) -> str:
    try:
        payload = json.loads(response.body.decode("utf-8"))
        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                if payload.get(key):
                    return str(payload[key])
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return response.body.decode("utf-8", errors="replace").strip()[:300] or "no body"


class KaggleApiClient:
    def __init__(
        self, credentials: KaggleCredentials, http: HostRateLimiter | None = None
    ) -> None:
        self._credentials = credentials
        self._http = http or HostRateLimiter(
            transport=UrllibTransport(),
            bucket_intervals={KAGGLE_HOST: 0.5},
        )

    @property
    def http_request_count(self) -> int:
        return self._http.request_count

    @property
    def configured(self) -> bool:
        return self._credentials.auth_header() is not None

    def _auth_headers(self) -> dict[str, str]:
        header = self._credentials.auth_header()
        if not header:
            raise KaggleAuthError(0, "no Kaggle credentials configured", BASE_URL)
        return {"Authorization": header}

    async def _get_url(
        self, url: str, params: dict[str, Any] | None = None
    ) -> HttpResponse:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        response = await self._http.get(url, self._auth_headers())
        if not response.ok:
            raise KaggleApiError(
                response.status, _error_message(response), response.url
            )
        return response

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> HttpResponse:
        return await self._get_url(BASE_URL + path, params)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._get(path, params)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise KaggleApiError(response.status, "not JSON", response.url) from error

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        return (await self._get(path, params)).body

    async def list_competitions(
        self,
        *,
        search: str = "",
        page: int = 1,
        sort_by: str = "latestDeadline",
        group: str = "general",
    ) -> list[dict]:
        params = {"page": max(page, 1), "sortBy": sort_by, "group": group}
        if search:
            params["search"] = search
        result = await self.get_json("/competitions/list", params)
        if not isinstance(result, list):
            return []
        for item in result:
            if isinstance(item, dict) and item.get("ref"):
                item["ref"] = slug_from_ref(item["ref"])
        return result

    async def get_competition(self, ref: str) -> dict:
        result = await self.get_json(f"/competitions/get/{ref}")
        return result if isinstance(result, dict) else {}

    async def list_data_files(self, ref: str) -> list[dict]:
        result = await self.get_json(f"/competitions/data/list/{ref}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            files = result.get("files") or result.get("datasets") or []
            return files if isinstance(files, list) else []
        return []

    async def download_competition(self, ref: str, dest_dir: str | Path) -> list[Path]:
        """经 kagglehub 下载竞赛全部数据到 ``dest_dir``，返回本地文件路径。"""
        target = Path(dest_dir)
        target.mkdir(parents=True, exist_ok=True)

        def _download() -> list[Path]:
            import kagglehub

            try:
                downloaded = kagglehub.competition_download(ref, output_dir=str(target))
            except (
                Exception
            ) as exc:  # noqa: BLE001 - kagglehub 异常类型不稳定，按状态码识别
                text = str(exc)
                if "403" in text:
                    raise KaggleApiError(
                        403,
                        (
                            f"{text}. Kaggle API credentials may be valid, but the "
                            f"competition rules were not accepted for '{ref}'. "
                            f"Accept them at https://www.kaggle.com/competitions/{ref}/rules "
                            "and retry."
                        ),
                        f"https://www.kaggle.com/competitions/{ref}",
                    ) from exc
                raise
            root = Path(downloaded)
            return [p for p in root.rglob("*") if p.is_file()]

        return await asyncio.to_thread(_download)

    async def list_notebooks(
        self,
        competition: str,
        *,
        search: str = "",
        page: int = 1,
        sort_by: str = "hotness",
    ) -> list[dict]:
        params = {"competition": competition, "page": max(page, 1), "sortBy": sort_by}
        if search:
            params["search"] = search
        result = await self.get_json("/kernels/list", params)
        return result if isinstance(result, list) else []

    async def get_notebook(self, ref: str) -> str:
        """Pull a public notebook's source text by ``owner/slug`` ref."""
        url = BASE_URL + "/kernels/pull?" + urllib.parse.urlencode({"kernel": ref})
        response = await self._http.get(url, self._auth_headers())
        if not response.ok:
            raise KaggleApiError(
                response.status, _error_message(response), response.url
            )
        return await asyncio.to_thread(_notebook_source_from_body, response.body)

    async def list_discussions(
        self,
        competition: str,
        *,
        page: int = 1,
        sort_by: str = "hotness",
    ) -> list[dict]:
        """List public Kaggle competition discussion threads (best-effort endpoint).

        Kaggle v1 does not expose a stable public discussions endpoint; this uses
        Kaggle's internal ``/api/i/...`` JSON endpoint. If that endpoint changes,
        callers degrade the same way as a failed notebook search.
        """
        params = {"page": max(page, 1), "sortBy": sort_by}
        url = (
            "https://www.kaggle.com/api/i/competitions/"
            f"{urllib.parse.quote(competition)}/discussions"
        )
        response = await self._get_url(url, params)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise KaggleApiError(
                response.status, "discussions response is not JSON", response.url
            ) from error
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("discussions", "threads", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_discussion(self, ref: str) -> str:
        """Read one Kaggle discussion thread and its comments as text."""
        url = (
            "https://www.kaggle.com/api/i/discussions/"
            f"{urllib.parse.quote(ref, safe='/')}"
        )
        response = await self._get_url(url)
        return await asyncio.to_thread(_discussion_source_from_body, response.body)

    async def _post(
        self, url: str, headers: dict[str, str], data: bytes
    ) -> HttpResponse:
        """一次 POST（JSON 或文件上传），在 worker 线程里执行阻塞 urllib。"""

        def do_post() -> HttpResponse:
            request = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=120.0) as response:
                    return HttpResponse(
                        status=response.status,
                        url=response.geturl(),
                        body=response.read(MAX_SUBMIT_BYTES),
                        headers=lower_headers(response.headers.items()),
                    )
            except urllib.error.HTTPError as error:
                return HttpResponse(
                    status=error.code,
                    url=url,
                    body=error.read(),
                    headers=lower_headers(error.headers.items()),
                )
            except urllib.error.URLError as error:
                raise KaggleApiError(0, f"POST failed: {error.reason}", url) from error

        return await asyncio.to_thread(do_post)

    async def submit_submission(self, competition: str, file_path: str | Path) -> dict:
        """提交预测文件到竞赛：先拿签名上传 URL，再把文件 multipart 上传。"""
        path = Path(file_path)
        content = path.read_bytes()
        file_name = path.name

        url = f"{BASE_URL}/competitions/submissions/url/{competition}"
        payload = json.dumps(
            {
                "fileName": file_name,
                "contentLength": len(content),
                "lastModifiedEpoch": int(path.stat().st_mtime),
            }
        ).encode("utf-8")
        response = await self._post(
            url, {**self._auth_headers(), "Content-Type": "application/json"}, payload
        )
        if not response.ok:
            raise KaggleApiError(
                response.status, _error_message(response), response.url
            )
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise KaggleApiError(response.status, "not JSON", response.url) from error
        create_url = data.get("createUrl") or data.get("create_url")
        if not create_url:
            raise KaggleApiError(
                response.status, "no createUrl in submission response", url
            )

        boundary = f"----AthenaKaggle{uuid4().hex}"
        body = _multipart_file(file_name, content, boundary)
        upload = await self._post(
            create_url,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            body,
        )
        if not upload.ok:
            raise KaggleApiError(upload.status, _error_message(upload), create_url)
        return {"competition": competition, "file": file_name, "status": "submitted"}


def _multipart_file(file_name: str, content: bytes, boundary: str) -> bytes:
    """构造 ``file`` 字段的 multipart/form-data 请求体。"""
    return (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode()
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _discussion_source_from_body(body: bytes) -> str:
    """Extract discussion text from Kaggle's internal JSON response.

    Accepted shapes:
    - ``{"title": ..., "body"|"content"|"text": ...}``
    - ``{"title": ..., "comments": [{"author": ..., "body"|"content"|"text": ...}]}``
    - ``{"result": {...}}`` or ``{"thread": {...}}`` wrappers.
    Falls back to raw UTF-8 text for non-JSON responses.
    """
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")
    if isinstance(payload, dict):
        for key in ("thread", "discussion", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                payload = nested
                break
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    parts: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(f"# {title.strip()}")
    for key in ("body", "content", "text", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
            break
    comments = payload.get("comments") or payload.get("replies")
    if isinstance(comments, list):
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            author = comment.get("author")
            if isinstance(author, dict):
                author = author.get("name", "")
            text = ""
            for key in ("body", "content", "text", "commentText"):
                value = comment.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            prefix = f"**{author}:**" if author else "**comment:**"
            if text:
                parts.append(f"{prefix} {text}")
    return "\n\n".join(parts) if parts else body.decode("utf-8", errors="replace")


def _notebook_source_from_body(body: bytes) -> str:
    """Extract notebook source from Kaggle's pull JSON, a zip archive, or raw text.

    The Kaggle v1 API nests the source as ``{"blob": {"source": "<notebook json>"}}``
    (see ``extract_agent.py`` for the concrete shape); a few older clients return a
    top-level ``source`` field. Both are accepted.
    """
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        if isinstance(payload, dict):
            blob = payload.get("blob")
            source = payload.get("source")
            if isinstance(blob, dict):
                source = blob.get("source", source)
            if isinstance(source, str) and source.strip():
                return source
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            for suffix in (".ipynb", ".py"):
                for name in names:
                    if name.endswith(suffix):
                        return archive.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError):
        pass
    return body.decode("utf-8", errors="replace")
