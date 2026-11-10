# Language parts

Each language the graph reads is one part behind the contract in
[base.py](base.py): `LanguagePart` with `name`, `version`, `extensions`,
`load()`, `grammar(path)`, `extract(tree, source)`,
`resolve_import(imp, from_path, files)` and `is_test(path)`. The core
(`graph/*.py`) knows only that contract. The registry in
[\_\_init\_\_.py](__init__.py) is the only place that names a language module.

## Adding a language

- [ ] Add the grammar package to `[project] dependencies` in `pyproject.toml`.
      Check that `uv.lock` lists a wheel (cp313 or abi3) for macOS arm64 and
      x86_64, Linux x86_64 and aarch64, and Windows amd64. Run `uv lock` and
      `uv sync`.
- [ ] Write `src/wscontext/graph/languages/<lang>.py`:
  - [ ] A `LanguagePart` subclass and a module-level `PART = <Class>()`.
  - [ ] `name` matches the registry key and the `[graph] languages` entry.
  - [ ] `version = 1`. Raise it whenever extraction or resolution changes, so
        that language's files reparse on the next refresh.
  - [ ] `extensions`: lower-case, with the dot, claimed by no other part.
  - [ ] `load()` imports `tree_sitter` and the grammar inside the function, never
        at module import, and raises `LanguageUnavailable` on any failure.
  - [ ] Override `grammar(path)` only when one language has several grammars
        (TSX next to TypeScript).
  - [ ] `extract` returns `FileFacts`: definitions with kind `function`,
        `class`, `method` or `type`, qualnames `Class.method` and
        `outer.inner`, 1-based lines, the start including decorators; imports
        as written; calls by short name with the enclosing definition's
        qualname, or None at file level.
  - [ ] `resolve_import` returns repository paths only, through `files`
        (`path in files`, `files.ending_with(suffix)`); an unresolved import
        returns `[]`.
  - [ ] `is_test` follows the language's own test-file conventions.
- [ ] Add one line to `REGISTRY` in `__init__.py`.
- [ ] Add sample files under `tests/fixtures/graph/<lang>/` that cover every
      construct the part extracts, including one file with a syntax error.
- [ ] Generate `tests/fixtures/graph/<lang>/expected.json` (sample path ->
      `asdict(parse.facts(part, source, path))`), then read every entry against
      the sample before keeping it.
- [ ] Write `tests/graph/languages/test_<lang>.py`: definitions, imports,
      resolution, calls, `is_test`, and a syntax error recorded as `error`.
- [ ] Run `.venv/bin/pytest tests/graph`. `test_language_contract.py` picks the
      new part up with no edit.
- [ ] Check the diff touches only the paths above. Another language's
      `expected.json` never changes.

## Changing or removing a language

- Changing extraction: update the part, raise its `version`, regenerate and
  re-read its `expected.json`, and name the change in the report.
- Removing a language: delete its module, registry line, fixtures, test file and
  grammar dependency. A `[graph] languages` entry that names it then shows as
  unavailable.
