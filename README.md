# co-pymol

*Drive PyMOL in plain English — from Codex, Claude Code, Cursor, or your phone.*

**`co-pymol`** is a PyMOL plugin that turns PyMOL into an MCP server, so you can drive it in English from any MCP client (Codex, Claude Code, Cursor) instead of typing PyMOL commands by hand. On startup it spins up an MCP server — built on the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — inside PyMOL's own Python process, exposing the `pymol.cmd` API as tools, so you can:

- **Automate analysis and visualisation** with an agent instead of doing it by hand
- **Read confidence values** (pLDDT / ipTM / pTM / PAE) on an agent's window via a gemmi-backed metrics layer that parses mmCIF
- **Drop in your own helpers** — point the agent at a `.py` of custom PyMOL presets / analysis functions and ask it to use them
- **Work over SSHFS-mounted cluster paths** as usual
- **Remote-control from your phone**, since it all runs through Claude Code — plus any other Claude capabilities

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

**2. Hook the plugin into PyMOL startup**

```bash
/Applications/PyMOL.app/Contents/bin/python -m co_pymol.cli install-hook
```

Appends one line to `~/.pymolrc.py` so PyMOL loads the plugin on launch. Safe to re-run.

**3. Restart PyMOL**

The PyMOL console should print:

```
co-pymol: MCP server running on http://127.0.0.1:8766/sse
```

If you don't see that line, `~/.pymolrc.py` isn't being loaded. The file must be in your home directory (`echo $HOME` to check), and you need a full PyMOL quit + relaunch, not just a window close.

By default the server binds `127.0.0.1:8766` (loopback) — PyMOL and your MCP client must run on the same machine. To override, run `start_mcp` from the PyMOL command line: `start_mcp 9000` for a different port, or `start_mcp 8766, 0.0.0.0` to also accept connections from other machines. If you change either, point the client at the matching URL (e.g. `install-config --host <host> --port <port>`).

**4. Wire up your MCP client**

These setups are global — every Codex, Cursor, or Claude Code session sees the `pymol` server, no need to `cd` into this repo.

There are two ways to connect, and **the proxy is recommended**:

- **Proxy (recommended)** — your client launches a small bundled stdio server (`co-pymol proxy`) that forwards to PyMOL and *survives PyMOL quitting/restarting*, so your session never loses the connection. While PyMOL is down, tool calls return a clear "PyMOL is not connected" message instead of dropping the link; the next call after PyMOL is back just works.
- **Direct SSE (simpler, where supported)** — your client connects straight to PyMOL's SSE server. One less moving part, but the connection drops whenever PyMOL restarts and you have to reconnect by hand. Codex's URL transport is Streamable HTTP rather than SSE, so use the proxy with Codex.

*Codex — proxy*

```bash
codex mcp add pymol -- /Applications/PyMOL.app/Contents/bin/python -m co_pymol proxy
```

Works from any directory. `codex mcp list` should show `pymol`; inside Codex, `/mcp` shows the active tools. The ChatGPT desktop app, Codex CLI, and Codex IDE extension share this MCP configuration on the same host. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

*Cursor — proxy*

Edit `~/.cursor/mcp.json` so the `pymol` entry is:

```json
{
  "mcpServers": {
    "pymol": {
      "command": "/Applications/PyMOL.app/Contents/bin/python",
      "args": ["-m", "co_pymol", "proxy"]
    }
  }
}
```

Fully quit Cursor (`Cmd+Q`, not just close the window) and reopen; verify under Settings → Cursor Settings → MCP that `pymol` is listed. *(Prefer direct SSE? Run `/Applications/PyMOL.app/Contents/bin/python -m co_pymol.cli install-config`, which writes the `{"url": …}` form instead.)*

*Claude Code — proxy*

```bash
claude mcp add --scope user pymol -- /Applications/PyMOL.app/Contents/bin/python -m co_pymol proxy
```

Works from any directory. `claude mcp list` should show `pymol`. *(Prefer direct SSE? `claude mcp add --transport sse --scope user pymol http://127.0.0.1:8766/sse`.)*

Running PyMOL on a non-default host/port? Pass it through to the proxy: `… -m co_pymol proxy --host <host> --port <port>`.

Once the plumbing is verified, open PyMOL and start a new Codex / Cursor / Claude Code session. The proxy also supports the reverse order: it announces the PyMOL tools when PyMOL connects after the client. This reverse-order flow requires Cursor 3.12.17 or newer.

**5. Confirm the agent is talking to PyMOL**

Ask the agent something like *"are you connected to PyMOL? what version is loaded?"* — if it calls a `pymol` tool (e.g. `get_version`) and reports back a real answer, you're wired up. If it says it can't see PyMOL or doesn't have any `pymol` tools, the MCP client isn't actually connected — re-check step 4 and make sure you opened a *new* session after wiring it up.

## Upgrading

Already have an older version? How you update depends on how you installed it:

- **Editable install** (`pip install -e .`, the recipe above) — `git pull` in the repo, restart PyMOL, and re-point your MCP client at the proxy (below). No reinstall needed.
- **Non-editable install** — `git pull`, then re-run `<pymol-python> -m pip install --user -e .` to pick up the new code.
- **Installed back when it was `pylot`** — uninstall `pylot`, remove its line from `~/.pymolrc.py`, then do a fresh install.

The change you'll actually feel in 0.2.0 is the **proxy wiring**: re-run `install-config` (Cursor), re-register Claude Code with `… -m co_pymol proxy`, or re-run the `codex mcp add` command above. If your client still points at the older `-m co_pymol.proxy`, it won't start — update it.

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

Reverses the install steps. There's no `uninstall` subcommand, so the config edits are manual — they're one line each.

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

```python
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

- **`run()` security** — executes locally with restricted Python builtins (no imports / file I/O), but full PyMOL access via `cmd`. Only connect trusted MCP clients.
- **Dev setup (optional)** — `pip install -e ".[dev]" && pytest`. Pre-commit hooks are available but not required — see `.pre-commit-config.yaml`.
