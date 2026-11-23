import logging

import pytest

from wscontext.config import Config, load_config, set_document_paths


def test_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg == Config()
    assert cfg.retention.raw_days == 180
    assert cfg.sync.remote is None


def test_overrides(tmp_path):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text(
        '[redaction]\nextra_patterns = ["acme-[0-9]+"]\n'
        '[retention]\nraw_days = 30\n'
        '[sync]\nremote = "origin"\nfetch_throttle_seconds = 10\n'
        '[hooks]\ntime_budget_ms = 500\n')
    cfg = load_config(tmp_path)
    assert cfg.redaction.extra_patterns == ("acme-[0-9]+",)
    assert cfg.retention.raw_days == 30
    assert cfg.sync.remote == "origin" and cfg.sync.fetch_throttle_seconds == 10
    assert cfg.hooks.time_budget_ms == 500


def test_unknown_keys_ignored_and_logged(tmp_path, caplog):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text('mystery = 1\n[retention]\nraw_days = 5\nbogus = true\n')
    with caplog.at_level(logging.WARNING):
        cfg = load_config(tmp_path)
    assert cfg.retention.raw_days == 5
    assert "mystery" in caplog.text and "retention.bogus" in caplog.text


def test_committed_config_matches_defaults():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    assert load_config(repo) == Config()


def test_capture_and_log_settings(tmp_path):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text('[capture]\nprecompact_snapshot = false\n[logs]\nmax_bytes = 10\n')
    cfg = load_config(tmp_path)
    assert cfg.capture.precompact_snapshot is False and cfg.logs.max_bytes == 10


def test_stage2_defaults():
    cfg = Config()
    assert cfg.capture.precompact_snapshot is False and cfg.capture.tool_result_max_chars == 0
    assert cfg.retrieval.enabled is True and cfg.retrieval.brief is True
    assert cfg.store.session_warn_bytes == 10 * 1024 ** 2
    assert cfg.store.store_warn_bytes == 1024 ** 3


def test_retrieval_store_and_capture_overrides(tmp_path):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text(
        '[retrieval]\nenabled = false\nbrief = false\n'
        '[store]\nsession_warn_bytes = 5\nstore_warn_bytes = 50\n'
        '[capture]\nprecompact_snapshot = true\ntool_result_max_chars = 8000\n')
    cfg = load_config(tmp_path)
    assert cfg.retrieval.enabled is False and cfg.retrieval.brief is False
    assert cfg.store.session_warn_bytes == 5 and cfg.store.store_warn_bytes == 50
    assert cfg.capture.precompact_snapshot is True and cfg.capture.tool_result_max_chars == 8000


def test_graph_defaults():
    g = Config().graph
    assert g.languages == ("python", "typescript", "javascript", "rust")
    assert g.exclude == () and g.query_budget_ms == 5000 and g.cochange_commits == 2000


def test_graph_overrides_and_unknown_language(tmp_path):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text(
        '[graph]\nlanguages = ["python", "cobol"]\nexclude = ["vendor/**"]\n'
        'query_budget_ms = 100\ncochange_commits = 10\n')
    g = load_config(tmp_path).graph
    assert g.languages == ("python", "cobol") and g.exclude == ("vendor/**",)
    assert g.query_budget_ms == 100 and g.cochange_commits == 10


def test_documents_default_and_loaded_list(tmp_path):
    assert Config().documents.paths == ()
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text('[documents]\npaths = ["docs/a.md", "README.md"]\n')
    assert load_config(tmp_path).documents.paths == ("docs/a.md", "README.md")


def test_unknown_documents_key_is_warned_about(tmp_path, caplog):
    d = tmp_path / ".wscontext"
    d.mkdir()
    (d / "config.toml").write_text('[documents]\npaths = []\nbogus = 1\n')
    with caplog.at_level(logging.WARNING):
        load_config(tmp_path)
    assert "documents.bogus" in caplog.text


def _cfg(tmp_path, text=None):
    path = tmp_path / ".wscontext" / "config.toml"
    if text is not None:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode())
    return path


def test_set_document_paths_appends_the_table(tmp_path):
    before = "# keep\n[retention]\nraw_days = 30  # days\n"
    path = _cfg(tmp_path, before)
    set_document_paths(tmp_path, ["docs/a.md"])
    text = path.read_text()
    assert text.startswith(before) and text.endswith('[documents]\npaths = [\n  "docs/a.md",\n]\n')
    assert load_config(tmp_path).documents.paths == ("docs/a.md",)


def test_set_document_paths_creates_a_missing_file(tmp_path):
    set_document_paths(tmp_path, ["a.md"])
    assert load_config(tmp_path).documents.paths == ("a.md",)


def test_set_document_paths_adds_the_key_to_an_existing_table(tmp_path):
    path = _cfg(tmp_path, "[documents]  # mine\n[graph]\nexclude = []\n")
    set_document_paths(tmp_path, ["a.md", "b.md"])
    assert path.read_text().startswith("[documents]  # mine\npaths = [")
    assert path.read_text().endswith("[graph]\nexclude = []\n")
    assert load_config(tmp_path).documents.paths == ("a.md", "b.md")


@pytest.mark.parametrize("old", ['paths = []', 'paths = ["x.md"]  # old',
                                 'paths = [\n  "x.md",  # why ] [\n  "y.md",\n]'])
def test_set_document_paths_replaces_single_and_multi_line(tmp_path, old):
    head, tail = "# top\n[retention]\nraw_days = 1\n\n", "\n[graph]\n# c\nexclude = []\n"
    path = _cfg(tmp_path, f"{head}[documents]\n{old}{tail}")
    set_document_paths(tmp_path, ["new.md"])
    text = path.read_text()
    assert text.startswith(head + "[documents]\npaths = [\n  \"new.md\",\n]")
    assert text.endswith(tail)
    assert load_config(tmp_path).documents.paths == ("new.md",)


def test_set_document_paths_keeps_other_bytes_and_empties_the_list(tmp_path):
    other = "[retention]\nraw_days = 1 # x\n\n\n[graph]\nexclude = ['a']\n"
    path = _cfg(tmp_path, other + '[documents]\npaths = ["a.md"]\n')
    set_document_paths(tmp_path, [])
    assert path.read_text() == other + "[documents]\npaths = []\n"


def test_set_document_paths_round_trips_quotes_and_backslashes(tmp_path):
    paths = ['we"ird.md', "back\\slash.md", "sp ace/ü.md"]
    _cfg(tmp_path, "[documents]\npaths = []\n")
    set_document_paths(tmp_path, paths)
    assert load_config(tmp_path).documents.paths == tuple(paths)


def test_set_document_paths_writes_nothing_when_the_result_is_bad(tmp_path):
    bad = "[documents]\npaths = []\n[documents]\n"
    path = _cfg(tmp_path, bad)
    with pytest.raises(ValueError):
        set_document_paths(tmp_path, ["a.md"])
    assert path.read_text() == bad
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]
    path.write_text('[documents]\npaths = "x"\n')
    with pytest.raises(ValueError):
        set_document_paths(tmp_path, ["a.md"])
    assert path.read_text() == '[documents]\npaths = "x"\n'
