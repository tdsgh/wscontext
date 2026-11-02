<!-- kit:python/rules/venv v1 -->
# Python environment

- Exactly one virtualenv, at `.venv` (Python 3.13), for every
  Python project in the repo. Never create a per-project venv.
  *(non-negotiable)*
- Call the venv's interpreter explicitly — `.venv/bin/python`,
  `.venv/bin/pytest`, `.venv/bin/pip` — never a bare `python`, `pytest`
  or `pip`.
- Install a local shared library editable. After `pip install -e .` in a project
  that depends on it, run `pip install -e <library>` again — the project's
  install replaces the editable library with a plain copy.
- Check an editable install with
  `.venv/bin/python -c "import <pkg>; print(<pkg>.__file__)"`: the path is
  inside the repo, not under `site-packages`.
- A new dependency goes into the owning project's `pyproject.toml`. Never
  install one ad hoc.
