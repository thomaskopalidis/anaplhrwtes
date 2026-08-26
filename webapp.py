# -*- coding: utf-8 -*-
"""
webapp.py — Τοπική ιστοσελίδα (web εφαρμογή) για το
ΣΥΣΤΗΜΑ ΑΝΑΛΥΣΗΣ ΠΙΝΑΚΩΝ ΑΝΑΠΛΗΡΩΤΩΝ

ΤΙ ΕΙΝΑΙ: μια σελίδα στον browser σου (τοπικά, όχι στο ίντερνετ) όπου
συμπληρώνεις κλάδο / τοποθεσία / μόρια και πατάς «Έλεγχος». Από κάτω τρέχει
ο ΙΔΙΟΣ κώδικας του main.py, χωρίς καμία αλλαγή στη λογική ανάλυσης.

ΕΓΚΑΤΑΣΤΑΣΗ (μία φορά):
  Άνοιξε το ίδιο παράθυρο (Anaconda Prompt / PowerShell) που χρησιμοποιείς
  ήδη και γράψε:
      pip install flask

ΤΡΕΞΙΜΟ (κάθε φορά):
  1) Βάλε αυτό το αρχείο (webapp.py) στον ΙΔΙΟ φάκελο με το main.py,
     config.py, loader.py, normalize.py, pipeline.py, audit.py.
  2) Γράψε:  python webapp.py
  3) Ανοίγει μόνος του ο browser σου στη διεύθυνση http://127.0.0.1:5000
     Άσε το παράθυρο του PowerShell ανοιχτό όσο χρησιμοποιείς τη σελίδα·
     κλείσε το όταν τελειώσεις (κλείνει και τη σελίδα).
"""
from __future__ import annotations

import builtins
import io
import re
import threading
import time
import traceback
import unicodedata
import webbrowser
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace

try:
    from flask import Flask, abort, render_template_string, request, send_from_directory
except ImportError:
    raise SystemExit(
        "❌ Χρειάζεται η βιβλιοθήκη Flask, που δεν είναι εγκατεστημένη.\n"
        "   Άνοιξε το Anaconda Prompt / PowerShell και γράψε:\n\n"
        "       pip install flask\n\n"
        "   και μετά ξανατρέξε:  python webapp.py"
    )

# ---------------------------------------------------------------------------
# Εισαγωγή του κύριου αρχείου ανάλυσης (main.py). Δοκιμάζουμε και main2.py σε
# περίπτωση που έτσι έχει αποθηκευτεί το αρχείο.
# ---------------------------------------------------------------------------
core = None
_import_errors = []
for _modname in ("main", "main2"):
    try:
        core = __import__(_modname)
        break
    except ImportError as exc:
        _import_errors.append(f"{_modname}.py: {exc}")

if core is None:
    raise SystemExit(
        "❌ Δεν βρέθηκε το main.py (ή main2.py) στον ίδιο φάκελο με το webapp.py.\n"
        "   Βάλε το webapp.py στον ίδιο φάκελο με το main.py, config.py, loader.py,\n"
        "   normalize.py, pipeline.py, audit.py και ξανατρέξε.\n\n"
        "   Λεπτομέρειες:\n   " + "\n   ".join(_import_errors)
    )

# Ίδιο module με αυτό που χρησιμοποιεί το main.py — χρειάζεται ξεχωριστά εδώ
# γιατί εκεί εισάγεται τοπικά μέσα σε συναρτήσεις, όχι σε επίπεδο module.
from loader import DATA_SUFFIXES, is_own_output

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Ασφάλεια: αν κάποια εσωτερική συνάρτηση ζητήσει input() που δεν το
# περιμέναμε από τη φόρμα, απαντάμε αυτόματα με Enter/κενό (= η προεπιλογή σε
# όλα σχεδόν τα σημεία του main.py) αντί να «κρεμάσει» το αίτημα.
# ---------------------------------------------------------------------------
@contextmanager
def _auto_answer_prompts():
    original = builtins.input

    def fake_input(prompt=""):
        print(prompt)
        print("   (παραλείφθηκε αυτόματα — συμπλήρωσε το αντίστοιχο πεδίο στη φόρμα αν χρειάζεται)")
        return ""

    builtins.input = fake_input
    try:
        yield
    finally:
        builtins.input = original


