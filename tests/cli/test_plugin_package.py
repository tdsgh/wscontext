import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact", "Stop", "SessionEnd"]
SH_ONLY = pytest.mark.skipif(os.name == "nt", reason="the sh launcher")


def test_plugin_manifest_shape():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "wscontext" and re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "wscontext" and market["owner"]["name"]
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("wscontext", "./")]


def test_hooks_json_shape():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert list(hooks) == EVENTS
    for event, entries in hooks.items():
        (entry,) = entries
        (hook,) = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == (
            '"${CLAUDE_PLUGIN_ROOT}/bin/ctx" --plugin-root "${CLAUDE_PLUGIN_ROOT}" '
            f'--plugin-data "${{CLAUDE_PLUGIN_DATA}}" hook {event}')
        assert (event == "PostToolUse") == (hook.get("async") is True)
        assert entry.get("matcher") == ("*" if event == "PostToolUse" else None)


def test_launchers_are_the_only_readers_of_plugin_variables():
    for path in (ROOT / "src").rglob("*.py"):
        assert "CLAUDE_PLUGIN" not in path.read_text(), path


def test_skills_state_their_triggers():
    for name in ("ctx", "init", "stats"):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        front = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
        assert front and front[1] == name and front[2].startswith("Use ")


def test_git_hook_shims_are_in_the_package():
    shims = ROOT / "src" / "wscontext" / "githooks" / "shims"
    assert {p.name for p in shims.iterdir()} == {"hook.sh", "hook-stdin.sh"}
    assert 'packages = ["src/wscontext"]' in (ROOT / "pyproject.toml").read_text()


FAKE_UV = """#!/bin/sh
# a stand-in for `uv sync`: records its arguments and creates the environment's `ctx`
echo "$*" >> "$FAKE_UV_LOG"
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/sh\\necho "venv-ctx $*"\\n' > "$UV_PROJECT_ENVIRONMENT/bin/ctx"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/ctx"
"""


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch):
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "ctx", root / "bin" / "ctx")
    shutil.copy(ROOT / "uv.lock", root / "uv.lock")
    (root / ".claude-plugin").mkdir()
    shutil.copy(ROOT / ".claude-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text(FAKE_UV)
    (fake / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "uv.log"))

    def run(*args: str, path: str | None = None):
        env = {**os.environ, **({"PATH": path} if path else {})}
        return subprocess.run([str(root / "bin" / "ctx"), *args], capture_output=True, text=True,
                              env=env, cwd=tmp_path)

    return root, tmp_path / "data", run, tmp_path / "uv.log"


@SH_ONLY
def test_launcher_builds_once_and_passes_plugin_paths(launcher):
    root, data, run, log = launcher
    out = run("--plugin-root", "ignored", "--plugin-data", str(data), "why", "a.py")
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"venv-ctx --plugin-root {root} --plugin-data {data} why a.py\n"
    sync = log.read_text()
    assert "sync --frozen --no-dev --no-editable" in sync and "--reinstall-package wscontext" in sync and f"--project {root}" in sync
    assert (data / "plugin-root").read_text() == str(root)
    run("--plugin-data", str(data), "why", "a.py")
    assert len(log.read_text().splitlines()) == 1  # same lock file: no second sync
    (root / "uv.lock").write_text((root / "uv.lock").read_text() + "\n# changed\n")
    run("--plugin-data", str(data), "why", "a.py")
    assert len(log.read_text().splitlines()) == 2
    manifest = root / ".claude-plugin" / "plugin.json"
    spec = json.loads(manifest.read_text())
    spec["version"] += ".1"  # a version bump, same folder
    manifest.write_text(json.dumps(spec))
    run("--plugin-data", str(data), "why", "a.py")
    assert len(log.read_text().splitlines()) == 3


@SH_ONLY
def test_launcher_selects_python_313_explicitly(launcher):
    _, data, run, log = launcher
    run("--plugin-data", str(data), "why")
    match = re.search(r"--python (\S+)", log.read_text())
    assert match and (match[1] == "3.13" or match[1].endswith("python3.13"))


@SH_ONLY
def test_hook_call_without_an_environment_exits_0_without_waiting(launcher):
    _, data, run, _ = launcher
    out = run("--plugin-data", str(data), "hook", "Stop")
    assert out.returncode == 0 and out.stdout == ""
    deadline = time.time() + 10  # the build runs detached
    while time.time() < deadline and not (data / "venv" / ".lock-hash").exists():
        time.sleep(0.05)
    assert (data / "venv" / ".lock-hash").exists()
    assert run("--plugin-data", str(data), "hook", "Stop").stdout.startswith("venv-ctx")


@SH_ONLY
def test_missing_uv_is_a_message_for_a_command_and_silence_for_a_hook(launcher, tmp_path):
    _, data, run, _ = launcher
    bare = tmp_path / "bare"  # the system tools the launcher needs, but not uv
    bare.mkdir()
    for tool in ("cksum", "cat", "mkdir", "dirname", "find", "rmdir", "sleep", "printf"):
        if shutil.which(tool):
            (bare / tool).symlink_to(shutil.which(tool))
    hook = run("--plugin-data", str(data), "hook", "Stop", path=str(bare))
    assert hook.returncode == 0 and hook.stderr == ""
    cmd = run("--plugin-data", str(data), "why", "x", path=str(bare))
    assert cmd.returncode == 1 and "uv is not installed" in cmd.stderr
