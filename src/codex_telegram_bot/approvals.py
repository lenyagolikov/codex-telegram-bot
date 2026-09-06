from __future__ import annotations

import ipaddress
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAFE_READ_ACTION_TYPES = frozenset({"read", "listFiles", "search"})
ARC_READ_ONLY_COMMANDS = frozenset(
    {
        "diff",
        "info",
        "log",
        "ls",
        "root",
        "show",
        "status",
    }
)
ARC_READ_ONLY_PR_COMMANDS = frozenset({"active-diff", "changes", "status"})
ARC_UNSAFE_ARGUMENT_PREFIXES = (
    "--config",
    "--exec",
    "--ext-diff",
    "--external",
    "--output",
    "--pager",
    "--template",
)
SHELL_EXECUTABLES = frozenset({"bash", "dash", "sh", "zsh"})
SHELL_CONTROL_CHARACTERS = frozenset(";&|<>`$*?{}[]()\n\r")
SENSITIVE_READ_FRAGMENTS = (
    ".env",
    "/.ssh/",
    "/.codex/",
    "/.stefania/",
    "/.zshrc",
    "/.bashrc",
    "/.bash_history",
    "/etc/shadow",
    "/proc/",
    "id_rsa",
    "id_ed25519",
)
NETWORK_OR_ENV_COMMANDS = (
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "rsync ",
    "printenv",
    "/usr/bin/env",
    "/dev/tcp",
)


@dataclass(frozen=True, slots=True)
class ReadOnlyApprovalAssessment:
    approved: bool
    reason: str
    action_types: tuple[str, ...] = ()


def is_safe_read_only_approval(
    params: dict[str, Any], allowed_roots: tuple[Path, ...]
) -> bool:
    """Return true only for App Server requests proven to be bounded reads."""
    return assess_safe_read_only_approval(params, allowed_roots).approved


def assess_safe_read_only_approval(
    params: dict[str, Any], allowed_roots: tuple[Path, ...]
) -> ReadOnlyApprovalAssessment:
    """Explain whether an App Server command approval is a bounded read."""
    roots = tuple(root.resolve() for root in allowed_roots)
    if not roots:
        return _rejected("no_allowed_roots")

    cwd = approval_path(params.get("cwd"))
    if cwd is None:
        return _rejected("missing_cwd")
    if not path_is_allowed(cwd, roots):
        return _rejected("cwd_outside_allowed_roots")

    available_decisions = params.get("availableDecisions")
    if isinstance(available_decisions, list) and "accept" not in available_decisions:
        return _rejected("accept_not_available")

    command = str(params.get("command") or "")
    lowered_command = command.lower()
    if any(fragment in lowered_command for fragment in NETWORK_OR_ENV_COMMANDS):
        return _rejected("network_or_environment_command")
    if any(fragment in lowered_command for fragment in SENSITIVE_READ_FRAGMENTS):
        return _rejected("sensitive_path")

    network_context = params.get("networkApprovalContext")
    if network_context is not None and not _is_loopback_network_context(network_context):
        return _rejected("external_network_access")

    if not _additional_permissions_are_read_only(
        params.get("additionalPermissions"), roots
    ):
        return _rejected("additional_permissions_not_read_only")

    actions = params.get("commandActions")
    action_types = _action_types(actions)
    if isinstance(actions, list) and actions:
        if any(not isinstance(action, dict) for action in actions):
            return _rejected("malformed_command_action", action_types)
        if all(
            isinstance(action, dict)
            and action.get("type") in SAFE_READ_ACTION_TYPES
            for action in actions
        ):
            for action in actions:
                path = approval_path(action.get("path")) or cwd
                if not path_is_allowed(path, roots, base=Path(cwd)):
                    return _rejected("action_path_outside_allowed_roots", action_types)
            return ReadOnlyApprovalAssessment(True, "structured_read", action_types)

        unsafe_types = set(action_types) - {"unknown"}
        if unsafe_types:
            return _rejected("unsafe_command_action", action_types)

    if _is_allowlisted_arc_read_command(command):
        return ReadOnlyApprovalAssessment(True, "allowlisted_arc_read", action_types)

    if not isinstance(actions, list) or not actions:
        return _rejected("missing_command_actions", action_types)
    return _rejected("unclassified_command", action_types)


def _rejected(
    reason: str, action_types: tuple[str, ...] = ()
) -> ReadOnlyApprovalAssessment:
    return ReadOnlyApprovalAssessment(False, reason, action_types)


def _action_types(actions: Any) -> tuple[str, ...]:
    if not isinstance(actions, list):
        return ()
    return tuple(
        sorted(
            {
                str(action.get("type") or "missing")
                for action in actions
                if isinstance(action, dict)
            }
        )
    )


def _is_allowlisted_arc_read_command(command: str) -> bool:
    arguments = _simple_command_arguments(command)
    if arguments is None or len(arguments) < 2:
        return False
    if Path(arguments[0]).name != "arc":
        return False
    if any(
        argument.startswith(ARC_UNSAFE_ARGUMENT_PREFIXES)
        for argument in arguments[2:]
    ):
        return False

    subcommand = arguments[1]
    if subcommand in ARC_READ_ONLY_COMMANDS:
        return True
    return (
        subcommand == "pr"
        and len(arguments) >= 3
        and arguments[2] in ARC_READ_ONLY_PR_COMMANDS
    )


def _simple_command_arguments(command: str) -> list[str] | None:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if not arguments:
        return None

    if Path(arguments[0]).name in SHELL_EXECUTABLES:
        if len(arguments) != 3 or arguments[1] not in {"-c", "-lc"}:
            return None
        command = arguments[2]

    if any(character in command for character in SHELL_CONTROL_CHARACTERS):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _is_loopback_network_context(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    host = str(value.get("host") or "").strip().lower().strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def approval_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return str(value["path"])
    return None


def path_is_allowed(
    value: str,
    allowed_roots: tuple[Path, ...],
    *,
    base: Path | None = None,
) -> bool:
    normalized = value.lower().replace("\\", "/")
    if any(fragment in normalized for fragment in SENSITIVE_READ_FRAGMENTS):
        return False
    try:
        path = Path(value)
        if not path.is_absolute():
            if base is None:
                return False
            path = base / path
        path = path.resolve()
    except (OSError, RuntimeError):
        return False
    return any(path == root or root in path.parents for root in allowed_roots)


def _additional_permissions_are_read_only(
    permissions: Any, allowed_roots: tuple[Path, ...]
) -> bool:
    if permissions is None:
        return True
    if not isinstance(permissions, dict):
        return False
    network = permissions.get("network")
    if isinstance(network, dict) and network.get("enabled"):
        return False
    file_system = permissions.get("fileSystem")
    if file_system is None:
        return True
    if not isinstance(file_system, dict) or file_system.get("write"):
        return False
    for value in file_system.get("read") or []:
        path = approval_path(value)
        if path is None or not path_is_allowed(path, allowed_roots):
            return False
    for entry in file_system.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("access") != "read":
            return False
        path = approval_path(entry.get("path"))
        if path is None or not path_is_allowed(path, allowed_roots):
            return False
    return True
