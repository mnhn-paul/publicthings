#!/usr/bin/env python3
"""
data.public.lu Keyword Manager
==============================

A Tkinter GUI to list the datasets of a data.public.lu organisation (or single
datasets by ID) and ADD / REMOVE keywords (tags) on one or many of them.

How it works
------------
1. Enter your API key, load the organisation's datasets (or add one by ID).
2. Tick the check boxes of the datasets you want to change (Shift-click ticks a
   range, the header box ticks all shown rows, Space ticks highlighted rows).
3. The right panel shows the tags of the ticked datasets as bubbles:
     grey   = existing tag        (click: stage its REMOVAL)
     red    = will be REMOVED     (click: undo)
     green  = will be ADDED       (click: undo)
     blue   = keyword you typed   (click ×: drop it)
   Type keywords, press Enter or comma to turn them into bubbles, then press
   "+ Add to ticked" (green) or "− Remove from ticked" (red).
4. Nothing is sent until you press "Upload staged changes". On upload each
   dataset is re-fetched, removals are applied, additions are merged without
   duplicates (case-insensitive) and the tag list is written back with PUT.

Extras: dark / light theme, adjustable text size (A− / A+, Ctrl +/−/0),
"Remove from list" (local only!), CSV import (ID|keyword, prefix a keyword
with "-" to remove it). Theme, text size and organisation ID are remembered in
~/.datapublic_keyword_manager.json – the API key is never stored.

Requirements: Python 3.8+, `pip install requests` (tkinter ships with Python;
on Debian/Ubuntu: `sudo apt install python3-tk`).
"""

import csv
import json
import queue
import re
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
import tkinter as tk

import requests

API_ENDPOINT = "https://data.public.lu/api/1/"
DEFAULT_ORG_ID = "57b5a8f16cd58279b1e2a86b"  # same organisation as the dashboard
PAGE_SIZE = 100
TIMEOUT = 30
CONFIG_PATH = Path.home() / ".datapublic_keyword_manager.json"
CHECK_ON, CHECK_OFF = "☑", "☐"
MINUS = "−"

