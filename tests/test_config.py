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
