#!/usr/bin/env python3
"""Prepare one examples_articles dataset entry from arXiv.

Usage:
    python scripts/prepare_examples_article.py --arxiv-id 2505.06120 \
        --out docs/examples_articles/llms_get_lost_in_multi_turn_conversation

Does (script side only):
  1. Fetch the arXiv abs page (User-Agent required) and parse metadata.
  2. Download the latest-version PDF.
  3. Extract full text with PyMuPDF into paper_text.md.
  4. Write paper_meta.json and dataset_manifest.json with sha256.

The LLM side (not done here): after reading paper_text.md, write candidate.md
containing only human-style vague a-priori ideas (no evidence, no conclusions).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

try:
    import fitz  # PyMuPDF
except ImportError:
    print("missing dependency: pip install pymupdf", file=sys.stderr)
    raise

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch(url: str) -> bytes:
    req = Request(url, headers=UA)
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def strip_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_abs(html: str, arxiv_id: str) -> dict:
    title = re.search(r'<h1 class="title mathjax">(.*?)</h1>', html, flags=re.S)
    authors = re.search(r'<div class="authors">(.*?)</div>', html, flags=re.S)
    abstract = re.search(
        r'<blockquote class="abstract mathjax">(.*?)</blockquote>', html, flags=re.S
    )
    comments = re.search(
        r'<td class="tablecell comments mathjax">(.*?)</td>', html, flags=re.S
    )
    versions = re.findall(re.escape(arxiv_id) + r"v(\d+)", html)

    def clean(x: str) -> str:
        return re.sub(r"\s+", " ", strip_tags(x)).strip()

    title = clean(title.group(1)) if title else ""
    if title.startswith("Title:"):
        title = title[len("Title:") :].strip()
    author_list = []
    if authors:
        author_list = [
            a.strip() for a in re.findall(r"<a[^>]*>([^<]+)</a>", authors.group(1))
        ]
    abstract_text = clean(abstract.group(1)) if abstract else ""
    comments_text = clean(comments.group(1)) if comments else ""
    version = max(int(v) for v in versions) if versions else 0
    return {
        "title": title,
        "authors": author_list,
        "abstract": abstract_text,
        "comments": comments_text,
        "version": str(version) if version else "latest",
    }


def write_manifest(out_dir: Path, meta_obj: dict, arxiv_id: str, version: str) -> None:
    manifest = {
        "dataset": "examples_articles",
        "entry": {
            "arxiv_id": arxiv_id,
            "arxiv_version": version,
            "title": meta_obj.get("title", ""),
            "authors": meta_obj.get("authors", []),
            "venue": meta_obj.get("venue", ""),
            "venue_hint": meta_obj.get("comments", ""),
        },
        "files": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tools": {
            "crawl_script": "scripts/prepare_examples_article.py",
            "text_extractor": "PyMuPDF",
        },
        "notes": [
            "candidate.md is written separately by an LLM; it must contain only "
            "human-style vague a-priori ideas, with no evidence and no paper conclusions."
        ],
    }
    file_order = [
        ("paper.pdf", out_dir / "paper.pdf"),
        ("paper_text.md", out_dir / "paper_text.md"),
        ("paper_meta.json", out_dir / "paper_meta.json"),
        ("candidate.md", out_dir / "candidate.md"),
    ]
    for name, path in file_order:
        if path.exists():
            manifest["files"][name] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
    (out_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arxiv-id", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--update-manifest-only", action="store_true")
    args = parser.parse_args()

    arxiv_id = args.arxiv_id
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.update_manifest_only:
        meta_path = out_dir / "paper_meta.json"
        if not meta_path.exists():
            print(
                "ERROR: paper_meta.json missing; run without --update-manifest-only first",
                file=sys.stderr,
            )
            sys.exit(1)
        meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
        if args.venue:
            meta_obj["venue"] = args.venue
            meta_path.write_text(
                json.dumps(meta_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        write_manifest(out_dir, meta_obj, arxiv_id, meta_obj.get("arxiv_version", "?"))
        print(f"manifest updated: {out_dir / 'dataset_manifest.json'}")
        return

    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    print(f"[1/5] fetch abs page: {abs_url}")
    abs_html = fetch(abs_url).decode("utf-8", "ignore")
    meta = parse_abs(abs_html, arxiv_id)
    if not meta["title"]:
        print("ERROR: could not parse title from abs page", file=sys.stderr)
        sys.exit(1)

    version_suffix = "" if meta["version"] == "latest" else f"v{meta['version']}"
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}{version_suffix}"
    print(
        f"[2/5] download PDF ({'latest' if version_suffix == '' else version_suffix}): {pdf_url}"
    )
    pdf_bytes = fetch(pdf_url)
    pdf_path = out_dir / "paper.pdf"
    pdf_path.write_bytes(pdf_bytes)

    print("[3/5] extract text with PyMuPDF")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(f"<!-- page {page.number + 1} -->\n" + page.get_text("text"))
    doc.close()
    paper_text = (
        f"# {meta['title']}\n\n"
        f"arXiv: {arxiv_id}v{meta['version']}\n\n"
        f"## Abstract\n\n{meta['abstract']}\n\n"
        "## Full text (extracted by PyMuPDF)\n\n" + "\n".join(pages)
    )
    text_path = out_dir / "paper_text.md"
    text_path.write_text(paper_text, encoding="utf-8")

    print("[4/5] write paper_meta.json")
    meta_path = out_dir / "paper_meta.json"
    meta_obj = {
        "arxiv_id": arxiv_id,
        "arxiv_version": meta["version"],
        "title": meta["title"],
        "authors": meta["authors"],
        "abstract": meta["abstract"],
        "comments": meta["comments"],
        "venue": args.venue,  # e.g. "ICLR 2026 Outstanding Paper"; provided by the human/LLM curator
        "source_urls": {
            "abs": abs_url,
            "pdf": pdf_url,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(
        json.dumps(meta_obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("[5/5] write dataset_manifest.json")
    write_manifest(out_dir, meta_obj, arxiv_id, meta["version"])

    print(f"done: {out_dir}")
    print(f"  title: {meta['title']}")
    print(f"  authors: {', '.join(meta['authors'])}")
    print(f"  comments: {meta['comments']}")
    print("  next (LLM step): read paper_text.md and write candidate.md")


if __name__ == "__main__":
    main()
