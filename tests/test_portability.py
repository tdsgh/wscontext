import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "wscontext"
BAD_MODULES = {"fcntl", "pwd", "grp", "termios", "resource"}
BAD_CALLS = {("os", "fork"), ("os", "setsid"), ("os", "killpg"), ("os", "getuid")}


def _problems(tree: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [f"import {a.name}" for a in n.names if a.name.split(".")[0] in BAD_MODULES]
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in BAD_MODULES:
            out.append(f"from {n.module} import")
        elif isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and (f.value.id, f.attr) in BAD_CALLS):
                out.append(f"{f.value.id}.{f.attr}()")
            for kw in n.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    out.append("shell=True")
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            for side in (n.left, n.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) \
                        and "/" in side.value:
                    out.append(f"string path join with '/': {side.value!r}")
        elif isinstance(n, ast.JoinedStr):
            pass
    return out


def test_no_posix_only_code():
    found = {}
    for py in SRC.rglob("*.py"):
        p = _problems(ast.parse(py.read_text(encoding="utf-8")))
        if p:
            found[str(py.relative_to(SRC))] = p
    assert not found, found


def test_guard_detects_violations():
    bad = ast.parse("import fcntl\nimport os\nos.fork()\nsubprocess.run(x, shell=True)\n"
                    "p = base / 'a/b'\n")
    assert len(_problems(bad)) == 4
