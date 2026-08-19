# Contributing to co-pymol

Thanks for the interest! co-pymol is an active WIP maintained by one person, so a quick issue before a big PR genuinely saves us both time.

## Scope

**In scope:**

- New MCP tools that expose useful PyMOL workflows
- Better metrics parsing
- Linux / non-macOS install paths — currently untested, so this is welcome
- Docs, examples, sample sessions

**Probably out of scope — open an issue first:**

- New transports beyond SSE
- Anything that pulls PyMOL into a pip dep
- UI / Electron wrappers

## Before you start

- For anything non-trivial, open an issue first so we can agree on the shape before you write code.
- For small fixes (typos, bugs, doc tweaks) — just send the PR, no issue needed.

## Dev setup, architecture, adding tools

See [`AGENTS.md`](./AGENTS.md) §1. It's written for coding agents, but the recipe is the same for humans: install into PyMOL's bundled Python with the `[dev]` extras, run `pytest`, and follow the `type: subject` commit style (see `git log` for examples).

Quick reference:

```bash
/Applications/PyMOL.app/Contents/bin/python -m pip install --user -e ".[dev]"
pytest
pre-commit install   # optional
```

Lint/format is `ruff` (config in `pyproject.toml`).

## PRs

- One logical change per PR.
- Run `pytest` and `pre-commit run --all-files` locally before pushing.
- If you're adding or renaming a tool, update `src/co_pymol/instructions.md` when its existence changes how agents should behave — that file is what the server pushes to every connected client.

## Reporting bugs

Open a GitHub issue with:

- PyMOL version (`pymol -c -q -k -e 'print(cmd.get_version())'`, or check the splash screen)
- MCP client (Codex / Claude Code / Cursor / other) and its version
- OS and PyMOL install path
- The `co-pymol: MCP server running on...` line from the PyMOL console — or a note that it's missing