def _run_capture(cmd_func, args_ns) -> str:
    """Τρέχει μια cmd_* συνάρτηση του main.py και επιστρέφει ό,τι θα τύπωνε
    στο τερματικό, ως ένα κείμενο, για να το δείξουμε μέσα στη σελίδα."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), _auto_answer_prompts():
            cmd_func(args_ns)
    except SystemExit as exc:
        buf.write(f"\n⛔ Διακόπηκε: {exc}\n")
    except Exception:                                              # noqa: BLE001
        buf.write("\n❌ Σφάλμα κατά την εκτέλεση:\n")
        buf.write(traceback.format_exc())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Βοηθητικά: λίστες κλάδων / ετών για τα πεδία επιλογής
# ---------------------------------------------------------------------------
# Αυτές οι δύο σαρώνουν όλα τα αρχεία δεδομένων και καλούνται σε ΚΑΘΕ φόρτωση
# σελίδας (ακόμα κι όταν απλά ανοίγεις μια φόρμα, όχι μόνο όταν τρέχεις κάτι).
# Μικρό cache 2 λεπτών ώστε να μη σαρώνει ξανά τον δίσκο σε κάθε κλικ —
# αρκετά σύντομο ώστε να δεις καινούρια αρχεία μέσα σε 2 λεπτά από τότε που
# τα ανέβασες, αρκετά μεγάλο ώστε να γλιτώνει τις επαναλαμβανόμενες σαρώσεις.
_CACHE_TTL = 120  # δευτερόλεπτα
_klados_cache = {"value": None, "ts": 0.0}
_years_cache = {"value": None, "ts": 0.0}

# Ρητή λίστα ασφαλείας: κωδικοί που δεν υπάρχουν ΠΟΤΕ σκέτοι, μόνο ως
# υποκλάδοι (π.χ. ΠΕ04 → μόνο ΠΕ04.01/.02/.03/.04). Λειτουργεί ΕΠΙΠΡΟΣΘΕΤΑ
# στη γενική λογική παρακάτω (που ήδη τους αποκλείει αυτόματα) — απλά
# εγγύηση ότι θα λείπουν σίγουρα, ό,τι κι αν γίνεται. Πρόσθεσε κι άλλους
# εδώ αν χρειαστεί.
_NEVER_STANDALONE = {"ΠΕ04"}


def _available_klados():
    now = time.monotonic()
    if _klados_cache["value"] is not None and now - _klados_cache["ts"] < _CACHE_TTL:
        return _klados_cache["value"]
    try:
        pinakes = core._files_of(core.CFG.pinakes_dir(), "base")
        codes = set()
        for f in pinakes:
            codes.update(core.extract_klados_codes(f.stem))
        # Κράτα μόνο κωδικούς που αντιστοιχούν σε ΠΡΑΓΜΑΤΙΚΟ αρχείο χωρίς
        # υποκωδικούς (π.χ. να ΜΗΝ εμφανίζεται σκέτο "ΠΕ04" στη λίστα αν
        # υπάρχουν μόνο αρχεία ΠΕ04.01/ΠΕ04.02 κ.λπ. — το σκέτο "ΠΕ04" τότε
        # δεν αντιστοιχεί σε δικό του πίνακα, μόνο σε συνδυασμό με το
        # "υποκλάδους" τσεκ-μποξ).
        real = set()
        for code in codes:
            rx = core.klados_regex(code, False)
            if any(rx.search(core.norm_code_text(f.stem)) for f in pinakes):
                real.add(code)
        result = sorted(real - _NEVER_STANDALONE)
    except Exception:
        result = []
    _klados_cache["value"], _klados_cache["ts"] = result, now
    return result


def _available_years():
    now = time.monotonic()
    if _years_cache["value"] is not None and now - _years_cache["ts"] < _CACHE_TTL:
        return _years_cache["value"]
    try:
        fdir = core.CFG.faseis_dir()
        if not fdir.exists():
            result = []
        else:
            out_dir = core.CFG.DATA_DIR / core.CFG.OUTPUT_SUBDIR
            files = [f for f in sorted(fdir.rglob("*"))
                     if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)]
            years = set()
            for f in files:
                rel = f.relative_to(fdir)
                _, y = core.extract_phase_year(" / ".join(rel.parts))
                if y:
                    years.add(y)
            result = sorted(years)
    except Exception:
        result = []
    _years_cache["value"], _years_cache["ts"] = result, now
    return result


_last_update_cache = {"value": None, "ts": 0.0}


def _recent_years_only(years, cutoff=None):
    """Κρατάει μόνο σχολικά έτη που ξεκινούν από το cutoff και μετά (π.χ.
    '2025-2026' με cutoff=2025 περνάει, '2024-2025' όχι). Ίδιο cutoff με το
    main.py, ώστε η Πλήρης Ανάλυση να δείχνει τα ίδια έτη με τη Γρήγορη."""
    cutoff = cutoff if cutoff is not None else getattr(core, "MIN_RELEVANT_SCHOOL_YEAR", 2025)

    def start(y):
        try:
            return int(str(y).split("-")[0])
        except (ValueError, IndexError):
            return 0

    return [y for y in years if start(y) >= cutoff]


def _last_data_update() -> "str | None":
    """Πιο πρόσφατη ημερομηνία τροποποίησης ανάμεσα στα αρχεία πινάκων/μονίμων/
    φάσεων, μορφοποιημένη στα ελληνικά (π.χ. '25 Αυγούστου 2026'). None αν δεν
    βρεθεί κανένα αρχείο."""
    now = time.monotonic()
    if _last_update_cache["value"] is not None and now - _last_update_cache["ts"] < _CACHE_TTL:
        return _last_update_cache["value"]
    result = None
    try:
        latest_ts = None
        for getter in (core.CFG.pinakes_dir, core.CFG.monimoi_dir, core.CFG.faseis_dir):
            d = getter()
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.suffix.lower() in DATA_SUFFIXES:
                    mtime = f.stat().st_mtime
                    if latest_ts is None or mtime > latest_ts:
                        latest_ts = mtime
        if latest_ts is not None:
            import datetime
            dt = datetime.datetime.fromtimestamp(latest_ts)
            result = f"{dt.day} {_ACADEMIC_MONTH_NAMES[dt.month]} {dt.year}"
    except Exception:                                                # noqa: BLE001
        result = None
    _last_update_cache["value"], _last_update_cache["ts"] = result, now
    return result


_moriodotisi_cache = {"value": None, "ts": 0.0}


def _load_moriodotisi() -> list:
    """Φορτώνει το data/moriodotisi.json (λίστα ΟΜΑΔΩΝ κατηγοριών μοριοδότησης),
    αν υπάρχει. Επιστρέφει [] αν λείπει το αρχείο ή έχει πρόβλημα."""
    now = time.monotonic()
    if _moriodotisi_cache["value"] is not None and now - _moriodotisi_cache["ts"] < _CACHE_TTL:
        return _moriodotisi_cache["value"]
    result = []
    path = core.CFG.DATA_DIR / "moriodotisi.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = data.get("ομάδες", [])
        except Exception:                                            # noqa: BLE001
            result = []
    _moriodotisi_cache["value"], _moriodotisi_cache["ts"] = result, now
    return result


_ACADEMIC_MONTH_ORDER = {9: 0, 10: 1, 11: 2, 12: 3, 1: 4, 2: 5, 3: 6,
                         4: 7, 5: 8, 6: 9, 7: 10, 8: 11}
_ACADEMIC_MONTH_NAMES = {
    1: "Ιανουαρίου", 2: "Φεβρουαρίου", 3: "Μαρτίου", 4: "Απριλίου",
    5: "Μαΐου", 6: "Ιουνίου", 7: "Ιουλίου", 8: "Αυγούστου",
    9: "Σεπτεμβρίου", 10: "Οκτωβρίου", 11: "Νοεμβρίου", 12: "Δεκεμβρίου",
}


def _average_phase_dates():
    """Κατά προσέγγιση μέσος όρος ημερομηνίας ανά φάση, σε όλα τα σχολικά έτη
    που υπάρχουν μέσα στο data/faseis_dates.json. Απλή προσέγγιση 30ήμερων
    μηνών (αρκετή για ενδεικτικό 'γύρω στις...', όχι ακριβής ημερολογιακή
    πράξη) — αν λείπει το αρχείο ή δεν έχει δεδομένα, επιστρέφει κενό."""
    import datetime
    dates = core._load_faseis_dates()
    by_phase: dict = {}
    for year, phases in dates.items():
        if year.startswith("_") or not isinstance(phases, dict):
            continue
        for phase, info in phases.items():
            frm, to = info.get("από"), info.get("έως")
            if not frm or not to:
                continue
            try:
                d1 = datetime.date.fromisoformat(frm)
                d2 = datetime.date.fromisoformat(to)
            except ValueError:
                continue
            if d1.month not in _ACADEMIC_MONTH_ORDER:
                continue
            mid_day = d1.day + (d2 - d1).days / 2
            offset = _ACADEMIC_MONTH_ORDER[d1.month] * 30 + mid_day
            by_phase.setdefault(phase, []).append((offset, year))

    result = {}
    reverse_order = {v: k for k, v in _ACADEMIC_MONTH_ORDER.items()}
    for phase, entries in sorted(by_phase.items()):
        avg_offset = sum(o for o, _ in entries) / len(entries)
        month_idx = int(avg_offset // 30)
        day = max(1, round(avg_offset % 30))
        month = reverse_order.get(month_idx, 9)
        result[phase] = {
            "text": f"γύρω στις {day} {_ACADEMIC_MONTH_NAMES[month]}",
            "years": ", ".join(y for _, y in entries),
        }
    return result


# ---------------------------------------------------------------------------
# Σχεδίαση σελίδας (κοινό περίβλημα + φόρμες ανά εργαλείο)
# ---------------------------------------------------------------------------
SHELL_TEMPLATE = """<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Πίνακες Αναπληρωτών</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%93%8B%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<style>
  :root {
    --ink: #16233B; --paper: #EEF2F6; --card: #FFFFFF;
    --brass: #B98A3D; --brass-dark: #8F6A2C; --line: #D7DEE6;
    --danger: #B23A3A; --radius: 10px; --muted: var(--muted); --muted-dark: var(--muted-dark);
    --input-bg: #FBFCFE;
  }
  [data-theme="dark"] {
    --ink: #E4E9F1; --paper: #10151D; --card: #182029;
    --brass: #D6A75C; --brass-dark: #E7BC7B; --line: #2B3542;
    --danger: #E4837E; --muted: #8B96A6; --muted-dark: #B7C0CD;
    --input-bg: #131A22;
  }
  [data-theme="dark"] .verdict-ok { background: #163524; border-color: #2C6644; color: #7FDB9F; }
  [data-theme="dark"] .verdict-no { background: #3A1D1D; border-color: #6B3232; color: #F0A6A2; }
  [data-theme="dark"] .error-banner { background: #3A1D1D; border-color: #6B3232; }
  [data-theme="dark"] .slip pre { color: #DCE3ED; }
  [data-theme="dark"] .slip .slip-head .tag { background: var(--brass); color: #10151D; }
  [data-theme="dark"] nav.sidebar { background: #0A0E13; }
  * { box-sizing: border-box; }
  html { transition: background .2s; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    font-size: 15px; line-height: 1.5;
  }
  .shell { display: flex; min-height: 100vh; }
  nav.sidebar {
    width: 240px; flex-shrink: 0; background: var(--ink); color: #E7ECF3;
    padding: 28px 0; display: flex; flex-direction: column;
  }
  .brand {
    font-family: Georgia,"Iowan Old Style","Times New Roman",serif;
    font-size: 19px; letter-spacing: .01em; padding: 0 24px 20px;
    border-bottom: 1px solid rgba(255,255,255,.12); margin-bottom: 16px;
  }
  .brand small {
    display: block; font-family: -apple-system,"Segoe UI",sans-serif;
    font-size: 10.5px; opacity: .6; letter-spacing: .09em; text-transform: uppercase;
    margin-top: 5px;
  }
  nav.sidebar a {
    display: flex; align-items: center; gap: 10px; color: #C9D2DE;
    text-decoration: none; padding: 11px 24px; font-size: 14px;
    border-left: 3px solid transparent; transition: background .15s,color .15s;
  }
  nav.sidebar a:hover { background: rgba(255,255,255,.06); color: #fff; }
  nav.sidebar a.active {
    background: rgba(185,138,61,.16); border-left-color: var(--brass); color: #fff;
  }
  .sidebar-footer {
    margin-top: auto; padding: 16px 24px 0; font-size: 11.5px; color: #8A93A3;
    border-top: 1px solid rgba(255,255,255,.1); word-break: break-word;
  }
  .sidebar-footer a { color: var(--brass); }
  main { flex: 1; padding: 36px 44px; max-width: 1180px; }
  h1.page-title {
    font-family: Georgia,"Iowan Old Style","Times New Roman",serif;
    font-size: 27px; margin: 0 0 6px;
  }
  p.page-sub { color: var(--muted-dark); margin: 0 0 26px; font-size: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 24px 26px; margin-bottom: 22px;
  }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 12.5px; font-weight: 600; color: var(--muted-dark); margin-bottom: 6px; }
  .field input[type=text], .field input[type=number], .field select {
    width: 100%; max-width: 340px; padding: 9px 11px; border: 1px solid var(--line);
    border-radius: 6px; font-size: 14px; background: var(--input-bg); color: var(--ink);
  }
  .field input:focus, .field select:focus {
    outline: 2px solid var(--brass); outline-offset: 1px; border-color: var(--brass);
  }
  .field .hint { font-size: 11.5px; color: var(--muted); margin-top: 4px; }
  .checkline { display: flex; align-items: center; gap: 8px; font-size: 13.5px; margin-bottom: 16px; }
  .checkline label { margin: 0; font-weight: 500; color: var(--ink); }
  .row { display: flex; gap: 24px; flex-wrap: wrap; }
  .row .field { flex: 1 1 220px; }
  button.run {
    background: var(--brass); color: #fff; border: none; padding: 11px 22px;
    border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer;
    transition: background .15s;
  }
  button.run:hover { background: var(--brass-dark); }
  .slip {
    background: var(--card); border: 1px solid var(--line);
    border-top: 3px double var(--ink); border-radius: var(--radius); overflow: hidden;
  }
  .slip .slip-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 13px 22px 12px; border-bottom: 1px dashed var(--line); font-size: 13px; color: var(--muted-dark);
  }
  .slip .slip-head .tag {
    font-family: Consolas,"Cascadia Code",ui-monospace,monospace; font-size: 12px;
    background: var(--ink); color: #EDE3D0; padding: 3px 9px; border-radius: 4px;
    letter-spacing: .04em;
  }
  .slip pre {
    margin: 0; padding: 18px 22px 22px;
    font-family: Consolas,"Cascadia Code",ui-monospace,monospace;
    font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
    color: #1E2A3F; max-height: 640px; overflow-y: auto;
  }
  .error-banner {
    background: #FBEBEA; border: 1px solid #E7B8B4; color: var(--danger);
    padding: 12px 16px; border-radius: 8px; font-size: 13.5px; margin-bottom: 18px;
  }
  .meta-note {
    font-size: 12.5px; color: var(--muted); margin: -12px 0 22px; line-height: 1.6;
  }
  .meta-note strong { color: var(--muted-dark); font-weight: 600; }
  .verdict-banner {
    padding: 18px 22px; border-radius: var(--radius); margin-bottom: 16px;
    font-size: 16.5px; font-weight: 700; display: flex; align-items: center; gap: 12px;
  }
  .verdict-ok { background: #E7F5EC; border: 1px solid #B7DDC3; color: #1F6B3A; }
  .verdict-no { background: #FBEBEA; border: 1px solid #E7B8B4; color: #B23A3A; }
  .copy-btn {
    background: none; border: 1px solid var(--line); color: var(--muted-dark);
    padding: 4px 10px; border-radius: 5px; font-size: 12px; cursor: pointer;
    transition: background .15s, color .15s, border-color .15s; white-space: nowrap;
  }
  .copy-btn:hover { background: var(--brass); color: #fff; border-color: var(--brass); }
  .copy-btn.copied { background: #2F7A4F; color: #fff; border-color: #2F7A4F; }
  .theme-toggle-btn {
    margin: 10px 24px 0; padding: 8px 12px; background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.15); color: #C9D2DE; border-radius: 6px;
    font-size: 12.5px; cursor: pointer; text-align: left; transition: background .15s;
  }
  .theme-toggle-btn:hover { background: rgba(255,255,255,.12); }
  button.run:disabled { opacity: .75; cursor: default; }
  .loading-dots span {
    display: inline-block; opacity: 0; animation: loadingBlink 1.2s infinite;
  }
  .loading-dots span:nth-child(2) { animation-delay: .2s; }
  .loading-dots span:nth-child(3) { animation-delay: .4s; }
  @keyframes loadingBlink {
    0%, 80%, 100% { opacity: 0; } 40% { opacity: 1; }
  }
  @media (max-width: 720px) {
    .shell { flex-direction: column; }
    nav.sidebar { width: 100%; flex-direction: row; overflow-x: auto; padding: 10px 0; }
    .brand { display: none; }
    nav.sidebar a { border-left: none; border-bottom: 3px solid transparent; white-space: nowrap; }
    nav.sidebar a.active { border-left: none; border-bottom-color: var(--brass); }
    main { padding: 22px; }
  }
</style>
</head>
<body>
<div class="shell">
  <nav class="sidebar">
    <div class="brand">Πίνακες Αναπληρωτών<small>Τοπική εφαρμογή ελέγχου</small></div>
    <a href="{{ url_for('home') }}" class="{{ 'active' if active=='home' else '' }}">🎯 Γρήγορος έλεγχος</a>
    <a href="{{ url_for('full') }}" class="{{ 'active' if active=='full' else '' }}">📊 Πλήρης ανάλυση</a>
    <a href="{{ url_for('summary') }}" class="{{ 'active' if active=='summary' else '' }}">🔍 Σύνοψη κλάδου</a>
    <a href="{{ url_for('phase') }}" class="{{ 'active' if active=='phase' else '' }}">🧭 Βάση φάσης</a>
    <a href="{{ url_for('upgrades') }}" class="{{ 'active' if active=='upgrades' else '' }}">🚀 Αναβαθμίσεις</a>
    <a href="{{ url_for('downloads') }}" class="{{ 'active' if active=='downloads' else '' }}">📥 Αρχεία αποτελεσμάτων</a>
    <button type="button" id="theme-toggle" class="theme-toggle-btn">🌙 Σκοτεινό θέμα</button>
    <div class="sidebar-footer">📁 {{ data_dir }}</div>
  </nav>
  <main>
    <h1 class="page-title">{{ title }}</h1>
    <p class="page-sub">{{ subtitle }}</p>
    {{ body|safe }}
  </main>
</div>
<script>
  // --- Σκοτεινό θέμα, με απομνημόνευση επιλογής ---
  (function () {
    var saved = localStorage.getItem("theme");
    if (saved === "dark") document.documentElement.setAttribute("data-theme", "dark");
    var btn = document.getElementById("theme-toggle");
    function updateLabel() {
      var isDark = document.documentElement.getAttribute("data-theme") === "dark";
      btn.textContent = isDark ? "☀️ Φωτεινό θέμα" : "🌙 Σκοτεινό θέμα";
    }
    updateLabel();
    btn.addEventListener("click", function () {
      var isDark = document.documentElement.getAttribute("data-theme") === "dark";
      if (isDark) {
        document.documentElement.removeAttribute("data-theme");
        localStorage.setItem("theme", "light");
      } else {
        document.documentElement.setAttribute("data-theme", "dark");
        localStorage.setItem("theme", "dark");
      }
      updateLabel();
    });
  })();

  // --- Ζωντανή ένδειξη φόρτωσης στο κουμπί, όσο περιμένουμε απάντηση ---
  document.querySelectorAll("form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var btn = f.querySelector("button.run");
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Επεξεργασία<span class="loading-dots"><span>.</span><span>.</span><span>.</span></span>';
      }
    });
  });

  // --- Αντιγραφή αποτελέσματος στο πρόχειρο ---
  function copyResult(btn) {
    var slip = btn.closest(".slip");
    var pre = slip ? slip.querySelector("pre") : null;
    if (!pre) return;
    navigator.clipboard.writeText(pre.innerText).then(function () {
      var original = btn.textContent;
      btn.textContent = "✓ Αντιγράφηκε";
      btn.classList.add("copied");
      setTimeout(function () {
        btn.textContent = original;
        btn.classList.remove("copied");
      }, 1500);
    });
  }

  // --- Χάρτης νομού: πραγματικό περίγραμμα από OpenStreetMap (Nominatim),
  // με χρωματισμό. Αν δεν βρεθεί περίγραμμα (άγνωστο όνομα στο OSM, ή
  // πρόβλημα δικτύου), πέφτει πίσω σε απλό pin πάνω στην πρωτεύουσα — ποτέ
  // δεν αφήνει τον χάρτη άδειο/σπασμένο.
  function loadNomosMap(elId, placeName, fallbackLat, fallbackLng, kind, dimoi) {
    var map = L.map(elId, { scrollWheelZoom: false }).setView([fallbackLat, fallbackLng], 9);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors", maxZoom: 18,
    }).addTo(map);
    // Ασφάλεια: αν το layout της σελίδας δεν είχε ακόμα σταθεροποιηθεί πλήρως
    // τη στιγμή της αρχικοποίησης (γνωστό ζήτημα του Leaflet σε flex/grid
    // διατάξεις), το ξαναϋπολογίζει μετά από μια στιγμή.
    setTimeout(function () { map.invalidateSize(); }, 200);

    var tooltip = kind === "island" ? "Νησί" : kind === "district" ? "Περιοχή μετάθεσης" : "Πρωτεύουσα νομού";
    // Μπλε κουκκίδα πάνω στο σημείο — πάντα ορατή, είτε βρεθεί περίγραμμα είτε όχι.
    L.circleMarker([fallbackLat, fallbackLng], {
      radius: 7, color: "#1D4ED8", weight: 2, fillColor: "#3B82F6", fillOpacity: 0.9,
    }).addTo(map).bindTooltip(tooltip, { direction: "top" });

    // Οι "περιοχές μετάθεσης" (συνδυασμοί πολλών δήμων, π.χ. "Α' Αθήνας") δεν
    // αντιστοιχούν σε πραγματική διοικητική μονάδα στο OpenStreetMap — δεν
    // έχει νόημα να ψάξουμε περίγραμμα, μόνο η κουκκίδα αρκεί.
    if (kind === "district") {
      return;
    }

    var boundaryStyle = { color: "#B98A3D", weight: 2, fillColor: "#B98A3D", fillOpacity: 0.28 };

    function searchBoundary(query) {
      var url = "https://nominatim.openstreetmap.org/search?format=json&polygon_geojson=1"
        + "&limit=1&countrycodes=gr&q=" + encodeURIComponent(query);
      return fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) {
          var geom = data && data.length ? data[0].geojson : null;
          return geom && (geom.type === "Polygon" || geom.type === "MultiPolygon") ? geom : null;
        })
        .catch(function () { return null; });
    }

    // Σαν το searchBoundary, αλλά επιστρέφει ΚΑΙ το σημείο (lat/lon) του
    // αποτελέσματος, όχι μόνο το πολύγωνο — το Nominatim δίνει πάντα ένα
    // σημείο, ακόμα κι όταν δεν έχει πλήρες περίγραμμα για πολύ μικρά νησιά.
    function searchPlace(query) {
      var url = "https://nominatim.openstreetmap.org/search?format=json&polygon_geojson=1"
        + "&limit=1&countrycodes=gr&q=" + encodeURIComponent(query);
      return fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) {
          if (!data || !data.length) return null;
          var item = data[0];
          var geom = item.geojson && (item.geojson.type === "Polygon" || item.geojson.type === "MultiPolygon")
            ? item.geojson : null;
          return { lat: parseFloat(item.lat), lng: parseFloat(item.lon), geom: geom };
        })
        .catch(function () { return null; });
    }

    // ΠΕΡΙΣΣΟΤΕΡΟΙ ΑΠΟ ΕΝΑΣ δήμοι μέσα σε "νησί" (π.χ. Δ' Δωδεκανήσου = 11
    // ξεχωριστά νησιά μαζί): ψάξε ΚΑΘΕ δήμο ξεχωριστά. Σε κάθε επιτυχία,
    // βάλε ένα μικρό μπλε σημαδάκι στο ίδιο το νησί (πάντα, έστω κι αν δεν
    // βρεθεί πλήρες περίγραμμα) ΚΑΙ χρωμάτισε το περίγραμμα αν υπάρχει.
    // Στο τέλος ζούμαρε ώστε να χωράνε όλα μαζί. Σειριακά (όχι όλα
    // ταυτόχρονα) για να μην «βομβαρδίζουμε» το δωρεάν Nominatim.
    if (dimoi && dimoi.length > 1) {
      var combinedBounds = null;
      var idx = 0;
      function nextDimos() {
        if (idx >= dimoi.length) {
          if (combinedBounds) {
            try { map.fitBounds(combinedBounds, { padding: [12, 12] }); } catch (e) {}
          }
          return;
        }
        var dimos = dimoi[idx];
        idx += 1;
        // 3 διαδοχικές διατυπώσεις ανά δήμο/νησί — κάποιες φορές μόνο μία
        // από τις τρεις ταιριάζει σωστά στο OpenStreetMap.
        var dimosAttempts = ["Δήμος " + dimos, dimos, "νησί " + dimos];
        function tryDimosAttempt(j) {
          if (j >= dimosAttempts.length) {
            setTimeout(nextDimos, 300);
            return;
          }
          searchPlace(dimosAttempts[j]).then(function (result) {
            if (!result) {
              tryDimosAttempt(j + 1);
              return;
            }
            // Μικρό μπλε σημαδάκι πάνω στο ίδιο το νησί, με το όνομά του.
            L.circleMarker([result.lat, result.lng], {
              radius: 5, color: "#1D4ED8", weight: 1.5, fillColor: "#3B82F6", fillOpacity: 0.85,
            }).addTo(map).bindTooltip(dimos, { direction: "top" });
            var b = L.latLngBounds([result.lat, result.lng], [result.lat, result.lng]);
            if (result.geom) {
              var layer = L.geoJSON(result.geom, { style: boundaryStyle }).addTo(map);
              b = layer.getBounds();
            }
            combinedBounds = combinedBounds ? combinedBounds.extend(b) : b;
            setTimeout(nextDimos, 300);
          });
        }
        tryDimosAttempt(0);
      }
      nextDimos();
      return;
    }

    // Ένας δήμος/νησί: δοκιμάζουμε πρώτα το ακριβές όνομα δήμου (πιο
    // αξιόπιστο — π.χ. "Δήμος Καλυμνίων" αντί για σκέτο "Κάλυμνος", που
    // μπορεί να ταιριάξει λάθος με μικρή γειτονική νησίδα), μετά γενικότερες
    // διατυπώσεις. Σταματάμε στην πρώτη που πετύχει.
    var attempts = [];
    if (dimoi && dimoi.length === 1) {
      attempts.push("Δήμος " + dimoi[0]);
    }
    if (kind === "island") {
      attempts.push("νησί " + placeName, placeName);
    } else {
      attempts.push("Περιφερειακή Ενότητα " + placeName, "Νομός " + placeName, placeName);
    }

    function tryAttempt(i) {
      if (i >= attempts.length) {
        return;  // η κουκκίδα υπάρχει ήδη — απλά δεν βρέθηκε περίγραμμα να χρωματίσουμε
      }
      searchBoundary(attempts[i]).then(function (geom) {
        if (geom) {
          var layer = L.geoJSON(geom, { style: boundaryStyle }).addTo(map);
          try {
            map.fitBounds(layer.getBounds(), { padding: [12, 12] });
          } catch (e) {
            tryAttempt(i + 1);
          }
        } else {
          tryAttempt(i + 1);
        }
      });
    }
    tryAttempt(0);
  }
