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

File uploader tab (second tab): add files or links to existing datasets.
  * Pick one or more datasets from the loaded list, fill in path / type /
    format / title / description and add the entry to the upload queue.
  * Or import a CSV / Excel table (columns: dataset_id, operation_type
    [file_upload | add_link], file_path, url, type, format, title,
    description) - file names are resolved against the "Data folder".
  * Review / edit the queue, then "Upload queue" sends everything (chunked
    upload, 10 MB parts) and writes an output_report_<timestamp>.csv.

Extras: dark / light theme, adjustable text size (A− / A+, Ctrl +/−/0),
"Remove from list" (local only!), CSV import (ID|keyword, prefix a keyword
with "-" to remove it). Theme, text size and organisation ID are remembered in
~/.datapublic_keyword_manager.json – the API key is never stored.

Requirements: Python 3.8+, `pip install requests` (tkinter ships with Python;
on Debian/Ubuntu: `sudo apt install python3-tk`).
"""

import csv
import io
import json
import logging
import math
import os
import queue
import re
import threading
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4
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
# File uploader: constants, helpers and upload engine
# (ported from the standalone data_public_fileUploader_GUI.py)
# ----------------------------------------------------------------------------
SUPPORTED_TYPES = {
    "main": "Fichier principal",
    "documentation": "Documentation",
    "update": "Mise à jour",
    "api": "API",
    "code": "Dépôt de code",
    "other": "Autre",
}

SUPPORTED_FORMATS = {
    "CSV": "text/csv",
    "JSON": "application/json",
    "GEOJSON": "application/geo+json",
    "JPG": "image/jpeg",
    "PNG": "image/png",
    "HTML": "text/html",
    "ZIP": "application/zip",
    "OTHER": "application/octet-stream",
    "URL": "url",
}

EXT_TO_FORMAT = {
    ".csv": "CSV", ".json": "JSON", ".geojson": "GEOJSON", ".jpg": "JPG",
    ".jpeg": "JPG", ".png": "PNG", ".html": "HTML", ".htm": "HTML", ".zip": "ZIP",
}
TYPE_DISPLAY = [f"{k} ({v})" for k, v in SUPPORTED_TYPES.items()]
REQUIRED_COLUMNS = ["dataset_id", "operation_type"]
TABLE_COLUMNS = ["dataset_id", "operation_type", "file_path", "url",
                 "type", "format", "title", "description"]
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
STATUS_TEXT = {"queued": "queued", "invalid": "⚠ invalid", "uploading": "⟳ uploading",
               "done": "✔ done", "error": "✖ error"}


def clean(value):
    """Convert table values (CSV / Excel / pandas) into clean strings."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def count_resources(ds):
    """Number of files of a dataset, if the API response tells us."""
    res = ds.get("resources")
    if isinstance(res, list):
        return len(res)
    if isinstance(res, dict) and isinstance(res.get("total"), int):
        return res["total"]
    return ""


def convert_type(value):
    value = clean(value).lower()
    if value.endswith(".0") and value[:-2].isdigit():  # Excel numbers
        value = value[:-2]
    if value in SUPPORTED_TYPES:
        return value
    numeric = {"0": "main", "1": "documentation", "2": "update",
               "3": "api", "4": "code", "5": "other"}
    french = {"fichier principal": "main", "documentation": "documentation",
              "mise à jour": "update", "mise a jour": "update", "api": "api",
              "dépôt de code": "code", "depot de code": "code", "autre": "other"}
    if value in numeric:
        return numeric[value]
    if value in french:
        return french[value]
    raise ValueError(f"Invalid type '{value}'")


def resolve_table_path(file_name, data_folder):
    """File names from an input table are looked up in the Data folder
    (only the file name is used, as in the original tool). An absolute path
    that exists is used as it is."""
    file_name = clean(file_name)
    if not file_name:
        return ""
    candidate = Path(file_name)
    if candidate.is_absolute() and candidate.is_file():
        return str(candidate)
    if data_folder:
        return os.path.join(os.path.abspath(data_folder), os.path.basename(file_name))
    return file_name


def validate_op(item):
    """Return an error message, or '' if the queue item is valid."""
    if not clean(item.get("dataset_id")):
        return "dataset_id is missing"
    op = clean(item.get("operation"))
    if op == "file_upload":
        path = clean(item.get("file_path"))
        if not path:
            return "file_path is missing"
        fp = Path(path)
        if not fp.exists():
            return f"File not found: {fp}"
        if not fp.is_file():
            return f"Not a file: {fp}"
        try:
            convert_type(item.get("type"))
        except ValueError as exc:
            return str(exc)
        fmt = clean(item.get("format")).upper()
        if fmt not in SUPPORTED_FORMATS:
            return (f"Unsupported format '{fmt}' (allowed: "
                    f"{', '.join(SUPPORTED_FORMATS)})")
    elif op == "add_link":
        if not clean(item.get("url")).startswith(("http://", "https://")):
            return "URL is missing or invalid (must start with http:// or https://)"
    else:
        return f"Invalid operation_type '{op}' (use file_upload or add_link)"
    return ""


