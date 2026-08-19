# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor, etc.) working with **co-pymol**.

**What co-pymol is:** a PyMOL plugin that starts an MCP server inside PyMOL's own Python process, exposing the `pymol.cmd` API (plus a gemmi-backed metrics layer for pLDDT/ipTM/pTM/PAE) as tools. Once installed, an MCP client like Codex, Claude Code, or Cursor can drive PyMOL in natural language.

Two scenarios — jump to whichever fits:

1. **You're editing this repo** → see [§1 Working on this repo](#1-working-on-this-repo).
2. **You're helping a user install co-pymol on their machine** → see [§2 Installing co-pymol on a user's machine](#2-installing-co-pymol-on-a-users-machine).

---

## 1. Working on this repo

### Architecture

- **Plugin runs inside PyMOL's process.** On startup (`__init_plugin__`), an MCP server launches in a daemon background thread on port 8766.
- **MCP server** (`src/co_pymol/server.py`) exposes PyMOL's `cmd` module as MCP tools at `http://localhost:8766/sse`. MCP clients normally launch the bundled stdio proxy, which forwards to that endpoint and survives PyMOL restarts.
- **Metrics** (`src/co_pymol/core/metrics.py`) uses gemmi for structure metadata extraction — not PyMOL. This keeps metric parsing clean and avoids polluting PyMOL's object state. Reads PAE/ipTM/pTM from `_ma_qa_metric_*` categories in mmCIF first, falls back to sibling JSON.
- **Triage** (`src/co_pymol/core/triage.py`) manages navigation/flagging state for reviewing batches of structures (mobile eval workflow).

### Layers

The package uses a **src-layout**: it lives at `src/co_pymol/`. Inside it:

- **package root** (`__init__.py`, `cli.py`, `server.py`) — entry points; `constants.py` holds shared constants (port, palette, etc.); `instructions.py` loads `MCP_INSTRUCTIONS` from the sibling `instructions.md`. No domain logic.
- **`core/`** — domain logic + state, no MCP: `session.py` (per-session state), `metrics.py` and `triage.py` (pure, no PyMOL). `triage_view.py` is the one exception — it drives PyMOL to render a focused structure (`triage_render`); the pure triage state stays in `triage.py`. `session` depends on `metrics`/`triage`.
- **`utils/pymol/`** — cross-cutting PyMOL primitives: `helper.py` (`ensure_pymol`, `pymol_lock`) and `render.py` (`render_image`, `apply_plddt_palette`).
- **`tools/`** — thin MCP wrappers, one `register_*_tools(mcp)` per file; no logic beyond marshalling to `core/` and `utils/`.

### Thread safety

All `pymol.cmd` calls are serialized with `pymol_lock` (a `threading.Lock`). The MCP server runs in a daemon thread; PyMOL's GUI runs on the main thread. Rendering (`cmd.ray`, `cmd.png`) definitely needs the lock. Most read operations work from threads in modern PyMOL, but we lock everything for safety.

### Agent-facing behavior

The MCP server pushes its own instructions (`src/co_pymol/instructions.md`) to every connected client. That file is the right place to change cross-client agent behavior (e.g. "don't auto-render after operations") — not this AGENTS.md, and not per-client config.

### Dev setup

Install into PyMOL's bundled Python (the same rule as the user-facing install — see §2 for the full playbook with troubleshooting):

```bash
/Applications/PyMOL.app/Contents/bin/python -m pip install --user -e ".[dev]"
```

Then:

- **Tests:** `/Applications/PyMOL.app/Contents/bin/python -m pytest`
- **Pre-commit hooks (optional):** see the caveat in `CONTRIBUTING.md`; the local
  Ruff hooks currently require `python3 -m ruff` to work in the shell environment.
- **Commit style:** `type: subject` (see `git log` for examples — `refactor:`, `docs:`, `chore:`, `fix:`, etc.)

### How to add new tools

1. Add a `register_*_tools(mcp)` function in the relevant `src/co_pymol/tools/` file (or a new one), then call it from `create_server()` in `src/co_pymol/server.py`
2. Inside the register function, add a new function decorated with `@mcp.tool()`
3. Use `pymol_lock` for any `pymol.cmd` calls
4. Return a string (status message) or `Image` (for rendered output)

```python
@mcp.tool()
def my_new_tool(arg: str) -> str:
    """Description shown to the MCP client."""
    cmd = ensure_pymol()
    with pymol_lock:
        cmd.some_operation(arg)
        return f"Done: {arg}"
```

### Dependencies

- `mcp~=1.27.1` — official MCP Python SDK; we use its bundled `mcp.server.fastmcp.FastMCP` (no standalone `fastmcp` package). Pinned tight on purpose — MCP is co-pymol's network-facing trust boundary and `FastMCP` has had API churn between minors; bump the pin deliberately, not opportunistically.
- `anyio>=4.0` — async task and stream primitives used by the stdio proxy.
- `gemmi>=0.6` — mmCIF/PDB parsing for metrics (atom data + AF3 `_ma_qa_metric_*`)
- `numpy` — array ops for pLDDT/PAE in metrics
- `cryptography<46` on Intel macOS only — keeps installation on a release line
  with a compatible prebuilt wheel because the Intel PyMOL bundle lacks the
  Python headers needed for a source build.
- PyMOL — **not a pip dependency**, install the app from pymol.org. Install this plugin into PyMOL's Python: `/Applications/PyMOL.app/Contents/bin/python -m pip install --user -e .`

---

## 2. Installing co-pymol on a user's machine

Because co-pymol lives inside PyMOL, it installs into **PyMOL's bundled Python**, not the system Python or any venv. On macOS that interpreter lives at `/Applications/PyMOL.app/Contents/bin/python`. On Linux/conda installs the path will differ — ask the user for it before running anything. If they're not sure, `which pymol` followed by checking for a sibling `python` in the same `bin/` directory is usually the right interpreter.

### Prerequisites to check

1. PyMOL is installed. On macOS, confirm `/Applications/PyMOL.app/Contents/bin/python` exists.
2. The repo is cloned and you are running commands from its root (the directory containing `pyproject.toml`).
3. The user is on macOS, or has told you where their PyMOL Python lives. If neither, ask — don't guess.

### Install steps

Run these in order. Each is idempotent; safe to re-run.

**0. Resolve PyMOL's Python**

Every command below assumes `$PYMOL_PYTHON` points at PyMOL's bundled interpreter. On macOS the default is `/Applications/PyMOL.app/Contents/bin/python`. On Linux/conda the path will differ — ask the user. Export it once before continuing:

```bash
export PYMOL_PYTHON=/Applications/PyMOL.app/Contents/bin/python
```

Sanity check it: `$PYMOL_PYTHON -c 'import pymol; print(pymol.__file__)'` should print a path inside the PyMOL install.

**1. Install the package into PyMOL's Python**

```bash
$PYMOL_PYTHON -m pip install --user -e .
```

**2. Set up exactly one MCP client**

Ask which client the user actually uses, then run only its setup command:

```bash
$PYMOL_PYTHON -m co_pymol setup codex
$PYMOL_PYTHON -m co_pymol setup cursor
$PYMOL_PYTHON -m co_pymol setup claude
```

Do not run all three. Each command installs the shared startup hook in
`~/.pymolrc.py` and configures only the selected client. It does not inspect or
require either of the other clients. The Codex and Claude choices require the
corresponding CLI on `PATH`; Cursor is configured through
`~/.cursor/mcp.json`. These setup commands configure an existing client — they
do not install Codex, Cursor, or Claude Code.

All three default to the bundled stdio proxy, which forwards to PyMOL's SSE
server and survives PyMOL restarts. Use the user's `$PYMOL_PYTHON` path as the
proxy command so it has the package's dependencies. Codex supports local stdio
and Streamable HTTP servers, not co-pymol's SSE endpoint, so Codex must use the
proxy.

For direct SSE with Cursor or Claude Code, run the normal setup first so the
PyMOL hook is present, then replace only the selected client's proxy entry:

```bash
# Cursor
$PYMOL_PYTHON -m co_pymol install-config --sse

# Claude Code
claude mcp remove pymol --scope user
claude mcp add --transport sse --scope user pymol http://127.0.0.1:8766/sse
```

The `--host` and `--port` setup options configure the client-side proxy target
only. They do not change the automatic PyMOL hook, which always starts the
server at `127.0.0.1:8766`. Do not pass them during a normal installation. The
legacy `install-hook`, `install-config`, and `install-codex` commands remain
available as single-operation helpers, but use `setup <client>` normally.

The proxy can start before PyMOL. It returns an empty tool list after a short bounded wait, then sends `notifications/tools/list_changed` so the client discovers the real tools when PyMOL appears. Require Cursor 3.12.17 or newer for this reverse-order flow; older Cursor versions may not refresh the tool list notification.

**3. Tell the user to restart PyMOL and the selected client**

You can't do this for them. They need a full quit + relaunch (not just closing the window). On success the PyMOL console prints:

```
co-pymol: MCP server running on http://127.0.0.1:8766/sse
```

Verify the selected client entry with `codex mcp list`, `claude mcp list`, or
Cursor Settings → MCP. In Codex's TUI, `/mcp` shows active servers and tools.
The ChatGPT desktop app, Codex CLI, and Codex IDE extension share MCP
configuration on the same Codex host.

### Upgrading an existing install (e.g. to 0.2.0)

The steps depend on *how* it was installed, so check first — don't assume.

**1. How is the package installed?**

```bash
$PYMOL_PYTHON -c "import co_pymol; print(co_pymol.__file__)"
```

- Path inside the cloned repo (`.../co-pymol/src/co_pymol/__init__.py`) →
  **editable** (`-e`). A pull updates source code immediately, but dependency
  constraints and package metadata still require reinstalling when they change.
- Path inside `site-packages` → **copied**. You must reinstall after pulling.
- `ModuleNotFoundError` but an old `pylot` package imports → **pre-rename install**; see the note at the end.

(Don't rely on the reported version to decide — for editable installs `pip show co-pymol` / the package metadata version lags behind the code on disk until a reinstall, so the repo's git state is the source of truth.)

**2. Update the code**

```bash
git -C <repo> pull
```

**3. Reinstall for the 0.2.0 upgrade**

```bash
$PYMOL_PYTHON -m pip install --user -e .
```

This installs the explicit proxy dependencies, applies the Intel macOS
`cryptography<46` compatibility constraint, refreshes package metadata, and
switches a copied install to editable.

**4. Re-point the MCP client at the proxy.** This is the main user-visible 0.2.0 change: the recommended wiring moved from direct SSE to the proxy, launched as `-m co_pymol proxy` (package + subcommand). If the client still points at a direct SSE url, or the interim `-m co_pymol.proxy` form (which no longer starts a server), update it:

- Cursor: re-run `$PYMOL_PYTHON -m co_pymol setup cursor`, then fully quit and reopen Cursor.
- Claude Code: re-run `$PYMOL_PYTHON -m co_pymol setup claude`. (`claude mcp get pymol` shows the current command — if its Args read `-m co_pymol.proxy`, it's stale.)
- Codex: re-run `$PYMOL_PYTHON -m co_pymol setup codex`. (`codex mcp get pymol --json` shows the current command and arguments.)

**5. Tell the user to restart PyMOL and the selected client** so they load the new code and MCP configuration.

**Pre-rename (`pylot`) installs.** Older installs used the `pylot` package name. Migrate to a clean co-pymol install: `$PYMOL_PYTHON -m pip uninstall pylot`, delete the old `pylot` startup line from `~/.pymolrc.py`, then follow the install steps above from step 1.

### Verifying the install

After the user restarts PyMOL, confirm the server is reachable:

```bash
curl -sN --max-time 2 http://127.0.0.1:8766/sse 2>/dev/null \
  | grep -q '^event: endpoint' && echo OK
```

(SSE stays open, so the timeout is intentional. Seeing its initial `endpoint`
event is the success signal.)

That proves the PyMOL SSE server is responding, not that the selected client is
wired correctly. For a real end-to-end check, ask the client "in pymol, what
version is loaded?" and confirm it calls `get_version`. If curl works but the
tool is missing, re-run the appropriate setup command from step 2 and restart
that client.

### If something goes wrong

- **No `MCP server running on...` line in PyMOL console** — `~/.pymolrc.py` isn't being loaded. Check `echo $HOME` matches where the file lives, and confirm the user did a full quit + relaunch.
- **`pip install` fails with "externally-managed-environment"** — you used the system Python, not PyMOL's. Re-check the interpreter path.
- **Port 8766 already in use** — the automatic bind failed. Run
  `start_mcp <free-port>` in PyMOL, then re-run the selected setup command with
  `--port <free-port>`. `start_mcp` cannot move a server that is already
  running; this fallback works because the original bind failed. It is a
  per-session workaround, not a persistent custom-port setting. Repeat it after
  a restart while the collision remains; if 8766 becomes free, re-run setup
  without `--port` so the client returns to the hook's default.
- **Client running on a different machine than PyMOL** — the automatic hook
  binds loopback and does not currently support a custom bind address. The
  setup command's `--host` option changes only the proxy destination. A remote
  arrangement therefore needs custom server startup outside the normal setup
  flow. Binding to a non-loopback interface exposes an unauthenticated tool
  server, including arbitrary-Python `run()`, so do not recommend it without a
  separately secured network path.

### What NOT to do

- Don't `pip install co-pymol` into the system Python or a venv — the plugin will load but PyMOL won't see it.
- Don't edit `~/.pymolrc.py` by hand; normal `setup <client>` installs the hook.
  Use `install-hook` only when repairing or configuring the hook separately.
- Don't restart PyMOL yourself — the user has unsaved session state. Ask them to do it.
- Don't add `pymol` as a pip dependency. It's not on PyPI in the form this plugin needs; the user installs PyMOL.app separately.

### Uninstall

If the user asks to uninstall:

1. Remove the MCP client entry: `codex mcp remove pymol`, `claude mcp remove pymol --scope user`, or for Cursor delete the `"pymol"` entry in `~/.cursor/mcp.json`.
2. Delete the two `co-pymol:` lines from `~/.pymolrc.py` (or the whole file if those are the only lines).
3. `$PYMOL_PYTHON -m pip uninstall co-pymol`
4. Ask the user to restart PyMOL.
