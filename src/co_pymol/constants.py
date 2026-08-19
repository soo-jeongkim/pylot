"""Shared constants for co-pymol."""

import os

DEFAULT_PORT = 8766
DEFAULT_HOST = "127.0.0.1"

# AlphaFold DB discrete pLDDT bin colors (not a continuous gradient). Bins are
# lower-inclusive — a residue exactly on a boundary takes the higher band — since
# the coloring uses strict `>` with epsilon cutoffs (PyMOL has no `>=`).
PLDDT_PALETTE = {
    "very_high": "0x0053D6",  # ≥90
    "confident": "0x65CBF3",  # 70 to <90
    "low": "0xFFDB13",  # 50 to <70
    "very_low": "0xFF7D45",  # <50
}
STRUCTURE_EXTENSIONS = {".cif", ".mmcif", ".pdb", ".ent"}

RENDER_POLL_ATTEMPTS = 50
RENDER_POLL_INTERVAL_S = 0.1

# --- MCP proxy (co_pymol.proxy) ---------------------------------------------
# Reconnect/backoff tuning, env-overridable so operators can adjust without code
# edits.
PROXY_BACKOFF_START = float(os.environ.get("PYMOL_PROXY_BACKOFF_START", "0.5"))
PROXY_BACKOFF_CAP = float(os.environ.get("PYMOL_PROXY_BACKOFF_CAP", "5.0"))
# How long an upstream initialize/tools/list waits for the *first* downstream
# connect before falling back to a synthesized (empty-tools) reply. Finite so a
# cold start with PyMOL not yet running doesn't hang the client's handshake.
PROXY_FIRST_CONNECT_WAIT = float(
    os.environ.get("PYMOL_PROXY_FIRST_CONNECT_WAIT", "5.0")
)
# JSON-RPC implementation-defined server error (valid range -32000..-32099).
JSONRPC_SERVER_ERROR_CODE = -32000
