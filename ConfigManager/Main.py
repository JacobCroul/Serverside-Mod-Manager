"""
Serverside Config Manager
-------------------------
Browse and edit mod config files on your server share as a form, with the
comments from the files shown as hover tooltips.

Formats handled (comments and layout are preserved on save):
- .toml           via tomlkit (round-trip, keeps comments/whitespace)
- .json/.jsonc/.json5  via a tolerant scanner ("//" and "/* */" comments,
                  single quotes, unquoted keys, trailing commas)
- .snbt           same scanner, "#" comments, optional commas, numeric
                  suffixes (1b, 2.5d, 3L ...) kept intact

How edits work (non-TOML): only the text of values you actually changed is
replaced in the original file. Everything else stays byte-for-byte identical.

Tooltips come from comments directly above a key (and a same-line trailing
comment). A blank line detaches a comment block, so file headers don't leak
onto the first key.

Folders scanned per server: config/, defaultconfigs/, <level-name>/serverconfig/
(level-name is read from server.properties, default "world").

Requires: pip install customtkinter tomlkit
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk
import tomlkit
from tomlkit import items as tk_items
from tomlkit.container import Container

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
BACKUP_DIR = APP_DIR / "backups"
MODMANAGER_CONFIG = APP_DIR.parent / "ModManager" / "config.json"

CONFIG_EXTS = {
    ".toml": "toml",
    ".json": "json",
    ".jsonc": "json",
    ".json5": "json",
    ".snbt": "snbt",
}
MAX_ROWS = 300                    # widgets are heavy; filter to reach the rest
BIG_FILE_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# App config persistence (same pattern as the Mod Manager)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Text IO (keeps BOM and original line endings)
# ---------------------------------------------------------------------------

def read_text(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    return raw.decode("utf-8"), bom


def write_text(path: Path, text: str, bom: bool) -> None:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    # Written in place (not temp + rename) so the file keeps its owner and
    # permissions on the server side of the share.
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# Entry model shared by all formats
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    idx: int
    path: tuple
    kind: str                 # header | bool | int | float | string | raw | static
    value: object = None      # bool for bool, str for everything else
    comment: str = ""
    depth: int = 0
    suffix: str = ""          # SNBT numeric suffix, e.g. "b", "d", "L"
    quote: str = '"'
    bare: bool = False        # unquoted string in the source
    span: tuple = (0, 0)      # (start, end) of the value text, scanner formats only
    options: list = field(default_factory=list)
    lo: float | None = None
    hi: float | None = None

    @property
    def key(self) -> str:
        return str(self.path[-1]) if self.path else ""

    @property
    def dotted(self) -> str:
        return ".".join(str(p) for p in self.path)


RANGE_RE = re.compile(r"Range:\s*(-?[\d.]+)\s*~\s*(-?[\d.]+)", re.I)
ALLOWED_RE = re.compile(r"Allowed Values:\s*(.+)", re.I)


def add_hints(e: Entry) -> None:
    """Pull range / allowed-value hints out of the comment text, if present."""
    if e.kind in ("int", "float"):
        m = RANGE_RE.search(e.comment)
        if m:
            try:
                e.lo, e.hi = float(m.group(1)), float(m.group(2))
            except ValueError:
                pass
    elif e.kind == "string":
        m = ALLOWED_RE.search(e.comment)
        if m:
            opts = [o.strip() for o in m.group(1).split(",") if o.strip()]
            # only trust it when the current value is one of the options
            if len(opts) > 1 and str(e.value) in opts:
                e.options = opts


def clean_comment(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^(?:#+|/{2,})\s?", "", s)
    return s.rstrip()


# ---------------------------------------------------------------------------
# Scanner for JSON / JSONC / JSON5 / SNBT
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0"}
INT_RE = re.compile(r"^([+-]?\d+)([bBsSlL]?)$")
FLOAT_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([fFdD]?)$")
BARE_KEY_RE = re.compile(r"[^\s:=,{}\[\]\"']+")
BARE_VAL_RE = re.compile(r"[^\s,\]\}]+")
SAFE_BARE_RE = re.compile(r"[A-Za-z0-9_.+\-]+")


class RelaxedParser:
    """Records every scalar leaf with the exact span of its text.

    Objects become 'header' entries and are recursed into. Lists are kept as
    a single 'raw' entry (edited as text), which also covers SNBT typed
    arrays like [I; 1, 2, 3].
    """

    def __init__(self, text: str, hash_comments: bool = False):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.hash = hash_comments
        self.entries: list[Entry] = []
        self.pending: list[str] = []

    def parse(self) -> list[Entry]:
        self._skip()
        if self.i >= self.n or self.s[self.i] != "{":
            raise ParseError(
                "The top level of this file is not an object ({...}), "
                "so it can't be shown as a form."
            )
        self._object((), 0)
        for e in self.entries:
            add_hints(e)
        return self.entries

    # -- helpers -------------------------------------------------------------

    def _line(self) -> int:
        return self.s.count("\n", 0, self.i) + 1

    def _line_comment_at(self, j: int) -> bool:
        return self.s.startswith("#", j) if self.hash else self.s.startswith("//", j)

    def _add(self, path, kind, value, comment, depth, **kw) -> Entry:
        e = Entry(len(self.entries), path, kind, value, comment, depth, **kw)
        self.entries.append(e)
        return e

    def _skip(self, collect: bool = True) -> None:
        s, n = self.s, self.n
        nl = 0
        while self.i < n:
            c = s[self.i]
            if c == "\n":
                nl += 1
                if nl >= 2 and collect:
                    self.pending.clear()      # blank line detaches a comment block
                self.i += 1
            elif c in " \t\r\ufeff":
                self.i += 1
            elif self._line_comment_at(self.i):
                j = s.find("\n", self.i)
                j = n if j == -1 else j
                if collect:
                    self.pending.append(clean_comment(s[self.i:j]))
                self.i = j
                nl = 0
            elif not self.hash and s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j == -1:
                    raise ParseError(f"Unterminated comment on line {self._line()}")
                if collect:
                    for ln in s[self.i + 2:j].splitlines():
                        ln = ln.strip().lstrip("*").strip()
                        if ln:
                            self.pending.append(ln)
                self.i = j + 2
                nl = 0
            else:
                break

    def _string(self) -> tuple[str, str]:
        s = self.s
        q = s[self.i]
        self.i += 1
        out = []
        while self.i < self.n:
            c = s[self.i]
            if c == q:
                self.i += 1
                return "".join(out), q
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    break
                e = s[self.i]
                hexpart = s[self.i + 1:self.i + 5]
                if e == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", hexpart):
                    out.append(chr(int(hexpart, 16)))
                    self.i += 5
                    continue
                if e == "\n":                 # JSON5 line continuation
                    self.i += 1
                    continue
                out.append(ESCAPES.get(e, e))
                self.i += 1
                continue
            out.append(c)
            self.i += 1
        raise ParseError(f"Unterminated string on line {self._line()}")

    def _skip_balanced(self) -> None:
        s, n = self.s, self.n
        depth = 0
        while self.i < n:
            c = s[self.i]
            if c in "[{":
                depth += 1
                self.i += 1
            elif c in "]}":
                depth -= 1
                self.i += 1
                if depth == 0:
                    return
            elif c in "\"'":
                self._string()
            elif self._line_comment_at(self.i):
                j = s.find("\n", self.i)
                self.i = n if j == -1 else j
            elif not self.hash and s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j == -1:
                    raise ParseError(f"Unterminated comment on line {self._line()}")
                self.i = j + 2
            else:
                self.i += 1
        raise ParseError("Unterminated list (missing ']')")

    def _key(self) -> str:
        c = self.s[self.i]
        if c in "\"'":
            val, _ = self._string()
            return val
        m = BARE_KEY_RE.match(self.s, self.i)
        if not m:
            raise ParseError(f"Unexpected '{c}' on line {self._line()}")
        self.i = m.end()
        return m.group()

    def _trailing(self, e: Entry) -> None:
        """Grab a comment on the same line after a value."""
        j = self.i
        while j < self.n and self.s[j] in " \t,":
            j += 1
        if self._line_comment_at(j):
            k = self.s.find("\n", j)
            k = self.n if k == -1 else k
            txt = clean_comment(self.s[j:k])
            e.comment = (e.comment + "\n" + txt).strip() if txt else e.comment
            self.i = k

    # -- grammar -------------------------------------------------------------

    def _object(self, prefix: tuple, depth: int) -> None:
        self.i += 1  # '{'
        while True:
            self._skip()
            if self.i >= self.n:
                raise ParseError("Unterminated object (missing '}')")
            c = self.s[self.i]
            if c == "}":
                self.i += 1
                self.pending.clear()
                return
            if c == ",":
                self.i += 1
                continue
            key = self._key()
            self._skip(collect=False)
            if self.i >= self.n or self.s[self.i] not in ":=":
                raise ParseError(f"Expected ':' after key '{key}' on line {self._line()}")
            self.i += 1
            self._skip(collect=False)
            if self.i >= self.n:
                raise ParseError("Unexpected end of file after a key")
            comment = "\n".join(p for p in self.pending if p)
            self.pending.clear()
            self._value(prefix + (key,), depth, comment)

    def _value(self, path: tuple, depth: int, comment: str) -> None:
        s = self.s
        c = s[self.i]
        if c == "{":
            self._add(path, "header", None, comment, depth)
            self._object(path, depth + 1)
            return
        start = self.i
        if c == "[":
            self._skip_balanced()
            e = self._add(path, "raw", s[start:self.i], comment, depth, span=(start, self.i))
        elif c in "\"'":
            val, q = self._string()
            e = self._add(path, "string", val, comment, depth, span=(start, self.i), quote=q)
        else:
            m = BARE_VAL_RE.match(s, self.i)
            if not m:
                raise ParseError(f"Unexpected '{c}' on line {self._line()}")
            tok = m.group()
            self.i = m.end()
            e = self._classify(path, tok, comment, depth, (start, self.i))
        self._trailing(e)

    def _classify(self, path, tok, comment, depth, span) -> Entry:
        if tok in ("true", "false"):
            return self._add(path, "bool", tok == "true", comment, depth, span=span)
        m = INT_RE.match(tok)
        if m:
            return self._add(path, "int", m.group(1), comment, depth, span=span, suffix=m.group(2))
        m = FLOAT_RE.match(tok)
        if m:
            return self._add(path, "float", m.group(1), comment, depth, span=span, suffix=m.group(2))
        if tok == "null" or re.match(r"^[+-]?(\d|\.\d|Infinity|NaN)", tok):
            return self._add(path, "raw", tok, comment, depth, span=span)
        return self._add(path, "string", tok, comment, depth, span=span, bare=True)


def render_relaxed(e: Entry, new) -> str:
    """Turn an edited value back into source text for a scanner-format file."""
    if e.kind == "bool":
        return "true" if new else "false"
    if e.kind == "int":
        return f"{int(str(new).strip())}{e.suffix}"
    if e.kind == "float":
        txt = str(new).strip()
        float(txt)
        return txt + e.suffix
    if e.kind == "string":
        txt = str(new)
        if e.bare and SAFE_BARE_RE.fullmatch(txt):
            return txt
        q = e.quote
        esc = (txt.replace("\\", "\\\\").replace(q, "\\" + q)
                  .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        return q + esc + q
    return str(new)                       # raw: whatever the user typed


class RelaxedDoc:
    def __init__(self, text: str, fmt: str):
        self.text = text
        self.fmt = fmt
        self.entries = RelaxedParser(text, hash_comments=(fmt == "snbt")).parse()

    def apply(self, changes: dict[int, object]) -> str:
        edits = []
        for idx, new in changes.items():
            e = self.entries[idx]
            edits.append((e.span[0], e.span[1], render_relaxed(e, new)))
        out = self.text
        for a, b, rep in sorted(edits, reverse=True):
            out = out[:a] + rep + out[b:]
        # the result has to still be readable (catches broken list text)
        RelaxedParser(out, hash_comments=(self.fmt == "snbt")).parse()
        return out


# ---------------------------------------------------------------------------
# TOML (tomlkit keeps comments and layout when a value is replaced)
# ---------------------------------------------------------------------------

def _body(node):
    cont = node if isinstance(node, Container) else node.value
    return cont.body


def _comment_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.lstrip().startswith("#"))


class TomlDoc:
    def __init__(self, text: str):
        self.text = text
        self.doc = tomlkit.parse(text)
        self.entries: list[Entry] = []
        self._walk(self.doc, (), 0)
        for e in self.entries:
            add_hints(e)

    def _add(self, path, kind, value, comment, depth) -> Entry:
        e = Entry(len(self.entries), path, kind, value, comment, depth)
        self.entries.append(e)
        return e

    def _walk(self, node, prefix: tuple, depth: int) -> list[str]:
        pending: list[str] = []
        for key, item in _body(node):
            if key is None:
                if isinstance(item, tk_items.Comment):
                    pending.append(clean_comment(item.as_string()))
                elif isinstance(item, tk_items.Whitespace) and "\n" in item.as_string():
                    pending.clear()
                continue
            path = prefix + (key.key,)
            lines = [p for p in pending if p]
            pending = []
            trivia = getattr(item, "trivia", None)
            if trivia is not None and trivia.comment:
                lines.append(clean_comment(trivia.comment))
            comment = "\n".join(lines)

            if isinstance(item, tk_items.Table):
                self._add(path, "header", None, comment, depth)
                # comments trailing a table body usually describe the next one
                pending = self._walk(item, path, depth + 1)
            elif isinstance(item, tk_items.AoT):
                self._add(path, "static", "(array of tables, edit the file by hand)", comment, depth)
            elif isinstance(item, tk_items.Bool):
                self._add(path, "bool", bool(item.value), comment, depth)
            elif isinstance(item, tk_items.Integer):
                self._add(path, "int", str(int(item)), comment, depth)
            elif isinstance(item, tk_items.Float):
                self._add(path, "float", item.as_string().strip(), comment, depth)
            elif isinstance(item, tk_items.String):
                self._add(path, "string", str(item.value), comment, depth)
            else:
                self._add(path, "raw", item.as_string().strip(), comment, depth)
        return pending

    @staticmethod
    def _to_item(e: Entry, new):
        if e.kind == "raw":
            return tomlkit.parse("v = " + str(new))["v"]
        if e.kind == "float":
            return float(new)
        return new

    def apply(self, changes: dict[int, object]) -> str:
        for idx, new in changes.items():
            e = self.entries[idx]
            parent = self.doc
            for k in e.path[:-1]:
                parent = parent[k]
            parent[e.path[-1]] = self._to_item(e, new)
        out = tomlkit.dumps(self.doc)
        tomlkit.parse(out)                                   # still valid TOML?
        if _comment_lines(out) != _comment_lines(self.text):
            raise ValueError("Safety check failed: this edit would have dropped comments.")
        return out


# ---------------------------------------------------------------------------
# Server discovery helpers
# ---------------------------------------------------------------------------

def looks_like_server(p: Path) -> bool:
    try:
        return (p / "config").is_dir() or (p / "server.properties").is_file()
    except OSError:
        return False


def config_roots(server: Path) -> list[Path]:
    level = "world"
    try:
        for line in (server / "server.properties").read_text(
                encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("level-name="):
                level = line.split("=", 1)[1].strip() or "world"
    except OSError:
        pass
    cands = [server / "config", server / "defaultconfigs", server / level / "serverconfig"]
    out = []
    for c in cands:
        try:
            if c.is_dir():
                out.append(c)
        except OSError:
            pass
    return out


def describe_server(p: Path) -> tuple[str, str]:
    """Best-effort (motd, info) so servers are recognisable without the panel."""
    motd = ""
    try:
        for line in (p / "server.properties").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("motd="):
                raw = line.split("=", 1)[1]
                motd = re.sub(r"(?:§|\\u00[Aa]7).", "", raw).strip()
    except OSError:
        pass
    loader = ""
    for sub, name in (("libraries/net/neoforged", "NeoForge"),
                      ("libraries/net/minecraftforge", "Forge"),
                      (".fabric", "Fabric"),
                      ("libraries/org/quiltmc", "Quilt")):
        try:
            if (p / sub).exists():
                loader = name
                break
        except OSError:
            pass
    mods = ""
    try:
        n = sum(1 for f in (p / "mods").iterdir() if f.suffix.lower() == ".jar")
        mods = f"{n} mods"
    except OSError:
        pass
    return motd, ", ".join(x for x in (loader, mods) if x)


# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class Tooltip:
    active: "Tooltip | None" = None

    def __init__(self, widgets, text: str, delay: int = 450):
        self.text = text
        self.delay = delay
        self.tip = None
        self._job = None
        self._job_widget = None
        self._pos = (0, 0)
        for w in widgets:
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")
            w.bind("<ButtonPress>", self._leave, add="+")

    @classmethod
    def hide_active(cls):
        if cls.active:
            cls.active._hide()

    def _enter(self, e):
        self._cancel()
        self._pos = (e.x_root, e.y_root)
        self._job_widget = e.widget
        self._job = e.widget.after(self.delay, self._show)

    def _leave(self, _e=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._job and self._job_widget:
            try:
                self._job_widget.after_cancel(self._job)
            except tk.TclError:
                pass
        self._job = None

    def _show(self):
        self._job = None
        Tooltip.hide_active()
        tip = tk.Toplevel()
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self.text, justify="left", wraplength=460,
                 bg="#1e1e1e", fg="#e8e8e8", relief="solid", borderwidth=1,
                 padx=8, pady=6, font=("Segoe UI", 10)).pack()
        tip.update_idletasks()
        x = min(self._pos[0] + 16, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8)
        y = min(self._pos[1] + 20, tip.winfo_screenheight() - tip.winfo_reqheight() - 8)
        tip.wm_geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.tip = tip
        Tooltip.active = self

    def _hide(self):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None
        if Tooltip.active is self:
            Tooltip.active = None


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

@dataclass
class RowState:
    entry: Entry
    var: tk.Variable
    orig: object


class ConfigManagerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Serverside Config Manager")
        self.geometry("1150x740")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.cfg = load_config()
        self._seed_from_modmanager()

        self.ui_font = ctk.CTkFont(family="Segoe UI", size=13)
        self.bold_font = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.mono_font = ctk.CTkFont(family="Consolas", size=12)

        self.root_var = ctk.StringVar(value=self.cfg.get("root", ""))
        self.server_var = ctk.StringVar(value="")
        self.file_search_var = ctk.StringVar()
        self.key_filter_var = ctk.StringVar()
        self.file_label_var = ctk.StringVar(value="No file open")
        self.dirty_var = ctk.StringVar(value="")
        self.status_var = ctk.StringVar(value="Ready.")

        self.servers: dict[str, Path] = {}        # menu label -> server folder
        self.all_files: list[str] = []            # posix paths relative to the server
        self.scan_queue: queue.Queue = queue.Queue()
        self.scan_token = 0
        self.current_rel: str | None = None
        self.doc = None
        self.bom = False
        self.rows: dict[int, RowState] = {}
        self._filter_job = None
        self._suppress_select = False

        self._build_layout()
        self.file_search_var.trace_add("write", lambda *_: self._populate_tree())
        self.key_filter_var.trace_add("write", lambda *_: self._debounce_render())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_servers()

    # -- setup ---------------------------------------------------------------

    def _seed_from_modmanager(self):
        """First run: borrow the share and server from the Mod Manager's config."""
        if self.cfg.get("root") or not MODMANAGER_CONFIG.exists():
            return
        try:
            dest = json.loads(MODMANAGER_CONFIG.read_text()).get("dest", "")
        except (json.JSONDecodeError, OSError):
            return
        if dest:
            p = Path(dest)
            self.cfg["root"] = p.parent.parent.as_posix()
            self.cfg["server"] = p.parent.name

    def _build_layout(self):
        pad = {"padx": 12, "pady": 6}

        top = ctk.CTkFrame(self)
        top.pack(fill="x", **pad)
        ctk.CTkLabel(top, text="Servers root:", width=100, anchor="w", font=self.ui_font).pack(side="left", padx=8)
        ctk.CTkEntry(top, textvariable=self.root_var, font=self.ui_font).pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(top, text="Rescan servers", width=110, font=self.ui_font, command=self.refresh_servers).pack(side="left", padx=8)

        srv = ctk.CTkFrame(self)
        srv.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(srv, text="Server:", width=100, anchor="w", font=self.ui_font).pack(side="left", padx=8)
        self.server_menu = ctk.CTkOptionMenu(srv, variable=self.server_var, values=["(none)"],
                                             font=self.ui_font, width=360, command=self._on_server_change)
        self.server_menu.pack(side="left", padx=4)
        ctk.CTkButton(srv, text="Rename", width=80, font=self.ui_font, command=self.rename_server).pack(side="left", padx=8)
        ctk.CTkButton(srv, text="Rescan files", width=100, font=self.ui_font, command=self.rescan).pack(side="left", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=0)
        body.grid_columnconfigure(0, weight=0, minsize=340)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # left: file tree
        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ctk.CTkEntry(left, textvariable=self.file_search_var, font=self.ui_font,
                     placeholder_text="Filter files...").pack(fill="x", padx=8, pady=8)
        tree_wrap = ctk.CTkFrame(left, fg_color="transparent")
        tree_wrap.pack(fill="both", expand=True, padx=(8, 4), pady=(0, 8))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2b2b2b", foreground="#dce4ee",
                        fieldbackground="#2b2b2b", borderwidth=0, rowheight=24,
                        font=("Segoe UI", 10))
        style.map("Treeview", background=[("selected", "#1f6aa5")])

        self.tree = ttk.Treeview(tree_wrap, show="tree", selectmode="browse")
        sb = ctk.CTkScrollbar(tree_wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # right: editor
        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(head, textvariable=self.file_label_var, anchor="w", font=self.bold_font).pack(side="left", fill="x", expand=True)
        ctk.CTkEntry(head, textvariable=self.key_filter_var, width=240, font=self.ui_font,
                     placeholder_text="Filter keys / comments...").pack(side="right")

        self.editor = ctk.CTkScrollableFrame(right)
        self.editor.pack(fill="both", expand=True, padx=8, pady=4)

        foot = ctk.CTkFrame(right, fg_color="transparent")
        foot.pack(fill="x", padx=8, pady=(4, 8))
        self.save_btn = ctk.CTkButton(foot, text="Save", width=90, font=self.ui_font, command=self.save)
        self.save_btn.pack(side="left", padx=(0, 6))
        ctk.CTkButton(foot, text="Reload", width=90, font=self.ui_font, command=self.reload).pack(side="left", padx=6)
        ctk.CTkButton(foot, text="Open in editor", width=120, font=self.ui_font, command=self.open_external).pack(side="left", padx=6)
        ctk.CTkLabel(foot, textvariable=self.dirty_var, anchor="e", font=self.ui_font).pack(side="right")

        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", font=self.ui_font).pack(fill="x", padx=12, pady=(6, 10))

    # -- servers ---------------------------------------------------------------

    def _label_for(self, p: Path) -> str:
        # your own alias wins, then the server's MOTD, then just the folder id
        alias = self.cfg.get("aliases", {}).get(p.name, "")
        motd, info = describe_server(p)
        name = alias or motd
        parts = [name] if name else []
        if info:
            parts.append(f"[{info}]")
        parts.append(f"({p.name[:8]})")
        return "  ".join(parts)

    def current_server(self) -> Path | None:
        return self.servers.get(self.server_var.get())

    def refresh_servers(self):
        if not self._confirm_discard():
            return
        root = Path(self.root_var.get().strip()) if self.root_var.get().strip() else None
        self.servers = {}
        try:
            if root is None or not root.is_dir():
                self.status_var.set("Servers root not found. Check the path.")
                self._set_servers_menu([])
                self._clear_all()
                return
            cands = [root] if looks_like_server(root) else sorted(
                (d for d in root.iterdir() if d.is_dir() and looks_like_server(d)),
                key=lambda d: d.name.lower())
        except OSError as e:
            self.status_var.set(f"Couldn't read servers root: {e}")
            return
        for d in cands:
            self.servers[self._label_for(d)] = d
        self.cfg["root"] = self.root_var.get().strip()
        save_config(self.cfg)

        labels = list(self.servers)
        self._set_servers_menu(labels)
        wanted = self.cfg.get("server")
        pick = next((lb for lb, p in self.servers.items() if p.name == wanted), labels[0] if labels else "")
        self.server_var.set(pick)
        self.rescan()

    def _set_servers_menu(self, labels: list[str]):
        self.server_menu.configure(values=labels or ["(none)"])
        if not labels:
            self.server_var.set("(none)")

    def _on_server_change(self, _label):
        if not self._confirm_discard():
            return
        s = self.current_server()
        if s:
            self.cfg["server"] = s.name
            save_config(self.cfg)
        self.rescan()

    def rename_server(self):
        s = self.current_server()
        if not s:
            return
        dlg = ctk.CTkInputDialog(text=f"Friendly name for {s.name}:", title="Rename server")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        self.cfg.setdefault("aliases", {})[s.name] = name
        save_config(self.cfg)
        self.refresh_servers()

    # -- scanning ----------------------------------------------------------------

    def _clear_all(self):
        self.all_files = []
        self._populate_tree()
        self._unload()

    def rescan(self):
        if not self._confirm_discard():
            return
        self._unload()
        server = self.current_server()
        self.all_files = []
        self._populate_tree()
        if not server:
            return
        self.scan_token += 1
        token = self.scan_token
        self.status_var.set("Scanning config folders...")
        threading.Thread(target=self._scan_worker, args=(server, token), daemon=True).start()
        self.after(100, self._poll_scan)

    def _scan_worker(self, server: Path, token: int):
        files, err = [], ""
        try:
            for root in config_roots(server):
                for dirpath, _dirs, names in os.walk(root):
                    for fn in names:
                        if Path(fn).suffix.lower() in CONFIG_EXTS:
                            files.append((Path(dirpath) / fn).relative_to(server).as_posix())
        except OSError as e:
            err = str(e)
        files.sort(key=str.lower)
        self.scan_queue.put((token, files, err))

    def _poll_scan(self):
        try:
            token, files, err = self.scan_queue.get_nowait()
        except queue.Empty:
            if self.scan_token:
                self.after(100, self._poll_scan)
            return
        if token != self.scan_token:
            self.after(100, self._poll_scan)
            return
        self.all_files = files
        self._populate_tree()
        self.status_var.set(f"{len(files)} config file(s) found." if not err else f"Scan problem: {err}")

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        q = self.file_search_var.get().strip().lower()
        made: set[str] = set()
        for rel in self.all_files:
            if q and q not in rel.lower():
                continue
            parts = rel.split("/")
            parent = ""
            for depth, part in enumerate(parts[:-1]):
                dir_id = "d:" + "/".join(parts[:depth + 1])
                if dir_id not in made:
                    self.tree.insert(parent, "end", iid=dir_id, text=part, open=bool(q) or depth == 0)
                    made.add(dir_id)
                parent = dir_id
            self.tree.insert(parent, "end", iid="f:" + rel, text=parts[-1])
        if self.current_rel and self.tree.exists("f:" + self.current_rel):
            self._suppress_select = True
            self.tree.selection_set("f:" + self.current_rel)
            self.after(100, lambda: setattr(self, "_suppress_select", False))

    def _on_tree_select(self, _e):
        if self._suppress_select:
            return
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("f:"):
            return
        rel = sel[0][2:]
        if rel == self.current_rel:
            return
        if not self._confirm_discard():
            self._restore_selection()
            return
        self._load_file(rel)

    def _restore_selection(self):
        self._suppress_select = True
        if self.current_rel and self.tree.exists("f:" + self.current_rel):
            self.tree.selection_set("f:" + self.current_rel)
        self.after(100, lambda: setattr(self, "_suppress_select", False))

    # -- loading -------------------------------------------------------------------

    def _unload(self):
        self.doc = None
        self.current_rel = None
        self.rows = {}
        self.file_label_var.set("No file open")
        self.dirty_var.set("")
        self._render_rows()

    def _file_path(self) -> Path | None:
        s = self.current_server()
        return (s / self.current_rel) if (s and self.current_rel) else None

    def _load_file(self, rel: str, force: bool = False):
        server = self.current_server()
        if not server:
            return
        path = server / rel
        fmt = CONFIG_EXTS[path.suffix.lower()]
        self.current_rel = rel
        self.file_label_var.set(rel)
        if not force:
            self.key_filter_var.set("")
        try:
            if not force and path.stat().st_size > BIG_FILE_BYTES:
                if not messagebox.askyesno("Large file", f"{rel} is over 2 MB. Open it anyway?"):
                    self._unload()
                    return
            text, bom = read_text(path)
            doc = TomlDoc(text) if fmt == "toml" else RelaxedDoc(text, fmt)
        except Exception as e:  # parse errors, decode errors, SMB errors
            self.doc = None
            self.rows = {}
            self._show_message(f"Couldn't open this file as a form:\n\n{e}")
            self.status_var.set(f"Failed to open {rel}")
            return
        self.doc, self.bom = doc, bom
        self.rows = {}
        for e in doc.entries:
            if e.kind in ("header", "static"):
                continue
            if e.kind == "bool":
                var: tk.Variable = ctk.BooleanVar(value=bool(e.value))
                orig: object = bool(e.value)
            else:
                var = ctk.StringVar(value=str(e.value))
                orig = str(e.value)
            var.trace_add("write", lambda *_: self._update_dirty())
            self.rows[e.idx] = RowState(e, var, orig)
        self._render_rows()
        self._update_dirty()
        editable = len(self.rows)
        self.status_var.set(f"Opened {rel}: {editable} editable value(s).")

    def reload(self):
        if not self.current_rel:
            return
        if self._dirty_count() and not messagebox.askyesno(
                "Discard changes?", "Reload from disk and throw away unsaved edits?"):
            return
        self._load_file(self.current_rel, force=True)

    def open_external(self):
        p = self._file_path()
        if p:
            try:
                os.startfile(p)  # Windows
            except (OSError, AttributeError) as e:
                messagebox.showerror("Can't open", str(e))

    # -- rendering ----------------------------------------------------------------

    def _debounce_render(self):
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(250, self._render_rows)

    def _show_message(self, text: str):
        Tooltip.hide_active()
        for w in self.editor.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.editor, text=text, justify="left", wraplength=640,
                     anchor="w", font=self.ui_font).pack(fill="x", padx=8, pady=8)
        ctk.CTkButton(self.editor, text="Open in editor", width=120, font=self.ui_font,
                      command=self.open_external).pack(anchor="w", padx=8)

    def _render_rows(self):
        Tooltip.hide_active()
        for w in self.editor.winfo_children():
            w.destroy()
        if not self.doc:
            return
        q = self.key_filter_var.get().strip().lower()
        flat = bool(q)
        shown = total = 0
        for e in self.doc.entries:
            if q:
                if e.kind == "header":
                    continue
                if q not in e.dotted.lower() and q not in e.comment.lower():
                    continue
            total += 1
            if shown >= MAX_ROWS:
                continue
            self._make_row(e, flat)
            shown += 1
        if total > shown:
            ctk.CTkLabel(self.editor, text=f"{total - shown} more entries hidden. Use the filter to find them.",
                         anchor="w", font=self.ui_font).pack(fill="x", padx=8, pady=8)
        elif shown == 0:
            ctk.CTkLabel(self.editor, text="Nothing to show.", anchor="w",
                         font=self.ui_font).pack(fill="x", padx=8, pady=8)

    def _make_row(self, e: Entry, flat: bool):
        if e.kind == "header":
            lbl = ctk.CTkLabel(self.editor, text="[" + e.dotted + "]", anchor="w", font=self.bold_font)
            lbl.pack(fill="x", padx=(8 + e.depth * 16, 8), pady=(12, 2))
            if e.comment:
                Tooltip([lbl], e.comment)
            return

        row = ctk.CTkFrame(self.editor, fg_color="transparent")
        row.pack(fill="x", pady=1)
        text = e.dotted if flat else e.key
        if e.comment:
            text += "  (i)"
        indent = 0 if flat else e.depth * 16
        lbl = ctk.CTkLabel(row, text=text, anchor="w", justify="left", width=280,
                           wraplength=270, font=self.ui_font)
        lbl.pack(side="left", padx=(8 + indent, 6))
        widgets = [row, lbl]

        if e.kind == "static":
            w = ctk.CTkLabel(row, text=str(e.value), anchor="w", font=self.ui_font, text_color="gray")
            w.pack(side="left", fill="x", expand=True, padx=(0, 8))
        else:
            st = self.rows[e.idx]
            if e.kind == "bool":
                w = ctk.CTkSwitch(row, text="", variable=st.var, width=46)
                w.pack(side="left", padx=(0, 8))
            elif e.options:
                w = ctk.CTkOptionMenu(row, variable=st.var, values=e.options, font=self.ui_font)
                w.pack(side="left", padx=(0, 8))
            elif e.kind == "raw":
                w = ctk.CTkButton(row, text=self._preview(st.var.get()), anchor="w", font=self.mono_font,
                                  fg_color="#343638", hover_color="#3f4244")
                w.configure(command=lambda s=st, b=w: self._edit_raw(s, b))
                w.pack(side="left", fill="x", expand=True, padx=(0, 8))
            else:
                w = ctk.CTkEntry(row, textvariable=st.var, font=self.mono_font if e.kind != "string" else self.ui_font)
                w.pack(side="left", fill="x", expand=True, padx=(0, 8))
            widgets.append(w)

        if e.comment:
            Tooltip(widgets, e.comment)

    @staticmethod
    def _preview(raw: str) -> str:
        flat = " ".join(raw.split())
        return flat if len(flat) <= 70 else flat[:67] + "..."

    def _edit_raw(self, st: RowState, button):
        win = ctk.CTkToplevel(self)
        win.title(st.entry.dotted)
        win.geometry("660x420")
        win.transient(self)
        box = ctk.CTkTextbox(win, font=self.mono_font, wrap="none")
        box.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        box.insert("1.0", st.var.get())

        def ok():
            st.var.set(box.get("1.0", "end-1c"))
            if button.winfo_exists():
                button.configure(text=self._preview(st.var.get()))
            win.destroy()

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(bar, text="OK", width=80, command=ok).pack(side="right", padx=4)
        ctk.CTkButton(bar, text="Cancel", width=80, command=win.destroy).pack(side="right", padx=4)
        win.after(150, win.grab_set)

    # -- dirty tracking -------------------------------------------------------------

    def _dirty_count(self) -> int:
        return sum(1 for st in self.rows.values() if st.var.get() != st.orig)

    def _update_dirty(self):
        n = self._dirty_count()
        self.dirty_var.set(f"{n} unsaved change(s)" if n else "No changes")

    def _confirm_discard(self) -> bool:
        if not self._dirty_count():
            return True
        ans = messagebox.askyesnocancel("Unsaved changes", f"Save changes to {self.current_rel} first?")
        if ans is None:
            return False
        if ans:
            self.save()
            return self._dirty_count() == 0
        return True

    def _on_close(self):
        if self._confirm_discard():
            Tooltip.hide_active()
            self.destroy()

    # -- saving ---------------------------------------------------------------------

    @staticmethod
    def _coerce(e: Entry, cur):
        if e.kind == "bool":
            return bool(cur)
        if e.kind == "int":
            return int(str(cur).strip())
        if e.kind == "float":
            s = str(cur).strip()
            float(s)
            return s
        return cur

    def _backup(self, path: Path):
        server = self.current_server()
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest_dir = BACKUP_DIR / (server.name if server else "unknown") / Path(self.current_rel).parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"{path.name}.{ts}.bak").write_bytes(path.read_bytes())

    def save(self):
        path = self._file_path()
        if not self.doc or not path:
            return
        changes: dict[int, object] = {}
        for idx, st in self.rows.items():
            cur = st.var.get()
            if cur == st.orig:
                continue
            e = st.entry
            try:
                new = self._coerce(e, cur)
            except ValueError:
                messagebox.showerror("Invalid value", f"'{e.dotted}' expects a {e.kind} value, got:\n{cur}")
                return
            if e.lo is not None and e.hi is not None and e.kind in ("int", "float"):
                if not (e.lo <= float(new) <= e.hi):
                    if not messagebox.askyesno(
                            "Out of range",
                            f"'{e.dotted}' should be between {e.lo:g} and {e.hi:g} "
                            f"according to the file's own comment.\nSave anyway?"):
                        return
            changes[idx] = new
        if not changes:
            self.status_var.set("Nothing to save.")
            return

        # spans/trivia are only valid against the text we parsed
        try:
            disk_text, _ = read_text(path)
        except OSError as e:
            messagebox.showerror("Can't read file", str(e))
            return
        if disk_text != self.doc.text:
            if messagebox.askyesno("File changed on disk",
                                   "This file was modified since you opened it (the server may have "
                                   "rewritten it).\nReload it now? Your unsaved edits will be lost."):
                self._load_file(self.current_rel, force=True)
            return

        try:
            new_text = self.doc.apply(changes)
        except Exception as e:
            messagebox.showerror("Can't apply changes", f"{e}\n\nNothing was written.")
            return
        try:
            self._backup(path)
            write_text(path, new_text, self.bom)
        except OSError as e:
            messagebox.showerror("Write failed", str(e))
            return
        n = len(changes)
        self._load_file(self.current_rel, force=True)
        self.status_var.set(f"Saved {n} change(s) to {self.current_rel}. A server restart may be needed.")


if __name__ == "__main__":
    app = ConfigManagerApp()
    app.mainloop()
