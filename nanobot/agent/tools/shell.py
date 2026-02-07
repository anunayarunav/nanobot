"""Shell execution tool."""

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        allowed_git_repos: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"\b(format|mkfs|diskpart)\b",   # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.allowed_git_repos = allowed_git_repos or []
    
    @property
    def name(self) -> str:
        return "exec"
    
    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error
        
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"
            
            output_parts = []
            
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")
            
            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")
            
            result = "\n".join(output_parts) if output_parts else "(no output)"
            
            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
            
            return result
            
        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        # Git clone gets special handling: whitelist check + destination check
        git_clone_match = re.match(r"git\s+clone\s+(\S+)(?:\s+(\S+))?", cmd)
        if git_clone_match:
            return self._guard_git_clone(git_clone_match.group(1), git_clone_match.group(2), cwd)

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            # Strip URLs before extracting filesystem paths to avoid false positives
            cmd_no_urls = re.sub(r"[a-z+]+://\S+", "", cmd)  # https://, git://, ssh://
            cmd_no_urls = re.sub(r"\S+@\S+:\S+", "", cmd_no_urls)  # git@host:path

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd_no_urls)
            posix_paths = re.findall(r"/[^\s\"']+", cmd_no_urls)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _guard_git_clone(self, url: str, dest: str | None, cwd: str) -> str | None:
        """Guard git clone: check repo whitelist and destination path."""
        if not self._is_git_repo_allowed(url):
            hint = ", ".join(self.allowed_git_repos) if self.allowed_git_repos else "none configured"
            return f"Error: Repository not in allowlist. Allowed: {hint}"

        # Destination must be within workspace (if restrict_to_workspace is on)
        if self.restrict_to_workspace and dest:
            cwd_path = Path(cwd).resolve()
            try:
                dest_path = Path(dest).resolve() if dest.startswith("/") else (cwd_path / dest).resolve()
                if cwd_path not in dest_path.parents and dest_path != cwd_path:
                    return "Error: Clone destination must be within workspace"
            except Exception:
                pass

        return None

    def _is_git_repo_allowed(self, url: str) -> bool:
        """Check if a git repo URL matches the allowed_git_repos whitelist."""
        if not self.allowed_git_repos:
            return False

        # Normalize: strip scheme and user prefix, unify separators
        normalized = re.sub(r"^[a-z+]+://", "", url)  # https://, git+ssh://
        normalized = re.sub(r"^[^@]+@", "", normalized)  # git@github.com:...
        normalized = normalized.replace(":", "/", 1)  # github.com:user/repo
        normalized = normalized.rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]

        for pattern in self.allowed_git_repos:
            if fnmatch.fnmatch(normalized, pattern.rstrip("/")):
                return True
        return False
