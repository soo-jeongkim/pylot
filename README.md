# co-pymol

*Drive PyMOL in plain English — from Codex, Claude Code, Cursor, or your phone.*

**`co-pymol`** is a PyMOL plugin that turns PyMOL into an MCP server, so you can drive it in English from any MCP client (Codex, Claude Code, Cursor) instead of typing PyMOL commands by hand. On startup it spins up an MCP server — built on the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — inside PyMOL's own Python process, exposing the `pymol.cmd` API as tools, so you can:

- **Automate analysis and visualisation** with an agent instead of doing it by hand
- **Read confidence values** (pLDDT / ipTM / pTM / PAE) on an agent's window via a gemmi-backed metrics layer that parses mmCIF
- **Drop in your own helpers** — point the agent at a `.py` of custom PyMOL presets / analysis functions and ask it to use them
- **Work over SSHFS-mounted cluster paths** as usual
- **Remote-control from your phone** through an MCP client that supports remote sessions

An example session in Codex / Claude Code / Cursor:

```
> Load all the CIF files in /path/to/dir/w/predicted/structures/
[all the structures visible on PyMOL window]
Loaded all structures, sorted by mean pLDDT.

> Which one has the worst ipTM?
model_3 — ipTM 0.41 (others are 0.7+).

> Show me the low-confidence loops on structure_500.
[renders cartoon on PyMOL window, residues 142–168 highlighted, mean pLDDT 38]
```

## Requirements

