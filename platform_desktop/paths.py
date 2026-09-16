"""Path safety for every filesystem tool.

The blocklist is enforced here rather than in the system prompt, so it holds
whatever the model is asked or persuaded to do. The entry that matters most is
Bantu's own config: without it, "turn off confirmations" is a valid tool call
and the whole permission model collapses.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from core.tools.registry import ToolError

#: Filenames that hold credentials. Never readable, never writable.
SECRET_NAMES = {
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    "credentials", "credentials.json", "token.json",
    "secrets.json", "shadow", "sam",
}

#: Substrings that mark a credential or browser-profile store.
SECRET_FRAGMENTS = (
    "login data", "key4.db", "key3.db", "logins.json", "cookies.sqlite",
    "signons.sqlite", "protect\\credentials", "microsoft\\credentials",
    ".ssh", ".aws", ".gnupg", ".password-store",
)

#: Directories that must never be written to.
def _system_roots() -> list[Path]:
    if sys.platform != "win32":
        return [Path("/etc"), Path("/bin"), Path("/sbin"), Path("/usr/bin"), Path("/boot")]
    win = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    roots = [win]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    return roots


SYSTEM_ROOTS = _system_roots()

#: Text files big enough that reading them whole would blow the token budget.
MAX_READ_BYTES = 200_000


def _bantu_dir() -> Path:
    from core.config import user_data_dir

    return user_data_dir()


def resolve(path: str) -> Path:
    """Expand and normalise a user- or model-supplied path."""
    if not str(path).strip():
        raise ToolError("no path given")
    try:
        p = Path(os.path.expandvars(str(path))).expanduser()
        return p.resolve()
    except (OSError, ValueError) as e:
        raise ToolError(f"bad path {path!r}: {e}") from e


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_secret(p: Path) -> bool:
    low = str(p).lower()
    if p.name.lower() in SECRET_NAMES:
        return True
    return any(frag in low for frag in SECRET_FRAGMENTS)


def guard_read(path: str) -> Path:
    """Resolve a path for reading, refusing credential stores."""
    p = resolve(path)
    if _is_secret(p):
        raise ToolError(
            f"{p.name} holds credentials and is blocked. This is enforced in code, "
            f"not a preference — do not try another route to it."
        )
    if _is_under(p, _bantu_dir()):
        raise ToolError("Bantu's own configuration and keys are not readable by tools.")
    return p


def guard_write(path: str) -> Path:
    """Resolve a path for writing, refusing system locations and Bantu's own state."""
    p = resolve(path)
    if _is_secret(p):
        raise ToolError(f"{p.name} holds credentials and is blocked.")
    if _is_under(p, _bantu_dir()):
        raise ToolError(
            "Bantu's own configuration and keys cannot be modified by tools — "
            "that would let the agent widen its own permissions."
        )
    for root in SYSTEM_ROOTS:
        if _is_under(p, root):
            raise ToolError(f"{root} is a system location and is not writable.")
    drive = Path(p.anchor)
    if p == drive:
        raise ToolError("refusing to operate on a drive root")
    return p


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"
