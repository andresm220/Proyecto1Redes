"""F5 tests: turning a server declaration into an argv that actually launches.

Both rewrites here are platform detail that config/servers.json deliberately
does not carry, so the same declaration file works on Windows, macOS and Linux.
They are tested through a pure function rather than by patching os.name, so the
Windows behaviour is verified when the suite runs on Linux too.
"""

from __future__ import annotations

import sys

import pytest

from host.mcp.stdio_transport import WINDOWS_SHELL_COMMANDS, build_argv


# --------------------------------------------------------------------------
# python resolves to the interpreter running the host
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["python", "python3"])
def test_python_becomes_the_running_interpreter(command):
    """A bare "python" would resolve against PATH and miss the virtual
    environment, starting the server without its dependencies."""
    assert build_argv(command, ["-m", "servers.netops.stdio_server"]) == [
        sys.executable,
        "-m",
        "servers.netops.stdio_server",
    ]


def test_the_interpreter_rewrite_is_not_platform_specific():
    assert build_argv("python", [], is_windows=True)[0] == sys.executable
    assert build_argv("python", [], is_windows=False)[0] == sys.executable


# --------------------------------------------------------------------------
# Node launchers on Windows go through the command interpreter
# --------------------------------------------------------------------------


def test_npx_is_wrapped_in_cmd_on_windows():
    """npx ships as npx.cmd and CreateProcess does not consult PATHEXT, so
    launching it directly raises FileNotFoundError."""
    assert build_argv(
        "npx", ["-y", "@modelcontextprotocol/server-filesystem", "./workspace"],
        is_windows=True,
    ) == [
        "cmd",
        "/c",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "./workspace",
    ]


def test_npx_is_left_alone_off_windows():
    assert build_argv("npx", ["-y", "pkg"], is_windows=False) == ["npx", "-y", "pkg"]


@pytest.mark.parametrize("command", sorted(WINDOWS_SHELL_COMMANDS))
def test_every_node_launcher_is_wrapped(command):
    assert build_argv(command, ["x"], is_windows=True)[:2] == ["cmd", "/c"]


def test_the_wrapper_is_case_insensitive():
    """PATH lookups on Windows are, so a declaration saying NPX still works."""
    assert build_argv("NPX", ["-y"], is_windows=True)[:3] == ["cmd", "/c", "NPX"]


def test_uvx_is_never_wrapped():
    """uvx is a real executable. Wrapping it only adds a layer that can mangle
    argument quoting - the assignment calls this out explicitly."""
    argv = build_argv("uvx", ["mcp-server-git", "--repository", "./workspace"], is_windows=True)
    assert argv == ["uvx", "mcp-server-git", "--repository", "./workspace"]
    assert "cmd" not in argv


def test_uvx_is_not_in_the_wrapped_set():
    assert "uvx" not in WINDOWS_SHELL_COMMANDS
    assert "uv" not in WINDOWS_SHELL_COMMANDS


# --------------------------------------------------------------------------
# Anything else is launched as declared
# --------------------------------------------------------------------------


def test_an_ordinary_command_passes_through_unchanged():
    assert build_argv("docker", ["run", "-i", "netops"], is_windows=True) == [
        "docker",
        "run",
        "-i",
        "netops",
    ]


def test_arguments_are_never_reordered_or_dropped():
    args = ["--repository", "C:/a b/repo", "--verbose", ""]
    assert build_argv("uvx", args, is_windows=True)[1:] == args


def test_the_original_argument_list_is_not_mutated():
    args = ["-y", "pkg"]
    build_argv("npx", args, is_windows=True)
    assert args == ["-y", "pkg"]
