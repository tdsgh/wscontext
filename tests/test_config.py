import logging

from wscontext.config import Config, load_config


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
