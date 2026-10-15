"""Regression tests for the arXiv article preparation script."""

import sys
from types import SimpleNamespace

import pytest

from scripts import prepare_examples_article as article


def test_pdf_document_closes_when_text_extraction_fails(tmp_path, monkeypatch):
    """A page extraction error must not leak the opened PyMuPDF document."""
    html = (
        '<h1 class="title mathjax">Title: Example</h1>'
        '<div class="authors"><a>A. Author</a></div>'
        '<blockquote class="abstract mathjax">Summary</blockquote>'
    )
    monkeypatch.setattr(
        article, "fetch", lambda url: html.encode() if "/abs/" in url else b"pdf"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_examples_article.py",
            "--arxiv-id",
            "1234.56789",
            "--out",
            str(tmp_path),
        ],
    )

    class Document:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.closed = True

        def __iter__(self):
            return iter([SimpleNamespace(number=0, get_text=lambda _kind: _fail())])

    def _fail():
        raise RuntimeError("extraction failed")

    document = Document()
    monkeypatch.setattr(article.fitz, "open", lambda **_kwargs: document)

    with pytest.raises(RuntimeError, match="extraction failed"):
        article.main()

    assert document.closed
