"""Tests for setup commands that do not require a running PyMOL instance."""

from __future__ import annotations

import json
import subprocess

import pytest

from co_pymol import cli


def test_public_help_only_advertises_setup_and_proxy() -> None:
    help_text = cli.build_parser().format_help()

    assert "setup" in help_text
    assert "proxy" in help_text
    assert "install-hook" not in help_text
    assert "install-config" not in help_text
    assert "install-codex" not in help_text


def test_install_codex_command_was_removed() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["install-codex"])

    assert exc_info.value.code == 2


def test_hidden_install_hook_alias_still_works(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cli.main(["install-hook"]) == 0
    assert cli.PYMOLRC_LINE in (tmp_path / ".pymolrc.py").read_text()


def test_hidden_install_config_alias_still_works(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cli.main(["install-config", "--sse"]) == 0
    config = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["pymol"] == {"url": "http://127.0.0.1:8766/sse"}


def test_setup_cursor_installs_hook_and_only_configures_cursor(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "executable", "/Applications/PyMOL/python")

    def unexpected_lookup(name):
        raise AssertionError(f"setup cursor should not inspect {name}")

    monkeypatch.setattr(cli.shutil, "which", unexpected_lookup)

    assert cli.main(["setup", "cursor", "--host", "10.0.0.8", "--port", "9000"]) == 0

    assert cli.PYMOLRC_LINE in (tmp_path / ".pymolrc.py").read_text()
    config = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["pymol"] == {
        "command": "/Applications/PyMOL/python",
        "args": [
            "-m",
            "co_pymol",
            "proxy",
            "--host",
            "10.0.0.8",
            "--port",
            "9000",
        ],
    }


def test_setup_codex_installs_hook_and_only_configures_codex(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    looked_up = []

    def fake_which(name):
        looked_up.append(name)
        return "/opt/bin/codex"

    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    assert cli.main(["setup", "codex"]) == 0

    assert cli.PYMOLRC_LINE in (tmp_path / ".pymolrc.py").read_text()
    assert looked_up == ["codex", "codex"]


def test_setup_missing_selected_client_does_not_install_hook(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    looked_up = []

    def fake_which(name):
        looked_up.append(name)
        return None

    monkeypatch.setattr(cli.shutil, "which", fake_which)

    assert cli.main(["setup", "claude"]) == 1
    assert looked_up == ["claude"]
    assert not (tmp_path / ".pymolrc.py").exists()


def test_install_codex_registers_proxy_with_current_python(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/codex")
    monkeypatch.setattr(cli.sys, "executable", "/Applications/PyMOL/python")
    called = []

    def fake_run(command, **kwargs):
        called.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, "Added global MCP server 'pymol'.\n", ""
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    message = cli.install_codex_mcp("10.0.0.8", 9000)

    assert message == "Added global MCP server 'pymol'."
    assert called == [
        (
            [
                "/opt/bin/codex",
                "mcp",
                "add",
                "pymol",
                "--",
                "/Applications/PyMOL/python",
                "-m",
                "co_pymol",
                "proxy",
                "--host",
                "10.0.0.8",
                "--port",
                "9000",
            ],
            {"capture_output": True, "text": True},
        )
    ]


def test_setup_codex_reports_missing_cli(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli.main(["setup", "codex"]) == 1
    assert not (tmp_path / ".pymolrc.py").exists()


def test_setup_codex_reports_codex_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/codex")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "config is read-only\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["setup", "codex"]) == 1


def test_install_claude_registers_user_scoped_proxy(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/claude")
    monkeypatch.setattr(cli.sys, "executable", "/Applications/PyMOL/python")
    called = []

    def fake_run(command, **kwargs):
        called.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "Added pymol.\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    message = cli.install_claude_mcp("127.0.0.1", 8766)

    assert message == "Added pymol."
    assert called == [
        (
            [
                "/opt/bin/claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "pymol",
                "--",
                "/Applications/PyMOL/python",
                "-m",
                "co_pymol",
                "proxy",
            ],
            {"capture_output": True, "text": True},
        )
    ]


def test_install_claude_replaces_existing_user_entry(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/claude")
    results = iter(
        [
            subprocess.CompletedProcess([], 1, "", "pymol already exists"),
            subprocess.CompletedProcess([], 0, "Removed pymol.\n", ""),
            subprocess.CompletedProcess([], 0, "Added pymol.\n", ""),
        ]
    )
    called = []

    def fake_run(command, **kwargs):
        called.append(command)
        return next(results)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.install_claude_mcp("127.0.0.1", 8766) == "Added pymol."
    assert called[1] == [
        "/opt/bin/claude",
        "mcp",
        "remove",
        "pymol",
        "--scope",
        "user",
    ]
    assert called[2] == called[0]