PALETTES = {
    "light": {
        "bg": "#f3f4f6", "panel": "#ffffff", "fg": "#1f2937", "muted": "#6b7280",
        "border": "#d1d5db", "accent": "#2563eb", "field": "#ffffff",
        "header": "#e5e7eb", "btn": "#e5e7eb", "btn_fg": "#1f2937",
        "sel_bg": "#dbeafe", "sel_fg": "#1e3a8a",
        "add_bg": "#dcfce7", "add_fg": "#166534", "add_bd": "#86efac", "add_btn": "#16a34a",
        "rem_bg": "#fee2e2", "rem_fg": "#991b1b", "rem_bd": "#fca5a5", "rem_btn": "#dc2626",
        "both_bg": "#fef3c7", "both_fg": "#92400e",
        "chip_bg": "#e5e7eb", "chip_fg": "#111827", "chip_bd": "#cbd5e1",
        "kw_bg": "#dbeafe", "kw_fg": "#1e40af", "kw_bd": "#93c5fd",
        "sug_bg": "#ffffff", "sug_fg": "#374151", "sug_bd": "#cbd5e1",
    },
    "dark": {
        "bg": "#1b1c21", "panel": "#25272e", "fg": "#e5e7eb", "muted": "#9ca3af",
        "border": "#3b3f4a", "accent": "#3b82f6", "field": "#2d303a",
        "header": "#30333d", "btn": "#3a3e4a", "btn_fg": "#f3f4f6",
        "sel_bg": "#1e3a5f", "sel_fg": "#dbeafe",
        "add_bg": "#14532d", "add_fg": "#bbf7d0", "add_bd": "#15803d", "add_btn": "#16a34a",
        "rem_bg": "#7f1d1d", "rem_fg": "#fecaca", "rem_bd": "#b91c1c", "rem_btn": "#dc2626",
        "both_bg": "#78350f", "both_fg": "#fde68a",
        "chip_bg": "#3b3f4a", "chip_fg": "#f3f4f6", "chip_bd": "#4b5563",
        "kw_bg": "#1e3a8a", "kw_fg": "#bfdbfe", "kw_bd": "#2563eb",
        "sug_bg": "#25272e", "sug_fg": "#d1d5db", "sug_bd": "#4b5563",
    },
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def normalise_tags(raw_tags):
    """uData may return tags as strings or as objects; always return strings."""
    tags = []
    for tag in raw_tags or []:
        if isinstance(tag, str):
            tags.append(tag)
        elif isinstance(tag, dict):
            name = tag.get("name") or tag.get("label") or tag.get("id")
            if name:
                tags.append(str(name))
    return tags


def parse_keywords(text):
    """Split 'a, b; c' into ['a', 'b', 'c'] (empty entries dropped)."""
    return [k.strip() for k in re.split(r"[,;\n]", text) if k.strip()]


def merge_tags(existing, additions):
    """Return (combined, actually_added); case-insensitive duplicate check."""
    seen = {t.strip().casefold() for t in existing}
    added = []
    for kw in additions:
        key = kw.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            added.append(kw.strip())
    return list(existing) + added, added


def extract_dataset_ref(text):
    """Accept an ID, a slug or a full data.public.lu dataset URL."""
    text = text.strip().rstrip("/")
    if not text:
        return ""
    if text.startswith("http"):
        text = text.split("?")[0].split("#")[0].rstrip("/")
        text = text.split("/")[-1]
    return text


def shade(hex_color, factor):
    """factor > 1 lightens, factor < 1 darkens."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    if factor >= 1:
        r, g, b = (int(c + (255 - c) * (factor - 1)) for c in (r, g, b))
    else:
        r, g, b = (int(c * factor) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def load_config():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ----------------------------------------------------------------------------
# Bubble ("chip") widget
# ----------------------------------------------------------------------------
class TagFlow(ttk.Frame):
    """Wrapping rows of rounded bubbles drawn on a canvas.

    chips: list of dicts with keys
        text  - label
        kind  - existing | add | remove | kw | suggest   (decides colours)
        icon  - optional trailing symbol, e.g. "×"
    Any other keys are passed back to on_click(chip) untouched.
    """

    PAD_X, PAD_Y, GAP = 10, 3, 6

    def __init__(self, master, app, on_click, lines=3, empty_text=""):
        super().__init__(master)
        self.app = app
        self.on_click = on_click
        self.lines = lines
        self.empty_text = empty_text
        self.chips = []

        self.canvas = tk.Canvas(self, highlightthickness=1, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda _e: self._scroll(-1))
        self.canvas.bind("<Button-5>", lambda _e: self._scroll(1))
        self.apply_height()

    def apply_height(self):
        line = self.app.chip_font.metrics("linespace") + 2 * self.PAD_Y + self.GAP
        self.canvas.configure(height=line * self.lines + self.GAP)

    def set_chips(self, chips, empty_text=None):
        self.chips = chips
        if empty_text is not None:
            self.empty_text = empty_text
        self.redraw()

    # -- drawing -----------------------------------------------------------
    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
               x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r,
               x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def redraw(self):
        c, p, gap = self.canvas, self.app.p, self.GAP
        c.delete("all")
        c.configure(bg=p["panel"], highlightbackground=p["border"],
                    highlightcolor=p["border"])
        width = max(c.winfo_width(), 60)
        font, strike = self.app.chip_font, self.app.chip_font_strike
        h = font.metrics("linespace") + 2 * self.PAD_Y
        if not self.chips:
            c.create_text(gap + 2, gap + 2, anchor="nw", text=self.empty_text,
                          fill=p["muted"], font=font, width=width - 2 * gap)
            c.configure(scrollregion=(0, 0, width, h + 2 * gap))
            return
        x, y = gap, gap
        for idx, chip in enumerate(self.chips):
            kind = chip.get("kind", "existing")
            bg, fg, bd = self.app.chip_colors(kind)
            text_font = strike if kind == "remove" else font
            icon = chip.get("icon", "")
            text_w = text_font.measure(chip["text"])
            icon_w = font.measure(icon) + 8 if icon else 0
            w = min(text_w + icon_w + 2 * self.PAD_X, width - 2 * gap)
            if x + w > width - gap and x > gap:
                x, y = gap, y + h + gap
            tag = f"chip{idx}"
            self._round_rect(x, y, x + w, y + h, h // 2, fill=bg, outline=bd, tags=(tag,))
            c.create_text(x + self.PAD_X, y + h / 2, anchor="w", text=chip["text"],
                          fill=fg, font=text_font, tags=(tag,))
            if icon:
                c.create_text(x + w - self.PAD_X, y + h / 2, anchor="e", text=icon,
                              fill=fg, font=font, tags=(tag,))
            x += w + gap
        c.configure(scrollregion=(0, 0, width, y + h + gap))

    # -- interaction -------------------------------------------------------
    def _chip_under_pointer(self):
        items = self.canvas.find_withtag("current")
        if not items:
            return None
        for tag in self.canvas.gettags(items[0]):
            if tag.startswith("chip"):
                idx = int(tag[4:])
                if idx < len(self.chips):
                    return idx
        return None

    def _on_press(self, _event):
        idx = self._chip_under_pointer()
        if idx is not None:
            self.on_click(self.chips[idx])

    def _on_motion(self, _event):
        self.canvas.configure(cursor="hand2" if self._chip_under_pointer() is not None else "")

    def _on_wheel(self, event):
        self._scroll(-1 if event.delta > 0 else 1)

    def _scroll(self, units):
        self.canvas.yview_scroll(units, "units")


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
class KeywordManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("data.public.lu – Keyword Manager")
        self.geometry("1400x900")
        self.minsize(1100, 700)

        cfg = load_config()
        self.theme = cfg.get("theme") if cfg.get("theme") in PALETTES else "light"
        self.font_size = min(max(int(cfg.get("font_size", 10)), 8), 26) \
            if str(cfg.get("font_size", "10")).isdigit() else 10
        self.p = PALETTES[self.theme]

        self.datasets = {}   # id -> {"id", "title", "slug", "tags", "page"}
        self.order = []      # ids in load order
        self.checked = set() # ticked dataset ids
        self.pending = {}    # id -> [keywords to add]
        self.removals = {}   # id -> [existing tags to remove]
        self.input_kws = []  # keyword bubbles typed by the user
        self.sort_state = {"col": None, "reverse": False}
        self._last_checked = None
        self.busy = False
        self.ui_queue = queue.Queue()

        self.api_key = tk.StringVar()
        self.org_id = tk.StringVar(value=cfg.get("org_id") or DEFAULT_ORG_ID)
        self.dataset_ref = tk.StringVar()
        self.filter_text = tk.StringVar()
        self.only_checked = tk.BooleanVar(value=False)
        self.only_staged = tk.BooleanVar(value=False)
        self.kw_var = tk.StringVar()
        self.status = tk.StringVar(value="Enter your API key to get started.")
        self.show_key = tk.BooleanVar(value=False)

        # fonts (resized together by apply_fonts)
        self.base_fonts = []
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallFont", "TkTooltipFont", "TkFixedFont"):
            try:
                self.base_fonts.append(tkfont.nametofont(name))
            except tk.TclError:
                pass
        self.bold_font = tkfont.Font(font="TkDefaultFont")
        self.title_font = tkfont.Font(font="TkDefaultFont")
        self.chip_font = tkfont.Font(font="TkDefaultFont")
        self.chip_font_strike = tkfont.Font(font="TkDefaultFont")
        self.chip_font_strike.configure(overstrike=True)

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.flows = []

        self._build_ui()
        self.apply_theme(self.theme)
        self.apply_fonts()
        self.refresh_tree()
        self.refresh_input_chips()

        self.filter_text.trace_add("write", lambda *_: self.refresh_tree())
        self.only_checked.trace_add("write", lambda *_: self.refresh_tree())
        self.only_staged.trace_add("write", lambda *_: self.refresh_tree())
        for seq, delta in (("<Control-plus>", 1), ("<Control-equal>", 1),
                           ("<Control-KP_Add>", 1), ("<Control-minus>", -1),
                           ("<Control-KP_Subtract>", -1)):
            self.bind_all(seq, lambda _e, d=delta: self.change_font(d))
        self.bind_all("<Control-0>", lambda _e: self.change_font(0, reset=True))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------ UI build
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}

        # --- top bar -------------------------------------------------------
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(top, text="data.public.lu  ·  Keyword Manager",
                  font=self.title_font).pack(side="left")
        self.btn_theme = ttk.Button(top, command=self.toggle_theme)
        self.btn_theme.pack(side="right")
        ttk.Button(top, text="A+", width=3,
                   command=lambda: self.change_font(1)).pack(side="right", padx=(4, 10))
        ttk.Button(top, text="A" + MINUS, width=3,
                   command=lambda: self.change_font(-1)).pack(side="right", padx=4)
        ttk.Label(top, text="Text size:", style="Muted.TLabel").pack(side="right", padx=4)

        # --- connection ----------------------------------------------------
        conn = ttk.LabelFrame(self, text="1 · Connection")
        conn.pack(fill="x", padx=10, pady=(8, 4))
        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(4, weight=1)

        ttk.Label(conn, text="API key:").grid(row=0, column=0, sticky="w", **pad)
        self.key_entry = ttk.Entry(conn, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Checkbutton(conn, text="show", variable=self.show_key,
                        command=self._toggle_key).grid(row=0, column=2, **pad)
        self.btn_test = ttk.Button(conn, text="Test key", command=self.test_key)
        self.btn_test.grid(row=0, column=3, **pad)

        ttk.Label(conn, text="Organisation ID:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.org_id).grid(row=1, column=1, sticky="ew", **pad)
        self.btn_load = ttk.Button(conn, text="Load all datasets",
                                   style="Accent.TButton", command=self.load_org_datasets)
        self.btn_load.grid(row=1, column=3, **pad)

        ttk.Label(conn, text="…or a single dataset (ID / slug / URL):").grid(
            row=0, column=4, sticky="w", **pad)
        ref_entry = ttk.Entry(conn, textvariable=self.dataset_ref)
        ref_entry.grid(row=1, column=4, sticky="ew", **pad)
        ref_entry.bind("<Return>", lambda _e: self.add_single_dataset())
        self.btn_add_one = ttk.Button(conn, text="Add dataset",
                                      command=self.add_single_dataset)
        self.btn_add_one.grid(row=1, column=5, **pad)

        # --- main split ----------------------------------------------------
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.LabelFrame(main, text="2 · Datasets  (tick the boxes of the ones to change)")
        main.add(left, weight=5)

        bar = ttk.Frame(left)
        bar.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(bar, text="Filter:").pack(side="left")
        ttk.Entry(bar, textvariable=self.filter_text, width=26).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="only ticked", variable=self.only_checked).pack(
            side="left", padx=6)
        ttk.Checkbutton(bar, text="only with staged changes",
                        variable=self.only_staged).pack(side="left", padx=6)
        self.count_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count_label.pack(side="right")

        bar2 = ttk.Frame(left)
        bar2.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(bar2, text="Tick all shown", command=self.check_all_shown).pack(side="left")
        ttk.Button(bar2, text="Untick all", command=self.uncheck_all).pack(side="left", padx=4)
        ttk.Button(bar2, text="Clear list", command=self.clear_list).pack(side="right")
        ttk.Button(bar2, text="Remove ticked from list",
                   command=self.remove_from_list).pack(side="right", padx=4)

        cols = ("check", "title", "id", "tags", "pending", "removal")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        self.col_base = {"check": 46, "title": 270, "id": 170, "tags": 250,
                         "pending": 150, "removal": 150}
        titles = {"title": "Title", "id": "ID", "tags": "Existing tags",
                  "pending": "+ To add", "removal": MINUS + " To remove"}
        self.tree.heading("check", text=CHECK_OFF, command=self.toggle_all_visible)
        self.tree.column("check", width=self.col_base["check"], stretch=False, anchor="center")
        for col, text in titles.items():
            self.tree.heading(col, text=text, command=lambda c=col: self.sort_by(c))
            self.tree.column(col, width=self.col_base[col], anchor="w")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_targets_changed())
        self.tree.bind("<space>", self.on_space)
        self.tree.bind("<Control-a>", lambda _e: (self.check_all_shown(), "break")[1])
        self.tree.bind("<Double-1>", self.open_in_browser)

        # --- right: keywords ----------------------------------------------
        right = ttk.LabelFrame(main, text="3 · Keywords")
        main.add(right, weight=3)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=3)
        right.rowconfigure(9, weight=2)

        self.sel_label = ttk.Label(right, text="", font=self.bold_font)
        self.sel_label.grid(row=0, column=0, sticky="w", **pad)

        legend = ttk.Frame(right)
        legend.grid(row=1, column=0, sticky="w", padx=6)
        ttk.Label(legend, text="existing", style="LegExisting.TLabel").pack(side="left", padx=(0, 4))
        ttk.Label(legend, text="+ will be ADDED", style="LegAdd.TLabel").pack(side="left", padx=4)
        ttk.Label(legend, text=MINUS + " will be REMOVED", style="LegRemove.TLabel").pack(
            side="left", padx=4)
        ttk.Label(legend, text="keyword to apply", style="LegKw.TLabel").pack(side="left", padx=4)

        ttk.Label(right, text="Tags on the ticked datasets  (click a tag to stage / undo removal)",
                  style="Muted.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self.flow_tags = TagFlow(right, self, self.on_tag_chip, lines=5)
        self.flow_tags.grid(row=3, column=0, sticky="nsew", **pad)

        ttk.Label(right, text="Keywords to apply  (type, then Enter or comma)",
                  style="Muted.TLabel").grid(row=4, column=0, sticky="w", **pad)
        entry_row = ttk.Frame(right)
        entry_row.grid(row=5, column=0, sticky="ew", **pad)
        entry_row.columnconfigure(0, weight=1)
        self.kw_entry = ttk.Entry(entry_row, textvariable=self.kw_var)
        self.kw_entry.grid(row=0, column=0, sticky="ew")
        self.kw_entry.bind("<Return>", lambda _e: (self.commit_entry(), "break")[1])
        self.kw_entry.bind("<KeyRelease>", self.on_kw_key)
        self.kw_entry.bind("<BackSpace>", self.on_kw_backspace)
        ttk.Button(entry_row, text="Clear", command=self.clear_input).grid(
            row=0, column=1, padx=(6, 0))
        self.flow_input = TagFlow(right, self, self.on_input_chip, lines=2,
                                  empty_text="Typed keywords appear here as bubbles.")
        self.flow_input.grid(row=6, column=0, sticky="ew", **pad)

        actions = ttk.Frame(right)
        actions.grid(row=7, column=0, sticky="ew", **pad)
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="+  Add to ticked", style="Add.TButton",
                   command=self.stage_add_clicked).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(actions, text=MINUS + "  Remove from ticked", style="Remove.TButton",
                   command=self.stage_remove_clicked).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Label(right, text="Popular keywords on loaded datasets  (click to use)",
                  style="Muted.TLabel").grid(row=8, column=0, sticky="w", **pad)
        self.flow_pop = TagFlow(right, self, self.on_pop_chip, lines=4,
                                empty_text="Load datasets to see their most used keywords.")
        self.flow_pop.grid(row=9, column=0, sticky="nsew", **pad)

        misc = ttk.Frame(right)
        misc.grid(row=10, column=0, sticky="ew", **pad)
        ttk.Button(misc, text="Un-stage ticked", command=self.unstage_selected).pack(side="left")
        ttk.Button(misc, text="Clear all staged", command=self.clear_all_staged).pack(
            side="left", padx=6)
        ttk.Button(misc, text="Import CSV…", command=self.import_csv).pack(side="right")
        self.flows = [self.flow_tags, self.flow_input, self.flow_pop]

        # --- upload + log --------------------------------------------------
        bottom = ttk.LabelFrame(self, text="4 · Upload")
        bottom.pack(fill="both", padx=10, pady=(4, 4))
        topline = ttk.Frame(bottom)
        topline.pack(fill="x", padx=4, pady=4)
        self.sum_ds = ttk.Label(topline, text="No staged changes.")
        self.sum_ds.pack(side="left")
        self.sum_add = ttk.Label(topline, text="", style="AddText.TLabel")
        self.sum_add.pack(side="left", padx=(10, 0))
        self.sum_rem = ttk.Label(topline, text="", style="RemText.TLabel")
        self.sum_rem.pack(side="left", padx=(10, 0))
        self.btn_upload = ttk.Button(topline, text="Upload staged changes",
                                     style="Accent.TButton", command=self.upload_changes)
        self.btn_upload.pack(side="right")
        self.progress = ttk.Progressbar(topline, length=220, mode="determinate")
        self.progress.pack(side="right", padx=10)

        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.log_box = tk.Text(log_frame, height=7, wrap="word", state="disabled",
                               relief="flat", highlightthickness=1, bd=0)
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        ttk.Label(self, textvariable=self.status, style="Status.TLabel", anchor="w").pack(
            fill="x", side="bottom")

    # -------------------------------------------------- theme and font size
    def chip_colors(self, kind):
        p = self.p
        return {
            "existing": (p["chip_bg"], p["chip_fg"], p["chip_bd"]),
            "add": (p["add_bg"], p["add_fg"], p["add_bd"]),
            "remove": (p["rem_bg"], p["rem_fg"], p["rem_bd"]),
            "kw": (p["kw_bg"], p["kw_fg"], p["kw_bd"]),
            "suggest": (p["sug_bg"], p["sug_fg"], p["sug_bd"]),
        }.get(kind, (p["chip_bg"], p["chip_fg"], p["chip_bd"]))

    def apply_theme(self, name):
        self.theme = name
        p = self.p = PALETTES[name]
        st = self.style
        dark = name == "dark"

        def hover(colour):
            return shade(colour, 1.15 if dark else 0.93)

        self.configure(bg=p["bg"])
        st.configure(".", background=p["bg"], foreground=p["fg"], bordercolor=p["border"],
                     lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["bg"],
                     fieldbackground=p["field"], focuscolor=p["accent"],
                     insertcolor=p["fg"], selectbackground=p["sel_bg"],
                     selectforeground=p["sel_fg"])
        st.configure("TFrame", background=p["bg"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("Muted.TLabel", foreground=p["muted"])
        st.configure("Status.TLabel", background=p["header"], foreground=p["muted"],
                     padding=(8, 3))
        st.configure("AddText.TLabel", foreground=p["add_btn"], font=self.bold_font)
        st.configure("RemText.TLabel", foreground=p["rem_btn"], font=self.bold_font)
        for style_name, bg, fg in (("LegExisting.TLabel", p["chip_bg"], p["chip_fg"]),
                                   ("LegAdd.TLabel", p["add_bg"], p["add_fg"]),
                                   ("LegRemove.TLabel", p["rem_bg"], p["rem_fg"]),
                                   ("LegKw.TLabel", p["kw_bg"], p["kw_fg"])):
            st.configure(style_name, background=bg, foreground=fg, padding=(8, 2))
        st.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                     lightcolor=p["bg"], darkcolor=p["bg"])
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"],
                     font=self.bold_font)

        def style_button(name, bg, fg, border):
            st.configure(name, background=bg, foreground=fg, bordercolor=border,
                         lightcolor=bg, darkcolor=bg, focuscolor=bg,
                         padding=(10, 4), relief="flat", borderwidth=1)
            st.map(name,
                   background=[("disabled", p["border"]), ("pressed", shade(bg, 0.8)),
                               ("active", hover(bg))],
                   foreground=[("disabled", p["muted"])],
                   lightcolor=[("active", hover(bg))], darkcolor=[("active", hover(bg))])

        style_button("TButton", p["btn"], p["btn_fg"], p["border"])
        style_button("Accent.TButton", p["accent"], "#ffffff", p["accent"])
        style_button("Add.TButton", p["add_btn"], "#ffffff", p["add_btn"])
        style_button("Remove.TButton", p["rem_btn"], "#ffffff", p["rem_btn"])

        st.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                     bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                     insertcolor=p["fg"], padding=3)
        st.map("TEntry", bordercolor=[("focus", p["accent"])],
               lightcolor=[("focus", p["accent"])], darkcolor=[("focus", p["accent"])])
        st.configure("TCheckbutton", background=p["bg"], foreground=p["fg"],
                     indicatorbackground=p["field"], indicatorforeground=p["fg"],
                     indicatorcolor=p["field"])
        st.map("TCheckbutton", background=[("active", p["bg"])],
               indicatorcolor=[("selected", p["accent"])])

        st.configure("Treeview", background=p["panel"], fieldbackground=p["panel"],
                     foreground=p["fg"], bordercolor=p["border"],
                     lightcolor=p["panel"], darkcolor=p["panel"])
        st.map("Treeview", background=[("selected", p["sel_bg"])],
               foreground=[("selected", p["sel_fg"])])
        st.configure("Treeview.Heading", background=p["header"], foreground=p["fg"],
                     bordercolor=p["border"], lightcolor=p["header"],
                     darkcolor=p["header"], relief="flat", padding=(6, 4))
        st.map("Treeview.Heading", background=[("active", hover(p["header"]))])

        for orient in ("Vertical", "Horizontal"):
            st.configure(f"{orient}.TScrollbar", background=p["btn"], troughcolor=p["bg"],
                         bordercolor=p["bg"], arrowcolor=p["fg"],
                         lightcolor=p["btn"], darkcolor=p["btn"])
            st.map(f"{orient}.TScrollbar", background=[("active", hover(p["btn"]))])
        st.configure("Horizontal.TProgressbar", background=p["accent"],
                     troughcolor=p["header"], bordercolor=p["border"],
                     lightcolor=p["accent"], darkcolor=p["accent"])
        st.configure("TPanedwindow", background=p["bg"])

        self.tree.tag_configure("pending", background=p["add_bg"], foreground=p["add_fg"])
        self.tree.tag_configure("removal", background=p["rem_bg"], foreground=p["rem_fg"])
        self.tree.tag_configure("both", background=p["both_bg"], foreground=p["both_fg"])
        self.log_box.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                               highlightbackground=p["border"], highlightcolor=p["border"],
                               selectbackground=p["sel_bg"], selectforeground=p["sel_fg"])
        self.btn_theme.configure(text="Light mode" if dark else "Dark mode")
        for flow in self.flows:
            flow.redraw()

    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")
        self.save_config()

    def apply_fonts(self):
        size = self.font_size
        for f in self.base_fonts:
            f.configure(size=size)
        self.bold_font.configure(size=size, weight="bold")
        self.title_font.configure(size=size + 4, weight="bold")
        for f in (self.chip_font, self.chip_font_strike):
            f.configure(size=max(size - 1, 7))
        base = tkfont.nametofont("TkDefaultFont")
        self.style.configure("Treeview", rowheight=base.metrics("linespace") + 10)
        for col, width in self.col_base.items():
            self.tree.column(col, width=int(width * size / 10))
        for flow in self.flows:
            flow.apply_height()
            flow.redraw()

    def change_font(self, delta, reset=False):
        self.font_size = 10 if reset else min(max(self.font_size + delta, 8), 26)
        self.apply_fonts()
        self.save_config()

    def save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps({
                "theme": self.theme, "font_size": self.font_size,
                "org_id": self.org_id.get().strip()}), encoding="utf-8")
        except OSError:
            pass  # settings are a convenience, never fatal

    def on_close(self):
        self.save_config()
        self.destroy()

    # ------------------------------------------------------------ plumbing
    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _poll_queue(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def ui(self, func):
        """Schedule func on the Tk main thread (safe from worker threads)."""
        self.ui_queue.put(func)

    def log(self, message):
        def _write():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.ui(_write)

    def set_status(self, message):
        self.ui(lambda: self.status.set(message))

    def set_busy(self, busy):
        def _apply():
            self.busy = busy
            state = "disabled" if busy else "normal"
            for b in (self.btn_test, self.btn_load, self.btn_add_one, self.btn_upload):
                b.configure(state=state)
        self.ui(_apply)

    def run_in_thread(self, target):
        if self.busy:
            messagebox.showinfo("Busy", "Another operation is still running.")
            return
        self.set_busy(True)
        self.busy = True

        def wrapper():
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - surface everything to the user
                self.log(f"UNEXPECTED ERROR: {exc}")
                self.set_status(f"Error: {exc}")
            finally:
                self.set_busy(False)

        threading.Thread(target=wrapper, daemon=True).start()

    def headers(self):
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        key = self.api_key.get().strip()
        if key:
            h["X-API-KEY"] = key
        return h

    def require_key(self):
        if not self.api_key.get().strip():
            messagebox.showwarning("API key missing", "Please enter your API key first.")
            return False
        return True

    # --------------------------------------------------------- API actions
    def test_key(self):
        if not self.require_key():
            return
        hdrs = self.headers()

        def work():
            self.set_status("Checking API key…")
            try:
                r = requests.get(f"{API_ENDPOINT}me/", headers=hdrs, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self.log(f"Could not connect: {exc}")
                self.set_status("Connection failed.")
                return
            if r.status_code == 200:
                me = r.json()
                name = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip()
                self.log(f"Authentication OK – logged in as {name or me.get('slug', '?')}.")
                self.set_status("API key is valid.")
            else:
                self.log(f"Authentication failed ({r.status_code}): {r.text[:300]}")
                self.set_status("API key rejected.")

        self.run_in_thread(work)

    def load_org_datasets(self):
        org = self.org_id.get().strip()
        if not org:
            messagebox.showwarning("Organisation ID missing", "Please enter an organisation ID.")
            return
        hdrs = self.headers()
        self.save_config()

        def work():
            self.log(f"Loading datasets of organisation {org}…")
            loaded, page = [], 1
            while True:
                self.set_status(f"Loading datasets… page {page} ({len(loaded)} so far)")
                try:
                    r = requests.get(
                        f"{API_ENDPOINT}datasets/",
                        params={"organization": org, "page": page, "page_size": PAGE_SIZE},
                        headers=hdrs, timeout=TIMEOUT)
                except requests.RequestException as exc:
                    self.log(f"ERROR loading page {page}: {exc}")
                    self.set_status("Loading failed.")
                    return
                if r.status_code != 200:
                    self.log(f"ERROR loading page {page}: {r.status_code} {r.text[:300]}")
                    self.set_status("Loading failed.")
                    return
                payload = r.json()
                items = payload.get("data", [])
                loaded.extend(items)
                if not items or not payload.get("next_page"):
                    break
                page += 1

            def apply():
                for ds in loaded:
                    self._store_dataset(ds)
                self.refresh_tree()
                self.rebuild_frequent()
            self.ui(apply)
            self.log(f"Loaded {len(loaded)} dataset(s).")
            self.set_status(f"Loaded {len(loaded)} dataset(s).")

        self.run_in_thread(work)

    def add_single_dataset(self):
        ref = extract_dataset_ref(self.dataset_ref.get())
        if not ref:
            messagebox.showwarning("Nothing entered", "Enter a dataset ID, slug or URL.")
            return
        hdrs = self.headers()

        def work():
            self.set_status(f"Fetching dataset {ref}…")
            try:
                r = requests.get(f"{API_ENDPOINT}datasets/{ref}/", headers=hdrs, timeout=TIMEOUT)
            except requests.RequestException as exc:
                self.log(f"ERROR fetching {ref}: {exc}")
                return
            if r.status_code != 200:
                self.log(f"ERROR fetching {ref}: {r.status_code} {r.text[:300]}")
                self.set_status("Dataset not found.")
                return
            ds = r.json()

            def apply():
                self._store_dataset(ds)
                self.checked.add(ds["id"])
                self.refresh_tree()
                self.rebuild_frequent()
                if self.tree.exists(ds["id"]):
                    self.tree.see(ds["id"])
            self.ui(apply)
            self.log(f"Added dataset: {ds.get('title')} ({ds['id']})")
            self.set_status("Dataset added and ticked.")

        self.run_in_thread(work)

    def upload_changes(self):
        if not self.pending and not self.removals:
            messagebox.showinfo("Nothing to upload", "No staged changes.")
            return
        if not self.require_key():
            return
        job_ids = list(dict.fromkeys(list(self.pending) + list(self.removals)))
        n_add = sum(len(v) for v in self.pending.values())
        n_rem = sum(len(v) for v in self.removals.values())
        if not messagebox.askyesno(
                "Confirm upload",
                f"Apply changes to {len(job_ids)} dataset(s) on data.public.lu?\n\n"
                f"  +  {n_add} keyword(s) to ADD\n"
                f"  {MINUS}  {n_rem} keyword(s) to REMOVE\n\n"
                f"All other existing tags are kept."):
            return
        jobs = {i: (list(self.pending.get(i, [])), list(self.removals.get(i, [])))
                for i in job_ids}
        hdrs = self.headers()

        def work():
            ok = unchanged = failed = 0
            self.ui(lambda: self.progress.configure(maximum=len(jobs), value=0))
            for i, (ds_id, (adds, rems)) in enumerate(jobs.items(), start=1):
                title = self.datasets.get(ds_id, {}).get("title", ds_id)
                self.set_status(f"Uploading {i}/{len(jobs)}: {title}")
                url = f"{API_ENDPOINT}datasets/{ds_id}/"
                try:
                    r = requests.get(url, headers=hdrs, timeout=TIMEOUT)
                    if r.status_code != 200:
                        self.log(f"FAILED {ds_id}: could not fetch ({r.status_code}) {r.text[:200]}")
                        failed += 1
                        continue
                    current = normalise_tags(r.json().get("tags"))
                    rem_keys = {t.strip().casefold() for t in rems}
                    kept = [t for t in current if t.strip().casefold() not in rem_keys]
                    removed = [t for t in current if t.strip().casefold() in rem_keys]
                    combined, added = merge_tags(kept, adds)
                    if not added and not removed:
                        self.log(f"UNCHANGED {ds_id} ({title}): nothing to add or remove.")
                        unchanged += 1
                        self.ui(lambda d=ds_id, t=current: self._mark_done(d, t))
                        continue
                    u = requests.put(url, headers=hdrs, json={"tags": combined}, timeout=TIMEOUT)
                    if u.status_code in (200, 201):
                        new_tags = normalise_tags(u.json().get("tags"))
                        self.log(f"OK {ds_id} ({title}): added {added}, removed {removed}")
                        ok += 1
                        self.ui(lambda d=ds_id, t=new_tags: self._mark_done(d, t))
                    else:
                        self.log(f"FAILED {ds_id} ({title}): {u.status_code} {u.text[:300]}")
                        failed += 1
                except requests.RequestException as exc:
                    self.log(f"FAILED {ds_id} ({title}): {exc}")
                    failed += 1
                finally:
                    self.ui(lambda v=i: self.progress.configure(value=v))
                    time.sleep(0.3)  # be polite to the API

            summary = f"Upload finished – updated: {ok}, unchanged: {unchanged}, failed: {failed}"
            self.log(summary)
            self.set_status(summary)
            self.ui(self.refresh_tree)
            self.ui(self.rebuild_frequent)

        self.run_in_thread(work)

    def _mark_done(self, ds_id, tags):
        # Called on the main thread after a dataset was handled successfully.
        if ds_id in self.datasets:
            self.datasets[ds_id]["tags"] = tags
        self.pending.pop(ds_id, None)
        self.removals.pop(ds_id, None)

    # -------------------------------------------------------- data handling
    def _store_dataset(self, ds):
        ds_id = ds["id"]
        if ds_id not in self.datasets:
            self.order.append(ds_id)
        self.datasets[ds_id] = {
            "id": ds_id,
            "title": ds.get("title", ""),
            "slug": ds.get("slug", ""),
            "tags": normalise_tags(ds.get("tags")),
            "page": ds.get("page", ""),
        }

    def visible_ids(self):
        needle = self.filter_text.get().strip().casefold()
        only_checked, only_staged = self.only_checked.get(), self.only_staged.get()
        ids = []
        for ds_id in self.order:
            if only_checked and ds_id not in self.checked:
                continue
            if only_staged and ds_id not in self.pending and ds_id not in self.removals:
                continue
            ds = self.datasets[ds_id]
            haystack = " ".join([ds["title"], ds_id, " ".join(ds["tags"]),
                                 " ".join(self.pending.get(ds_id, [])),
                                 " ".join(self.removals.get(ds_id, []))]).casefold()
            if not needle or needle in haystack:
                ids.append(ds_id)
        col = self.sort_state["col"]
        if col:
            def key(i):
                d = self.datasets[i]
                if col == "title":
                    return d["title"].casefold()
                if col == "id":
                    return i
                if col == "tags":
                    return len(d["tags"])
                if col == "pending":
                    return len(self.pending.get(i, []))
                return len(self.removals.get(i, []))
            ids.sort(key=key, reverse=self.sort_state["reverse"])
        return ids

    def refresh_tree(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        ids = self.visible_ids()
        for ds_id in ids:
            ds = self.datasets[ds_id]
            pend = self.pending.get(ds_id, [])
            rem = self.removals.get(ds_id, [])
            row_tag = "both" if pend and rem else "pending" if pend else "removal" if rem else None
            self.tree.insert(
                "", "end", iid=ds_id,
                values=(CHECK_ON if ds_id in self.checked else CHECK_OFF,
                        ds["title"], ds_id, ", ".join(ds["tags"]),
                        "+ " + ", ".join(pend) if pend else "",
                        MINUS + " " + ", ".join(rem) if rem else ""),
                tags=(row_tag,) if row_tag else ())
        keep = [i for i in ids if i in selected]
        if keep:
            self.tree.selection_set(keep)
        self.count_label.configure(
            text=f"{len(ids)} shown · {len(self.order)} loaded · {len(self.checked)} ticked")
        n_add = sum(len(v) for v in self.pending.values())
        n_rem = sum(len(v) for v in self.removals.values())
        n_ds = len(set(self.pending) | set(self.removals))
        if n_ds:
            self.sum_ds.configure(text=f"{n_ds} dataset(s) with staged changes:")
            self.sum_add.configure(text=f"+{n_add} to add")
            self.sum_rem.configure(text=f"{MINUS}{n_rem} to remove")
        else:
            self.sum_ds.configure(text="No staged changes.")
            self.sum_add.configure(text="")
            self.sum_rem.configure(text="")
        self._update_header_check()
        self.on_targets_changed()

    def _update_header_check(self):
        children = self.tree.get_children()
        all_on = bool(children) and all(c in self.checked for c in children)
        self.tree.heading("check", text=CHECK_ON if all_on else CHECK_OFF)

    def rebuild_frequent(self):
        counter = Counter(t for d in self.datasets.values() for t in d["tags"])
        chips = [{"text": f"{tag}  {n}", "kind": "suggest", "icon": "+", "key": tag}
                 for tag, n in counter.most_common(60)]
        self.flow_pop.set_chips(chips)

    def sort_by(self, col):
        if self.sort_state["col"] == col:
            self.sort_state["reverse"] = not self.sort_state["reverse"]
        else:
            self.sort_state = {"col": col, "reverse": False}
        self.refresh_tree()

    # -------------------------------------------------------- tick boxes
    def target_ids(self):
        """Ticked datasets; if nothing is ticked, the highlighted rows."""
        if self.checked:
            return [i for i in self.order if i in self.checked]
        return [i for i in self.tree.selection() if i in self.datasets]

    def _set_check_cells(self, ids):
        for i in ids:
            if self.tree.exists(i):
                self.tree.set(i, "check", CHECK_ON if i in self.checked else CHECK_OFF)
        self.count_label.configure(
            text=f"{len(self.tree.get_children())} shown · {len(self.order)} loaded · "
                 f"{len(self.checked)} ticked")
        self._update_header_check()
        self.on_targets_changed()

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        new_state = row not in self.checked
        ids = [row]
        children = list(self.tree.get_children())
        if event.state & 0x0001 and self._last_checked in children:  # Shift = range
            a, b = children.index(self._last_checked), children.index(row)
            ids = children[min(a, b):max(a, b) + 1]
        for i in ids:
            (self.checked.add if new_state else self.checked.discard)(i)
        self._last_checked = row
        self._set_check_cells(ids)
        return "break"

    def on_space(self, _event):
        sel = [i for i in self.tree.selection()]
        if sel:
            new_state = not all(i in self.checked for i in sel)
            for i in sel:
                (self.checked.add if new_state else self.checked.discard)(i)
            self._set_check_cells(sel)
        return "break"

    def toggle_all_visible(self):
        children = list(self.tree.get_children())
        if not children:
            return
        if all(c in self.checked for c in children):
            self.checked.difference_update(children)
        else:
            self.checked.update(children)
        self._set_check_cells(children)

    def check_all_shown(self):
        children = list(self.tree.get_children())
        self.checked.update(children)
        self._set_check_cells(children)

    def uncheck_all(self):
        ids = list(self.checked)
        self.checked.clear()
        if self.only_checked.get():
            self.refresh_tree()
        else:
            self._set_check_cells(ids)

    def open_in_browser(self, event):
        if self.tree.identify_column(event.x) == "#1":
            return
        row = self.tree.identify_row(event.y)
        if row and self.datasets[row].get("page"):
            webbrowser.open(self.datasets[row]["page"])

    # ----------------------------------------------- remove from list (local)
    def remove_from_list(self):
        if self.busy:
            messagebox.showinfo("Busy", "Wait until the running operation has finished.")
            return
        ids = self.target_ids()
        if not ids:
            messagebox.showinfo("Nothing ticked", "Tick the datasets to remove from the list.")
            return
        staged = [i for i in ids if i in self.pending or i in self.removals]
        msg = (f"Remove {len(ids)} dataset(s) from this list?\n\n"
               f"This only removes them from this window – nothing is deleted "
               f"on data.public.lu.")
        if staged:
            msg += f"\n\n{len(staged)} of them have staged changes that will be discarded."
        if messagebox.askyesno("Remove from list", msg):
            self._drop(ids)
            self.set_status(f"Removed {len(ids)} dataset(s) from the list.")

    def clear_list(self):
        if self.busy:
            messagebox.showinfo("Busy", "Wait until the running operation has finished.")
            return
        if not self.order:
            return
        msg = ("Remove ALL datasets from this list?\n\nThis only clears this window – "
               "nothing is deleted on data.public.lu.")
        if self.pending or self.removals:
            msg += "\n\nAll staged changes will be discarded."
        if messagebox.askyesno("Clear list", msg):
            self._drop(list(self.order))
            self.set_status("List cleared.")

    def _drop(self, ids):
        for i in ids:
            self.datasets.pop(i, None)
            self.pending.pop(i, None)
            self.removals.pop(i, None)
            self.checked.discard(i)
        self.order = [i for i in self.order if i in self.datasets]
        self._last_checked = None
        self.refresh_tree()
        self.rebuild_frequent()

    # ----------------------------------------------- keyword bubbles (input)
    def on_kw_key(self, event):
        if event.keysym in ("Return", "BackSpace"):
            return
        if "," in self.kw_var.get() or ";" in self.kw_var.get():
            self.commit_entry()

    def on_kw_backspace(self, _event):
        if not self.kw_var.get() and self.input_kws:
            self.input_kws.pop()
            self.refresh_input_chips()

    def commit_entry(self):
        new = parse_keywords(self.kw_var.get())
        self.kw_var.set("")
        known = {k.casefold() for k in self.input_kws}
        changed = False
        for kw in new:
            if kw.casefold() not in known:
                self.input_kws.append(kw)
                known.add(kw.casefold())
                changed = True
        if changed:
            self.refresh_input_chips()

    def clear_input(self):
        self.kw_var.set("")
        self.input_kws = []
        self.refresh_input_chips()

    def refresh_input_chips(self):
        self.flow_input.set_chips(
            [{"text": kw, "kind": "kw", "icon": "×", "key": kw} for kw in self.input_kws])

    def on_input_chip(self, chip):
        self.input_kws = [k for k in self.input_kws if k != chip["key"]]
        self.refresh_input_chips()

    def on_pop_chip(self, chip):
        if chip["key"].casefold() not in {k.casefold() for k in self.input_kws}:
            self.input_kws.append(chip["key"])
            self.refresh_input_chips()

    # ------------------------------------- bubbles for ticked datasets' tags
    def on_targets_changed(self):
        ids = self.target_ids()
        n = len(ids)
        if self.checked:
            self.sel_label.configure(text=f"{n} dataset(s) ticked")
        elif n:
            self.sel_label.configure(text=f"{n} dataset(s) highlighted  (tick boxes to select)")
        else:
            self.sel_label.configure(text="No datasets ticked")

        counter, display = Counter(), {}
        staged_rm, staged_add, add_display = Counter(), Counter(), {}
        for i in ids:
            rem_keys = {t.casefold() for t in self.removals.get(i, [])}
            for t in self.datasets[i]["tags"]:
                k = t.casefold()
                counter[k] += 1
                display.setdefault(k, t)
                if k in rem_keys:
                    staged_rm[k] += 1
            for t in self.pending.get(i, []):
                k = t.casefold()
                staged_add[k] += 1
                add_display.setdefault(k, t)

        chips = []
        for k, cnt in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            label = display[k] + (f"  {cnt}/{n}" if n > 1 else "")
            if staged_rm[k]:
                if n > 1 and staged_rm[k] < cnt:
                    label += f"  ({MINUS}{staged_rm[k]})"
                chips.append({"text": label, "kind": "remove", "icon": "↺",
                              "action": "toggle_remove", "key": k})
            else:
                chips.append({"text": label, "kind": "existing", "icon": "×",
                              "action": "toggle_remove", "key": k})
        for k, cnt in sorted(staged_add.items(), key=lambda kv: (-kv[1], kv[0])):
            label = "+ " + add_display[k] + (f"  {cnt}/{n}" if n > 1 else "")
            chips.append({"text": label, "kind": "add", "icon": "×",
                          "action": "unstage_add", "key": k})
        empty = ("Tick datasets in the table to see their tags here." if not ids
                 else "The ticked datasets have no tags.")
        self.flow_tags.set_chips(chips, empty_text=empty)

    def on_tag_chip(self, chip):
        ids = self.target_ids()
        key = chip["key"]
        if chip["action"] == "unstage_add":
            self.unstage_add(ids, key)
        else:
            have = [i for i in ids
                    if key in {t.casefold() for t in self.datasets[i]["tags"]}]
            all_staged = bool(have) and all(
                key in {t.casefold() for t in self.removals.get(i, [])} for i in have)
            if all_staged:
                self.unstage_removal(have, key)
            else:
                self.stage_removal(have, [key])
        self.refresh_tree()

    # --------------------------------------------------------------- staging
    def stage(self, ids, keywords):
        # Stage keywords to ADD; returns the number of assignments changed.
        count = 0
        for ds_id in ids:
            ds = self.datasets.get(ds_id)
            if not ds:
                continue
            # Adding a tag that is staged for removal just cancels the removal.
            rem = self.removals.get(ds_id, [])
            rem_keys = {t.casefold() for t in rem}
            rest = []
            for kw in keywords:
                key = kw.strip().casefold()
                if key in rem_keys:
                    rem[:] = [t for t in rem if t.casefold() != key]
                    rem_keys.discard(key)
                    count += 1
                else:
                    rest.append(kw)
            if ds_id in self.removals and not self.removals[ds_id]:
                del self.removals[ds_id]
            already = ds["tags"] + self.pending.get(ds_id, [])
            _, added = merge_tags(already, rest)
            if added:
                self.pending.setdefault(ds_id, []).extend(added)
                count += len(added)
        return count

    def stage_removal(self, ids, keywords):
        # Stage keywords to REMOVE; datasets lacking a keyword are skipped.
        count = 0
        for ds_id in ids:
            ds = self.datasets.get(ds_id)
            if not ds:
                continue
            existing = {t.casefold(): t for t in ds["tags"]}
            staged = {t.casefold() for t in self.removals.get(ds_id, [])}
            for kw in keywords:
                key = kw.strip().casefold()
                if not key:
                    continue
                pend = self.pending.get(ds_id, [])
                if any(p.casefold() == key for p in pend):  # cancel a staged addition
                    pend[:] = [p for p in pend if p.casefold() != key]
                    if not pend:
                        self.pending.pop(ds_id, None)
                    count += 1
                if key in existing and key not in staged:
                    self.removals.setdefault(ds_id, []).append(existing[key])
                    staged.add(key)
                    count += 1
        return count

    def unstage_add(self, ids, key):
        for i in ids:
            if i in self.pending:
                self.pending[i] = [t for t in self.pending[i] if t.casefold() != key]
                if not self.pending[i]:
                    del self.pending[i]

    def unstage_removal(self, ids, key):
        for i in ids:
            if i in self.removals:
                self.removals[i] = [t for t in self.removals[i] if t.casefold() != key]
                if not self.removals[i]:
                    del self.removals[i]

    def _targets_and_keywords(self):
        self.commit_entry()
        ids = self.target_ids()
        if not ids:
            messagebox.showinfo("Nothing ticked", "Tick at least one dataset first.")
            return None, None
        if not self.input_kws:
            messagebox.showinfo("No keywords",
                                "Type at least one keyword (Enter or comma makes a bubble).")
            return None, None
        return ids, list(self.input_kws)

    def stage_add_clicked(self):
        ids, kws = self._targets_and_keywords()
        if ids is None:
            return
        n = self.stage(ids, kws)
        self.refresh_tree()
        self.set_status(f"Staged {n} addition(s) for {len(ids)} dataset(s) – shown in green. "
                        f"Nothing uploaded yet.")

    def stage_remove_clicked(self):
        ids, kws = self._targets_and_keywords()
        if ids is None:
            return
        n = self.stage_removal(ids, kws)
        self.refresh_tree()
        self.set_status(f"Staged {n} removal(s) across {len(ids)} dataset(s) – shown in red "
                        f"(datasets without the keyword are skipped). Nothing uploaded yet.")

    def unstage_selected(self):
        for ds_id in self.target_ids():
            self.pending.pop(ds_id, None)
            self.removals.pop(ds_id, None)
        self.refresh_tree()

    def clear_all_staged(self):
        if (self.pending or self.removals) and messagebox.askyesno(
                "Clear", "Discard all staged changes?"):
            self.pending.clear()
            self.removals.clear()
            self.refresh_tree()

    # ------------------------------------------------------------ CSV import
    def import_csv(self):
        path = filedialog.askopenfilename(
            title="Select CSV (ID|keyword – prefix a keyword with - to remove it)",
            filetypes=[("CSV / text", "*.csv *.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh, delimiter="|")
                if not reader.fieldnames or not {"ID", "keyword"} <= set(reader.fieldnames):
                    messagebox.showerror("Invalid CSV", "The file needs the header:  ID|keyword")
                    return
                rows = [(r["ID"].strip(), parse_keywords(r["keyword"] or ""))
                        for r in reader if (r.get("ID") or "").strip()]
        except OSError as exc:
            messagebox.showerror("Cannot read file", str(exc))
            return

        missing = [i for i, _ in rows if i not in self.datasets]
        hdrs = self.headers()

        def work():
            fetched = []
            for n, ds_id in enumerate(dict.fromkeys(missing), start=1):
                self.set_status(f"Fetching unknown dataset {ds_id} ({n})…")
                try:
                    r = requests.get(f"{API_ENDPOINT}datasets/{ds_id}/",
                                     headers=hdrs, timeout=TIMEOUT)
                    if r.status_code == 200:
                        fetched.append(r.json())
                    else:
                        self.log(f"CSV: dataset {ds_id} not found ({r.status_code}).")
                except requests.RequestException as exc:
                    self.log(f"CSV: error fetching {ds_id}: {exc}")

            def apply():
                for ds in fetched:
                    self._store_dataset(ds)
                total = 0
                for i, kws in rows:
                    adds = [k for k in kws if not k.startswith("-")]
                    rems = [k[1:].strip() for k in kws if k.startswith("-") and k[1:].strip()]
                    total += self.stage([i], adds) + self.stage_removal([i], rems)
                self.refresh_tree()
                self.rebuild_frequent()
                self.log(f"CSV import: staged {total} change(s) from {len(rows)} row(s).")
                self.set_status("CSV staged – review, then upload.")
            self.ui(apply)

        self.run_in_thread(work)


if __name__ == "__main__":
    KeywordManager().mainloop()
