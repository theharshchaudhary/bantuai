"""Phase 3 tests — file tools and path guards. No API key needed.

    .venv/Scripts/python.exe tests/test_phase3.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers.base import ToolCall
from core.tools.registry import Tier, ToolError, ToolRegistry
from platform_desktop import files, paths

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def run(tool, **kw):
    """Execute through the registry, auto-approving confirm-tier tools."""
    return reg.execute(ToolCall("t", tool, kw), confirm=lambda t, a: True)


reg = ToolRegistry()
files.register(reg)

SANDBOX = Path(tempfile.mkdtemp(prefix="bantu_test_"))
(SANDBOX / "docs").mkdir()
(SANDBOX / "notes.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
(SANDBOX / "docs" / "invoice_jan.txt").write_text("total: 100 USD\n", encoding="utf-8")
(SANDBOX / "docs" / "invoice_feb.txt").write_text("total: 250 USD\n", encoding="utf-8")
(SANDBOX / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
(SANDBOX / "song.mp3").write_bytes(b"ID3 fake")


# --- path guards ------------------------------------------------------------

print("\n[path guards]")


def refused(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
        return False
    except ToolError:
        return True


check("reading a .env is refused", refused(paths.guard_read, str(SANDBOX / ".env")))
check("reading an ssh key is refused", refused(paths.guard_read, str(SANDBOX / "id_rsa")))
check("a path containing .ssh is refused", refused(paths.guard_read, str(Path.home() / ".ssh" / "config")))
check("browser login data is refused", refused(paths.guard_read, r"C:\x\Default\Login Data"))
check("an ordinary file is allowed", paths.guard_read(str(SANDBOX / "notes.txt")).name == "notes.txt")

if sys.platform == "win32":
    win = os.environ.get("SystemRoot", r"C:\Windows")
    check("writing into Windows is refused", refused(paths.guard_write, win + r"\System32\drivers\etc\hosts"))
    pf = os.environ.get("ProgramFiles")
    check("writing into Program Files is refused", refused(paths.guard_write, pf + r"\app\x.dll") if pf else True)

from core.config import user_data_dir

check("writing Bantu's own config is refused — the agent cannot widen its own permissions",
      refused(paths.guard_write, str(user_data_dir() / "config.json")))
check("reading Bantu's own keys is refused", refused(paths.guard_read, str(user_data_dir() / "config.json")))
check("a drive root is refused", refused(paths.guard_write, "C:\\") if sys.platform == "win32" else True)
check("an empty path is refused", refused(paths.resolve, "   "))


# --- reading ----------------------------------------------------------------

print("\n[reading]")
out = run("read_file", path=str(SANDBOX / "notes.txt"))
check("read_file returns contents", "hello world" in out, out[:60])
check("read_file refuses a folder", run("read_file", path=str(SANDBOX / "docs")).startswith("Error"))
check("read_file refuses a missing file", run("read_file", path=str(SANDBOX / "nope.txt")).startswith("Error"))

big = SANDBOX / "big.txt"
big.write_text("x" * 5000, encoding="utf-8")
out = run("read_file", path=str(big), max_bytes=100)
check("read_file honours max_bytes and says so", len(out) < 400 and "truncated" in out, str(len(out)))

out = run("list_directory", path=str(SANDBOX))
check("list_directory shows folders", "[dir]  docs" in out, out[:120])
check("list_directory shows files", "notes.txt" in out)
out = run("list_directory", path=str(SANDBOX), pattern="*.jpg")
check("list_directory filters by pattern", "photo.jpg" in out and "notes.txt" not in out)

out = run("file_info", path=str(SANDBOX / "notes.txt"))
check("file_info reports size and dates", "size" in out and "modified" in out, out[:80])

out = run("search_files", query="invoice", root=str(SANDBOX))
check("search_files finds by name", "invoice_jan" in out and "invoice_feb" in out, out[:120])
out = run("search_files", query="250 USD", root=str(SANDBOX), search_contents=True)
check("search_files can search contents", "invoice_feb" in out, out[:120])
out = run("search_files", query="invoice", root=str(SANDBOX), extensions="jpg")
check("search_files filters by extension", "No files matching" in out, out[:80])
out = run("search_files", query="zzz", root=str(SANDBOX))
check("no matches is a clear message, not an error", out.startswith("No files"), out[:60])
check("search_files rejects a bad date",
      run("search_files", query="x", root=str(SANDBOX), modified_after="not-a-date").startswith("Error"))

out = run("disk_usage", path=str(SANDBOX))
check("disk_usage reports free space", "free" in out and "used of" in out, out[:90])


# --- writing ----------------------------------------------------------------

print("\n[writing]")
target = SANDBOX / "new.txt"
check("write_file creates", "Wrote" in run("write_file", path=str(target), content="alpha"))
check("...and the content is right", target.read_text(encoding="utf-8") == "alpha")
check("write_file refuses to clobber by default",
      run("write_file", path=str(target), content="beta").startswith("Error"))
check("...and the original survived", target.read_text(encoding="utf-8") == "alpha")
check("overwrite=true replaces",
      "Wrote" in run("write_file", path=str(target), content="beta", overwrite=True))
check("...with the new content", target.read_text(encoding="utf-8") == "beta")

check("edit_file replaces text", "Replaced" in run("edit_file", path=str(target), find="beta", replace="gamma"))
check("...correctly", target.read_text(encoding="utf-8") == "gamma")
check("edit_file reports missing text rather than silently doing nothing",
      run("edit_file", path=str(target), find="absent", replace="x").startswith("Error"))

check("create_folder works", "Created" in run("create_folder", path=str(SANDBOX / "sub" / "deep")))
check("...including parents", (SANDBOX / "sub" / "deep").is_dir())

check("copy_file works", "Copied" in run("copy_file", source=str(target), destination=str(SANDBOX / "copy.txt")))
check("...and both exist", target.exists() and (SANDBOX / "copy.txt").exists())
check("copy_file refuses to clobber",
      run("copy_file", source=str(target), destination=str(SANDBOX / "copy.txt")).startswith("Error"))

check("move_file works", "Moved" in run("move_file", source=str(SANDBOX / "copy.txt"),
                                        destination=str(SANDBOX / "moved.txt")))
check("...source is gone", not (SANDBOX / "copy.txt").exists())
check("...destination exists", (SANDBOX / "moved.txt").exists())
check("move_file refuses a missing source",
      run("move_file", source=str(SANDBOX / "ghost"), destination=str(SANDBOX / "x")).startswith("Error"))


# --- deletion ---------------------------------------------------------------

print("\n[deletion]")
doomed = SANDBOX / "doomed.txt"
doomed.write_text("bye", encoding="utf-8")
out = run("delete_file", path=str(doomed))
check("delete_file uses the Recycle Bin", "Recycle Bin" in out, out[:80])
check("...and the file is gone from disk", not doomed.exists())
check("delete_file refuses a missing path", run("delete_file", path=str(SANDBOX / "ghost")).startswith("Error"))
check("delete_file refuses Bantu's own config",
      run("delete_file", path=str(user_data_dir() / "config.json")).startswith("Error"))

# Match call syntax, not the words in prose - the docstring mentions "unlink"
# precisely because the code refuses to use it.
import re as _re
_code = _re.sub(r'(?s)""".*?"""', "", Path(files.__file__).read_text(encoding="utf-8"))
_hard = [c for c in ("os.remove(", "os.unlink(", "shutil.rmtree(", ".unlink(") if c in _code]
check("no hard delete is ever called in the file tools", not _hard, str(_hard))


# --- archives ---------------------------------------------------------------

print("\n[archives]")
arc = SANDBOX / "bundle.zip"
out = run("compress", paths=str(SANDBOX / "docs"), archive_path=str(arc))
check("compress creates a zip", arc.exists() and "Created" in out, out[:80])
check("...containing the files", len(zipfile.ZipFile(arc).namelist()) == 2)
check("compress refuses to clobber",
      run("compress", paths=str(SANDBOX / "docs"), archive_path=str(arc)).startswith("Error"))

out = run("extract", archive_path=str(arc), destination=str(SANDBOX / "unzipped"))
check("extract works", "Extracted" in out and (SANDBOX / "unzipped").is_dir(), out[:80])

evil = SANDBOX / "evil.zip"
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../escaped.txt", "pwned")
check("extract refuses a zip-slip path",
      run("extract", archive_path=str(evil), destination=str(SANDBOX / "safe")).startswith("Error"))
check("...and nothing escaped", not (SANDBOX.parent / "escaped.txt").exists())


# --- organize ---------------------------------------------------------------

print("\n[organize]")
messy = SANDBOX / "messy"
messy.mkdir()
for n in ("a.jpg", "b.png", "c.pdf", "d.mp3", "e.weirdext"):
    (messy / n).write_text("x", encoding="utf-8")

out = run("organize_folder", path=str(messy))
check("organize defaults to a dry run", "Nothing moved yet" in out, out[:120])
check("...and nothing actually moved", (messy / "a.jpg").exists())
check("...but it shows the plan", "Images" in out and "Documents" in out, out[:200])

out = run("organize_folder", path=str(messy), dry_run=False)
check("organize moves when asked", "Moved 5" in out, out[:80])
check("...images went to Images", (messy / "Images" / "a.jpg").exists())
check("...audio went to Audio", (messy / "Audio" / "d.mp3").exists())
check("...unknown types went to Other", (messy / "Other" / "e.weirdext").exists())
check("organize rejects an unknown strategy",
      run("organize_folder", path=str(messy), by="colour").startswith("Error"))


# --- registry wiring --------------------------------------------------------

print("\n[registry wiring]")
tiers = {n: t.tier for n, t in reg.tools.items()}
readonly = {"search_files", "read_file", "read_document", "list_directory", "file_info", "disk_usage"}
mutating = {"write_file", "edit_file", "move_file", "copy_file", "delete_file",
            "create_folder", "organize_folder", "compress", "extract"}
check("14 file tools registered", len(reg.tools) == 15, str(len(reg.tools)))
check("every read-only tool is AUTO", all(tiers[n] is Tier.AUTO for n in readonly),
      str([n for n in readonly if tiers[n] is not Tier.AUTO]))
check("every mutating tool needs confirmation", all(tiers[n] is Tier.CONFIRM for n in mutating),
      str([n for n in mutating if tiers[n] is not Tier.CONFIRM]))
check("all tools carry the files category",
      all(t.category == "files" for t in reg.tools.values()))
check("every tool has a description for the model",
      all(t.description for t in reg.tools.values()))
check("optional args are nullable for Groq",
      reg.tools["search_files"].parameters["properties"]["root"]["type"] == ["string", "null"],
      str(reg.tools["search_files"].parameters["properties"]["root"]))

no_confirm = reg.execute(ToolCall("x", "delete_file", {"path": str(SANDBOX / "notes.txt")}))
check("a mutating tool will not run without a confirmation route", "needs confirmation" in no_confirm)
check("...and the file survived", (SANDBOX / "notes.txt").exists())

import shutil as _sh
_sh.rmtree(SANDBOX, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
