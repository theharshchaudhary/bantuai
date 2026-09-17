"""Filesystem tools.

Deletes go to the Recycle Bin, never a hard unlink — an agent that can
permanently destroy files on a misread instruction is not one you would leave
running. Every path goes through the guards in paths.py first.
"""

from __future__ import annotations

import datetime
import fnmatch
import os
import shutil
import zipfile
from pathlib import Path

from core.tools.registry import Tier, ToolError, ToolRegistry

from .paths import MAX_READ_BYTES, guard_read, guard_write, human_size, resolve

#: Folders never worth walking into during a search.
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "$RECYCLE.BIN", "System Volume Information", ".idea", ".vscode",
    "AppData", "Windows", "site-packages", ".cache", "dist-info",
}

TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".csv", ".log", ".html", ".css", ".xml", ".sh", ".bat",
    ".ps1", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".php", ".sql",
}

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff"},
    "Documents": {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".pptx", ".xlsx", ".csv"},
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "Installers": {".exe", ".msi"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".rb", ".html", ".css"},
}


def _walk(root: Path, recursive: bool = True):
    """Yield files, skipping noise directories and anything unreadable."""
    if not recursive:
        try:
            yield from (p for p in root.iterdir() if p.is_file())
        except (PermissionError, OSError):
            pass
        return
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            yield Path(dirpath) / f


def _stat_line(p: Path) -> str:
    try:
        st = p.stat()
        when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{p}  ({human_size(st.st_size)}, modified {when})"
    except OSError:
        return str(p)


def extract_document_text(p: Path, max_pages: int = 40) -> str:
    """Text from a PDF, DOCX or XLSX. Also used to index the knowledge folder."""
    suffix = p.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            pages = reader.pages[:max_pages]
            out = [f"[page {i + 1}]\n{pg.extract_text() or ''}" for i, pg in enumerate(pages)]
            extra = ""
            if len(reader.pages) > max_pages:
                extra = f"\n... [{len(reader.pages) - max_pages} more pages]"
            return ("\n\n".join(out) or "(no extractable text — it may be scanned images)") + extra
        if suffix == ".docx":
            import docx

            d = docx.Document(str(p))
            body = "\n".join(par.text for par in d.paragraphs if par.text.strip())
            tables = [
                " | ".join(c.text.strip() for c in row.cells)
                for t in d.tables
                for row in t.rows
            ]
            return (body + ("\n\n" + "\n".join(tables) if tables else "")) or "(empty document)"
        if suffix in (".xlsx", ".xlsm"):
            import openpyxl

            wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"[sheet: {ws.title}]")
                for row in ws.iter_rows(values_only=True):
                    if any(c is not None for c in row):
                        out.append(" | ".join("" if c is None else str(c) for c in row))
                    if len(out) > 400:
                        out.append("... [truncated]")
                        break
            wb.close()
            return "\n".join(out) or "(empty workbook)"
    except ImportError as e:
        raise ToolError(f"missing reader for {suffix}: {e}") from e
    except Exception as e:
        raise ToolError(f"could not parse {p.name}: {type(e).__name__}: {e}") from e
    raise ToolError(f"{suffix or 'this file'} is not a supported document; try read_file")


