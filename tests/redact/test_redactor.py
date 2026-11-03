import json

import pytest

from wscontext.config import Config, RedactionConfig
from wscontext.redact.redactor import redact, redact_bytes, redact_text
from wscontext.redact.rules import BUILTIN_RULES

# Every secret below is made up. Positives must be redacted; negatives must survive untouched.
POSITIVE = {
    "private-key": [
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfake\nabcDEF123+/=\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----\\nb3BlbnNzaGZha2U=\\n-----END OPENSSH PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----\nMIIBVgIBADANfake",
    ],
    "aws-key": ["key AKIAIOSFODNN7EXAMPLE here", "ASIAY34FZKBOKMUTVV7A"],
    "gcp-api-key": ["AIzaSyA1234567890abcdefghijklmnopqrstuv"],
    "github-token": ["ghp_" + "a1B2c3D4" * 5, "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz0123"],
    "gitlab-token": ["glpat-abcdefghij0123456789"],
    "slack-token": ["xoxb-1234567890-abcdefghij"],
    "jwt": ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk123"],
    "bearer-token": ["Authorization: Bearer abcdef1234567890xyz", "bearer Zm9vYmFyYmF6cXV4MTIz"],
    "env-secret": ["DATABASE_PASSWORD=hunter2", "export API_KEY=abc123", 'AUTH_TOKEN="my secret"',
                   "STRIPE_SECRET_KEY='sk_live_x'"],
    "high-entropy-assignment": ['"token": "k9X2mQ7vL4pR8sT1wZ5yB3nC6"', "secret = Qw3rTy7UiOp9AsDf1GhJk"],
}
NEGATIVE = [
    "plain prose about tokens and passwords",
    "AKIA is a prefix but too short",
    "ghp_short",
    "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    "Bearer of bad news",
    "bearer token",
    "PATH=/usr/bin",
    "DEBUG=1",
    "token = some_function_name_here_long",
    "password: ",
    "max_tokens=4096",
    "git commit abcdef0123456789abcdef0123456789abcdef01",
]
CASES = [(rule, s) for rule, samples in POSITIVE.items() for s in samples]


def test_every_builtin_rule_has_positive_samples():
    assert {r.name for r in BUILTIN_RULES} == set(POSITIVE)


@pytest.mark.parametrize("rule,sample", CASES)
def test_no_positive_sample_survives(rule, sample):
    out, hits = redact(sample)
    assert rule in hits
    assert f"[REDACTED:{rule}]" in out
    for secret in ("EXAMPLE", "hunter2", "abc123", "my secret", "sk_live_x", "k9X2mQ7v",
                   "Qw3rTy7U", "MIIEow", "b3BlbnNz", "MIIBVg", "abcdef1234567890xyz",
                   "Zm9vYmFy", "eyJzdWIi", "abcdefghij0123456789", "1234567890-abc"):
        assert secret not in out


@pytest.mark.parametrize("sample", NEGATIVE)
def test_negative_sample_untouched(sample):
    assert redact(sample) == (sample, [])


def test_env_secret_keeps_the_name():
    assert redact("DB_PASSWORD=hunter2")[0] == "DB_PASSWORD=[REDACTED:env-secret]"


def test_redacted_json_line_stays_valid_json():
    line = json.dumps({"message": {"content": "run with API_KEY=abc123\nand AKIAIOSFODNN7EXAMPLE"}})
    out, hits = redact(line)
    assert json.loads(out)["message"]["content"] == \
        "run with API_KEY=[REDACTED:env-secret]\nand [REDACTED:aws-key]"
    assert sorted(hits) == ["aws-key", "env-secret"]


def test_idempotent():
    once, _ = redact("API_KEY=abc123 and AKIAIOSFODNN7EXAMPLE and Bearer abcdef1234567890xyz")
    assert redact(once) == (once, [])


def test_extra_rules_add_to_builtins():
    out, hits = redact("proj-ID-777 AKIAIOSFODNN7EXAMPLE", [r"proj-ID-\d+"])
    assert out == "[REDACTED:custom-1] [REDACTED:aws-key]"
    assert sorted(hits) == ["aws-key", "custom-1"]


def test_invalid_extra_pattern_is_skipped(caplog):
    out, _ = redact("AKIAIOSFODNN7EXAMPLE", ["(unclosed"])
    assert out == "[REDACTED:aws-key]"
    assert "invalid redaction pattern" in caplog.text


def test_config_entry_points():
    config = Config(redaction=RedactionConfig(extra_patterns=(r"acme-\w+",)))
    text = "acme-internal AKIAIOSFODNN7EXAMPLE"
    assert redact_text(text, config)[0] == "[REDACTED:custom-1] [REDACTED:aws-key]"
    out, hits = redact_bytes(text.encode(), config)
    assert out == b"[REDACTED:custom-1] [REDACTED:aws-key]" and len(hits) == 2


def test_redact_bytes_preserves_undecodable_bytes():
    out, hits = redact_bytes(b"ok \xff\xfe AKIAIOSFODNN7EXAMPLE", Config())
    assert out == b"ok \xff\xfe [REDACTED:aws-key]" and hits == ["aws-key"]
