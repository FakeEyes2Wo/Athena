"""补全测试 — 纯函数 candidates() 与 prompt_toolkit 适配器各测一遍。"""

from prompt_toolkit.document import Document

from athena.tui.completion import AthenaCompleter, candidates


def texts(items) -> list[str]:
    return [c.text for c in items]


def test_slash_prefix_filters():
    assert texts(candidates("/thr", 4)) == ["threads"]


def test_bare_slash_lists_all():
    assert len(candidates("/", 1)) > 5


def test_slash_only_at_line_start():
    assert candidates("读一下 /thr", 9) == []


def test_slash_candidate_replaces_prefix_only():
    matches = candidates("/mod", 4)
    assert texts(matches) == ["mode", "model"]
    assert all(c.replace_len == 3 for c in matches)


def test_no_candidates_for_plain_text():
    assert candidates("hello", 5) == []


def test_at_path_lists_directory(tmp_path):
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    assert texts(candidates("@", 1, cwd=tmp_path)) == ["beta/", "alpha.py"]


def test_at_path_filters_by_stem(tmp_path):
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    assert texts(candidates("@al", 3, cwd=tmp_path)) == ["alpha.py"]


def test_at_path_descends(tmp_path):
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "mod.py").write_text("x", encoding="utf-8")
    assert texts(candidates("@pkg/", 5, cwd=tmp_path)) == ["mod.py"]


def test_at_path_skips_noise_dirs(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "src").mkdir()
    assert texts(candidates("@", 1, cwd=tmp_path)) == ["src/"]


def test_at_needs_whitespace_before(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert candidates("mail@", 5, cwd=tmp_path) == []
    assert texts(candidates("看 @", 3, cwd=tmp_path)) == ["a.py"]


def test_at_stops_at_whitespace(tmp_path):
    assert candidates("@a b", 4, cwd=tmp_path) == []


def test_completer_adapter_yields_completions(tmp_path):
    completer = AthenaCompleter(cwd=tmp_path)
    document = Document("/hel", 4)
    results = list(completer.get_completions(document, None))
    assert results[0].text == "help"
    assert results[0].start_position == -3
