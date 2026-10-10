"""Find and open the user's own files (feature flag `files`).

- Only the folders in config.yaml `files:` are searched (Desktop, Documents, Downloads... by default).
- Only file *names* are read, never contents. Names that look private (passwords, keys, 2FA codes,
  ID cards) are never listed or opened.
- open_file only opens something a search found in those folders: the model can never hand it a path.
- Programs, scripts and shortcuts are never opened; JARVIS can show them in their folder instead.
- File names come from anywhere (downloads keep the web's names), so results are untrusted content.
"""

from __future__ import annotations

import fnmatch
import functools
import logging
import os
import re
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from core.registry import Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

# Replaced in tests.
_launch = getattr(os, "startfile", None)

KNOWN_FOLDERS = {  # Windows known-folder ids: they follow OneDrive and moved folders
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}

KINDS: dict[str, set[str] | None] = {
    "any": None,
    "document": {"pdf", "doc", "docx", "odt", "rtf", "txt", "md"},
    "pdf": {"pdf"},
    "spreadsheet": {"xls", "xlsx", "xlsm", "csv", "ods"},
    "presentation": {"ppt", "pptx", "odp"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "svg"},
    "video": {"mp4", "mkv", "mov", "avi", "wmv", "webm", "m4v"},
    "audio": {"mp3", "wav", "m4a", "flac", "aac", "ogg", "wma"},
    "archive": {"zip", "rar", "7z", "tar", "gz"},
    "folder": set(),
}
KIND_NAMES = {"pdf": "PDF", "doc": "Word document", "docx": "Word document", "odt": "document", "rtf": "document",
              "txt": "text file", "md": "Markdown file", "xls": "Excel sheet", "xlsx": "Excel sheet",
              "xlsm": "Excel sheet", "csv": "CSV sheet", "ppt": "PowerPoint", "pptx": "PowerPoint",
              "jpg": "photo", "jpeg": "photo", "heic": "photo", "png": "image", "gif": "GIF", "webp": "image",
              "mp4": "video", "mkv": "video", "mov": "video", "mp3": "song", "m4a": "audio file",
              "wav": "audio file", "zip": "zip file", "rar": "archive", "7z": "archive", "html": "web page",
              "json": "JSON file", "exe": "program", "msi": "installer", "lnk": "shortcut"}

# Opening these runs code, so JARVIS never does it (it can still show them in their folder).
NEVER_OPEN = {
    "exe", "msi", "msp", "bat", "cmd", "com", "scr", "pif", "ps1", "psm1", "psd1", "vbs", "vbe", "js", "jse",
    "wsf", "wsh", "hta", "lnk", "url", "reg", "cpl", "msc", "jar", "py", "pyw", "pyz", "dll", "sys", "drv",
    "ocx", "inf", "application", "appx", "appxbundle", "msix", "msixbundle", "appref-ms", "gadget", "scf",
    "iso", "img", "vhd", "vhdx", "xll", "sh", "ahk", "chm", "library-ms", "settingcontent-ms", "diagcab",
}

# Never listed or opened: names that suggest secrets, sign-in codes, keys or ID documents.
# Always applied; config `files.also_hide` adds more.
HIDDEN_NAMES = [
    "*password*", "*passwd*", "*credential*", "*secret*", "*recovery*code*", "*backup*code*", "*2fa*",
    "*seed*phrase*", "*private*key*", "*api*key*", "*token*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk",
    "*.kdbx", "*.kdb", "*.keystore", "*.jks", "*.ovpn", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".env", "*.env",
    "wallet.dat", "*aadhaar*", "*aadhar*", "*pan*card*", "*passport*", "*driving*licen*", "*voter*id*",
    "*credit*card*", "*debit*card*", "*cvv*",
]
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages", "appdata", "system volume information",
             "windows", "program files", "program files (x86)", "programdata"}
STOPWORDS = {"my", "the", "a", "an", "file", "files", "called", "named", "of", "for", "in", "on", "from", "and",
             "to", "that", "this", "please", "open", "find", "show", "me", "one", "latest", "recent"}
HIDDEN_ATTRS = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
INDEX_TTL_S = 600  # rebuilt in the background when older; top levels are always re-read
SCAN_LIMIT = 200_000
SCAN_TIME_S = 15.0