- **PyMOL** (a normal desktop install — the plugin installs into PyMOL's bundled Python, not your system Python)
- **An MCP client** — Codex, Claude Code, or Cursor 3.12.17 or newer. Older
  Cursor versions may not refresh the tool list when PyMOL starts after Cursor.
- **macOS** — that's all I've tested on :/ Linux / conda / non-standard installs should work in principle (the recipe is just "install into PyMOL's bundled Python") but these haven't been tested.

## Installing

If you prefer to have a coding agent (Claude Code, Cursor, Codex, etc.) do the install for you, point it at [`AGENTS.md`](./AGENTS.md) — it's the same recipe written for an agent to execute.

**1. Clone and install**

```bash
git clone https://github.com/soo-jeongkim/co-pymol.git
cd co-pymol
/Applications/PyMOL.app/Contents/bin/python -m pip install --user -e .
```

**2. Set up the MCP client you use**

Choose exactly one:

```bash
# Codex
/Applications/PyMOL.app/Contents/bin/python -m co_pymol setup codex

# Cursor
/Applications/PyMOL.app/Contents/bin/python -m co_pymol setup cursor

# Claude Code
/Applications/PyMOL.app/Contents/bin/python -m co_pymol setup claude
```

Each command installs the shared PyMOL startup hook and configures only the
selected client. The other clients do not need to be installed. The Codex and
Claude choices require their existing CLI on `PATH`; the Cursor choice writes
`~/.cursor/mcp.json` directly. These commands configure co-pymol for a client —
they do not install Codex, Cursor, or Claude Code themselves. All are safe to
re-run.

The client setup is global, so `pymol` is available from every session and
there is no need to `cd` into this repo. It uses the recommended bundled stdio
proxy (`co-pymol proxy`), which forwards to PyMOL and survives PyMOL
quitting/restarting. After one successful connection, tool calls made while
PyMOL is down return a clear message. On a cold start, the tools appear when
PyMOL first connects.

Codex supports local stdio and Streamable HTTP servers, not co-pymol's SSE
endpoint, so Codex must use the proxy. Cursor and Claude Code can use direct SSE
instead, but their connections drop whenever PyMOL restarts. To switch after
normal setup, use:

```bash
# Cursor: replace its proxy entry with the direct SSE URL
/Applications/PyMOL.app/Contents/bin/python -m co_pymol install-config --sse

# Claude Code: replace its user-scoped proxy entry with the direct SSE URL
claude mcp remove pymol --scope user
claude mcp add --transport sse --scope user pymol http://127.0.0.1:8766/sse
```

The `--host` and `--port` options on setup commands change where the client-side
proxy connects; they do not change the address started by the automatic PyMOL
hook, which is currently fixed at `127.0.0.1:8766`. Leave those options off for
the normal installation.

**3. Restart PyMOL and your selected client**

The PyMOL console should print:

```
co-pymol: MCP server running on http://127.0.0.1:8766/sse
```

If you don't see that line, `~/.pymolrc.py` isn't being loaded. The file must be in your home directory (`echo $HOME` to check), and you need a full PyMOL quit + relaunch, not just a window close.

By default the server binds `127.0.0.1:8766` (loopback), so PyMOL and the MCP
client must run on the same machine. The proxy supports starting the client
before PyMOL; that reverse-order flow requires Cursor 3.12.17 or newer.

Confirm the selected client has the entry: run `codex mcp list` or
`claude mcp list`, or open Cursor Settings → MCP. Codex's MCP configuration is
shared by the ChatGPT desktop app, Codex CLI, and Codex IDE extension on the
same host. See the
[official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

**4. Confirm the agent is talking to PyMOL**

Ask the agent something like *"are you connected to PyMOL? what version is
loaded?"* — if it calls a `pymol` tool (e.g. `get_version`) and reports a real
answer, you're wired up. If it cannot see any `pymol` tools, re-check step 2 and
make sure you opened a new client session after setup.

## Upgrading

Already have an older version? How you update depends on how you installed it:

- **Editable install** (`pip install -e .`, the recipe above) — `git pull` updates
  the source immediately, but re-run the install when dependencies or package
  metadata change. Do that once for the 0.2.0 upgrade.
- **Non-editable install** — `git pull`, then re-run
  `<pymol-python> -m pip install --user -e .` to pick up the new code and switch
  to an editable install.
- **Installed back when it was `pylot`** — uninstall `pylot`, remove its line from `~/.pymolrc.py`, then do a fresh install.

For 0.2.0, pull the code and re-run:

```bash
/Applications/PyMOL.app/Contents/bin/python -m pip install --user -e .
```

Then re-run the setup command from step 2 for the client you use. If its entry
still points at the older `-m co_pymol.proxy`, it won't start.

The full step-by-step, including how to tell which kind of install you have, is in the **"Upgrading an existing install"** section of [`AGENTS.md`](./AGENTS.md) — or just point your coding agent at that file.

## Experimenting!

1. Open PyMOL (the MCP server auto-starts).
2. Open Codex, Claude Code (`claude` in a terminal), or Cursor with MCP enabled.
3. Talk to it:
   - "Load all CIF files in `<dir>`, sorted by ipTM"
   - "Color by pLDDT, then render a ray-traced PNG"
   - "Align model_0 onto model_1; what's the RMSD?"
   - "Look at `~/scripts/my_pymol_helpers.py` — apply the publication-style view to all objects"

Want sample data? **[Click here](https://500.kim/resources/pizza-and-pymol.zip)** to download a few sample CIF files (AF3 predictions, antibodies, multi-domain proteins) to play with.

## Uninstalling

Reverses the install steps. There's no `uninstall` subcommand, so remove the
client entry and the two startup-hook lines as described below.

**1. Unwire your MCP client**

Codex:

```bash
codex mcp remove pymol
```

Cursor: edit `~/.cursor/mcp.json` and delete the `"pymol"` entry under `mcpServers` (leave any other servers intact). Quit Cursor (`Cmd+Q`) and reopen.

Claude Code:

```bash
claude mcp remove pymol --scope user
```

**2. Remove the PyMOL startup hook**

Delete these two lines from `~/.pymolrc.py`:

```text
# co-pymol: auto-start MCP server on PyMOL launch
from co_pymol import __init_plugin__; __init_plugin__()
```

If that was the only thing in the file, you can delete `~/.pymolrc.py` entirely.

**3. Uninstall the package**

```bash
/Applications/PyMOL.app/Contents/bin/python -m pip uninstall co-pymol
```

**4. Restart PyMOL**

A full quit + relaunch. The `MCP server running on...` line should be gone. The plugin keeps no caches or logs of its own, so nothing else is left behind. (The cloned repo is yours to `rm -rf` whenever.)

## Notes

- **`run()` security** — executes arbitrary Python locally with full imports,
  file I/O, and PyMOL access. Only connect trusted MCP clients, and keep the
  server on loopback unless you have separately secured the network path.
- **Dev setup (optional)** — install `.[dev]` into PyMOL's Python, then run tests
  with that same interpreter. See [`AGENTS.md`](./AGENTS.md) and
  `.pre-commit-config.yaml`.