def register(reg: ToolRegistry) -> None:
    reg.describe_category("files", "find, read, write, move, copy, organise, zip and delete files; read PDF, Word and Excel documents; disk usage")

    # --- reading ------------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="files")
    def search_files(
        query: str,
        root: str = "",
        extensions: str = "",
        modified_after: str = "",
        search_contents: bool = False,
        limit: int = 40,
    ) -> str:
        """Find files by name, and optionally by their contents.

        Args:
            query: Text to match in the filename, or in the file if search_contents is true.
            root: Folder to search. Defaults to the user's home directory.
            extensions: Comma-separated filter such as 'pdf,docx'. Empty means any.
            modified_after: Only files changed since this ISO date, e.g. '2025-07-01'.
            search_contents: Also search inside text files. Slower.
            limit: Maximum results to return.
        """
        base = guard_read(root) if root else Path.home()
        if not base.is_dir():
            raise ToolError(f"{base} is not a folder")

        needle = query.lower().strip()
        exts = {
            ("." + e.strip().lstrip(".")).lower()
            for e in extensions.split(",")
            if e.strip()
        }
        cutoff = None
        if modified_after:
            try:
                cutoff = datetime.datetime.fromisoformat(modified_after).timestamp()
            except ValueError as e:
                raise ToolError(f"modified_after must be an ISO date like 2025-07-01: {e}") from e

        hits, scanned = [], 0
        for p in _walk(base):
            scanned += 1
            if scanned > 300_000:
                break
            if exts and p.suffix.lower() not in exts:
                continue
            try:
                if cutoff and p.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue

            matched = needle in p.name.lower()
            if not matched and search_contents and p.suffix.lower() in TEXT_SUFFIXES:
                try:
                    if p.stat().st_size < MAX_READ_BYTES:
                        matched = needle in p.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    pass
            if matched:
                hits.append(p)
                if len(hits) >= limit:
                    break

        if not hits:
            where = f" under {base}" if root else ""
            return f"No files matching {query!r}{where} (scanned {scanned:,})."
        head = f"{len(hits)} match(es) for {query!r} (scanned {scanned:,}):\n"
        return head + "\n".join(_stat_line(p) for p in hits)

    @reg.register(tier=Tier.AUTO, category="files")
    def read_file(path: str, max_bytes: int = MAX_READ_BYTES) -> str:
        """Read a text file. Use read_document for PDF, DOCX, XLSX or PPTX.

        Args:
            path: File to read.
            max_bytes: Stop after this many bytes.
        """
        p = guard_read(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        size = p.stat().st_size
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
        except OSError as e:
            raise ToolError(f"could not read {p.name}: {e}") from e
        if size > max_bytes:
            text += f"\n... [truncated; file is {human_size(size)}]"
        return text or "(the file is empty)"

    @reg.register(tier=Tier.AUTO, category="files")
    def read_document(path: str, max_pages: int = 40) -> str:
        """Extract text from a PDF, DOCX, XLSX or PPTX file.

        Args:
            path: Document to read.
            max_pages: For PDFs, stop after this many pages.
        """
        p = guard_read(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        return extract_document_text(p, max_pages)

    @reg.register(tier=Tier.AUTO, category="files")
    def list_directory(path: str = "", pattern: str = "", recursive: bool = False) -> str:
        """List what is in a folder.

        Args:
            path: Folder to list. Defaults to the user's home directory.
            pattern: Optional glob such as '*.pdf'.
            recursive: Include subfolders.
        """
        base = guard_read(path) if path else Path.home()
        if not base.is_dir():
            raise ToolError(f"{base} is not a folder")

        dirs, files = [], []
        if recursive:
            files = [p for p in _walk(base) if not pattern or fnmatch.fnmatch(p.name, pattern)]
        else:
            try:
                for p in sorted(base.iterdir()):
                    if p.is_dir():
                        dirs.append(p)
                    elif not pattern or fnmatch.fnmatch(p.name, pattern):
                        files.append(p)
            except PermissionError as e:
                raise ToolError(f"no permission to list {base}: {e}") from e

        lines = [f"{base}:"]
        lines += [f"  [dir]  {d.name}" for d in dirs[:100]]
        lines += [f"  {human_size(f.stat().st_size):>8}  {f.name if not recursive else f}"
                  for f in files[:200] if f.exists()]
        if not dirs and not files:
            lines.append("  (empty)")
        if len(files) > 200:
            lines.append(f"  ... and {len(files) - 200} more files")
        return "\n".join(lines)

    @reg.register(tier=Tier.AUTO, category="files")
    def file_info(path: str) -> str:
        """Size, timestamps and type of a file or folder.

        Args:
            path: What to inspect.
        """
        p = guard_read(path)
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        st = p.stat()
        fmt = "%Y-%m-%d %H:%M:%S"
        kind = "folder" if p.is_dir() else "file"
        out = [
            f"{p}",
            f"  type      {kind}{'' if p.is_dir() else ' (' + (p.suffix or 'no extension') + ')'}",
            f"  size      {human_size(st.st_size)}",
            f"  modified  {datetime.datetime.fromtimestamp(st.st_mtime).strftime(fmt)}",
            f"  created   {datetime.datetime.fromtimestamp(st.st_ctime).strftime(fmt)}",
        ]
        if p.is_dir():
            try:
                out.append(f"  contains  {sum(1 for _ in p.iterdir())} items")
            except OSError:
                pass
        return "\n".join(out)

    @reg.register(tier=Tier.AUTO, category="files")
    def disk_usage(path: str = "", top: int = 15) -> str:
        """Show free space and the largest subfolders — answers 'what is eating my disk'.

        Args:
            path: Folder or drive to measure. Defaults to the user's home directory.
            top: How many of the biggest subfolders to list.
        """
        base = guard_read(path) if path else Path.home()
        if not base.is_dir():
            raise ToolError(f"{base} is not a folder")
        total, used, free = shutil.disk_usage(str(base))
        lines = [
            f"Drive {Path(base).anchor}  {human_size(used)} used of {human_size(total)}, "
            f"{human_size(free)} free ({used / total * 100:.0f}% full)",
            f"\nLargest folders in {base}:",
        ]
        sizes: list[tuple[int, Path]] = []
        try:
            for child in base.iterdir():
                if not child.is_dir() or child.name in SKIP_DIRS:
                    continue
                n = 0
                for f in _walk(child):
                    try:
                        n += f.stat().st_size
                    except OSError:
                        pass
                sizes.append((n, child))
        except PermissionError:
            pass
        for n, child in sorted(sizes, reverse=True)[:top]:
            lines.append(f"  {human_size(n):>9}  {child.name}")
        return "\n".join(lines) if sizes else lines[0]

    # --- writing ------------------------------------------------------------

    @reg.register(tier=Tier.CONFIRM, category="files")
    def write_file(path: str, content: str, overwrite: bool = False) -> str:
        """Create a file, or replace one entirely.

        Args:
            path: File to write.
            content: Full text to write.
            overwrite: Must be true to replace a file that already exists.
        """
        p = guard_write(path)
        if p.exists() and not overwrite:
            raise ToolError(
                f"{p.name} already exists. Pass overwrite=true to replace it, "
                f"or use edit_file to change part of it."
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {human_size(len(content.encode()))} to {p}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def edit_file(path: str, find: str, replace: str, count: int = 0) -> str:
        """Replace text inside a file, leaving the rest untouched.

        Args:
            path: File to edit.
            find: Exact text to look for.
            replace: What to put in its place.
            count: Replace at most this many; 0 means all.
        """
        p = guard_write(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(find)
        if not n:
            raise ToolError(f"{find!r} does not appear in {p.name}")
        new = text.replace(find, replace, count) if count else text.replace(find, replace)
        p.write_text(new, encoding="utf-8")
        return f"Replaced {count or n} occurrence(s) in {p.name}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def move_file(source: str, destination: str) -> str:
        """Move or rename a file or folder.

        Args:
            source: What to move.
            destination: Where to move it, or its new name.
        """
        src, dst = guard_write(source), guard_write(destination)
        if not src.exists():
            raise ToolError(f"{src} does not exist")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"{dst} already exists; choose another name")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {src.name} to {dst}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def copy_file(source: str, destination: str) -> str:
        """Copy a file or folder.

        Args:
            source: What to copy.
            destination: Where to put the copy.
        """
        src, dst = guard_read(source), guard_write(destination)
        if not src.exists():
            raise ToolError(f"{src} does not exist")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"{dst} already exists; choose another name")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        return f"Copied {src.name} to {dst}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def delete_file(path: str) -> str:
        """Send a file or folder to the Recycle Bin, where it can be restored.

        Args:
            path: What to delete.
        """
        p = guard_write(path)
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        try:
            from send2trash import send2trash

            send2trash(str(p))
        except ImportError as e:
            # Never fall back to a hard delete: recoverability is the point.
            raise ToolError(f"send2trash is unavailable, so nothing was deleted: {e}") from e
        except Exception as e:
            raise ToolError(f"could not move {p.name} to the Recycle Bin: {e}") from e
        return f"Sent {p.name} to the Recycle Bin (restorable from there)."

    @reg.register(tier=Tier.CONFIRM, category="files")
    def create_folder(path: str) -> str:
        """Create a folder, including any missing parents.

        Args:
            path: Folder to create.
        """
        p = guard_write(path)
        if p.exists():
            return f"{p} already exists."
        p.mkdir(parents=True)
        return f"Created {p}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def organize_folder(path: str, by: str = "type", dry_run: bool = True) -> str:
        """Sort a folder's files into subfolders by type or date.

        Defaults to a dry run so the plan can be reviewed before anything moves.

        Args:
            path: Folder to tidy.
            by: Either 'type' (Images, Documents, ...) or 'date' (2026-09).
            dry_run: Preview without moving anything. Set false to actually move.
        """
        base = guard_write(path)
        if not base.is_dir():
            raise ToolError(f"{base} is not a folder")
        if by not in ("type", "date"):
            raise ToolError("by must be 'type' or 'date'")

        plan: list[tuple[Path, Path]] = []
        for f in base.iterdir():
            if not f.is_file():
                continue
            if by == "type":
                bucket = next(
                    (name for name, exts in CATEGORIES.items() if f.suffix.lower() in exts),
                    "Other",
                )
            else:
                bucket = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m")
            plan.append((f, base / bucket / f.name))

        if not plan:
            return f"No loose files in {base}."
        if dry_run:
            groups: dict[str, int] = {}
            for _, dst in plan:
                groups[dst.parent.name] = groups.get(dst.parent.name, 0) + 1
            summary = "\n".join(f"  {k}: {v} file(s)" for k, v in sorted(groups.items()))
            return (
                f"Plan for {base} ({len(plan)} files):\n{summary}\n\n"
                f"Nothing moved yet. Call again with dry_run=false to do it."
            )

        moved = 0
        for src, dst in plan:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.move(str(src), str(dst))
                    moved += 1
            except OSError:
                pass
        return f"Moved {moved} of {len(plan)} files into subfolders of {base}."

    @reg.register(tier=Tier.CONFIRM, category="files")
    def compress(paths: str, archive_path: str) -> str:
        """Zip one or more files or folders.

        Args:
            paths: Comma-separated list of files or folders to include.
            archive_path: The .zip file to create.
        """
        items = [guard_read(p.strip()) for p in paths.split(",") if p.strip()]
        if not items:
            raise ToolError("nothing to compress")
        out = guard_write(archive_path)
        if out.exists():
            raise ToolError(f"{out.name} already exists")
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for item in items:
                if not item.exists():
                    raise ToolError(f"{item} does not exist")
                if item.is_dir():
                    for f in _walk(item):
                        z.write(f, f.relative_to(item.parent))
                        n += 1
                else:
                    z.write(item, item.name)
                    n += 1
        return f"Created {out} with {n} file(s), {human_size(out.stat().st_size)}"

    @reg.register(tier=Tier.CONFIRM, category="files")
    def extract(archive_path: str, destination: str = "") -> str:
        """Unzip an archive.

        Args:
            archive_path: The .zip file to open.
            destination: Where to extract. Defaults to a folder beside the archive.
        """
        src = guard_read(archive_path)
        if not zipfile.is_zipfile(src):
            raise ToolError(f"{src.name} is not a zip file")
        dst = guard_write(destination) if destination else src.with_suffix("")
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            for name in names:
                # Refuse zip-slip: an entry that escapes the destination.
                target = (dst / name).resolve()
                if not str(target).startswith(str(dst.resolve())):
                    raise ToolError(f"archive contains an unsafe path: {name}")
            z.extractall(dst)
        return f"Extracted {len(names)} entries to {dst}"