# ---------------------------------------------------------------- pure helpers (tested directly)

def tokens(text: str) -> list[str]:
    """Words of a file name or query: accents dropped, camelCase and letters/digits split, lower case."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def query_tokens(query: str) -> list[str]:
    return [t for t in tokens(query) if t not in STOPWORDS]


@functools.lru_cache(maxsize=8)
def _hidden_pattern(extra: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(fnmatch.translate(p.lower()) for p in [*HIDDEN_NAMES, *extra]))


def is_hidden_name(name: str, extra: list[str] = ()) -> bool:
    return _hidden_pattern(tuple(extra)).match(name.lower()) is not None


def extension(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""


@dataclass(frozen=True)
class Entry:
    location: str  # full path (a str: making 50,000 Path objects slows the scan down)
    root: str  # the searched folder it's in, e.g. "Downloads"
    rel_dir: str  # its folder inside that root ("" = the root itself)
    mtime: float
    is_dir: bool
    words: tuple[str, ...] = field(compare=False)
    is_root: bool = False  # one of the searched folders itself

    @property
    def path(self) -> Path:
        return Path(self.location)

    @property
    def name(self) -> str:
        return os.path.basename(self.location.rstrip("\\/")) or self.location

    @property
    def text(self) -> str:
        return " ".join(self.words)

    @property
    def ext(self) -> str:
        return "" if self.is_dir else extension(self.name)


def make_entry(path: str | Path, root: str, rel_dir: str, mtime: float, is_dir: bool, is_root: bool = False) -> Entry:
    path = str(path)
    words = tokens(root) if is_root else tokens(os.path.basename(path))  # "Documents" whatever the folder's name
    return Entry(path, root, rel_dir, mtime, is_dir, tuple(words), is_root)


def _token_score(q: str, words: tuple[str, ...], fuzzy: bool) -> float:
    best = 0.0
    for w in words:
        if w == q:
            return 3.0
        if len(q) >= 2 and w.startswith(q):
            best = max(best, 2.0)
        elif len(q) >= 3 and q in w:
            best = max(best, 1.5)
        elif (fuzzy and best < 1 and len(q) >= 4 and w[0] == q[0] and abs(len(w) - len(q)) <= 2
              and SequenceMatcher(None, q, w).ratio() >= 0.8):
            best = 1.0  # a small typo or mis-hearing: "resumee", "invioce"
    return best


def score(query: list[str], entry: Entry, fuzzy: bool = True) -> tuple[float, int]:
    """(score, how many query words were missing). Higher is better."""
    if not query:
        return 0.0, 0
    parts = [_token_score(q, entry.words, fuzzy) for q in query]
    missing = sum(1 for p in parts if p == 0)
    total = sum(parts)
    if not missing:
        stem = entry.words
        if entry.ext and not entry.is_root:
            stem = stem[:len(stem) - len(tokens(entry.ext))]  # "resume.pdf" -> ["resume"]
        if list(stem) == query:
            total += 2.0  # the whole name, exactly
    return total, missing


def search(entries: list[Entry], query: str = "", kind: str = "any", within_days: float = 0, folder: str = "",
           now: float | None = None, limit: int = 5) -> list[Entry]:
    """Best matches first; with no query, the newest files first."""
    now = time.time() if now is None else now
    exts = KINDS.get(kind or "any")
    folder = (folder or "").strip().lower()
    candidates = []
    for e in entries:
        if kind == "folder" and not e.is_dir:
            continue
        if exts and (e.is_dir or e.ext not in exts):
            continue
        if kind in {"any", ""} and e.is_dir and not query:
            continue  # "what did I download today" means files
        if folder and e.root.lower() != folder:
            continue
        if within_days and now - e.mtime > within_days * 86400:
            continue
        candidates.append(e)
    words = query_tokens(query)
    if not words:
        return sorted(candidates, key=lambda e: -e.mtime)[:limit]
    for fuzzy in (False, True):  # typo-tolerant matching is slow, so only when plain matching finds nothing
        pool = candidates
        if not fuzzy:  # quick pre-filter: plain matches contain the word somewhere in the name
            need = len(words) - (1 if len(words) >= 3 else 0)
            pool = [e for e in candidates if sum(q in e.text for q in words) >= need]
        scored = [(*score(words, e, fuzzy), e) for e in pool]
        for allowed_missing in (0, 1) if len(words) >= 3 else (0,):  # every word, else all but one of 3+
            hits = [(s, e) for s, missing, e in scored if missing <= allowed_missing and s > 0]
            if hits:
                hits.sort(key=lambda h: (-h[0], -h[1].mtime))
                return [e for _, e in hits[:limit]]
    return []


def clear_winner(query: str, found: list[Entry]) -> Entry | None:
    """The one result to open without asking: the only match, or clearly the best."""
    if len(found) == 1:
        return found[0]
    if len(found) >= 2:
        words = query_tokens(query)
        first, second = score(words, found[0])[0], score(words, found[1])[0]
        if first >= second + 2:
            return found[0]
    return None


def spoken_age(mtime: float, now: datetime | None = None) -> str:
    now = now or datetime.now()
    then = datetime.fromtimestamp(mtime)
    days = (now.date() - then.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if then.year == now.year:
        return f"on {then.day} {then:%B}"
    return f"in {then:%B %Y}"


def describe(entry: Entry, now: datetime | None = None) -> str:
    where = entry.root + (f"\\{entry.rel_dir}" if entry.rel_dir else "")
    if entry.is_root:
        return f"your {entry.root} folder"
    if entry.is_dir:
        return f'the folder "{entry.name[:120]}" in {where}'
    kind = KIND_NAMES.get(entry.ext, f"{entry.ext} file" if entry.ext else "file")
    stem = entry.name[:-len(entry.ext) - 1] if entry.ext else entry.name  # the type is said in words instead
    return f'"{stem[:120]}" ({kind}) in {where}, changed {spoken_age(entry.mtime, now)}'


def result_list(found: list[Entry], now: datetime | None = None) -> str:
    return " ".join(f"{i}. {describe(e, now)}." for i, e in enumerate(found, 1))


# ---------------------------------------------------------------- Windows folders and scanning

def known_folder(name: str) -> Path | None:
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    guid = KNOWN_FOLDERS.get(name.lower())
    if guid is None:
        return None
    g = GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
    p = ctypes.c_void_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return Path(ctypes.wstring_at(p.value))
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


def resolve_roots(cfg: dict) -> dict[str, Path]:
    """Friendly name -> folder, for the configured known folders and extra folders that exist."""
    roots: dict[str, Path] = {}
    for name in cfg.get("folders") or []:
        path = known_folder(str(name))
        if path is not None and path.is_dir():
            roots[str(name).capitalize()] = path
        elif path is None:
            log.warning("files.folders: unknown folder %r", name)
    for extra in cfg.get("extra_folders") or []:
        path = Path(os.path.expandvars(str(extra))).expanduser()
        if path.is_dir():
            roots.setdefault(path.name or str(path), path)
        else:
            log.warning("files.extra_folders: %s doesn't exist", path)
    return roots


def _skip_dir(name: str) -> bool:
    return name.startswith((".", "$", "~")) or name.lower() in SKIP_DIRS


def list_folder(folder: str, root_name: str, rel_dir: str, hidden: re.Pattern) -> tuple[list[Entry], list[str]]:
    """The visible files and folders directly inside one folder, and the sub-folders to look in next."""
    try:
        with os.scandir(folder) as it:
            items = list(it)
    except OSError:
        return [], []  # no access
    entries, subdirs = [], []
    for item in items:
        try:
            if item.is_symlink() or item.is_junction():
                continue  # "My Music" links and the like: never follow
            st = item.stat(follow_symlinks=False)
        except OSError:
            continue
        if getattr(st, "st_file_attributes", 0) & HIDDEN_ATTRS or hidden.match(item.name.lower()):
            continue
        is_dir = item.is_dir(follow_symlinks=False)
        if is_dir and _skip_dir(item.name):
            continue
        entries.append(make_entry(item.path, root_name, rel_dir, st.st_mtime, is_dir))
        if is_dir:
            subdirs.append(item.path)
    return entries, subdirs


def scan(roots: dict[str, Path], also_hide: list[str] = (), limit: int = SCAN_LIMIT,
         time_limit_s: float = SCAN_TIME_S) -> tuple[list[Entry], bool]:
    """Every visible file and folder under the roots. (entries, finished) - huge trees stop early."""
    entries: list[Entry] = []
    hidden = _hidden_pattern(tuple(also_hide))
    seen: set[str] = set()
    deadline = time.monotonic() + time_limit_s
    for root_name, root in roots.items():
        entries.append(make_entry(root, root_name, "", _mtime(root), True, is_root=True))
        stack = [str(root)]
        while stack:
            folder = stack.pop()
            key = os.path.normcase(folder)
            if key in seen:
                continue  # an extra folder inside Documents, say
            seen.add(key)
            if len(entries) >= limit or time.monotonic() > deadline:
                return entries, False
            rel = os.path.relpath(folder, root)
            found, subdirs = list_folder(folder, root_name, "" if rel == "." else rel, hidden)
            entries.extend(found)
            stack.extend(subdirs)
    return entries, True


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def reveal_in_explorer(path: Path) -> None:
    """Open Explorer at the file's folder with the file selected."""
    import ctypes

    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize(None)  # tools run in a worker thread
    try:
        pidl = shell32.ILCreateFromPathW(str(path))
        if not pidl:
            raise ToolError("I couldn't find that file any more.")
        try:
            if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) != 0:
                raise ToolError("Windows couldn't open that folder.")
        finally:
            shell32.ILFree(pidl)
    finally:
        ole32.CoUninitialize()


