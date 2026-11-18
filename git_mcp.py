#!/usr/bin/env python3
"""Git MCP Server for Snapgent - provides Git operations via MCP."""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

MAX_RESULTS = 500
MAX_FILE_CHARS = 200_000
SAFE_GIT_COMMANDS = {
    "status",
    "diff",
    "log",
    "branch",
    "show",
    "ls-files",
    "ls-tree",
    "config",
    "remote",
    "tag",
    "stash",
    "merge-base",
    "rev-parse",
    "describe",
    "name-rev",
    "shortlog",
    "blame",
    "grep",
}

ALLOWED_GIT_WRITE_COMMANDS = {
    "add",
    "commit",
    "push",
    "pull",
    "fetch",
    "merge",
    "rebase",
    "branch",
    "checkout",
    "switch",
    "restore",
    "stash",
    "tag",
    "reset",
    "clean",
    "submodule",
}


def get_root() -> Path:
    """Get the git repository root."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except Exception:
        pass
    # Fallback to current directory
    return Path.cwd().resolve()


def _run_git(
    command: List[str], root: Path, timeout: int = 30, allow_write: bool = False
) -> str:
    """Run a git command safely."""
    if not command or command[0] != "git":
        raise ValueError("Command must start with 'git'")

    subcommand = command[1] if len(command) > 1 else ""

    # Check if it's a write command - only when actually modifying state
    # "branch" with -l/-a/-r is read-only; only write when creating/deleting
    is_write = False
    if subcommand in ALLOWED_GIT_WRITE_COMMANDS:
        if subcommand == "branch":
            # branch -l/-a/-r are read-only; branch without args or with -d/-D/-m are write
            args = command[2:] if len(command) > 2 else []
            is_write = not any(
                arg in ("-l", "-a", "-r", "--list", "--all", "--remote", "--format")
                for arg in args
            )
        elif subcommand in ("tag", "config", "remote", "stash", "reset", "clean"):
            # These can be read or write depending on args
            args = command[2:] if len(command) > 2 else []
            read_flags = (
                "-l",
                "--list",
                "--get",
                "-v",
                "list",
                "show",
                "--show-current",
            )
            is_write = not any(arg in read_flags for arg in args)
        else:
            is_write = True

    is_safe_read = subcommand in SAFE_GIT_COMMANDS

    if is_write and not allow_write:
        raise RuntimeError(
            f"Write command 'git {subcommand}' not allowed without explicit permission"
        )
    if not is_write and not is_safe_read:
        raise RuntimeError(f"Git command 'git {subcommand}' not in allowed list")

    result = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip() or "(no output)"
    if result.returncode:
        raise RuntimeError(
            f"Git command exited with status {result.returncode}:\n{output[:MAX_FILE_CHARS]}"
        )
    return output[:MAX_FILE_CHARS]


def get_workspace_info() -> Dict[str, Any]:
    root = get_root()
    is_git = (root / ".git").exists()
    return {
        "name": root.name,
        "path": str(root),
        "is_git_repository": is_git,
    }


def get_repo_info() -> Dict[str, Any]:
    root = get_root()
    info: Dict[str, Any] = {"workspace": root.name}

    # Remotes
    try:
        remotes = _run_git(["git", "remote", "-v"], root)
        info["remotes"] = remotes
    except Exception as e:
        info["remotes_error"] = str(e)

    # Current branch
    try:
        branch = _run_git(["git", "branch", "--show-current"], root)
        info["current_branch"] = branch
    except Exception as e:
        info["branch_error"] = str(e)

    # Last commit
    try:
        last_commit = _run_git(["git", "log", "-1", "--oneline"], root)
        info["last_commit"] = last_commit
    except Exception as e:
        info["last_commit_error"] = str(e)

    return info


def git_status(path: str = ".") -> Dict[str, Any]:
    root = get_root()
    try:
        # Porcelain format for parsing
        status = _run_git(["git", "status", "--porcelain=v1", "-u", path], root)
        lines = status.splitlines() if status else []
        files = []
        for line in lines:
            if len(line) >= 3:
                xy = line[:2]
                filepath = line[3:]
                files.append(
                    {
                        "index_status": xy[0],
                        "worktree_status": xy[1],
                        "path": filepath,
                    }
                )
        return {"files": files, "clean": len(files) == 0}
    except Exception as e:
        return {"error": str(e), "files": [], "clean": False}


def git_diff(path: str = ".", staged: bool = False, name_only: bool = False) -> str:
    root = get_root()
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--cached")
    if name_only:
        cmd.append("--name-only")
    cmd.append("--")
    cmd.append(path)
    return _run_git(cmd, root)


def git_log(
    max_count: int = 50,
    oneline: bool = True,
    since: Optional[str] = None,
    until: Optional[str] = None,
    author: Optional[str] = None,
    path: str = ".",
) -> List[Dict[str, str]]:
    root = get_root()
    cmd = ["git", "log"]
    if oneline:
        cmd.append("--oneline")
    else:
        cmd.append("--pretty=format:%H|%an|%ad|%s")
        cmd.append("--date=iso")
    cmd.append(f"-{max_count}")
    if since:
        cmd.append(f"--since={since}")
    if until:
        cmd.append(f"--until={until}")
    if author:
        cmd.append(f"--author={author}")
    cmd.append("--")
    cmd.append(path)
    output = _run_git(cmd, root)
    lines = output.splitlines() if output else []
    commits = []
    for line in lines:
        if oneline:
            parts = line.split(" ", 1)
            commits.append(
                {"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""}
            )
        else:
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append(
                    {
                        "hash": parts[0],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3],
                    }
                )
    return commits


def git_branch(list_all: bool = False, remote: bool = False) -> List[Dict[str, Any]]:
    root = get_root()
    cmd = ["git", "branch"]
    if list_all:
        cmd.append("-a")
    elif remote:
        cmd.append("-r")
    else:
        cmd.append("-l")
    cmd.append("--format=%(refname:short)|%(upstream:short)|%(HEAD)")
    output = _run_git(cmd, root)
    lines = output.splitlines() if output else []
    branches = []
    for line in lines:
        parts = line.split("|", 2)
        branches.append(
            {
                "name": parts[0],
                "upstream": parts[1] if len(parts) > 1 and parts[1] else None,
                "is_current": len(parts) > 2 and parts[2] == "*",
            }
        )
    return branches


def git_show(revision: str, path: str = "") -> str:
    root = get_root()
    cmd = ["git", "show", "--no-pager", revision]
    if path:
        cmd.append("--")
        cmd.append(path)
    return _run_git(cmd, root)


def git_ls_files(
    path: str = ".",
    cached: bool = False,
    deleted: bool = False,
    modified: bool = False,
    others: bool = False,
) -> List[str]:
    root = get_root()
    cmd = ["git", "ls-files"]
    if cached:
        cmd.append("-c")
    if deleted:
        cmd.append("-d")
    if modified:
        cmd.append("-m")
    if others:
        cmd.append("-o")
    cmd.append("--")
    cmd.append(path)
    output = _run_git(cmd, root)
    return output.splitlines() if output else []


def git_config_get(key: str, scope: str = "local") -> str:
    root = get_root()
    cmd = ["git", "config"]
    if scope == "local":
        cmd.append("--local")
    elif scope == "global":
        cmd.append("--global")
    elif scope == "system":
        cmd.append("--system")
    cmd.append("--get")
    cmd.append(key)
    return _run_git(cmd, root)


def git_config_set(key: str, value: str, scope: str = "local") -> str:
    root = get_root()
    cmd = ["git", "config"]
    if scope == "local":
        cmd.append("--local")
    elif scope == "global":
        cmd.append("--global")
    elif scope == "system":
        cmd.append("--system")
    cmd.append(key)
    cmd.append(value)
    return _run_git(cmd, root, allow_write=True)


def git_remote_list(verbose: bool = False) -> List[Dict[str, str]]:
    root = get_root()
    cmd = ["git", "remote"]
    if verbose:
        cmd.append("-v")
    output = _run_git(cmd, root)
    lines = output.splitlines() if output else []
    remotes = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            remotes.append({"name": parts[0], "url": parts[1]})
        elif len(parts) == 1:
            remotes.append({"name": parts[0], "url": ""})
    return remotes


def git_remote_add(name: str, url: str) -> str:
    root = get_root()
    return _run_git(["git", "remote", "add", name, url], root, allow_write=True)


def git_remote_remove(name: str) -> str:
    root = get_root()
    return _run_git(["git", "remote", "remove", name], root, allow_write=True)


def git_tag_list(pattern: str = "*") -> List[str]:
    root = get_root()
    output = _run_git(["git", "tag", "-l", pattern], root)
    return output.splitlines() if output else []


def git_tag_create(tag: str, message: str = "", revision: str = "HEAD") -> str:
    root = get_root()
    cmd = ["git", "tag"]
    if message:
        cmd.extend(["-a", "-m", message])
    cmd.append(tag)
    if revision != "HEAD":
        cmd.append(revision)
    return _run_git(cmd, root, allow_write=True)


def git_stash_list() -> List[Dict[str, str]]:
    root = get_root()
    output = _run_git(["git", "stash", "list", "--format=%gd|%gs|%gD"], root)
    lines = output.splitlines() if output else []
    stashes = []
    for line in lines:
        parts = line.split("|", 2)
        stashes.append(
            {
                "ref": parts[0],
                "message": parts[1] if len(parts) > 1 else "",
                "description": parts[2] if len(parts) > 2 else "",
            }
        )
    return stashes


def git_stash_push(message: str = "", include_untracked: bool = False) -> str:
    root = get_root()
    cmd = ["git", "stash", "push"]
    if message:
        cmd.extend(["-m", message])
    if include_untracked:
        cmd.append("-u")
    return _run_git(cmd, root, allow_write=True)


def git_stash_pop(stash_ref: str = "stash@{0}") -> str:
    root = get_root()
    return _run_git(["git", "stash", "pop", stash_ref], root, allow_write=True)


def git_stash_drop(stash_ref: str = "stash@{0}") -> str:
    root = get_root()
    return _run_git(["git", "stash", "drop", stash_ref], root, allow_write=True)


def git_merge_base(branch1: str, branch2: str) -> str:
    root = get_root()
    return _run_git(["git", "merge-base", branch1, branch2], root)


def git_rev_parse(rev: str) -> str:
    root = get_root()
    return _run_git(["git", "rev-parse", rev], root)


def git_describe(commit: str = "HEAD", tags: bool = True, always: bool = True) -> str:
    root = get_root()
    cmd = ["git", "describe"]
    if tags:
        cmd.append("--tags")
    if always:
        cmd.append("--always")
    cmd.append(commit)
    return _run_git(cmd, root)


def git_blame(
    path: str, start_line: int = 1, end_line: int = 0
) -> List[Dict[str, Any]]:
    root = get_root()
    cmd = [
        "git",
        "blame",
        "-L",
        f"{start_line},{end_line or '100'}",
        "--porcelain",
        "--",
        path,
    ]
    output = _run_git(cmd, root)
    lines = output.splitlines() if output else []
    results = []
    current = {}
    for line in lines:
        if line.startswith("\t"):
            if current:
                current["line"] = line[1:]
                results.append(current)
                current = {}
        else:
            parts = line.split(" ", 1)
            if len(parts) == 2:
                key, value = parts
                if key in (
                    "author",
                    "author-mail",
                    "author-time",
                    "author-tz",
                    "committer",
                    "committer-mail",
                    "committer-time",
                    "committer-tz",
                    "summary",
                    "previous",
                    "filename",
                ):
                    current[key] = value
    return results


def git_grep(
    pattern: str,
    path: str = ".",
    case_insensitive: bool = False,
    line_number: bool = True,
) -> List[Dict[str, Any]]:
    root = get_root()
    cmd = ["git", "grep"]
    if case_insensitive:
        cmd.append("-i")
    if line_number:
        cmd.append("-n")
    cmd.append("-P")
    cmd.append(pattern)
    cmd.append("--")
    cmd.append(path)
    try:
        output = _run_git(cmd, root)
    except RuntimeError as e:
        if "exited with status 1" in str(e):
            return []  # No matches
        raise
    lines = output.splitlines() if output else []
    results = []
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) >= 3:
            results.append({"file": parts[0], "line": int(parts[1]), "text": parts[2]})
        elif len(parts) == 2:
            results.append({"file": parts[0], "line": 0, "text": parts[1]})
    return results[:MAX_RESULTS]


def git_add(paths: List[str]) -> str:
    root = get_root()
    cmd = ["git", "add", "--"]
    cmd.extend(paths)
    return _run_git(cmd, root, allow_write=True)


def git_commit(message: str, allow_empty: bool = False, signoff: bool = False) -> str:
    root = get_root()
    cmd = ["git", "commit", "-m", message]
    if allow_empty:
        cmd.append("--allow-empty")
    if signoff:
        cmd.append("-s")
    return _run_git(cmd, root, allow_write=True)


def git_push(
    remote: str = "origin", branch: str = "", force: bool = False, tags: bool = False
) -> str:
    root = get_root()
    cmd = ["git", "push"]
    if force:
        cmd.append("--force-with-lease")
    if tags:
        cmd.append("--tags")
    cmd.append(remote)
    if branch:
        cmd.append(branch)
    return _run_git(cmd, root, allow_write=True)


def git_pull(remote: str = "origin", branch: str = "", rebase: bool = False) -> str:
    root = get_root()
    cmd = ["git", "pull"]
    if rebase:
        cmd.append("--rebase")
    cmd.append(remote)
    if branch:
        cmd.append(branch)
    return _run_git(cmd, root, allow_write=True)


def git_fetch(remote: str = "origin", tags: bool = False, prune: bool = False) -> str:
    root = get_root()
    cmd = ["git", "fetch"]
    if tags:
        cmd.append("--tags")
    if prune:
        cmd.append("--prune")
    cmd.append(remote)
    return _run_git(cmd, root, allow_write=True)


def git_merge(branch: str, no_ff: bool = False, no_commit: bool = False) -> str:
    root = get_root()
    cmd = ["git", "merge"]
    if no_ff:
        cmd.append("--no-ff")
    if no_commit:
        cmd.append("--no-commit")
    cmd.append(branch)
    return _run_git(cmd, root, allow_write=True)


def git_rebase(upstream: str, onto: str = "") -> str:
    root = get_root()
    cmd = ["git", "rebase"]
    if onto:
        cmd.extend(["--onto", onto])
    cmd.append(upstream)
    return _run_git(cmd, root, allow_write=True)


def git_checkout(branch: str, create: bool = False, track: bool = False) -> str:
    root = get_root()
    cmd = ["git", "checkout"]
    if create:
        cmd.append("-b")
    if track:
        cmd.append("--track")
    cmd.append(branch)
    return _run_git(cmd, root, allow_write=True)


def git_switch(branch: str, create: bool = False) -> str:
    root = get_root()
    cmd = ["git", "switch"]
    if create:
        cmd.append("-c")
    cmd.append(branch)
    return _run_git(cmd, root, allow_write=True)


def git_restore(path: str, source: str = "", staged: bool = False) -> str:
    root = get_root()
    cmd = ["git", "restore"]
    if source:
        cmd.extend(["-s", source])
    if staged:
        cmd.append("--staged")
    cmd.append("--")
    cmd.append(path)
    return _run_git(cmd, root, allow_write=True)


def git_reset(
    mode: str = "mixed", commit: str = "HEAD", paths: List[str] = None
) -> str:
    root = get_root()
    cmd = ["git", "reset"]
    if mode:
        cmd.append(f"--{mode}")
    cmd.append(commit)
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    return _run_git(cmd, root, allow_write=True)


def git_clean(
    force: bool = False, directories: bool = False, dry_run: bool = False
) -> str:
    root = get_root()
    cmd = ["git", "clean"]
    if force:
        cmd.append("-f")
    if directories:
        cmd.append("-d")
    if dry_run:
        cmd.append("-n")
    return _run_git(cmd, root, allow_write=True)


def git_submodule_update(init: bool = True, recursive: bool = True) -> str:
    root = get_root()
    cmd = ["git", "submodule", "update"]
    if init:
        cmd.append("--init")
    if recursive:
        cmd.append("--recursive")
    return _run_git(cmd, root, allow_write=True)


# MCP Server Implementation
import tempfile

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "git_workspace_info",
        "description": "Get workspace information including git repository status",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_repo_info",
        "description": "Get repository information: remotes, current branch, last commit",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_status",
        "description": "Get git status (porcelain format)",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "git_diff",
        "description": "Show git diff",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "staged": {"type": "boolean", "default": False},
                "name_only": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Show git log",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_count": {"type": "integer", "default": 50},
                "oneline": {"type": "boolean", "default": True},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "author": {"type": "string"},
                "path": {"type": "string", "default": "."},
            },
        },
    },
    {
        "name": "git_branch",
        "description": "List git branches",
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_all": {"type": "boolean", "default": False},
                "remote": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_show",
        "description": "Show git object (commit, tag, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "revision": {"type": "string"},
                "path": {"type": "string", "default": ""},
            },
            "required": ["revision"],
        },
    },
    {
        "name": "git_ls_files",
        "description": "List files in the index",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "cached": {"type": "boolean", "default": False},
                "deleted": {"type": "boolean", "default": False},
                "modified": {"type": "boolean", "default": False},
                "others": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_config_get",
        "description": "Get git config value",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": ["local", "global", "system"],
                    "default": "local",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "git_config_set",
        "description": "Set git config value",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": ["local", "global", "system"],
                    "default": "local",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "git_remote_list",
        "description": "List git remotes",
        "inputSchema": {
            "type": "object",
            "properties": {"verbose": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "git_remote_add",
        "description": "Add a git remote",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["name", "url"],
        },
    },
    {
        "name": "git_remote_remove",
        "description": "Remove a git remote",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "git_tag_list",
        "description": "List git tags",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "default": "*"}},
        },
    },
    {
        "name": "git_tag_create",
        "description": "Create a git tag",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "message": {"type": "string", "default": ""},
                "revision": {"type": "string", "default": "HEAD"},
            },
            "required": ["tag"],
        },
    },
    {
        "name": "git_stash_list",
        "description": "List git stashes",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_stash_push",
        "description": "Push a new stash",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "default": ""},
                "include_untracked": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_stash_pop",
        "description": "Pop a stash",
        "inputSchema": {
            "type": "object",
            "properties": {"stash_ref": {"type": "string", "default": "stash@{0}"}},
        },
    },
    {
        "name": "git_stash_drop",
        "description": "Drop a stash",
        "inputSchema": {
            "type": "object",
            "properties": {"stash_ref": {"type": "string", "default": "stash@{0}"}},
        },
    },
    {
        "name": "git_merge_base",
        "description": "Find merge base between two branches",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch1": {"type": "string"},
                "branch2": {"type": "string"},
            },
            "required": ["branch1", "branch2"],
        },
    },
    {
        "name": "git_rev_parse",
        "description": "Parse revision to commit hash",
        "inputSchema": {
            "type": "object",
            "properties": {"rev": {"type": "string"}},
            "required": ["rev"],
        },
    },
    {
        "name": "git_describe",
        "description": "Describe a commit relative to tags",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit": {"type": "string", "default": "HEAD"},
                "tags": {"type": "boolean", "default": True},
                "always": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "git_blame",
        "description": "Show git blame for a file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "end_line": {"type": "integer", "default": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "git_grep",
        "description": "Search in git repository",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "case_insensitive": {"type": "boolean", "default": False},
                "line_number": {"type": "boolean", "default": True},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_add",
        "description": "Stage files for commit",
        "inputSchema": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
        },
    },
    {
        "name": "git_commit",
        "description": "Create a commit",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "allow_empty": {"type": "boolean", "default": False},
                "signoff": {"type": "boolean", "default": False},
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push to remote",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "default": "origin"},
                "branch": {"type": "string", "default": ""},
                "force": {"type": "boolean", "default": False},
                "tags": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_pull",
        "description": "Pull from remote",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "default": "origin"},
                "branch": {"type": "string", "default": ""},
                "rebase": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_fetch",
        "description": "Fetch from remote",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "default": "origin"},
                "tags": {"type": "boolean", "default": False},
                "prune": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_merge",
        "description": "Merge a branch",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "no_ff": {"type": "boolean", "default": False},
                "no_commit": {"type": "boolean", "default": False},
            },
            "required": ["branch"],
        },
    },
    {
        "name": "git_rebase",
        "description": "Rebase onto upstream",
        "inputSchema": {
            "type": "object",
            "properties": {
                "upstream": {"type": "string"},
                "onto": {"type": "string", "default": ""},
            },
            "required": ["upstream"],
        },
    },
    {
        "name": "git_checkout",
        "description": "Checkout a branch",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "create": {"type": "boolean", "default": False},
                "track": {"type": "boolean", "default": False},
            },
            "required": ["branch"],
        },
    },
    {
        "name": "git_switch",
        "description": "Switch branch (modern git)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "create": {"type": "boolean", "default": False},
            },
            "required": ["branch"],
        },
    },
    {
        "name": "git_restore",
        "description": "Restore files (modern git)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "source": {"type": "string", "default": ""},
                "staged": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    },
    {
        "name": "git_reset",
        "description": "Reset HEAD",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["soft", "mixed", "hard"],
                    "default": "mixed",
                },
                "commit": {"type": "string", "default": "HEAD"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "git_clean",
        "description": "Clean untracked files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "default": False},
                "directories": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "git_submodule_update",
        "description": "Update submodules",
        "inputSchema": {
            "type": "object",
            "properties": {
                "init": {"type": "boolean", "default": True},
                "recursive": {"type": "boolean", "default": True},
            },
        },
    },
]


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")

    # JSON-RPC notifications carry no id and must never receive a response
    if request_id is None and str(method).startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "git-mcp", "version": SERVER_VERSION},
                },
            }
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": TOOLS},
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            # Route to tool function. Tool names carry a git_ prefix; the
            # workspace/repo info funcs are named get_workspace_info/get_repo_info.
            tool_func = globals().get(f"git_{tool_name.replace('.', '_')}")
            if not tool_func:
                # Try without git_ prefix
                tool_func = globals().get(tool_name)
            if not tool_func and tool_name.startswith("git_"):
                # git_workspace_info -> get_workspace_info
                tool_func = globals().get(f"get_{tool_name[4:].replace('.', '_')}")
            if not tool_func:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }

            result = tool_func(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ]
                },
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(e)},
        }


async def _read_stdin_lines():
    """Read lines from stdin asynchronously (cross-platform)."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        yield line


async def main() -> None:
    try:
        async for line in _read_stdin_lines():
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = await handle_request(request)
            if response is None:
                continue
            # Write directly to stdout (synchronous, but fast enough)
            sys.stdout.write(_json_line(response) + "\n")
            sys.stdout.flush()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
