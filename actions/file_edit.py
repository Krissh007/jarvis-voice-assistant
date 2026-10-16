"""Create, read, change and delete the user's own text files (feature flag `file_edit`).

- Only files inside the folders in config.yaml `file_edit:` (Desktop, Documents, Downloads by default)
  can be touched, never JARVIS's own folder. Paths are checked after resolving links, so `..` or a
  junction can't step outside.
- Names that look private (passwords, keys, 2FA codes, ID cards, .env) are never read, written or
  deleted, and nothing is written that looks like a password, key or card number.
- Programs, scripts Windows runs on a double-click (.bat, .ps1, .vbs...) and shortcuts are never written.
- Every change keeps the previous version in data/file_backups; deleting moves the file to the Recycle
  Bin and always asks first.
- File contents come from anywhere (downloads), so read_file's result is untrusted. After untrusted
  text, changing a file other than the one just read asks first.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from actions.files import extension, is_hidden_name, resolve_roots
from core.privacy import hide_secrets, secret_in_text
from core.registry import APPROVED_STEP, NeedsConfirmation, Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
# Windows runs these on a double-click (or they point somewhere), so JARVIS never writes them.
NEVER_WRITE = {
    "exe", "msi", "msp", "bat", "cmd", "com", "scr", "pif", "ps1", "psm1", "psd1", "vbs", "vbe", "jse", "wsf",
    "wsh", "hta", "lnk", "url", "reg", "cpl", "msc", "jar", "dll", "sys", "drv", "ocx", "inf", "scf",
    "application", "appref-ms", "library-ms", "settingcontent-ms", "diagcab", "gadget", "xll", "iso", "vhd", "vhdx",
}
RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
MAX_KB = 200
READ_CHARS = 30000  # the most of one file the model is shown
KEEP_BACKUPS = 200
CODE_EXE = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))

# Replaced in tests.
_launch = getattr(os, "startfile", None)


def _to_recycle_bin(path: Path) -> None:
    """Move one file to the Recycle Bin (it can be restored from there)."""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or path.exists():
        raise ToolError(f"Windows couldn't move {path.name} to the Recycle Bin (error {code}).")


recycle = _to_recycle_bin  # replaced in tests


def count_lines(text: str) -> int:
    return len(text.splitlines()) or (1 if text else 0)


def size_words(text: str) -> str:
    lines = count_lines(text)
    return f"{lines} line{'s' if lines != 1 else ''}, {len(text):,} characters"


class FileEditor:
    def __init__(self, config: dict, services=None) -> None:
        cfg = config.get("file_edit") or {}
        self._cfg = {"folders": cfg.get("folders", ["desktop", "documents", "downloads"]),
                     "extra_folders": cfg.get("extra_folders") or []}
        self.also_hide = [str(p) for p in (config.get("files") or {}).get("also_hide") or []]
        self.max_bytes = int(cfg.get("max_kb", MAX_KB)) * 1024
        self.backups = bool(cfg.get("backups", True))
        self.editor = str(cfg.get("editor", "code") or "")
        self.services = services
        data_dir = Path(getattr(services, "data_dir", PROJECT_DIR / "data"))
        self.backup_dir = data_dir / "file_backups"
        self._roots: dict[str, Path] | None = None
        self.folder: Path | None = None  # the folder last worked in: bare names like "index.html" go here
        self.last_read: Path | None = None  # changing the file just read doesn't ask, even after untrusted text

    # ------------------------------------------------------------ where

    def roots(self) -> dict[str, Path]:
        if self._roots is None:
            self._roots = {name: path.resolve() for name, path in resolve_roots(self._cfg).items()}
        return self._roots

    def where(self, path: Path) -> str:
        """'Downloads' or 'Downloads\\portfolio': how to say where a file is."""
        for name, root in self.roots().items():
            if path.parent == root:
                return name
            if path.parent.is_relative_to(root):
                return f"{name}\\{path.parent.relative_to(root)}"
        return str(path.parent)

    def _find_by_name(self, name: str) -> list[Path]:
        tools = getattr(self.services, "files", None)
        if tools is None:
            return []
        try:
            return [e.path for e in tools.entries() if not e.is_dir and e.name.lower() == name.lower()]
        except Exception:
            log.exception("couldn't search for %s", name)
            return []

    def resolve(self, path: str, must_exist: bool | None = None) -> Path:
        """The file `path` means, checked to be inside an allowed folder and not private."""
        raw = str(path or "").strip().strip('"').strip()
        if not raw:
            raise ToolError("Which file? Give its name and folder, e.g. Downloads/portfolio/index.html.")
        raw = os.path.expandvars(raw).replace("/", "\\")
        drive, rest_of_path = os.path.splitdrive(raw)
        for part in rest_of_path.split("\\"):  # before Windows tidies the name ("a.txt." becomes "a.txt")
            if part and part not in {".", ".."} and (":" in part or RESERVED.match(part) or part != part.rstrip(" .")
                                                     or part != part.lstrip(" ")):
                raise ToolError(f"{part} isn't a name I can use for a file.")
        roots = self.roots()
        if not roots:
            raise ToolError("I don't have any folders I'm allowed to change files in (config file_edit.folders).")
        given = Path(raw).expanduser()
        if given.is_absolute():
            target = given
        else:
            first, _, rest = raw.partition("\\")
            root = next((r for n, r in roots.items() if n.lower() == first.strip().lower()), None)
            if root is not None:
                if not rest.strip("\\"):
                    raise ToolError(f"{first} is a folder. Which file in it?")
                target = root / rest
            elif self.folder is not None:
                target = self.folder / raw
            elif "\\" not in raw:
                found = [p for p in self._find_by_name(raw) if self._allowed(p.resolve())]
                if len(found) == 1:
                    target = found[0]
                elif len(found) > 1:
                    places = "; ".join(self.where(p.resolve()) for p in found[:5])
                    raise ToolError(f"There's a {raw} in more than one folder ({places}). Which one?")
                else:
                    raise ToolError(f"Which folder should {raw} be in? Say it like Downloads/{raw} "
                                    f"(I can use {', '.join(roots)}).")
            else:
                raise ToolError(f"Which folder is that in? Start the path with one of {', '.join(roots)}.")
        target = Path(os.path.abspath(target)).resolve()  # after links, so nothing can point outside
        self._check(target)
        if must_exist is True and not target.is_file():
            raise ToolError(f"There's no {target.name} in {self.where(target)}.")
        return target

    def _allowed(self, target: Path) -> bool:
        return (any(target.is_relative_to(r) for r in self.roots().values())
                and not target.is_relative_to(PROJECT_DIR))

    def _check(self, target: Path) -> None:
        if not self._allowed(target):
            raise ToolError(f"I can only change files in {', '.join(self.roots())}, so I won't touch that one.")
        root = next(r for r in self.roots().values() if target.is_relative_to(r))
        for part in target.relative_to(root).parts:
            if is_hidden_name(part, self.also_hide):
                raise ToolError("That looks like a private file (passwords, keys or ID documents), so I won't "
                                "touch it.")
        if target.exists():
            if target.is_dir():
                raise ToolError(f"{target.name} is a folder, not a file.")
            attrs = getattr(target.stat(), "st_file_attributes", 0)
            if attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                raise ToolError(f"{target.name} is a hidden or system file, so I won't touch it.")

    # ------------------------------------------------------------ reading and writing

    def _read(self, target: Path) -> tuple[str, str]:
        """(text with \\n line ends, the file's own line ending)."""
        if target.stat().st_size > self.max_bytes:
            raise ToolError(f"{target.name} is over {self.max_bytes // 1024} KB, too big for me to handle.")
        data = target.read_bytes()
        if b"\0" in data:
            raise ToolError(f"{target.name} isn't a text file.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ToolError(f"{target.name} isn't a text file I can read (not UTF-8).") from None
        newline = "\r\n" if "\r\n" in text else "\n"
        return text.replace("\r\n", "\n"), newline

    def _backup(self, target: Path) -> None:
        if not self.backups or not target.exists():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, self.backup_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{target.name}")
        old = sorted(self.backup_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        for p in old[:max(0, len(old) - KEEP_BACKUPS)]:
            p.unlink(missing_ok=True)

    def _write(self, target: Path, text: str, newline: str = "\n") -> None:
        if extension(target.name) in NEVER_WRITE:
            raise ToolError(f"{target.name} would be a program, script or shortcut Windows can run, so I won't "
                            "write it.")
        if kind := secret_in_text(text):
            raise ToolError(f"That text looks like it contains {kind}, so I won't write it to a file.")
        data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
        if len(data) > self.max_bytes:
            raise ToolError(f"That's over {self.max_bytes // 1024} KB, too big for one file.")
        self._backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.jarvis-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)  # all or nothing: an editor never sees half a file
        self.folder = target.parent

    def _tainted(self) -> bool:
        if APPROVED_STEP.get():
            return False
        return bool(getattr(getattr(self.services, "dispatcher", None), "tainted", False))

    def _gate(self, target: Path, summary: str, run) -> str | NeedsConfirmation:
        """After untrusted text, changing a file other than the one just read asks first."""
        if self._tainted() and target != self.last_read:
            async def resume() -> str:
                return run()
            return NeedsConfirmation(summary=summary[:300], resume=resume,
                                     step={"file": target.name, "why": ["untrusted"]})
        return run()

    def _open(self, target: Path) -> str:
        try:
            if self.editor == "code" and CODE_EXE.is_file():
                subprocess.Popen([str(CODE_EXE), "--reuse-window", str(target)], close_fds=True)
                return " and opened it in VS Code"
            if _launch is not None and extension(target.name) in {"txt", "md", "html", "htm", "css", "json", "csv"}:
                _launch(str(target))
                return " and opened it"
        except OSError:
            log.exception("couldn't open %s", target)
        return ""

    # ------------------------------------------------------------ tools

    def read_file(self, path: str) -> str:
        target = self.resolve(path, must_exist=True)
        text, _ = self._read(target)
        self.last_read, self.folder = target, target.parent
        shown, hidden = hide_secrets(text)
        cut = len(shown) > READ_CHARS
        shown = shown[:READ_CHARS]
        notes = (" Some private-looking text is shown as [hidden]." if hidden else "") + (
            f" Only the first {READ_CHARS:,} characters are shown." if cut else "")
        return (f"{target.name} in {self.where(target)} ({size_words(text)}).{notes} Don't read code aloud "
                f"unless asked.\n{shown}")

    def write_file(self, path: str, content: str, overwrite: bool = False, open: bool = False
                   ) -> str | NeedsConfirmation:
        target = self.resolve(path)
        existed = target.exists()
        if existed and not overwrite:
            raise ToolError(f"{target.name} already exists in {self.where(target)}. Use edit_file to change "
                            "part of it, or overwrite to replace all of it.")

        def run() -> str:
            newline = self._read(target)[1] if existed else "\n"
            self._write(target, content, newline)
            opened = self._open(target) if open else ""
            log.info("%s %s (%d characters)", "replaced" if existed else "created", target, len(content))
            verb = "Replaced everything in" if existed else "Created"
            return f"{verb} {target.name} in {self.where(target)} ({size_words(content)}){opened}."
        return self._gate(target, f"{'replace' if existed else 'create'} {target.name} in {self.where(target)}", run)

    def edit_file(self, path: str, find: str = "", replace: str = "", append: str = "", replace_all: bool = False
                  ) -> str | NeedsConfirmation:
        target = self.resolve(path, must_exist=True)
        text, newline = self._read(target)
        find, replace, append = (s.replace("\r\n", "\n") for s in (find, replace, append))
        if not find and not append:
            raise ToolError("Tell me what to change: the exact text to find and its replacement, or text to append.")
        new, changed = text, 0
        if find:
            count = new.count(find)
            if count == 0:
                raise ToolError(f"I couldn't find that text in {target.name}, so I changed nothing. read_file it "
                                "again and copy the text exactly.")
            if count > 1 and not replace_all:
                raise ToolError(f"That text is in {target.name} {count} times. Give more of it so it's unique, or "
                                "set replace_all.")
            new, changed = new.replace(find, replace), count
        if append:
            new = new + ("" if not new or new.endswith("\n") else "\n") + append

        def run() -> str:
            self._write(target, new, newline)
            self.last_read = target
            what = [f"replaced {changed} place{'s' if changed != 1 else ''}"] if changed else []
            what += ["added text at the end"] if append else []
            log.info("edited %s: %s", target, ", ".join(what))
            return (f"Changed {target.name} in {self.where(target)}: {' and '.join(what)} "
                    f"(now {size_words(new)}).")
        return self._gate(target, f"change {target.name} in {self.where(target)}", run)

    def delete_file(self, path: str) -> str:
        target = self.resolve(path, must_exist=True)
        recycle(target)
        log.info("moved %s to the Recycle Bin", target)
        if self.last_read == target:
            self.last_read = None
        return f"Moved {target.name} from {self.where(target)} to the Recycle Bin."

    def summarize_delete(self, args: dict[str, Any]) -> str:
        try:
            target = self.resolve(str(args.get("path", "")))
            return f"delete {target.name} from {self.where(target)} (to the Recycle Bin)"
        except ToolError:
            return f"delete {str(args.get('path', ''))[:120]} (to the Recycle Bin)"


def register(registry: Registry, config: dict, services=None) -> None:
    editor = FileEditor(config, services)
    folders = ", ".join(str(f).capitalize() for f in (config.get("file_edit") or {}).get(
        "folders", ["desktop", "documents", "downloads"]))
    path_param = {"type": "string", "description": "The file, starting with its folder, e.g. "
                  "Downloads/portfolio/index.html. A bare name means the folder last used."}
    where = f"Only files in {folders}. "
    registry.register(Tool(
        name="read_file",
        description=("Read one of the user's text files (code, notes, HTML, CSS...), e.g. to see what's in it "
                     "before changing it with edit_file. " + where + "Don't read code aloud unless asked."),
        func=editor.read_file,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,  # files come from anywhere, e.g. downloads
        parameters={"type": "object", "properties": {"path": path_param}, "required": ["path"]},
        summarize=lambda a: f"read {a.get('path', '')}",
    ))
    registry.register(Tool(
        name="write_file",
        description=("Create a new text file with this content, e.g. 'create index.html for my portfolio in "
                     "Downloads/portfolio'. Folders are made as needed. To replace all of an existing file, set "
                     "overwrite; to change part of one, use edit_file. open=true opens it in VS Code. Prefer "
                     "this over type_in_app for writing code files. " + where),
        func=editor.write_file,
        tier=Tier.SAFE,
        self_gated=True,  # asks itself after untrusted text
        parameters={
            "type": "object",
            "properties": {
                "path": path_param,
                "content": {"type": "string", "description": "The whole file, exactly."},
                "overwrite": {"type": "boolean", "description": "Replace an existing file (its old version is kept)."},
                "open": {"type": "boolean", "description": "Open it in VS Code afterwards."},
            },
            "required": ["path", "content"],
        },
        summarize=lambda a: f"{'replace' if a.get('overwrite') else 'create'} {a.get('path', '')}",
    ))
    registry.register(Tool(
        name="edit_file",
        description=("Change part of an existing text file: replace the exact text find with replace (read_file "
                     "first to copy it exactly), and/or add append at the end. e.g. 'change the heading in "
                     "index.html', 'add a footer style to style.css'. The old version is kept. " + where),
        func=editor.edit_file,
        tier=Tier.SAFE,
        self_gated=True,
        parameters={
            "type": "object",
            "properties": {
                "path": path_param,
                "find": {"type": "string", "description": "Exact text now in the file (unique unless replace_all)."},
                "replace": {"type": "string", "description": "What it becomes (empty = delete that text)."},
                "append": {"type": "string", "description": "Text to add at the end of the file."},
                "replace_all": {"type": "boolean", "description": "Replace every place find appears."},
            },
            "required": ["path"],
        },
        summarize=lambda a: f"change {a.get('path', '')}",
    ))
    registry.register(Tool(
        name="delete_file",
        description=("Delete one of the user's files by moving it to the Recycle Bin (always asks first), "
                     "e.g. 'delete script.js'. Files only, not folders. " + where),
        func=editor.delete_file,
        tier=Tier.CONFIRM,
        parameters={"type": "object", "properties": {"path": path_param}, "required": ["path"]},
        summarize=editor.summarize_delete,
    ))
