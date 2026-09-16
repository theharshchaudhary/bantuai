"""The PowerShell escape hatch.

The widest-surface tool in the whole design, and the only one that can do
things nothing else covers. It is therefore the most carefully fenced:

- always CONFIRM, and the confirmation shows the literal command
- a deny-list refuses categories that are catastrophic or that would let the
  agent widen its own permissions
- never elevated, always time-limited, output always truncated

The deny-list is a backstop, not the security boundary — the confirmation
prompt is. A person seeing the exact command before it runs is what actually
protects them. The list exists to stop the obviously terrible from ever
reaching that prompt.
"""

from __future__ import annotations

import re
import subprocess

from core.tools.registry import Tier, ToolError, ToolRegistry

#: Patterns refused outright. Each entry is (regex, why).
DENY: list[tuple[str, str]] = [
    (r"\bformat(-volume)?\b|\bdiskpart\b|\bclear-disk\b|\bInitialize-Disk\b",
     "disk formatting or partitioning"),
    (r"\bremove-item\b[^|]*\b-recurse\b[^|]*\b-force\b.*\b(c:\\?|\$env:systemdrive)\\?\s*$",
     "recursive force-delete of a drive root"),
    (r"\bSet-MpPreference\b|\bDisable(-|\s)?(Windows)?Defender\b|NetFirewall\w*\b.*-Enabled\s+(False|\$false)",
     "disabling Defender or the firewall"),
    (r"\bNew-ItemProperty\b.*\bRun\b|\bSet-ItemProperty\b.*\bCurrentVersion\\Run\b",
     "installing a startup persistence entry"),
    (r"\breg(\.exe)?\s+(add|delete)\b|\bSet-ItemProperty\b.*\bHKLM:", "writing to the registry"),
    (r"\bStart-Process\b.*-Verb\s+RunAs|\bsudo\b", "requesting elevation"),
    (r"\bInvoke-Expression\b|\biex\b\s*\(|\bDownloadString\b|\bInvoke-WebRequest\b.*\|\s*iex",
     "downloading and executing remote code"),
    (r"\bcipher\s+/w\b|\bsdelete\b", "secure-wiping free space"),
    (r"\bbcdedit\b|\bbootrec\b", "modifying boot configuration"),
    (r"\bnet\s+user\b.*\/add|\bNew-LocalUser\b|\bAdd-LocalGroupMember\b.*Administrators",
     "creating or elevating a user account"),
    (r"BantuAI", "touching Bantu's own configuration"),
    (r"\.env\b|id_rsa|\bcredential(s)?\b.*\b(dump|export)\b", "reading credential stores"),
]

MAX_OUTPUT = 6000


def check_command(command: str) -> None:
    """Raise if a command matches the deny-list."""
    text = command.strip()
    if not text:
        raise ToolError("no command given")
    for pattern, why in DENY:
        if re.search(pattern, text, re.IGNORECASE):
            raise ToolError(
                f"refused: this command involves {why}, which is blocked in code. "
                f"Nothing was run. Do not attempt a variation of it."
            )


def register(reg: ToolRegistry, default_timeout: int = 60) -> None:
    @reg.register(tier=Tier.CONFIRM, category="shell")
    def run_powershell(command: str, timeout_seconds: int = 0) -> str:
        """Run a PowerShell command and return its output.

        The escape hatch for anything the other tools do not cover. Prefer a
        dedicated tool when one exists — they are safer, clearer to the user,
        and their output is easier to read. The user sees this exact command
        and must approve it before it runs.

        Runs unelevated. Formatting disks, disabling security software,
        registry writes, requesting elevation and downloading-and-executing
        remote code are refused in code.

        Args:
            command: The PowerShell to run.
            timeout_seconds: Give up after this long. 0 uses the default.
        """
        check_command(command)
        limit = timeout_seconds if timeout_seconds > 0 else default_timeout
        limit = max(1, min(limit, 300))

        try:
            proc = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"the command was still running after {limit}s and was stopped. "
                f"It may have been waiting for input — PowerShell runs here with no console."
            ) from None
        except FileNotFoundError as e:
            raise ToolError(f"PowerShell is not available: {e}") from e

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        parts = []
        if out:
            parts.append(out[:MAX_OUTPUT] + ("\n... [output truncated]" if len(out) > MAX_OUTPUT else ""))
        if err:
            parts.append(f"[stderr]\n{err[:2000]}")
        if proc.returncode != 0:
            parts.append(f"[exit code {proc.returncode}]")
        if not parts:
            return "Done. The command produced no output."
        return "\n\n".join(parts)