</script>
</body>
</html>"""


def render_page(active, title, subtitle, inner_template, **ctx):
    body = render_template_string(inner_template, **ctx)
    return render_template_string(
        SHELL_TEMPLATE, active=active, title=title, subtitle=subtitle, body=body,
        data_dir=str(core.CFG.DATA_DIR),
    )


def _klados_datalist(list_id, options):
    opts = "".join(f'<option value="{o}">' for o in options)
    return f'<datalist id="{list_id}">{opts}</datalist>'


# ---------------------------------------------------------------------------
# Καρτέλα: Γρήγορος έλεγχος (= εντολή predict)
# ---------------------------------------------------------------------------
HOME_TEMPLATE = """
<p class="meta-note">
  📖 Στοιχεία από δημόσιους πίνακες κατάταξης του Υπουργείου Παιδείας — ανεπίσημο εργαλείο ελέγχου.
  {% if last_update %}<br>🕓 Τελευταία ενημέρωση δεδομένων: <strong>{{ last_update }}</strong>{% endif %}
</p>
{% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
<div style="display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap;">

  <!-- Αριστερή, κύρια στήλη: φόρμα -> χάρτες -> αποτέλεσμα -->
  <div style="flex:1 1 500px; min-width:0;">

    <div class="card">
      <form method="post">
        <div class="row">
          <div class="field">
            <label for="klados">Κλάδος (ΠΕ)</label>
            <input list="klados-list" id="klados" name="klados" type="text"
                   value="{{ form.klados }}" placeholder="π.χ. ΠΕ06" required>
            {{ klados_datalist|safe }}
          </div>
          <div class="field">
            <label for="region">Τοποθεσία-στόχος</label>
            <input id="region" name="region" type="text" value="{{ form.region }}"
                   placeholder="π.χ. Α' ΕΒΡΟΥ" required>
          </div>
          <div class="field">
            <label for="moria">Μόρια</label>
            <input id="moria" name="moria" type="text" value="{{ form.moria }}"
                   placeholder="π.χ. 50.8" required>
          </div>
        </div>
        <div class="checkline">
          <input type="checkbox" id="subcodes" name="subcodes" {{ 'checked' if form.subcodes }}>
          <label for="subcodes">Να περιλαμβάνει υποκλάδους (π.χ. ΠΕ11.01)</label>
        </div>
        <button class="run" type="submit">Έλεγχος</button>
      </form>
    </div>

    {% if map_ctx %}
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:4px;">
        {% if map_ctx.kind == 'island' %}
        🏝️ Νησί: {{ map_ctx.nomos }}
        {% elif map_ctx.kind == 'district' %}
        📍 Περιοχή μετάθεσης: {{ map_ctx.nomos }}
        {% else %}
        🗺️ Νομός {{ map_ctx.nomos }} — πρωτεύουσα: {{ map_ctx.capital }}
        {% endif %}
      </div>
      {% if map_ctx.dimoi %}
      <div class="hint" style="margin-bottom:8px;">Δήμοι: {{ map_ctx.dimoi|join(', ') }}</div>
      {% endif %}
      <div id="nomos-map" style="height:380px; border-radius:8px; overflow:hidden; border:1px solid var(--line);"></div>
      <a href="{{ map_ctx.link_url }}" target="_blank" rel="noopener"
         style="display:inline-block; margin-top:8px; font-size:12px; color:var(--brass);">
        Άνοιγμα σε μεγαλύτερο χάρτη ↗
      </a>
      <script>
        window.addEventListener("DOMContentLoaded", function () {
          loadNomosMap("nomos-map", {{ map_ctx.search_name|tojson }}, {{ map_ctx.lat }}, {{ map_ctx.lng }},
                       {{ map_ctx.kind|tojson }}, {{ map_ctx.dimoi|tojson }});
        });
      </script>
    </div>

    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:4px;">
        🏫 Σχολικές μονάδες κοντά στη «{{ map_ctx.nomos }}» — Πανελλήνιο Σχολικό Δίκτυο (maps.sch.gr)
      </div>
      <div class="hint" style="margin-bottom:8px;">
        Ζούμαρε/μετακίνησε τον χάρτη, ή χρησιμοποίησε την αναζήτηση μέσα του (δήμος / διεύθυνση
        εκπαίδευσης / τύπος μονάδας) για ακριβέστερα αποτελέσματα.
      </div>
      <div style="border-radius:8px; overflow:hidden; border:1px solid var(--line);">
        <iframe src="{{ map_ctx.sch_url }}" width="100%" height="420" style="border:0; display:block;"
                scrolling="no" loading="lazy" title="Χάρτης σχολικών μονάδων ΠΣΔ κοντά στη {{ map_ctx.nomos }}"></iframe>
      </div>
      <a href="https://maps.sch.gr/main.html" target="_blank" rel="noopener"
         style="display:inline-block; margin-top:8px; font-size:12px; color:var(--brass);">
        Άνοιγμα πλήρους χάρτη σε νέα καρτέλα ↗
      </a>
    </div>
    {% endif %}

  </div>

  <!-- Δεξιά στήλη: γενικές πληροφορίες -->
  <div style="flex:1 1 420px; min-width:0;">
    {% if avg_dates %}
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:10px;">
        📅 Μέσος όρος ημερομηνιών ανά φάση (βάσει ιστορικού — ενδεικτικό, όχι εγγύηση)
      </div>
      <table style="width:100%; border-collapse:collapse; font-size:13.5px;">
        {% for phase, info in avg_dates.items() %}
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:7px 0; font-weight:600;">{{ phase }}΄ Φάση</td>
          <td style="padding:7px 0; text-align:right; color:var(--muted-dark);">
            {{ info.text }} <span style="opacity:.65; font-size:11.5px;">({{ info.years }})</span>
          </td>
        </tr>
        {% endfor %}
      </table>
    </div>
    {% endif %}
    {% if moriodotisi %}
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:12px;">
        📐 Μοριοδότηση ανά κατηγορία
      </div>
      {% for group in moriodotisi %}
      <div style="font-size:11px; font-weight:700; color:var(--brass); text-transform:uppercase;
                  letter-spacing:.03em; margin:14px 0 4px;">{{ group.τίτλος }}</div>
      <table style="width:100%; border-collapse:collapse; font-size:13px;">
        {% for item in group.κατηγορίες %}
        <tr>
          <td style="padding:6px 0 2px;">{{ item.κατηγορία }}</td>
        </tr>
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:0 0 7px; text-align:right; font-weight:600; color:var(--muted-dark);">
            {{ item.μόρια }} μόρια <span style="opacity:.65; font-weight:400;">({{ item.μονάδα }})</span>
          </td>
        </tr>
        {% endfor %}
      </table>
      {% endfor %}
    </div>
    {% endif %}
  </div>

</div>

{% if verdict %}
<div class="verdict-banner {{ 'verdict-ok' if verdict.ok else 'verdict-no' }}">
  <span style="font-size:22px;">{{ '✅' if verdict.ok else '❌' }}</span> {{ verdict.text }}
</div>
{% endif %}
{% if output %}
<div class="slip">
  <div class="slip-head">
    <span>Αποτέλεσμα ελέγχου</span>
    <span style="display:flex; align-items:center; gap:10px;">
      <button type="button" class="copy-btn" onclick="copyResult(this)">📋 Αντιγραφή</button>
      <span class="tag">{{ form.klados }}</span>
    </span>
  </div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


# ---------------------------------------------------------------------------
# Χάρτης: αναγνώριση νομού από το κείμενο μιας "περιοχής διορισμού" (π.χ.
# "Α' ΕΒΡΟΥ", "Δ' ΚΥΚΛΑΔΩΝ (Δ.Ε.)"), και συντεταγμένες πρωτεύουσας για να
# δείξουμε OpenStreetMap χάρτη γύρω από εκεί.
# ---------------------------------------------------------------------------
NOMOI = {
    "ΑΤΤΙΚΗΣ": {"πρωτεύουσα": "Αθήνα", "lat": 37.9838, "lng": 23.7275},
    "ΘΕΣΣΑΛΟΝΙΚΗΣ": {"πρωτεύουσα": "Θεσσαλονίκη", "lat": 40.6401, "lng": 22.9444},
    "ΕΒΡΟΥ": {"πρωτεύουσα": "Αλεξανδρούπολη", "lat": 40.8481, "lng": 25.8742},
    "ΡΟΔΟΠΗΣ": {"πρωτεύουσα": "Κομοτηνή", "lat": 41.1217, "lng": 25.4064},
    "ΞΑΝΘΗΣ": {"πρωτεύουσα": "Ξάνθη", "lat": 41.1352, "lng": 24.8880},
    "ΚΑΒΑΛΑΣ": {"πρωτεύουσα": "Καβάλα", "lat": 40.9397, "lng": 24.4023},
    "ΔΡΑΜΑΣ": {"πρωτεύουσα": "Δράμα", "lat": 41.1524, "lng": 24.1477},
    "ΣΕΡΡΩΝ": {"πρωτεύουσα": "Σέρρες", "lat": 41.0864, "lng": 23.5486},
    "ΚΙΛΚΙΣ": {"πρωτεύουσα": "Κιλκίς", "lat": 40.9950, "lng": 22.8756},
    "ΧΑΛΚΙΔΙΚΗΣ": {"πρωτεύουσα": "Πολύγυρος", "lat": 40.3750, "lng": 23.4444},
    "ΠΕΛΛΑΣ": {"πρωτεύουσα": "Έδεσσα", "lat": 40.7999, "lng": 22.0463},
    "ΗΜΑΘΙΑΣ": {"πρωτεύουσα": "Βέροια", "lat": 40.5233, "lng": 22.2019},
    "ΠΙΕΡΙΑΣ": {"πρωτεύουσα": "Κατερίνη", "lat": 40.2700, "lng": 22.5031},
    "ΚΟΖΑΝΗΣ": {"πρωτεύουσα": "Κοζάνη", "lat": 40.3006, "lng": 21.7885},
    "ΓΡΕΒΕΝΩΝ": {"πρωτεύουσα": "Γρεβενά", "lat": 40.0864, "lng": 21.4256},
    "ΚΑΣΤΟΡΙΑΣ": {"πρωτεύουσα": "Καστοριά", "lat": 40.5167, "lng": 21.2686},
    "ΦΛΩΡΙΝΑΣ": {"πρωτεύουσα": "Φλώρινα", "lat": 40.7826, "lng": 21.4111},
    "ΙΩΑΝΝΙΝΩΝ": {"πρωτεύουσα": "Ιωάννινα", "lat": 39.6650, "lng": 20.8537},
    "ΘΕΣΠΡΩΤΙΑΣ": {"πρωτεύουσα": "Ηγουμενίτσα", "lat": 39.5017, "lng": 20.2597},
    "ΑΡΤΑΣ": {"πρωτεύουσα": "Άρτα", "lat": 39.1611, "lng": 20.9836},
    "ΠΡΕΒΕΖΑΣ": {"πρωτεύουσα": "Πρέβεζα", "lat": 38.9581, "lng": 20.7528},
    "ΛΑΡΙΣΑΣ": {"πρωτεύουσα": "Λάρισα", "lat": 39.6390, "lng": 22.4191},
    "ΤΡΙΚΑΛΩΝ": {"πρωτεύουσα": "Τρίκαλα", "lat": 39.5556, "lng": 21.7679},
    "ΚΑΡΔΙΤΣΑΣ": {"πρωτεύουσα": "Καρδίτσα", "lat": 39.3644, "lng": 21.9218},
    "ΜΑΓΝΗΣΙΑΣ": {"πρωτεύουσα": "Βόλος", "lat": 39.3622, "lng": 22.9427},
    "ΚΕΡΚΥΡΑΣ": {"πρωτεύουσα": "Κέρκυρα", "lat": 39.6243, "lng": 19.9217},
    "ΛΕΥΚΑΔΑΣ": {"πρωτεύουσα": "Λευκάδα", "lat": 38.7089, "lng": 20.6444},
    "ΚΕΦΑΛΛΗΝΙΑΣ": {"πρωτεύουσα": "Αργοστόλι", "lat": 38.1755, "lng": 20.4880},
    "ΖΑΚΥΝΘΟΥ": {"πρωτεύουσα": "Ζάκυνθος", "lat": 37.7870, "lng": 20.8993},
    "ΑΙΤΩΛΟΑΚΑΡΝΑΝΙΑΣ": {"πρωτεύουσα": "Μεσολόγγι", "lat": 38.3712, "lng": 21.4283},
    "ΑΧΑΙΑΣ": {"πρωτεύουσα": "Πάτρα", "lat": 38.2466, "lng": 21.7346},
    "ΗΛΕΙΑΣ": {"πρωτεύουσα": "Πύργος", "lat": 37.6706, "lng": 21.4428},
    "ΒΟΙΩΤΙΑΣ": {"πρωτεύουσα": "Λιβαδειά", "lat": 38.4360, "lng": 22.8757},
    "ΕΥΒΟΙΑΣ": {"πρωτεύουσα": "Χαλκίδα", "lat": 38.4638, "lng": 23.5959},
    "ΦΘΙΩΤΙΔΑΣ": {"πρωτεύουσα": "Λαμία", "lat": 38.9008, "lng": 22.4353},
    "ΦΩΚΙΔΑΣ": {"πρωτεύουσα": "Άμφισσα", "lat": 38.5261, "lng": 22.3775},
    "ΕΥΡΥΤΑΝΙΑΣ": {"πρωτεύουσα": "Καρπενήσι", "lat": 39.0004, "lng": 21.7749},
    "ΑΡΚΑΔΙΑΣ": {"πρωτεύουσα": "Τρίπολη", "lat": 37.5083, "lng": 22.3765},
    "ΑΡΓΟΛΙΔΑΣ": {"πρωτεύουσα": "Ναύπλιο", "lat": 37.5673, "lng": 22.7997},
    "ΚΟΡΙΝΘΙΑΣ": {"πρωτεύουσα": "Κόρινθος", "lat": 37.9407, "lng": 22.9573},
    "ΛΑΚΩΝΙΑΣ": {"πρωτεύουσα": "Σπάρτη", "lat": 37.0745, "lng": 22.4318},
    "ΜΕΣΣΗΝΙΑΣ": {"πρωτεύουσα": "Καλαμάτα", "lat": 37.0389, "lng": 22.1142},
    "ΧΑΝΙΩΝ": {"πρωτεύουσα": "Χανιά", "lat": 35.5138, "lng": 24.0180},
    "ΡΕΘΥΜΝΟΥ": {"πρωτεύουσα": "Ρέθυμνο", "lat": 35.3667, "lng": 24.4833},
    "ΗΡΑΚΛΕΙΟΥ": {"πρωτεύουσα": "Ηράκλειο", "lat": 35.3387, "lng": 25.1442},
    "ΛΑΣΙΘΙΟΥ": {"πρωτεύουσα": "Άγιος Νικόλαος", "lat": 35.1892, "lng": 25.7189},
    "ΔΩΔΕΚΑΝΗΣΟΥ": {"πρωτεύουσα": "Ρόδος", "lat": 36.4341, "lng": 28.2176},
    "ΚΥΚΛΑΔΩΝ": {"πρωτεύουσα": "Ερμούπολη (Σύρος)", "lat": 37.4467, "lng": 24.9425},
    "ΣΑΜΟΥ": {"πρωτεύουσα": "Σάμος (Βαθύ)", "lat": 37.7539, "lng": 26.9739},
    "ΧΙΟΥ": {"πρωτεύουσα": "Χίος", "lat": 38.3686, "lng": 26.1364},
    "ΛΕΣΒΟΥ": {"πρωτεύουσα": "Μυτιλήνη", "lat": 39.1064, "lng": 26.5556},
    "ΠΕΙΡΑΙΩΣ": {"πρωτεύουσα": "Πειραιάς", "lat": 37.9475, "lng": 23.6362},
}
# Εναλλακτικές γραφές που εμφανίζονται συχνά στις "περιοχές διορισμού"
NOMOI_ALIASES = {
    "ΑΘΗΝΩΝ": "ΑΤΤΙΚΗΣ", "ΑΘΗΝΑΣ": "ΑΤΤΙΚΗΣ", "ΠΕΙΡΑΙΑ": "ΠΕΙΡΑΙΩΣ",
    "ΘΕΣΣΑΛΟΝΙΚΗ": "ΘΕΣΣΑΛΟΝΙΚΗΣ",
}

# Ζητούμενο: κάποιες "περιοχές διορισμού" με πρόθεμα (π.χ. "Β' ΕΒΡΟΥ") ΔΕΝ
# αναφέρονται στην ίδια την ηπειρωτική έδρα του νομού, αλλά σε συγκεκριμένο
# μικρό νησί που ανήκει διοικητικά εκεί (π.χ. Β' ΕΒΡΟΥ = Σαμοθράκη, όχι
# Αλεξανδρούπολη). Κλειδί: (πρόθεμα, βασικός_νομός) -> στοιχεία νησιού. Αυτές
# οι εξαιρέσεις υπερισχύουν του γενικού κανόνα όταν ταιριάζουν.
_subregions_cache = {"value": None, "ts": 0.0}


def _load_subregions() -> dict:
    """Φορτώνει το data/subregions.json (υποπεριοχές μετάθεσης ανά νομό), αν
    υπάρχει. Επιστρέφει {} αν λείπει το αρχείο ή έχει πρόβλημα."""
    now = time.monotonic()
    if _subregions_cache["value"] is not None and now - _subregions_cache["ts"] < _CACHE_TTL:
        return _subregions_cache["value"]
    result = {}
    path = core.CFG.DATA_DIR / "subregions.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception:                                            # noqa: BLE001
            result = {}
    _subregions_cache["value"], _subregions_cache["ts"] = result, now
    return result


def _strip_greek_accents(s: str) -> str:
    """Αφαιρεί τόνους (π.χ. 'ΘΕΣΣΑΛΟΝΊΚΗΣ' -> 'ΘΕΣΣΑΛΟΝΙΚΗΣ') — το .upper() στα
    ελληνικά ΔΕΝ το κάνει αυτόματα, το κρατάει τον τόνο στο κεφαλαίο."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _split_prefix(location_text: str):
    """"Β' ΕΒΡΟΥ" -> ("Β", "ΕΒΡΟΥ (υπόλοιπο, πριν αφαιρεθούν παρενθέσεις/επιθέματα)")."""
    s = _strip_greek_accents(location_text.strip().upper())
    m = re.match(r"^([ΑΒΓΔΕΖΗ])['΄]\s*(.*)$", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _clean_core(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", s)      # (Δ.Ε.), (Π.Ε.) κ.λπ.
    s = re.sub(r"[-–].*$", "", s)         # "- ΜΕΙΩΜΕΝΟΥ ΩΡΑΡΙΟΥ" κ.λπ.
    return s.strip()


def _find_nomos_key(core_text: str):
    """Επιστρέφει το κανονικό όνομα νομού (κλειδί του NOMOI) που ταιριάζει
    καλύτερα στο core_text, ή None. Ελέγχει ΠΡΩΤΑ ειδικές πολυλεκτικές
    περιοχές (π.χ. 'ΑΝΑΤΟΛΙΚΗΣ ΑΤΤΙΚΗΣ') πριν το γενικό substring-match στο
    NOMOI — αλλιώς το 'ΑΝΑΤ. ΑΤΤΙΚΗΣ' θα ταίριαζε λανθασμένα στο απλό
    'ΑΤΤΙΚΗΣ' (αφού το δεύτερο είναι substring του πρώτου)."""
    if "ΑΝΑΤ" in core_text and "ΑΤΤΙΚ" in core_text:
        return "ΑΝΑΤΟΛΙΚΗΣ ΑΤΤΙΚΗΣ"
    if core_text in NOMOI:
        return core_text
    if core_text in NOMOI_ALIASES:
        return NOMOI_ALIASES[core_text]
    for key in NOMOI:
        if key in core_text or core_text in key:
            return key
    for alias, key in NOMOI_ALIASES.items():
        if alias in core_text or core_text in alias:
            return key
    return None


def _match_location(location_text: str):
    """Αναγνωρίζει μια 'περιοχή διορισμού' — είτε ως ειδική υποπεριοχή
    (νησί ή απλά ξεχωριστή περιοχή μετάθεσης, από το subregions.json,
    υπερισχύει), είτε αλλιώς ως γενικό νομό. Επιστρέφει dict {name, capital,
    lat, lng, kind, search_name} όπου kind είναι 'island' / 'district' /
    'nomos', ή None αν δεν αναγνωριστεί με σιγουριά."""
    if not location_text:
        return None
    prefix, rest = _split_prefix(location_text)
    core_text = _clean_core(rest)
    if not core_text:
        return None
    nomos_key = _find_nomos_key(core_text)

    if prefix and nomos_key:
        sub = _load_subregions().get(nomos_key, {}).get(prefix)
        if sub:
            return {
                "name": sub["ετικέτα"], "capital": sub["ετικέτα"],
                "lat": sub["lat"], "lng": sub["lng"],
                "kind": "island" if sub.get("νησί") else "district",
                "search_name": sub["ετικέτα"].split("(")[0].strip(),
                "δήμοι": sub.get("δήμοι", []),
            }

    if nomos_key and nomos_key in NOMOI:
        info = NOMOI[nomos_key]
        return {
            "name": nomos_key.capitalize(), "capital": info["πρωτεύουσα"],
            "lat": info["lat"], "lng": info["lng"],
            "kind": "nomos", "search_name": nomos_key.capitalize(), "δήμοι": [],
        }
    return None


def _osm_embed_url(lat: float, lng: float, span: float = 0.55) -> str:
    left, right = lng - span, lng + span
    bottom, top = lat - span * 0.7, lat + span * 0.7
    return (f"https://www.openstreetmap.org/export/embed.html?"
            f"bbox={left},{bottom},{right},{top}&layer=mapnik&marker={lat},{lng}")


def _osm_link(lat: float, lng: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=9/{lat}/{lng}"


def _sch_embed_url(lat: float, lng: float, zoom: int = 9) -> str:
    return f"https://maps.sch.gr/embed.html?zoom={zoom}&lat={lat}&lng={lng}"


def _extract_verdict(output_text: str):
    """Απλή ανίχνευση θετικής/αρνητικής έκβασης μέσα στο κείμενο του predict,
    για το έγχρωμο πλαίσιο πάνω από το τεχνικό output. None αν δεν βρέθηκε
    καθαρή ένδειξη (π.χ. σφάλμα, ή δεν βρέθηκαν καθόλου δεδομένα)."""
    if not output_text:
        return None
    if "✅ ΘΑ ΕΜΠΑΙΝΕΣ" in output_text:
        return {"ok": True, "text": "Θα έμπαινες, με βάση τα διαθέσιμα ιστορικά στοιχεία."}
    if "❌" in output_text:
        return {"ok": False, "text": "Δεν θα έμπαινες ακόμα, με βάση τα διαθέσιμα ιστορικά στοιχεία."}
    return None


@app.route("/", methods=["GET", "POST"])
def home():
    form = {"klados": "", "region": "", "moria": "", "subcodes": False}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["region"] = request.form.get("region", "").strip()
        form["moria"] = request.form.get("moria", "").strip()
        form["subcodes"] = bool(request.form.get("subcodes"))
        if not form["klados"] or not form["region"] or not form["moria"]:
            error = "Χρειάζονται και τα τρία πεδία: κλάδος, τοποθεσία και μόρια."
        else:
            try:
                moria = float(form["moria"].replace(",", "."))
            except ValueError:
                error = "Τα μόρια πρέπει να είναι αριθμός (π.χ. 50.8)."
            else:
                args = SimpleNamespace(klados=form["klados"], region=form["region"],
                                        moria=moria, name=None, subcodes=form["subcodes"])
                output = _run_capture(core.cmd_predict, args)

    loc = _match_location(form["region"]) if form["region"] else None
    map_ctx = None
    if loc:
        map_ctx = {
            "nomos": loc["name"], "capital": loc["capital"], "kind": loc["kind"],
            "search_name": loc["search_name"], "dimoi": loc.get("δήμοι") or [],
            "lat": loc["lat"], "lng": loc["lng"],
            "link_url": _osm_link(loc["lat"], loc["lng"]),
            "sch_url": _sch_embed_url(loc["lat"], loc["lng"]),
        }

    return render_page(
        "home", "Γρήγορος έλεγχος", "Δώσε κλάδο, τοποθεσία-στόχο και τα μόριά σου — θα δεις πού θα έμπαινες φέτος.",
        HOME_TEMPLATE, form=form, output=output, error=error,
        verdict=_extract_verdict(output),
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        avg_dates=_average_phase_dates(),
        last_update=_last_data_update(),
        moriodotisi=_load_moriodotisi(),
        map_ctx=map_ctx,
    )


# ---------------------------------------------------------------------------
# Καρτέλα: Σύνοψη κλάδου (= εντολή summary)
# ---------------------------------------------------------------------------
SUMMARY_TEMPLATE = """
{% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
<div class="card">
  <form method="post">
    <div class="field">
      <label for="klados">Κλάδος (ΠΕ)</label>
      <input list="klados-list" id="klados" name="klados" type="text"
             value="{{ form.klados }}" placeholder="π.χ. ΠΕ06" required>
      {{ klados_datalist|safe }}
    </div>
    <div class="checkline">
      <input type="checkbox" id="subcodes" name="subcodes" {{ 'checked' if form.subcodes }}>
      <label for="subcodes">Να περιλαμβάνει υποκλάδους</label>
    </div>
    <button class="run" type="submit">Εμφάνιση σύνοψης</button>
  </form>
</div>
{% if output %}
<div class="slip">
  <div class="slip-head">
    <span>Σύνοψη κλάδου</span>
    <span style="display:flex; align-items:center; gap:10px;">
      <button type="button" class="copy-btn" onclick="copyResult(this)">📋 Αντιγραφή</button>
      <span class="tag">{{ form.klados }}</span>
    </span>
  </div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


@app.route("/summary", methods=["GET", "POST"])
def summary():
    form = {"klados": "", "subcodes": False}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["subcodes"] = bool(request.form.get("subcodes"))
        if not form["klados"]:
            error = "Χρειάζεται κωδικός κλάδου."
        else:
            args = SimpleNamespace(base=str(core.CFG.pinakes_dir()), klados=form["klados"],
                                    subcodes=form["subcodes"])
            output = _run_capture(core.cmd_summary, args)
    return render_page(
        "summary", "Σύνοψη κλάδου", "Πλήθος υποψηφίων και μόρια πρώτου/τελευταίου στον πίνακα.",
        SUMMARY_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
    )


# ---------------------------------------------------------------------------
# Καρτέλα: Πλήρης ανάλυση (= εντολή full)
# ---------------------------------------------------------------------------
FULL_TEMPLATE = """
{% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
<div class="card">
  <form method="post">
    <div class="row">
      <div class="field">
        <label for="klados">Κλάδος (ΠΕ)</label>
        <input list="klados-list" id="klados" name="klados" type="text"
               value="{{ form.klados }}" placeholder="π.χ. ΠΕ06" required>
        {{ klados_datalist|safe }}
      </div>
      <div class="field">
        <label for="region">Περιοχή διορισμού (κενό = όλες)</label>
        <input id="region" name="region" type="text" value="{{ form.region }}" placeholder="π.χ. Θεσσαλονίκης">
      </div>
    </div>
    <div class="row">
      <div class="field" style="flex:1 1 260px;">
        <label>Εξαίρεση ήδη προσληφθέντων στα έτη (προαιρετικό — μπορείς να διαλέξεις παραπάνω από ένα)</label>
        {% if year_options %}
        {% for y in year_options %}
        <div class="checkline" style="margin-bottom:5px;">
          <input type="checkbox" id="excl_{{ loop.index }}" name="exclude_years" value="{{ y }}"
                 {{ 'checked' if y in form.exclude_years }}>
          <label for="excl_{{ loop.index }}" style="font-weight:400;">{{ y }}</label>
        </div>
        {% endfor %}
        {% else %}
        <p class="hint" style="margin:0;">(δεν βρέθηκαν διαθέσιμα έτη στον φάκελο φάσεων)</p>
        {% endif %}
      </div>
      <div class="field">
        <label for="top_regions">Πλήθος περιοχών στη λίστα</label>
        <input id="top_regions" name="top_regions" type="text" value="{{ form.top_regions }}" style="max-width:120px;">
      </div>
    </div>
    <div class="checkline">
      <input type="checkbox" id="subcodes" name="subcodes" {{ 'checked' if form.subcodes }}>
      <label for="subcodes">Να περιλαμβάνει υποκλάδους</label>
    </div>
    <div class="checkline">
      <input type="checkbox" id="monimoi" name="monimoi" {{ 'checked' if form.monimoi }}>
      <label for="monimoi">Αφαίρεση μονίμων/διορισμένων</label>
    </div>
    <button class="run" type="submit">Εκτέλεση πλήρους ανάλυσης</button>
  </form>
</div>
{% if output %}
<div class="slip">
  <div class="slip-head">
    <span>Πλήρης ανάλυση</span>
    <span style="display:flex; align-items:center; gap:10px;">
      <button type="button" class="copy-btn" onclick="copyResult(this)">📋 Αντιγραφή</button>
      <span class="tag">{{ form.klados }}</span>
    </span>
  </div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


@app.route("/full", methods=["GET", "POST"])
def full():
    form = {"klados": "", "region": "", "subcodes": False, "monimoi": True,
            "exclude_years": [], "top_regions": "25"}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["region"] = request.form.get("region", "").strip()
        form["subcodes"] = bool(request.form.get("subcodes"))
        form["monimoi"] = bool(request.form.get("monimoi"))
        form["exclude_years"] = request.form.getlist("exclude_years")
        form["top_regions"] = request.form.get("top_regions", "25").strip()
        if not form["klados"]:
            error = "Χρειάζεται κωδικός κλάδου."
        else:
            try:
                top_regions = int(form["top_regions"] or 25)
            except ValueError:
                error = "Το πλήθος περιοχών πρέπει να είναι αριθμός."
            else:
                args = SimpleNamespace(
                    base=str(core.CFG.pinakes_dir()), klados=form["klados"],
                    monimoi=str(core.CFG.monimoi_dir()) if form["monimoi"] else None,
                    hires=None, region=form["region"] or None,
                    subcodes=form["subcodes"], top_regions=top_regions,
                    exclude_year=",".join(form["exclude_years"]) if form["exclude_years"] else None,
                )
                output = _run_capture(core.cmd_full, args)
    return render_page(
        "full", "Πλήρης ανάλυση",
        "Κλάδος, αφαίρεση μονίμων, περιοχή διορισμού και αποθήκευση αρχείου επαλήθευσης.",
        FULL_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        year_options=_recent_years_only(_available_years()),
    )


# ---------------------------------------------------------------------------
# Καρτέλα: Βάση φάσης (= εντολή phase)
# ---------------------------------------------------------------------------
PHASE_TEMPLATE = """
{% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
<div class="card">
  <form method="post">
    <div class="row">
      <div class="field">
        <label for="klados">Κλάδος (ΠΕ)</label>
        <input list="klados-list" id="klados" name="klados" type="text"
               value="{{ form.klados }}" placeholder="π.χ. ΠΕ06" required>
        {{ klados_datalist|safe }}
      </div>
      <div class="field">
        <label for="region">Περιοχή διορισμού</label>
        <input id="region" name="region" type="text" value="{{ form.region }}" placeholder="π.χ. ΧΑΝΙΩΝ" required>
      </div>
    </div>
    <div class="field">
      <label for="year">Σχολικό έτος</label>
      <select id="year" name="year">
        <option value="" {{ 'selected' if not form.year }}>— συνδυασμός όλων των διαθέσιμων —</option>
        {% for y in year_options %}
        <option value="{{ y }}" {{ 'selected' if form.year==y }}>{{ y }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="checkline">
      <input type="checkbox" id="subcodes" name="subcodes" {{ 'checked' if form.subcodes }}>
      <label for="subcodes">Να περιλαμβάνει υποκλάδους</label>
    </div>
    <button class="run" type="submit">Εύρεση βάσης</button>
  </form>
</div>
{% if output %}
<div class="slip">
  <div class="slip-head">
    <span>Βάση φάσης</span>
    <span style="display:flex; align-items:center; gap:10px;">
      <button type="button" class="copy-btn" onclick="copyResult(this)">📋 Αντιγραφή</button>
      <span class="tag">{{ form.klados }}</span>
    </span>
  </div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


@app.route("/phase", methods=["GET", "POST"])
def phase():
    form = {"klados": "", "region": "", "year": "", "subcodes": False}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["region"] = request.form.get("region", "").strip()
        form["year"] = request.form.get("year", "").strip()
        form["subcodes"] = bool(request.form.get("subcodes"))
        if not form["klados"] or not form["region"]:
            error = "Χρειάζονται κλάδος ΚΑΙ περιοχή."
        else:
            args = SimpleNamespace(klados=form["klados"], region=form["region"], file=None,
                                    year=form["year"] or None, subcodes=form["subcodes"])
            output = _run_capture(core.cmd_phase, args)
    return render_page(
        "phase", "Βάση φάσης", "Πόσα μόρια χρειάστηκε ο τελευταίος για μια περιοχή, ανά φάση/έτος.",
        PHASE_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        year_options=_available_years(),
    )


# ---------------------------------------------------------------------------
# Καρτέλα: Αναβαθμίσεις (= εντολή upgrades)
# ---------------------------------------------------------------------------
UPGRADES_TEMPLATE = """
{% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
<div class="card">
  <form method="post">
    <div class="row">
      <div class="field">
        <label for="klados">Κλάδος (ΠΕ)</label>
        <input list="klados-list" id="klados" name="klados" type="text"
               value="{{ form.klados }}" placeholder="π.χ. ΠΕ04.01" required>
        {{ klados_datalist|safe }}
      </div>
      <div class="field">
        <label for="region">Περιοχή (κενό = όλες)</label>
        <input id="region" name="region" type="text" value="{{ form.region }}" placeholder="π.χ. Α' ΕΒΡΟΥ">
      </div>
    </div>
    <div class="field">
      <label for="year">Σχολικό έτος</label>
      <select id="year" name="year">
        <option value="" {{ 'selected' if not form.year }}>— όλα τα διαθέσιμα —</option>
        {% for y in year_options %}
        <option value="{{ y }}" {{ 'selected' if form.year==y }}>{{ y }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="checkline">
      <input type="checkbox" id="subcodes" name="subcodes" {{ 'checked' if form.subcodes }}>
      <label for="subcodes">Να περιλαμβάνει υποκλάδους</label>
    </div>
    <button class="run" type="submit">Εύρεση αναβαθμίσεων</button>
  </form>
</div>
{% if output %}
<div class="slip">
  <div class="slip-head">
    <span>Μειωμένο → Πλήρες</span>
    <span style="display:flex; align-items:center; gap:10px;">
      <button type="button" class="copy-btn" onclick="copyResult(this)">📋 Αντιγραφή</button>
      <span class="tag">{{ form.klados }}</span>
    </span>
  </div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


@app.route("/upgrades", methods=["GET", "POST"])
def upgrades():
    form = {"klados": "", "region": "", "year": "", "subcodes": False}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["region"] = request.form.get("region", "").strip()
        form["year"] = request.form.get("year", "").strip()
        form["subcodes"] = bool(request.form.get("subcodes"))
        if not form["klados"]:
            error = "Χρειάζεται κωδικός κλάδου."
        else:
            args = SimpleNamespace(klados=form["klados"], region=form["region"] or None,
                                    year=form["year"] or None, subcodes=form["subcodes"])
            output = _run_capture(core.cmd_upgrades, args)
    return render_page(
        "upgrades", "Αναβαθμίσεις μειωμένου ωραρίου",
        "Ποιοι προσλήφθηκαν με μειωμένο ωράριο και αναβαθμίστηκαν σε πλήρες στην επόμενη φάση.",
        UPGRADES_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        year_options=_available_years(),
    )


# ---------------------------------------------------------------------------
# Καρτέλα: Αρχεία αποτελεσμάτων (λήψη των Excel που έχουν δημιουργηθεί)
# ---------------------------------------------------------------------------
DOWNLOADS_TEMPLATE = """
<div class="card">
{% if files %}
  <ul style="list-style:none; padding:0; margin:0;">
    {% for f in files %}
    <li style="padding:10px 0; border-bottom:1px solid var(--line);
               display:flex; justify-content:space-between; align-items:center; gap:12px;">
      <span style="font-family:Consolas,'Cascadia Code',ui-monospace,monospace; font-size:13px;">{{ f.name }}</span>
      <a href="{{ url_for('download_file', filename=f.name) }}"
         style="color:var(--brass); font-weight:600; text-decoration:none; white-space:nowrap;">Λήψη ↓</a>
    </li>
    {% endfor %}
  </ul>
{% else %}
  <p style="margin:0; color:var(--muted);">Δεν έχουν δημιουργηθεί ακόμα αρχεία. Τρέξε μια Πλήρη Ανάλυση, Βάση
     Φάσης ή Αναβαθμίσεις — τα Excel που παράγουν θα εμφανιστούν εδώ.</p>
{% endif %}
</div>
{% if is_hosted %}
<div class="error-banner" style="background:#FFF8E8; border-color:#E7D8B4; color:#8F6A2C;">
  ⚠️ Αυτός ο server δεν κρατάει μόνιμα τα αρχεία — αν κάνει επανεκκίνηση, χάνονται. Κατέβασέ τα σύντομα
  μετά την ανάλυση.
</div>
{% endif %}
"""


@app.route("/downloads")
def downloads():
    out_dir = core.CFG.output_dir()
    files = sorted(
        (f for f in out_dir.glob("*.xlsx") if f.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return render_page(
        "downloads", "Αρχεία αποτελεσμάτων", "Τα πιο πρόσφατα Excel που έχουν δημιουργηθεί, έτοιμα για λήψη.",
        DOWNLOADS_TEMPLATE, files=files, is_hosted=(__name__ != "__main__"),
    )


@app.route("/downloads/<path:filename>")
def download_file(filename):
    out_dir = core.CFG.output_dir().resolve()
    target = (out_dir / filename).resolve()
    if out_dir != target.parent or not target.exists():
        abort(404)
    return send_from_directory(out_dir, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Ελαφριά διαδρομή για keep-alive ping (π.χ. UptimeRobot) — απαντάει αμέσως,
# χωρίς να αγγίζει καθόλου τα αρχεία δεδομένων. Καλύτερη επιλογή για ping από
# την αρχική σελίδα.
@app.route("/healthz")
def healthz():
    return "OK", 200


def _open_browser():
    webbrowser.open("http://127.0.0.1:5000/")


def _warm_cache():
    """Προθερμαίνει τις cache (λίστες κλάδων/ετών + αρχεία φάσεων του πιο
    πρόσφατου σχολικού έτους) ΠΡΙΝ έρθει ο πρώτος πραγματικός επισκέπτης.
    Χωρίς αυτό, μετά από κάθε ξύπνημα του Render από τον ύπνο (= καινούριο
    container, άδεια μνήμη) ο πρώτος επισκέπτης θα πλήρωνε ξανά το κόστος
    της πρώτης, αργής φόρτωσης — έτσι το κάνει ο server μόνος του, στο
    παρασκήνιο, όσο ακόμα ξεκινάει."""
    try:
        _available_klados()
        _available_years()
        fdir = core.CFG.faseis_dir()
        if not fdir.exists():
            return
        out_dir = core.CFG.DATA_DIR / core.CFG.OUTPUT_SUBDIR
        cutoff = getattr(core, "MIN_RELEVANT_SCHOOL_YEAR", 2025)
        for f in fdir.rglob("*"):
            if f.suffix.lower() not in DATA_SUFFIXES or is_own_output(f, out_dir):
                continue
            rel = f.relative_to(fdir)
            _, y = core.extract_phase_year(" / ".join(rel.parts))
            try:
                y_start = int(str(y).split("-")[0]) if y else None
            except (ValueError, IndexError):
                y_start = None
            if y_start is not None and y_start >= cutoff:
                core._load_file_cached(f)
    except Exception:                                                # noqa: BLE001
        pass  # η προθέρμανση είναι απλά βελτιστοποίηση — ποτέ δεν πρέπει να ρίξει τον server


# Ξεκινάει αμέσως μόλις εισαχθεί αυτό το module — δηλαδή και όταν το τρέχεις
# τοπικά (python webapp.py) ΚΑΙ όταν το εισάγει το gunicorn στο Render.
threading.Thread(target=_warm_cache, daemon=True).start()


if __name__ == "__main__":
    threading.Timer(1.0, _open_browser).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
