# Obsidian Mermaid → Anki Converter  v5
# Folder: Anki/addons21/obsidian_mermaid_converter/
#
# Why previous versions failed:
#   Obsidian→Anki uses AnkiConnect's HTTP API.  AnkiConnect writes notes
#   directly to the collection and bypasses the editor GUI hooks entirely.
#   The monkey-patch of Collection.add_note also fails because AnkiConnect
#   caches a reference to the method before our patch runs.
#
# Solution:
#   After every AnkiConnect operation, do a targeted sweep of ALL notes in
#   the collection looking for unconverted Mermaid diagrams and fix them in
#   one batch.  The sweep is triggered by:
#     1. A timer that fires 3 s after each AnkiConnect HTTP request lands
#        (gives AnkiConnect time to finish writing).
#     2. Anki startup (catches anything imported before the addon was installed).
#     3. The sync_did_finish hook (backup trigger).
#
#   The sweep is fast — it only touches notes that actually contain mermaid
#   diagram keywords.

import re
import time

from aqt import gui_hooks, mw
from aqt.qt import QTimer
from aqt.utils import showInfo

# ── Conversion logic ───────────────────────────────────────────────────────

DIAGRAM_TYPES = (
    "flowchart", "graph ", "sequencediagram", "classdiagram",
    "statediagram", "erdiagram", "gantt", "pie ", "gitgraph",
    "mindmap", "timeline", "quadrantchart", "xychart",
)

FENCE_RE = re.compile(
    r"```+\s*mermaid\s*\n(.*?)\n```+",
    re.DOTALL | re.IGNORECASE,
)

DIAGRAM_START_RE = re.compile(
    r"^[ \t]*(?:" + "|".join(re.escape(k.strip()) for k in DIAGRAM_TYPES) + r")",
    re.IGNORECASE | re.MULTILINE,
)


def _already_converted(text: str) -> bool:
    return "[mermaid]" in text and "[/mermaid]" in text


def _to_single_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines()]
    return "    ".join(l for l in lines if l)


def convert_field(text: str) -> str:
    if not text.strip() or _already_converted(text):
        return text

    # Case 1: fenced block (```mermaid … ```)
    def replace_fence(m: re.Match) -> str:
        return f"[mermaid]{_to_single_line(m.group(1).strip())}[/mermaid]"

    result, n = FENCE_RE.subn(replace_fence, text)
    if n:
        return result

    # Case 2: bare diagram (fences already stripped upstream)
    lines = text.split("\n")
    start_idx = next(
        (i for i, l in enumerate(lines) if DIAGRAM_START_RE.match(l)),
        None,
    )
    if start_idx is None:
        return text

    before = "\n".join(lines[:start_idx])
    diagram = "\n".join(lines[start_idx:])
    converted = f"[mermaid]{_to_single_line(diagram)}[/mermaid]"
    return (before + "\n" + converted) if before.strip() else converted


# ── Collection sweep ───────────────────────────────────────────────────────

# Keywords we search for in the DB to avoid loading every note
_SEARCH_TERMS = " OR ".join(
    f'"{k.strip()}"' for k in DIAGRAM_TYPES
) + ' OR "```mermaid"'


def sweep_collection(silent: bool = True) -> int:
    """
    Find all notes whose fields contain raw Mermaid diagrams and convert them.
    Returns the number of notes modified.
    """
    col = mw.col
    if col is None:
        return 0

    try:
        note_ids = col.find_notes(_SEARCH_TERMS)
    except Exception:
        return 0

    modified = 0
    for nid in note_ids:
        try:
            note = col.get_note(nid)
        except Exception:
            continue

        changed = False
        for i, value in enumerate(note.fields):
            new_value = convert_field(value)
            if new_value != value:
                note.fields[i] = new_value
                changed = True

        if changed:
            col.update_note(note)
            modified += 1

    if modified and not silent:
        showInfo(f"Mermaid Converter: converted {modified} note(s).")

    return modified


# ── Intercept AnkiConnect's HTTP server ────────────────────────────────────
# AnkiConnect uses a QThread-based web server.  We patch its handler so that
# after every addNote / updateNoteFields / addNotes action we schedule a
# deferred sweep (100 ms later, back on the main thread via QTimer).

_sweep_timer: QTimer | None = None


def _schedule_sweep() -> None:
    global _sweep_timer
    if _sweep_timer is not None and _sweep_timer.isActive():
        _sweep_timer.stop()
    _sweep_timer = QTimer()
    _sweep_timer.setSingleShot(True)
    _sweep_timer.timeout.connect(lambda: sweep_collection(silent=True))
    _sweep_timer.start(300)   # 300 ms — AnkiConnect will have finished writing by then


def _patch_ankiconnect() -> bool:
    """
    Wrap AnkiConnect's handler so we get called after every write action.
    Returns True if the patch was applied.
    """
    try:
        import anki_connect  # type: ignore
        # AnkiConnect < 24 uses a 'handler' function
        if hasattr(anki_connect, "handler"):
            _orig = anki_connect.handler

            def _wrapped(action, **params):
                result = _orig(action, **params)
                if action in (
                    "addNote", "addNotes", "updateNoteFields",
                    "updateNote", "updateNotes",
                ):
                    _schedule_sweep()
                return result

            anki_connect.handler = _wrapped
            return True
    except ImportError:
        pass

    # AnkiConnect >= 24 uses a class-based approach; try patching the web handler
    try:
        import anki_connect.web  # type: ignore
        _orig_handle = anki_connect.web.RequestHandler.do_POST

        def _wrapped_post(self, *args, **kwargs):
            result = _orig_handle(self, *args, **kwargs)
            _schedule_sweep()
            return result

        anki_connect.web.RequestHandler.do_POST = _wrapped_post
        return True
    except Exception:
        pass

    return False


# ── Startup sweep + hooks ──────────────────────────────────────────────────

def _on_collection_loaded(col) -> None:
    # Patch AnkiConnect now that all addons are loaded
    _patch_ankiconnect()
    # Sweep once at startup to fix any previously imported notes
    QTimer.singleShot(2000, lambda: sweep_collection(silent=True))


def _on_sync_did_finish() -> None:
    QTimer.singleShot(500, lambda: sweep_collection(silent=True))


gui_hooks.collection_did_load.append(_on_collection_loaded)

try:
    gui_hooks.sync_did_finish.append(_on_sync_did_finish)
except AttributeError:
    pass

# Also hook note_will_flush for any notes saved through the GUI editor
try:
    def _process_note(note) -> None:
        for i, value in enumerate(note.fields):
            new_value = convert_field(value)
            if new_value != value:
                note.fields[i] = new_value

    gui_hooks.note_will_flush.append(_process_note)
except AttributeError:
    pass


# ── Manual menu entry ──────────────────────────────────────────────────────

def _setup_menu() -> None:
    from aqt.qt import QAction
    action = QAction("Convert Mermaid diagrams (scan all notes)", mw)
    action.triggered.connect(lambda: sweep_collection(silent=False))
    mw.form.menuTools.addAction(action)


gui_hooks.main_window_did_init.append(_setup_menu)
