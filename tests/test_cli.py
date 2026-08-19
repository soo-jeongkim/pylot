"""Tests for setup commands that do not require a running PyMOL instance."""

from __future__ import annotations

import subprocess

from co_pymol import cli


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


def test_install_codex_reports_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli.main(["install-codex"]) == 1


def test_install_codex_reports_codex_failure(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/codex")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "config is read-only\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["install-codex"]) == 1