_reveal = reveal_in_explorer  # replaced in tests


# ---------------------------------------------------------------- tools

class FileTools:
    def __init__(self, config: dict) -> None:
        cfg = config.get("files") or {}
        self._cfg = cfg
        self.also_hide = [str(p) for p in cfg.get("also_hide") or []]
        self.max_results = int(cfg.get("max_results", 5))
        self._roots: dict[str, Path] | None = None
        self._entries: list[Entry] = []
        self._complete = True
        self._scanned_at = 0.0
        self._scan_lock = threading.Lock()
        self.last: list[Entry] = []  # the last results shown, for "open number two"

    def roots(self) -> dict[str, Path]:
        if self._roots is None:
            self._roots = resolve_roots(self._cfg)
        return self._roots

    def warm(self) -> None:
        """Build the index in the background when JARVIS starts, so the first search is quick."""
        threading.Thread(target=self._refresh, name="file-index", daemon=True).start()

    def _refresh(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return  # already scanning
        try:
            t0 = time.monotonic()
            entries, complete = scan(self.roots(), self.also_hide)
            self._entries, self._complete, self._scanned_at = entries, complete, time.monotonic()
            log.info("file index: %d items in %.1fs%s", len(entries), self._scanned_at - t0,
                     "" if complete else " (stopped early)")
        except Exception:
            log.exception("file index failed")
        finally:
            self._scan_lock.release()

    def entries(self) -> list[Entry]:
        """The index, plus a fresh look at the top of each folder (where new downloads land)."""
        if not self._scanned_at:
            with self._scan_lock:
                pass  # a warm-up scan is running: wait for it
            if not self._scanned_at:
                self._refresh()
        elif time.monotonic() - self._scanned_at > INDEX_TTL_S:
            self.warm()  # use the old index now; the new one is ready for next time
        hidden = _hidden_pattern(tuple(self.also_hide))
        fresh: list[Entry] = []
        for name, root in self.roots().items():
            fresh.extend(list_folder(str(root), name, "", hidden)[0])
        return [*fresh, *(e for e in self._entries if e.rel_dir or e.is_root)]  # top levels: only the fresh look

    def _find(self, query: str, kind: str, within_days: float, folder: str) -> list[Entry]:
        if kind not in KINDS:
            raise ToolError(f"I can look for these kinds of file: {', '.join(KINDS)}.")
        if folder and folder.lower() not in {r.lower() for r in self.roots()}:
            raise ToolError(f"I only look in {', '.join(self.roots())}.")
        return search(self.entries(), query, kind, within_days, folder, limit=self.max_results)

    def _none_found(self, query: str) -> str:
        where = ", ".join(self.roots()) or "no folders (none are set up)"
        partial = "" if self._complete else " There are a lot of files, so I may not have seen them all."
        what = f'matching "{query}"' if query else "like that"
        return f"I couldn't find any files {what} in {where}.{partial}"

    def find_files(self, query: str = "", kind: str = "any", modified_within_days: float = 0,
                   folder: str = "") -> str:
        """Search file and folder names in the configured folders. Rung 2 (local file system).
        Tier SAFE, read-only; the names are untrusted content."""
        found = self._find(query, kind, float(modified_within_days or 0), folder)
        if not found:
            return self._none_found(query)
        self.last = found
        return (f"Found {len(found)}{' (the best matches)' if len(found) >= self.max_results else ''}: "
                f"{result_list(found)} To open one, use open_file with its number.")

    def _resolve(self, number: int = 0, query: str = "", kind: str = "any") -> Entry | list[Entry] | str:
        if number:
            if not 1 <= int(number) <= len(self.last):
                return "I don't have a result with that number. Search again first."
            return self.last[int(number) - 1]
        if not query.strip():
            return "Which file? Tell me part of its name."
        found = self._find(query, kind, 0, "")
        if not found:
            return self._none_found(query)
        winner = clear_winner(query, found)
        if winner is None:
            self.last = found
            return found
        return winner

    def open_file(self, number: int = 0, query: str = "", kind: str = "any", show_in_folder: bool = False) -> str:
        """Open a file or folder the search found, with its usual app, or show it in Explorer.
        Rung 2 (ShellExecute). Tier SAFE (CONFIRM after untrusted content); programs are never opened."""
        target = self._resolve(int(number or 0), query, kind)
        if isinstance(target, str):
            return target
        if isinstance(target, list):
            return (f"I found {len(target)} that could be it: {result_list(target)} "
                    "Ask the user which one, then use open_file with its number.")
        path = target.path
        if not path.exists() or is_hidden_name(path.name, self.also_hide):
            return "That file isn't there any more."
        if show_in_folder:
            if target.is_root:
                _launch(str(path))
            else:
                _reveal(path)
            return f"Showing {target.name} in File Explorer."
        if not target.is_dir and target.ext in NEVER_OPEN:
            return (f"{target.name} is a program, script or shortcut, so I won't open it. "
                    "I can show it in its folder instead.")
        _launch(str(path))
        log.info("opened %s", path)
        return f"Opened {describe(target)}."

    def summarize_open(self, args: dict[str, Any]) -> str:
        verb = "Show in File Explorer" if args.get("show_in_folder") else "Open"
        number = int(args.get("number") or 0)
        if number and 1 <= number <= len(self.last):
            return f"{verb} {describe(self.last[number - 1])}"
        return f'{verb} the file matching "{str(args.get("query", ""))[:80]}"'


def register(registry: Registry, config: dict, services=None) -> None:
    tools = FileTools(config)
    if services is not None:
        services.on_start.append(tools.warm)
    folder_names = [str(f).capitalize() for f in (config.get("files") or {}).get("folders") or []]
    kind_param = {"type": "string", "enum": list(KINDS), "description": "Type of file; 'any' if not said."}
    registry.register(Tool(
        name="find_files",
        description=("Search the user's files and folders by name (" + ", ".join(folder_names or ["their folders"])
                     + "), e.g. 'find my resume', 'what did I download today'. Names only, never contents. "
                     "Read the results briefly; don't spell out file extensions unless it helps."),
        func=tools.find_files,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,  # file names come from anywhere, e.g. downloads
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words from the file's name only (empty for 'recent files')."},
                "kind": kind_param,
                "modified_within_days": {"type": "number", "description": "Only files changed in the last N days "
                                         "(1 = today and yesterday). 0 for any time."},
                "folder": {"type": "string", "description": "Only this folder, e.g. 'Downloads'. Empty for all."},
            },
        },
    ))
    registry.register(Tool(
        name="open_file",
        description=("Open one of the user's files or folders with its usual app (e.g. 'open my resume', "
                     "'open the second one', 'open my Downloads folder'), or show it in File Explorer. "
                     "Give the number from find_files, or words from its name to search and open in one go. "
                     "If several match, it lists them: ask which one."),
        func=tools.open_file,
        tier=Tier.SAFE,
        untrusted_output=True,
        parameters={
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Result number from the last search (1 = first)."},
                "query": {"type": "string", "description": "Words from the name, when there's no number."},
                "kind": kind_param,
                "show_in_folder": {"type": "boolean", "description": "Show it in File Explorer instead of opening it."},
            },
        },
        summarize=tools.summarize_open,
    ))
