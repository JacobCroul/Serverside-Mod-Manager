"""
Serverside Mod Manager
-----------------------
Copies/syncs .jar mods from a client modpack folder to a server mods
folder (mounted network drive), and lets you toggle mods on/off on the
server side by renaming X.jar <-> X.jar.disabled.

v1 scope (per Notion build doc):
- No jar-parsing. Pure file operations.
- Two folder pickers (source / destination), paths persisted in config.json.
- Scrollable CTk list of mods with checkboxes.
- "Sync to server" button - copies checked mods, skips if already present
  with matching size (fast re-runs).
- Per-mod or bulk enable/disable toggle (renames on destination side).
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
import json
import shutil
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


# ---------------------------------------------------------------------------
# Config persistence
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
# Mod file helpers
# ---------------------------------------------------------------------------

def list_mods(folder: str) -> list[Path]:
    """Return all .jar / .jar.disabled files in folder, sorted by name."""
    if not folder or not Path(folder).is_dir():
        return []
    p = Path(folder)
    mods = list(p.glob("*.jar")) + list(p.glob("*.jar.disabled"))
    return sorted(mods, key=lambda f: f.name.lower())


def base_name(mod_path: Path) -> str:
    """Strip .disabled suffix so 'foo.jar' and 'foo.jar.disabled' compare equal."""
    name = mod_path.name
    return name[:-9] if name.endswith(".disabled") else name


def is_enabled(mod_path: Path) -> bool:
    return not mod_path.name.endswith(".disabled")


def dest_status(base: str, dest_folder: str) -> str:
    """Check a mod's state on the destination side.

    Returns "enabled", "disabled", "missing" (not synced yet), or ""
    if the destination folder isn't valid.
    """
    if not dest_folder or not Path(dest_folder).is_dir():
        return ""
    dest = Path(dest_folder)
    if (dest / base).exists():
        return "enabled"
    if (dest / f"{base}.disabled").exists():
        return "disabled"
    return "missing"


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class ModManagerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Serverside Mod Manager")
        self.geometry("720x560")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.config_data = load_config()
        self.source_var = ctk.StringVar(value=self.config_data.get("source", ""))
        self.dest_var = ctk.StringVar(value=self.config_data.get("dest", ""))
        self.search_var = ctk.StringVar(value="")
        self.search_var.trace_add("write", lambda *_: self.refresh_mod_list())

        # Fonts: a normal UI font for labels/buttons, and a monospace font
        # for the mod list itself (long jar filenames are easier to scan
        # when they're not proportionally spaced).
        self.ui_font = ctk.CTkFont(family="Segoe UI", size=13)
        self.mono_font = ctk.CTkFont(family="Consolas", size=12)

        # checked_state persists across search filtering, so checking a mod,
        # then searching for something else, doesn't lose the selection.
        self.checked_state: dict[str, bool] = {}

        # checkbox state keyed by filename (source side) -- only for mods
        # currently rendered (i.e. matching the active search filter)
        self.mod_vars: dict[str, ctk.BooleanVar] = {}
        self.mod_checkboxes: list[ctk.CTkCheckBox] = []

        self._build_layout()
        self.refresh_mod_list()

    # -- layout ------------------------------------------------------------

    def _build_layout(self):
        pad = {"padx": 12, "pady": 6}

        # Source picker
        src_frame = ctk.CTkFrame(self)
        src_frame.pack(fill="x", **pad)
        ctk.CTkLabel(src_frame, text="Source (client mods):", width=170, anchor="w", font=self.ui_font).pack(side="left", padx=8)
        ctk.CTkEntry(src_frame, textvariable=self.source_var, font=self.ui_font).pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(src_frame, text="Browse", width=80, font=self.ui_font, command=self.pick_source).pack(side="left", padx=8)

        # Destination picker
        dst_frame = ctk.CTkFrame(self)
        dst_frame.pack(fill="x", **pad)
        ctk.CTkLabel(dst_frame, text="Destination (server mods):", width=170, anchor="w", font=self.ui_font).pack(side="left", padx=8)
        ctk.CTkEntry(dst_frame, textvariable=self.dest_var, font=self.ui_font).pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(dst_frame, text="Browse", width=80, font=self.ui_font, command=self.pick_dest).pack(side="left", padx=8)

        # Search bar
        search_frame = ctk.CTkFrame(self)
        search_frame.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(search_frame, text="Search:", width=170, anchor="w", font=self.ui_font).pack(side="left", padx=8)
        search_entry = ctk.CTkEntry(
            search_frame,
            textvariable=self.search_var,
            font=self.ui_font,
            placeholder_text="Filter by filename...",
        )
        search_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(search_frame, text="Clear", width=80, font=self.ui_font,
                      command=lambda: self.search_var.set("")).pack(side="left", padx=8)

        # Mod list
        list_label = ctk.CTkLabel(self, text="Mods found in source:", anchor="w", font=self.ui_font)
        list_label.pack(fill="x", padx=12, pady=(10, 0))

        self.mod_list_frame = ctk.CTkScrollableFrame(self, height=280)
        self.mod_list_frame.pack(fill="both", expand=True, padx=12, pady=6)

        # Bulk select buttons
        bulk_frame = ctk.CTkFrame(self)
        bulk_frame.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(bulk_frame, text="Select All", width=100, font=self.ui_font, command=self.select_all).pack(side="left", padx=4)
        ctk.CTkButton(bulk_frame, text="Select None", width=100, font=self.ui_font, command=self.select_none).pack(side="left", padx=4)
        ctk.CTkButton(bulk_frame, text="Refresh List", width=100, font=self.ui_font, command=self.refresh_mod_list).pack(side="left", padx=4)

        # Action buttons
        action_frame = ctk.CTkFrame(self)
        action_frame.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(action_frame, text="Sync to server", font=self.ui_font, command=self.sync_mods).pack(side="left", padx=4)
        ctk.CTkButton(action_frame, text="Enable selected on server", font=self.ui_font, command=lambda: self.toggle_selected(True)).pack(side="left", padx=4)
        ctk.CTkButton(action_frame, text="Disable selected on server", font=self.ui_font, command=lambda: self.toggle_selected(False)).pack(side="left", padx=4)

        self.status_var = ctk.StringVar(value="Ready.")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", font=self.ui_font).pack(fill="x", padx=12, pady=(0, 10))

    # -- folder pickers ------------------------------------------------------

    def pick_source(self):
        folder = filedialog.askdirectory(title="Select source (client) mods folder")
        if folder:
            self.source_var.set(folder)
            self._persist_paths()
            self.refresh_mod_list()

    def pick_dest(self):
        folder = filedialog.askdirectory(title="Select destination (server) mods folder")
        if folder:
            self.dest_var.set(folder)
            self._persist_paths()
            self.refresh_mod_list()

    def _persist_paths(self):
        self.config_data["source"] = self.source_var.get()
        self.config_data["dest"] = self.dest_var.get()
        save_config(self.config_data)

    # -- mod list rendering --------------------------------------------------

    def refresh_mod_list(self):
        # flush any visible checkbox changes into the persistent state
        # before we tear the checkboxes down
        for name, var in self.mod_vars.items():
            self.checked_state[name] = var.get()

        for cb in self.mod_checkboxes:
            cb.destroy()
        self.mod_checkboxes.clear()
        self.mod_vars.clear()

        all_mods = list_mods(self.source_var.get())
        if not all_mods:
            self.status_var.set("No .jar files found in source folder.")
            return

        query = self.search_var.get().strip().lower()
        mods = [m for m in all_mods if query in m.name.lower()] if query else all_mods

        dest = self.dest_var.get()
        for mod in mods:
            var = ctk.BooleanVar(value=self.checked_state.get(mod.name, False))
            var.trace_add("write", lambda *_, n=mod.name, v=var: self.checked_state.__setitem__(n, v.get()))
            self.mod_vars[mod.name] = var

            label = mod.name
            if not is_enabled(mod):
                label += "  [disabled in source]"

            status = dest_status(base_name(mod), dest)
            if status == "disabled":
                label += "  [disabled on server]"
            elif status == "missing":
                label += "  [not synced]"

            cb = ctk.CTkCheckBox(self.mod_list_frame, text=label, variable=var, font=self.mono_font)
            cb.pack(fill="x", anchor="w", padx=6, pady=2)
            self.mod_checkboxes.append(cb)

        if query:
            self.status_var.set(f"{len(mods)} of {len(all_mods)} mod file(s) match '{query}'.")
        else:
            self.status_var.set(f"{len(mods)} mod file(s) found in source.")

    def select_all(self):
        for var in self.mod_vars.values():
            var.set(True)

    def select_none(self):
        for var in self.mod_vars.values():
            var.set(False)

    def _selected_mod_names(self) -> list[str]:
        # includes mods checked earlier but currently hidden by the search
        # filter, not just what's on screen right now
        for name, var in self.mod_vars.items():
            self.checked_state[name] = var.get()
        valid_names = {m.name for m in list_mods(self.source_var.get())}
        return [name for name, checked in self.checked_state.items() if checked and name in valid_names]

    # -- core actions ---------------------------------------------------------

    def sync_mods(self):
        source, dest = self.source_var.get(), self.dest_var.get()
        if not source or not dest:
            messagebox.showwarning("Missing folder", "Pick both a source and destination folder first.")
            return
        if not Path(dest).is_dir():
            messagebox.showerror("Bad destination", f"Destination folder doesn't exist:\n{dest}")
            return

        selected = self._selected_mod_names()
        if not selected:
            messagebox.showinfo("Nothing selected", "Check at least one mod to sync.")
            return

        copied, skipped, errors = 0, 0, []
        for name in selected:
            src_path = Path(source) / name
            dst_path = Path(dest) / name
            try:
                if dst_path.exists() and dst_path.stat().st_size == src_path.stat().st_size:
                    skipped += 1
                    continue
                shutil.copy2(src_path, dst_path)
                copied += 1
            except OSError as e:
                errors.append(f"{name}: {e}")

        msg = f"Synced: {copied} copied, {skipped} already up to date."
        if errors:
            msg += f" {len(errors)} error(s) - see console."
            for e in errors:
                print("Sync error:", e)
        self.refresh_mod_list()
        self.status_var.set(msg)

    def toggle_selected(self, enable: bool):
        dest = self.dest_var.get()
        if not dest or not Path(dest).is_dir():
            messagebox.showwarning("Missing folder", "Pick a valid destination folder first.")
            return

        selected = self._selected_mod_names()
        if not selected:
            messagebox.showinfo("Nothing selected", "Check at least one mod to toggle.")
            return

        toggled, missing, errors = 0, 0, []
        for name in selected:
            target_base = base_name(Path(name))
            enabled_path = Path(dest) / target_base
            disabled_path = Path(dest) / f"{target_base}.disabled"

            try:
                if enable:
                    if disabled_path.exists():
                        disabled_path.rename(enabled_path)
                        toggled += 1
                    elif not enabled_path.exists():
                        missing += 1
                else:
                    if enabled_path.exists():
                        enabled_path.rename(disabled_path)
                        toggled += 1
                    elif not disabled_path.exists():
                        missing += 1
            except OSError as e:
                errors.append(f"{target_base}: {e}")

        action = "enabled" if enable else "disabled"
        msg = f"{toggled} mod(s) {action} on server."
        if missing:
            msg += f" {missing} not found on server (sync first)."
        if errors:
            msg += f" {len(errors)} error(s) - see console."
            for e in errors:
                print("Toggle error:", e)
        self.refresh_mod_list()
        self.status_var.set(msg)


if __name__ == "__main__":
    app = ModManagerApp()
    app.mainloop()