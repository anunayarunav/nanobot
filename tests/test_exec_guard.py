"""Tests for the exec tool guard — path restriction logic."""

import pytest

from nanobot.agent.tools.shell import ExecTool


@pytest.fixture
def tool(tmp_path):
    """ExecTool with restrict_to_workspace=True, workspace at tmp_path."""
    return ExecTool(
        working_dir=str(tmp_path),
        timeout=10,
        restrict_to_workspace=True,
    )


# ===========================================================================
# Commands that should be ALLOWED
# ===========================================================================


def test_simple_command(tool):
    assert tool._guard_command("ls", str(tool.working_dir)) is None


def test_relative_path_with_slashes(tool):
    """Relative paths like 'projects/foo/sessions/' must NOT trigger the guard."""
    assert tool._guard_command(
        "ls -lt projects/kitty-vs-the-universe/sessions/ | head -10",
        str(tool.working_dir),
    ) is None


def test_relative_output_path_in_quotes(tool):
    """'-o artisan-out/result.mp4' should not be treated as /result.mp4."""
    assert tool._guard_command(
        'artisan chat -m "hello" -o "artisan-out/result.mp4"',
        str(tool.working_dir),
    ) is None


def test_dollar_pwd_path(tool):
    """$(pwd)/artisan-out should not extract /artisan-out as absolute path."""
    assert tool._guard_command(
        'artisan chat -o "$(pwd)/artisan-out/result.png"',
        str(tool.working_dir),
    ) is None


def test_absolute_path_within_workspace(tool):
    """Absolute paths within the workspace should be allowed."""
    cwd = str(tool.working_dir)
    assert tool._guard_command(
        f"ls {cwd}/projects/foo",
        cwd,
    ) is None


def test_cd_to_workspace_then_command(tool):
    """cd to workspace + command should be fine."""
    cwd = str(tool.working_dir)
    assert tool._guard_command(
        f"cd {cwd} && artisan chat -m 'hello'",
        cwd,
    ) is None


def test_pipe_with_head(tool):
    """Piped commands without absolute paths should be fine."""
    assert tool._guard_command(
        "ls -la | head -10",
        str(tool.working_dir),
    ) is None


def test_find_within_workspace(tool):
    """find with workspace path is allowed."""
    cwd = str(tool.working_dir)
    assert tool._guard_command(
        f'find {cwd} -name "*.py"',
        cwd,
    ) is None


def test_long_artisan_command_with_prompt(tool):
    """The full artisan command that was being blocked — relative paths in args."""
    cmd = (
        'timeout 600 artisan chat --project "kitty-vs-the-universe" '
        '--aspect-ratio 9:16 -m "Generate an 8-second video for Scene 1 Shot 1 '
        'using interpolation mode with [0-2s] soldiers charging. [2-4s] Tiger '
        'lunges forward. [4-6s] Dust cloud. [6-8s] Aftermath." '
        '-o "artisan-out/shot1_video.mp4" -v'
    )
    assert tool._guard_command(cmd, str(tool.working_dir)) is None


# ===========================================================================
# Commands that should be BLOCKED
# ===========================================================================


def test_absolute_path_outside_workspace(tool):
    """Absolute path outside workspace should be blocked."""
    result = tool._guard_command("cat /etc/passwd", str(tool.working_dir))
    assert result is not None
    assert "path outside" in result


def test_path_traversal_blocked(tool):
    """../ traversal should be blocked."""
    result = tool._guard_command("cat ../../../etc/passwd", str(tool.working_dir))
    assert result is not None


def test_absolute_path_after_redirect(tool):
    """Redirecting to absolute path outside workspace should be blocked."""
    result = tool._guard_command("echo hi > /tmp/evil", str(tool.working_dir))
    assert result is not None
    assert "path outside" in result


def test_dangerous_rm_blocked(tool):
    """rm -rf should be caught by deny patterns."""
    result = tool._guard_command("rm -rf /", str(tool.working_dir))
    assert result is not None
    assert "dangerous" in result or "blocked" in result


def test_absolute_path_in_semicolon_chain(tool):
    """Path after ; should be caught."""
    result = tool._guard_command("echo hi; cat /etc/shadow", str(tool.working_dir))
    assert result is not None
    assert "path outside" in result


def test_absolute_path_after_pipe(tool):
    """Path after | should be caught."""
    result = tool._guard_command("echo hi | tee /tmp/x", str(tool.working_dir))
    assert result is not None
    assert "path outside" in result


def test_absolute_path_after_ampersand(tool):
    """Path after && should be caught."""
    result = tool._guard_command("true && cat /etc/hosts", str(tool.working_dir))
    assert result is not None
    assert "path outside" in result
