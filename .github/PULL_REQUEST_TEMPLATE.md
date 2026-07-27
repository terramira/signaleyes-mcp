**What & why**

**Testing**
- [ ] `uv run pytest` passes (100% coverage required — check `--cov-report=term-missing`)
- [ ] `uv run ruff format --check . && uv run ruff check .` passes
- [ ] `uv run mypy src/` passes
- [ ] If this touches auth, pagination, or task polling: ran `uv run pytest -m network` against a real environment, or explain why not

**Version bump needed?** (new tool/behavior change → yes, per README's release section)