def read_input_table(path):
    """Read a CSV / XLSX / XLS table and return a list of {column: text}."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p}")
    ext = p.suffix.lower()
    if ext == ".csv":
        text = p.read_text(encoding="utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;|\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        rows = [{(k or "").strip(): clean(v) for k, v in r.items()
                 if k is not None and not isinstance(v, list)} for r in reader]
    elif ext in (".xlsx", ".xls"):
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("Reading Excel files needs pandas and openpyxl:\n"
                               "    pip install pandas openpyxl") from exc
        engine = "openpyxl" if ext == ".xlsx" else "xlrd"
        df = pd.read_excel(p, engine=engine, keep_default_na=False)
        df.columns = [str(c).strip() for c in df.columns]
        headers = list(df.columns)
        rows = [{c: clean(v) for c, v in rec.items()} for rec in df.to_dict("records")]
    else:
        raise ValueError("Unsupported input file type.\n\n"
                         "Please select a CSV, XLSX, or XLS file.")
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise ValueError("The following required columns are missing:\n\n"
                         + "\n".join(f"- {c}" for c in missing)
                         + "\n\nExpected columns are:\n\n"
                         + "\n".join(f"- {c}" for c in TABLE_COLUMNS))
    return rows


class ResourceUploader:
    """Adds files / links to existing datasets (logic of the original tool)."""

    def __init__(self, api_key, progress_callback=None):
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "X-API-KEY": api_key})
        self.progress_callback = progress_callback

    def _progress(self, current, total, message=""):
        if self.progress_callback:
            try:
                self.progress_callback(current, total, message)
            except Exception:  # noqa: BLE001 - never let the GUI break an upload
                logging.exception("Progress callback failed")

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            return {"raw_response": response.text}

    @staticmethod
    def _resource_id(data):
        if not isinstance(data, dict):
            return None
        if data.get("id"):
            return data["id"]
        resource = data.get("resource")
        if isinstance(resource, dict) and resource.get("id"):
            return resource["id"]
        return None

    def upload_file(self, row):
        dataset_id = clean(row["dataset_id"])
        file_path = Path(clean(row["file_path"]))
        if not file_path.exists():
            return {"status": "error", "error": f"File not found: {file_path}"}
        if not file_path.is_file():
            return {"status": "error", "error": f"Path is not a file: {file_path}"}

        fmt = clean(row["format"]).upper()
        mime_type = SUPPORTED_FORMATS.get(fmt, "application/octet-stream")
        upload_url = f"{API_ENDPOINT}datasets/{dataset_id}/upload/"
        file_size = os.path.getsize(file_path)
        parts = max(1, math.ceil(file_size / CHUNK_SIZE))
        upload_uuid = str(uuid4())
        self._progress(0, parts, f"Starting upload: {file_path.name} "
                                 f"({file_size:,} bytes, {parts} part(s))")
        try:
            last_response_data = None
            with file_path.open("rb") as handle:
                chunk_index = 0
                chunk_offset = 0
                while True:
                    chunk_data = handle.read(CHUNK_SIZE)  # one chunk in memory at a time
                    if not chunk_data:
                        break
                    chunk_size = len(chunk_data)
                    self._progress(chunk_index, parts,
                                   f"Uploading {file_path.name}: part {chunk_index + 1} of {parts}")
                    data = {
                        "partindex": str(chunk_index),
                        "partbyteoffset": str(chunk_offset),
                        "chunksize": str(chunk_size),
                        "totalparts": str(parts),
                        "size": str(file_size),
                        "filename": file_path.name,
                        "uuid": upload_uuid,
                    }
                    files = {"file": (file_path.name, chunk_data, mime_type)}
                    response = self.session.post(upload_url, data=data, files=files,
                                                 timeout=(30, 1800))
                    if not response.ok:
                        return {"status": "error", "code": response.status_code,
                                "error": f"Chunk {chunk_index + 1} of {parts} failed with HTTP "
                                         f"{response.status_code}:\n{response.text}"}
                    last_response_data = self._json(response)
                    chunk_index += 1
                    chunk_offset += chunk_size
                    self._progress(chunk_index, parts,
                                   f"Uploaded {file_path.name}: part {chunk_index} of {parts}")
                    if parts == 1:
                        # Small files: the single request is the complete upload;
                        # do NOT send a second POST to finalise it.
                        break

            resource_id = self._resource_id(last_response_data)

            if parts > 1:
                self._progress(parts, parts,
                               f"All chunks uploaded for {file_path.name}. Finalizing...")
                finalize = self.session.post(
                    upload_url,
                    data={"uuid": upload_uuid, "filename": file_path.name,
                          "size": str(file_size), "totalparts": str(parts)},
                    timeout=60)
                if not finalize.ok:
                    return {"status": "error", "code": finalize.status_code,
                            "error": f"Upload finalization failed with HTTP "
                                     f"{finalize.status_code}:\n{finalize.text}"}
                last_response_data = self._json(finalize)
                resource_id = self._resource_id(last_response_data)

            if not resource_id:
                return {"status": "error",
                        "error": "The file upload request succeeded, but the API did not "
                                 f"return a resource ID.\n\nAPI response:\n{last_response_data}"}

            metadata = {"type": convert_type(row["type"]), "format": fmt}
            if clean(row.get("title")):
                metadata["title"] = clean(row["title"])
            if clean(row.get("description")):
                metadata["description"] = clean(row["description"])
            update = self.session.put(
                f"{API_ENDPOINT}datasets/{dataset_id}/resources/{resource_id}/",
                json=metadata, timeout=60)
            if not update.ok:
                return {"status": "error", "resource_id": resource_id,
                        "error": f"Metadata update failed with HTTP "
                                 f"{update.status_code}:\n{update.text}"}
            self._progress(parts, parts, f"Completed: {file_path.name}")
            return {"status": "success", "resource_id": resource_id}

        except requests.RequestException as exc:
            logging.exception("HTTP error during file upload")
            return {"status": "error", "error": f"HTTP/network error: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logging.exception("File upload failed")
            return {"status": "error", "error": str(exc)}

    def add_link(self, row):
        dataset_id = clean(row["dataset_id"])
        url_value = clean(row["url"])
        if not url_value.startswith(("http://", "https://")):
            return {"status": "error", "error": f"Invalid URL: {url_value}"}
        payload = {"title": clean(row.get("title")), "description": clean(row.get("description")),
                   "url": url_value, "type": "other", "filetype": "remote"}
        try:
            response = self.session.post(f"{API_ENDPOINT}datasets/{dataset_id}/resources/",
                                         json=payload, timeout=60)
            if not response.ok:
                return {"status": "error", "code": response.status_code,
                        "error": response.text}
            return {"status": "success", "resource_id": response.json().get("id")}
        except requests.RequestException as exc:
            logging.exception("HTTP error while creating URL resource")
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logging.exception("URL resource creation failed")
            return {"status": "error", "error": str(exc)}


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
        self.geometry("1400x960")
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
        self.queue = []      # file-uploader queue items
        self._uid = 0
        self.edit_uid = None
        self.col_bases = []  # (treeview, {column: base width}) for font scaling
        self._auto_title = True
        self._auto_format = True
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
        self.data_folder = tk.StringVar(value=cfg.get("data_folder") or "")
        self.up_filter = tk.StringVar()
        self.f_op = tk.StringVar(value="file_upload")
        self.f_path = tk.StringVar()
        self.f_url = tk.StringVar()
        self.f_type = tk.StringVar(value=TYPE_DISPLAY[0])
        self.f_format = tk.StringVar()
        self.f_title = tk.StringVar()

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
        self.up_filter.trace_add("write", lambda *_: self.refresh_up_tree())
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

        ttk.Label(self, textvariable=self.status, style="Status.TLabel", anchor="w").pack(
            fill="x", side="bottom")

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

        # --- mode tabs -----------------------------------------------------
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(4, 6))
        self.tab_kw = ttk.Frame(self.notebook)
        self.tab_up = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_kw, text="Keyword editor")
        self.notebook.add(self.tab_up, text="File uploader")

        # --- keyword editor: main split -------------------------------------
        main = ttk.PanedWindow(self.tab_kw, orient="horizontal")
        main.pack(fill="both", expand=True, padx=4, pady=4)

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
        self.col_bases.append((self.tree, self.col_base))
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
        bottom = ttk.LabelFrame(self.tab_kw, text="4 · Upload keyword changes")
        bottom.pack(fill="both", padx=4, pady=(4, 4))
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

        self._build_upload_tab()

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
               lightcolor=[("focus", p["accent"])], darkcolor=[("focus", p["accent"])],
               fieldbackground=[("disabled", p["header"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TCombobox", fieldbackground=p["field"], background=p["btn"],
                     foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"],
                     lightcolor=p["field"], darkcolor=p["field"], padding=3,
                     selectbackground=p["field"], selectforeground=p["fg"])
        st.map("TCombobox",
               fieldbackground=[("disabled", p["header"]), ("readonly", p["field"])],
               foreground=[("disabled", p["muted"])],
               selectbackground=[("readonly", p["field"])],
               selectforeground=[("readonly", p["fg"])],
               bordercolor=[("focus", p["accent"])])
        self.option_add("*TCombobox*Listbox.background", p["field"])
        self.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["sel_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["sel_fg"])
        st.configure("TRadiobutton", background=p["bg"], foreground=p["fg"],
                     indicatorbackground=p["field"], indicatorforeground=p["fg"],
                     indicatorcolor=p["field"])
        st.map("TRadiobutton", background=[("active", p["bg"])],
               indicatorcolor=[("selected", p["accent"])])
        st.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                     lightcolor=p["bg"], darkcolor=p["bg"], tabmargins=(0, 4, 0, 0))
        st.configure("TNotebook.Tab", background=p["header"], foreground=p["muted"],
                     padding=(22, 7), bordercolor=p["border"], lightcolor=p["header"],
                     darkcolor=p["header"], font=self.bold_font)
        st.map("TNotebook.Tab",
               background=[("selected", p["accent"]), ("active", hover(p["header"]))],
               foreground=[("selected", "#ffffff"), ("active", p["fg"])],
               lightcolor=[("selected", p["accent"])],
               darkcolor=[("selected", p["accent"])])
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
        self.queue_tree.tag_configure("q_ok", background=p["add_bg"], foreground=p["add_fg"])
        self.queue_tree.tag_configure("q_err", background=p["rem_bg"], foreground=p["rem_fg"])
        self.queue_tree.tag_configure("q_busy", background=p["both_bg"], foreground=p["both_fg"])
        for box in (self.log_box, self.ulog_box, self.f_desc):
            box.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                          highlightbackground=p["border"], highlightcolor=p["accent"],
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
        for tree, bases in self.col_bases:
            for col, width in bases.items():
                tree.column(col, width=int(width * size / 10))
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
                "org_id": self.org_id.get().strip(),
                "data_folder": self.data_folder.get().strip()}), encoding="utf-8")
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
            for b in (self.btn_test, self.btn_load, self.btn_add_one, self.btn_upload,
                      self.btn_queue_upload):
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
            "n_resources": count_resources(ds),
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
        self.refresh_up_tree()  # keeps the uploader's dataset list in sync

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

    # ======================================================== FILE UPLOADER TAB
    def _build_upload_tab(self):
        tab = self.tab_up
        pad = {"padx": 6, "pady": 3}

        folder = ttk.Frame(tab)
        folder.pack(fill="x", padx=6, pady=(8, 2))
        folder.columnconfigure(1, weight=1)
        ttk.Label(folder, text="Data folder (file names of imported tables are looked up here):"
                  ).grid(row=0, column=0, sticky="w")
        ttk.Entry(folder, textvariable=self.data_folder).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(folder, text="Browse…", command=self.browse_data_folder).grid(row=0, column=2)

        # bottom block is packed first so it always keeps its space
        bottom = ttk.LabelFrame(tab, text="D · Upload")
        bottom.pack(fill="x", side="bottom", padx=6, pady=(2, 6))
        line = ttk.Frame(bottom)
        line.pack(fill="x", padx=4, pady=4)
        self.queue_summary = ttk.Label(line, text="Queue is empty.")
        self.queue_summary.pack(side="left")
        self.btn_queue_upload = ttk.Button(line, text="Upload queue", style="Accent.TButton",
                                           command=self.upload_queue)
        self.btn_queue_upload.pack(side="right")
        self.up_progress = ttk.Progressbar(line, length=220, mode="determinate")
        self.up_progress.pack(side="right", padx=10)
        ulog_frame = ttk.Frame(bottom)
        ulog_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.ulog_box = tk.Text(ulog_frame, height=6, wrap="word", state="disabled",
                                relief="flat", highlightthickness=1, bd=0)
        usb = ttk.Scrollbar(ulog_frame, orient="vertical", command=self.ulog_box.yview)
        self.ulog_box.configure(yscrollcommand=usb.set)
        self.ulog_box.pack(side="left", fill="both", expand=True)
        usb.pack(side="right", fill="y")

        vpane = ttk.PanedWindow(tab, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=6, pady=4)
        top = ttk.PanedWindow(vpane, orient="horizontal")
        vpane.add(top, weight=3)

        # --- A: dataset picker ---------------------------------------------
        left = ttk.LabelFrame(top, text="A · Pick dataset(s)  (highlight one or more)")
        top.add(left, weight=3)
        bar = ttk.Frame(left)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(bar, text="Filter:").pack(side="left")
        ttk.Entry(bar, textvariable=self.up_filter, width=26).pack(side="left", padx=4)
        ttk.Button(bar, text="Select all shown",
                   command=lambda: self.up_tree.selection_set(self.up_tree.get_children())
                   ).pack(side="left", padx=4)
        self.up_count = ttk.Label(bar, text="", style="Muted.TLabel")
        self.up_count.pack(side="right")
        self.up_tree = ttk.Treeview(left, columns=("title", "id", "res"), show="headings",
                                    selectmode="extended", height=8)
        up_base = {"title": 330, "id": 190, "res": 70}
        for col, text in (("title", "Title"), ("id", "ID"), ("res", "Files")):
            self.up_tree.heading(col, text=text)
            self.up_tree.column(col, width=up_base[col], anchor="w")
        uvsb = ttk.Scrollbar(left, orient="vertical", command=self.up_tree.yview)
        self.up_tree.configure(yscrollcommand=uvsb.set)
        uvsb.pack(side="right", fill="y")
        self.up_tree.pack(fill="both", expand=True)
        self.col_bases.append((self.up_tree, up_base))
        self.up_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_up_select())

        # --- B: add a file / link ------------------------------------------
        right = ttk.LabelFrame(top, text="B · Add a file or link to the queue")
        top.add(right, weight=3)
        right.columnconfigure(1, weight=1)
        right.rowconfigure(7, weight=1)
        self.up_sel_label = ttk.Label(right, text="", font=self.bold_font)
        self.up_sel_label.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ops = ttk.Frame(right)
        ops.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(ops, text="Upload a file", value="file_upload", variable=self.f_op,
                        command=self.on_op_changed).pack(side="left")
        ttk.Radiobutton(ops, text="Add a link (URL)", value="add_link", variable=self.f_op,
                        command=self.on_op_changed).pack(side="left", padx=14)

        ttk.Label(right, text="File path:").grid(row=2, column=0, sticky="w", **pad)
        fp = ttk.Frame(right)
        fp.grid(row=2, column=1, sticky="ew", **pad)
        fp.columnconfigure(0, weight=1)
        self.f_path_entry = ttk.Entry(fp, textvariable=self.f_path)
        self.f_path_entry.grid(row=0, column=0, sticky="ew")
        self.f_path_entry.bind("<FocusOut>", lambda _e: self.autofill_from_path())
        self.f_path_entry.bind("<Return>", lambda _e: self.autofill_from_path())
        self.btn_browse_file = ttk.Button(fp, text="Browse…", command=self.browse_upload_file)
        self.btn_browse_file.grid(row=0, column=1, padx=(6, 0))

        ttk.Label(right, text="URL:").grid(row=3, column=0, sticky="w", **pad)
        self.f_url_entry = ttk.Entry(right, textvariable=self.f_url)
        self.f_url_entry.grid(row=3, column=1, sticky="ew", **pad)

        ttk.Label(right, text="Type:").grid(row=4, column=0, sticky="w", **pad)
        self.f_type_combo = ttk.Combobox(right, textvariable=self.f_type, values=TYPE_DISPLAY,
                                         state="readonly")
        self.f_type_combo.grid(row=4, column=1, sticky="ew", **pad)

        ttk.Label(right, text="Format:").grid(row=5, column=0, sticky="w", **pad)
        self.f_format_combo = ttk.Combobox(right, textvariable=self.f_format,
                                           values=list(SUPPORTED_FORMATS), state="readonly")
        self.f_format_combo.grid(row=5, column=1, sticky="ew", **pad)
        self.f_format_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: setattr(self, "_auto_format", False))

        ttk.Label(right, text="Title:").grid(row=6, column=0, sticky="w", **pad)
        self.f_title_entry = ttk.Entry(right, textvariable=self.f_title)
        self.f_title_entry.grid(row=6, column=1, sticky="ew", **pad)
        self.f_title_entry.bind("<KeyRelease>", self._on_title_key)

        ttk.Label(right, text="Description:").grid(row=7, column=0, sticky="nw", **pad)
        self.f_desc = tk.Text(right, height=4, wrap="word", font="TkDefaultFont",
                              relief="flat", highlightthickness=1, bd=0)
        self.f_desc.grid(row=7, column=1, sticky="nsew", **pad)

        btns = ttk.Frame(right)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", **pad)
        self.btn_add_queue = ttk.Button(btns, text="+  Add to queue", style="Add.TButton",
                                        command=self.add_to_queue)
        self.btn_add_queue.pack(side="left")
        ttk.Button(btns, text="Clear form", command=self.clear_form).pack(side="left", padx=8)
        self.edit_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.edit_label.grid(row=9, column=0, columnspan=2, sticky="w", **pad)

        # --- C: queue ------------------------------------------------------
        qf = ttk.LabelFrame(vpane, text="C · Upload queue  (double-click a row to edit it)")
        vpane.add(qf, weight=3)
        qbar = ttk.Frame(qf)
        qbar.pack(fill="x", padx=4, pady=4)
        ttk.Button(qbar, text="Import table (CSV / Excel)…", command=self.import_table
                   ).pack(side="left")
        ttk.Button(qbar, text="Save template…", command=self.save_template
                   ).pack(side="left", padx=6)
        ttk.Button(qbar, text="Clear queue", command=self.clear_queue).pack(side="right")
        ttk.Button(qbar, text="Remove finished", command=self.remove_finished
                   ).pack(side="right", padx=6)
        ttk.Button(qbar, text="Remove selected", command=self.remove_selected_queue
                   ).pack(side="right")
        qcols = ("status", "dataset", "operation", "target", "type", "format", "title", "message")
        self.queue_tree = ttk.Treeview(qf, columns=qcols, show="headings",
                                       selectmode="extended", height=6)
        q_base = {"status": 100, "dataset": 220, "operation": 70, "target": 330,
                  "type": 100, "format": 70, "title": 170, "message": 260}
        q_head = {"status": "Status", "dataset": "Dataset", "operation": "Action",
                  "target": "File / URL", "type": "Type", "format": "Format",
                  "title": "Title", "message": "Info"}
        for col in qcols:
            self.queue_tree.heading(col, text=q_head[col])
            self.queue_tree.column(col, width=q_base[col], anchor="w")
        qvsb = ttk.Scrollbar(qf, orient="vertical", command=self.queue_tree.yview)
        qhsb = ttk.Scrollbar(qf, orient="horizontal", command=self.queue_tree.xview)
        self.queue_tree.configure(yscrollcommand=qvsb.set, xscrollcommand=qhsb.set)
        qhsb.pack(side="bottom", fill="x")
        qvsb.pack(side="right", fill="y")
        self.queue_tree.pack(fill="both", expand=True)
        self.col_bases.append((self.queue_tree, q_base))
        self.queue_tree.bind("<Double-1>", self.on_queue_double_click)
        self.queue_tree.bind("<Delete>", lambda _e: self.remove_selected_queue())

        self.on_op_changed()
        self.on_up_select()

    # ------------------------------------------------------------- helpers
    def ulog(self, message):
        def _write():
            self.ulog_box.configure(state="normal")
            self.ulog_box.insert("end", message + "\n")
            self.ulog_box.see("end")
            self.ulog_box.configure(state="disabled")
        self.ui(_write)

    def browse_data_folder(self):
        folder = filedialog.askdirectory(title="Select data folder")
        if folder:
            self.data_folder.set(os.path.normpath(folder))
            self.save_config()

    def refresh_up_tree(self):
        if not hasattr(self, "up_tree"):
            return
        selected = set(self.up_tree.selection())
        self.up_tree.delete(*self.up_tree.get_children())
        needle = self.up_filter.get().strip().casefold()
        shown = 0
        for ds_id in self.order:
            ds = self.datasets[ds_id]
            if needle and needle not in f"{ds['title']} {ds_id}".casefold():
                continue
            self.up_tree.insert("", "end", iid=ds_id,
                                values=(ds["title"], ds_id, ds.get("n_resources", "")))
            shown += 1
        keep = [i for i in selected if self.up_tree.exists(i)]
        if keep:
            self.up_tree.selection_set(keep)
        self.up_count.configure(text=f"{shown} shown · {len(self.order)} loaded")
        self.on_up_select()

    def up_selected_ids(self):
        return [i for i in self.up_tree.selection() if i in self.datasets]

    def on_up_select(self):
        ids = self.up_selected_ids()
        if not ids:
            text = "No dataset selected – pick one in list A"
        elif len(ids) == 1:
            ds = self.datasets[ids[0]]
            text = f"Dataset: {ds['title'] or ids[0]}"
        else:
            text = f"{len(ids)} datasets selected – the entry is added to each"
        self.up_sel_label.configure(text=text)

    # ---------------------------------------------------------------- form
    def on_op_changed(self):
        file_mode = self.f_op.get() == "file_upload"
        self.f_path_entry.configure(state="normal" if file_mode else "disabled")
        self.btn_browse_file.configure(state="normal" if file_mode else "disabled")
        self.f_url_entry.configure(state="disabled" if file_mode else "normal")
        self.f_type_combo.configure(state="readonly" if file_mode else "disabled")
        self.f_format_combo.configure(state="readonly" if file_mode else "disabled")

    def _on_title_key(self, event):
        if event.keysym not in ("Tab", "ISO_Left_Tab", "Shift_L", "Shift_R"):
            self._auto_title = False

    def browse_upload_file(self):
        path = filedialog.askopenfilename(title="Select the file to upload")
        if path:
            self.f_path.set(os.path.normpath(path))
            self.autofill_from_path()

    def autofill_from_path(self):
        raw = self.f_path.get().strip().strip('"')  # tolerate "Copy as path"
        if raw != self.f_path.get():
            self.f_path.set(raw)
        if not raw:
            return
        fp = Path(raw)
        if self._auto_format or not self.f_format.get():
            self.f_format.set(EXT_TO_FORMAT.get(fp.suffix.lower(), "OTHER"))
            self._auto_format = True
        if (self._auto_title or not self.f_title.get().strip()) and fp.stem:
            self.f_title.set(fp.stem)
            self._auto_title = True

    def clear_form(self):
        self.f_op.set("file_upload")
        for var in (self.f_path, self.f_url, self.f_format, self.f_title):
            var.set("")
        self.f_type.set(TYPE_DISPLAY[0])
        self.f_desc.delete("1.0", "end")
        self._auto_title = self._auto_format = True
        self.edit_uid = None
        self.btn_add_queue.configure(text="+  Add to queue")
        self.edit_label.configure(text="")
        self.on_op_changed()

    def collect_form(self):
        op = self.f_op.get()
        item = {"operation": op, "file_path": "", "url": "", "type": "", "format": "",
                "title": self.f_title.get().strip(),
                "description": self.f_desc.get("1.0", "end").strip()}
        if op == "file_upload":
            item["file_path"] = self.f_path.get().strip().strip('"')
            item["type"] = self.f_type.get().split(" ")[0]
            item["format"] = self.f_format.get().strip().upper()
        else:
            item["url"] = self.f_url.get().strip()
            item["type"] = "other"
        return item

    # --------------------------------------------------------------- queue
    def _new_item(self, ds_id, base):
        self._uid += 1
        item = dict(base)
        item.update({"uid": self._uid, "dataset_id": ds_id,
                     "dataset_title": self.datasets.get(ds_id, {}).get("title", ""),
                     "status": "queued", "message": ""})
        return item

    def _check_item(self, item):
        err = validate_op(item)
        if err:
            item["status"], item["message"] = "invalid", err
            return
        item["status"], item["message"] = "queued", ""
        if item["operation"] == "file_upload":
            item["type"] = convert_type(item["type"])
            item["format"] = clean(item["format"]).upper()
            size = os.path.getsize(item["file_path"])
            item["message"] = f"{human_size(size)} · {max(1, math.ceil(size / CHUNK_SIZE))} part(s)"

    def add_to_queue(self):
        base = self.collect_form()
        if self.edit_uid is not None:  # saving an edited row
            item = next((it for it in self.queue if it["uid"] == self.edit_uid), None)
            if item:
                item.update(base)
                self._check_item(item)
                if item["status"] == "invalid":
                    messagebox.showwarning("Row saved but invalid", item["message"])
            self.clear_form()
            self.refresh_queue()
            return
        ids = self.up_selected_ids()
        if not ids:
            messagebox.showinfo("No dataset", "Select one or more datasets in list A first.")
            return
        probe = dict(base, dataset_id=ids[0])
        err = validate_op(probe)
        if err:
            messagebox.showwarning("Check the form", err)
            return
        for ds_id in ids:
            item = self._new_item(ds_id, base)
            self._check_item(item)
            self.queue.append(item)
        self.refresh_queue()
        self.set_status(f"Added to the queue for {len(ids)} dataset(s). "
                        f"Nothing uploaded yet – press \"Upload queue\".")
        self.clear_form()

    def on_queue_double_click(self, event):
        row = self.queue_tree.identify_row(event.y)
        item = next((it for it in self.queue if str(it["uid"]) == row), None)
        if not item or item["status"] in ("done", "uploading"):
            return
        self.edit_uid = item["uid"]
        self.f_op.set(item["operation"])
        self.f_path.set(item["file_path"])
        self.f_url.set(item["url"])
        self.f_type.set(next((d for d in TYPE_DISPLAY if d.split(" ")[0] == item["type"]),
                             TYPE_DISPLAY[0]))
        self.f_format.set(item["format"])
        self.f_title.set(item["title"])
        self.f_desc.delete("1.0", "end")
        self.f_desc.insert("1.0", item["description"])
        self._auto_title = self._auto_format = False
        self.on_op_changed()
        self.btn_add_queue.configure(text="Save changes to row")
        self.edit_label.configure(
            text=f"Editing a queue row (dataset {item['dataset_title'] or item['dataset_id']}). "
                 f"\"Clear form\" cancels.")

    def refresh_queue(self):
        selected = set(self.queue_tree.selection())
        self.queue_tree.delete(*self.queue_tree.get_children())
        tags = {"done": "q_ok", "error": "q_err", "invalid": "q_err", "uploading": "q_busy"}
        for it in self.queue:
            is_file = it["operation"] == "file_upload"
            self.queue_tree.insert(
                "", "end", iid=str(it["uid"]),
                values=(STATUS_TEXT.get(it["status"], it["status"]),
                        it["dataset_title"] or it["dataset_id"],
                        "file" if is_file else "link",
                        it["file_path"] if is_file else it["url"],
                        it["type"] if is_file else "", it["format"] if is_file else "",
                        it["title"], it["message"]),
                tags=(tags[it["status"]],) if it["status"] in tags else ())
        keep = [i for i in selected if self.queue_tree.exists(i)]
        if keep:
            self.queue_tree.selection_set(keep)
        counts = Counter(it["status"] for it in self.queue)
        if not self.queue:
            self.queue_summary.configure(text="Queue is empty.")
        else:
            self.queue_summary.configure(
                text=f"{len(self.queue)} row(s): {counts['queued']} ready, "
                     f"{counts['done']} done, {counts['error']} failed, "
                     f"{counts['invalid']} invalid")

    def _queue_guard(self):
        if self.busy:
            messagebox.showinfo("Busy", "Wait until the running upload has finished.")
            return False
        return True

    def remove_selected_queue(self):
        if not self._queue_guard():
            return
        uids = {int(i) for i in self.queue_tree.selection()}
        self.queue = [it for it in self.queue if it["uid"] not in uids]
        self.refresh_queue()

    def remove_finished(self):
        if self._queue_guard():
            self.queue = [it for it in self.queue if it["status"] != "done"]
            self.refresh_queue()

    def clear_queue(self):
        if self._queue_guard() and self.queue and messagebox.askyesno(
                "Clear queue", "Remove all rows from the upload queue?\n\n"
                               "(Nothing is deleted on data.public.lu.)"):
            self.queue = []
            self.refresh_queue()

    # --------------------------------------------------- table import / export
    def import_table(self):
        path = filedialog.askopenfilename(
            title="Select input table",
            filetypes=[("CSV and Excel files", "*.csv *.xlsx *.xls"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            rows = read_input_table(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Cannot read table", str(exc))
            return
        folder = self.data_folder.get().strip()
        added = invalid = 0
        for r in rows:
            op = clean(r.get("operation_type"))
            base = {
                "operation": op,
                "file_path": (resolve_table_path(r.get("file_path"), folder)
                              if op == "file_upload" else clean(r.get("file_path"))),
                "url": clean(r.get("url")),
                "type": clean(r.get("type")) if op == "file_upload" else "other",
                "format": clean(r.get("format")).upper(),
                "title": clean(r.get("title")),
                "description": clean(r.get("description")),
            }
            item = self._new_item(clean(r.get("dataset_id")), base)
            self._check_item(item)
            self.queue.append(item)
            added += 1
            invalid += item["status"] == "invalid"
        self.refresh_queue()
        msg = f"Imported {added} row(s) from {Path(path).name}"
        if invalid:
            msg += f" – {invalid} invalid (see the Info column; double-click a row to fix it)"
        self.ulog(msg)
        self.set_status(msg)

    def save_template(self):
        path = filedialog.asksaveasfilename(
            title="Save template", defaultextension=".csv", initialfile="upload_template.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        examples = [
            ["<dataset id>", "file_upload", "my_data.csv", "", "main", "CSV",
             "My data (2026)", "Short description"],
            ["<dataset id>", "add_link", "", "https://example.org/page", "", "",
             "Project website", "Link to the project"],
        ]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(TABLE_COLUMNS)
                writer.writerows(examples)
        except OSError as exc:
            messagebox.showerror("Cannot save", str(exc))

    # -------------------------------------------------------------- upload
    def upload_queue(self):
        if self.busy:
            messagebox.showinfo("Busy", "Another operation is still running.")
            return
        if not self.require_key():
            return
        todo = [it for it in self.queue if it["status"] in ("queued", "error", "invalid")]
        for it in todo:  # re-check: files may have changed since they were queued
            self._check_item(it)
        self.refresh_queue()
        items = [it for it in todo if it["status"] == "queued"]
        invalid = [it for it in todo if it["status"] == "invalid"]
        if not items:
            messagebox.showinfo("Nothing to upload",
                                "The queue has no valid rows to upload."
                                + (f"\n\nFirst problem: {invalid[0]['message']}" if invalid else ""))
            return
        files = [it for it in items if it["operation"] == "file_upload"]
        total = sum(os.path.getsize(it["file_path"]) for it in files)
        msg = (f"Upload to data.public.lu?\n\n"
               f"  {len(files)} file(s), {human_size(total)}\n"
               f"  {len(items) - len(files)} link(s)\n"
               f"  {len({it['dataset_id'] for it in items})} dataset(s)")
        if invalid:
            msg += f"\n\n{len(invalid)} invalid row(s) will be skipped."
        if not messagebox.askyesno("Confirm upload", msg):
            return
        key = self.api_key.get().strip()
        n = len(items)
        state = {"idx": 0}
        self.up_progress.configure(value=0)

        def cb(current, total_parts, message):
            frac = (state["idx"] + (current / total_parts if total_parts else 1)) / n
            self.ui(lambda f=frac: self.up_progress.configure(value=min(f, 1) * 100))
            self.ulog(message)

        def work():
            uploader = ResourceUploader(key, progress_callback=cb)
            report, ok, failed = [], 0, 0
            for idx, item in enumerate(items):
                state["idx"] = idx
                self.ui(lambda it=item: self._set_item_state(it, "uploading", ""))
                self.set_status(f"Uploading {idx + 1}/{n}…")
                self.ulog(f"[{idx + 1}/{n}] {item['operation']} -> dataset {item['dataset_id']}")
                if item["operation"] == "file_upload":
                    result = uploader.upload_file(item)
                else:
                    result = uploader.add_link(item)
                self.ui(lambda it=item, res=result: self._apply_result(it, res))
                if result.get("status") == "success":
                    ok += 1
                    self.ulog(f"    OK – resource {result.get('resource_id', '')}")
                else:
                    failed += 1
                    self.ulog(f"    FAILED – {str(result.get('error', ''))[:400]}")
                report.append({
                    "dataset_id": item["dataset_id"], "operation": item["operation"],
                    "status": result.get("status"), "resource_id": result.get("resource_id", ""),
                    "error": result.get("error", ""),
                    "file_or_url": item["file_path"] or item["url"], "title": item["title"]})
                self.ui(lambda v=(idx + 1) / n * 100: self.up_progress.configure(value=v))
            report_path = self.write_report(report)
            summary = f"Upload finished – succeeded: {ok}, failed: {failed}"
            self.ulog(summary + (f"\nReport: {report_path}" if report_path else ""))
            self.set_status(summary)
            self.ui(lambda: messagebox.showinfo(
                "Upload finished",
                summary + (f"\n\nReport:\n{report_path}" if report_path else "")))

        self.run_in_thread(work)

    def _set_item_state(self, item, status, message):
        item["status"], item["message"] = status, message
        self.refresh_queue()

    def _apply_result(self, item, result):
        if result.get("status") == "success":
            item["status"] = "done"
            item["message"] = f"resource {result.get('resource_id', '')}"
            ds = self.datasets.get(item["dataset_id"])
            if ds and isinstance(ds.get("n_resources"), int):
                ds["n_resources"] += 1
        else:
            item["status"] = "error"
            item["message"] = str(result.get("error", "")).replace("\n", " ")[:300]
        self.refresh_queue()
        self.refresh_up_tree()

    def write_report(self, rows):
        folder = self.data_folder.get().strip()
        base = Path(folder) if folder and os.path.isdir(folder) else Path.home()
        path = base / f"output_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
        fields = ["dataset_id", "operation", "status", "resource_id", "error",
                  "file_or_url", "title"]
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            return str(path)
        except OSError as exc:
            self.ulog(f"Could not write report: {exc}")
            return ""


if __name__ == "__main__":
    KeywordManager().mainloop()
