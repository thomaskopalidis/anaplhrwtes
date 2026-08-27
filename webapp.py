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
import datetime
import io
import json
import os
import re
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
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


_mistho_cache = {"value": None, "ts": 0.0}


def _load_mistho_klimakia() -> list:
    """Φορτώνει το data/mistho_klimakia.json (μισθολογικά κλιμάκια), αν
    υπάρχει. Επιστρέφει [] αν λείπει το αρχείο ή έχει πρόβλημα."""
    now = time.monotonic()
    if _mistho_cache["value"] is not None and now - _mistho_cache["ts"] < _CACHE_TTL:
        return _mistho_cache["value"]
    result = []
    path = core.CFG.DATA_DIR / "mistho_klimakia.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = data.get("κλιμάκια", [])
        except Exception:                                            # noqa: BLE001
            result = []
    _mistho_cache["value"], _mistho_cache["ts"] = result, now
    return result


_epidomata_cache = {"value": None, "ts": 0.0}


def _load_epidomata() -> list:
    """Φορτώνει το data/epidomata.json (λοιπά επιδόματα — οικογενειακό κ.λπ.),
    αν υπάρχει. Επιστρέφει [] αν λείπει το αρχείο ή έχει πρόβλημα."""
    now = time.monotonic()
    if _epidomata_cache["value"] is not None and now - _epidomata_cache["ts"] < _CACHE_TTL:
        return _epidomata_cache["value"]
    result = []
    path = core.CFG.DATA_DIR / "epidomata.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = data.get("επιδόματα", [])
        except Exception:                                            # noqa: BLE001
            result = []
    _epidomata_cache["value"], _epidomata_cache["ts"] = result, now
    return result


_nomos_units_cache = {"value": None, "ts": 0.0}


def _load_nomos_units_info() -> dict:
    """Φορτώνει το data/nomos_units_info.json (πρωτεύουσα/συντεταγμένες/
    περιφέρεια για τις 74 πραγματικές μονάδες), για τη σελίδα Σχολεία."""
    now = time.monotonic()
    if _nomos_units_cache["value"] is not None and now - _nomos_units_cache["ts"] < _CACHE_TTL:
        return _nomos_units_cache["value"]
    result = {}
    path = core.CFG.DATA_DIR / "nomos_units_info.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                result = json.load(fh)
        except Exception:                                            # noqa: BLE001
            result = {}
    _nomos_units_cache["value"], _nomos_units_cache["ts"] = result, now
    return result


_nomos_cities_cache = {"value": None, "ts": 0.0}


def _load_nomos_cities() -> dict:
    """Φορτώνει το data/nomos_cities.json (μεγάλες πόλεις ανά μονάδα, πέρα
    από την πρωτεύουσα), για τη σελίδα Σχολεία. Επεκτάσιμο σταδιακά."""
    now = time.monotonic()
    if _nomos_cities_cache["value"] is not None and now - _nomos_cities_cache["ts"] < _CACHE_TTL:
        return _nomos_cities_cache["value"]
    result = {}
    path = core.CFG.DATA_DIR / "nomos_cities.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = data.get("πόλεις", {})
        except Exception:                                            # noqa: BLE001
            result = {}
    _nomos_cities_cache["value"], _nomos_cities_cache["ts"] = result, now
    return result


_paramethorios_cache = {"value": None, "ts": 0.0}


def _load_paramethorios() -> dict:
    """Φορτώνει το data/paramethorios.json (λίστα παραμεθόριων/προβληματικών
    περιοχών κατηγορίας Α), αν υπάρχει."""
    now = time.monotonic()
    if _paramethorios_cache["value"] is not None and now - _paramethorios_cache["ts"] < _CACHE_TTL:
        return _paramethorios_cache["value"]
    result = {"πλήρως": [], "υποπεριοχές": {}, "μερικώς": {}}
    path = core.CFG.DATA_DIR / "paramethorios.json"
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            result = {
                "πλήρως": data.get("πλήρως_παραμεθόριοι_νομοί", []),
                "υποπεριοχές": data.get("παραμεθόριες_υποπεριοχές", {}),
                "μερικώς": data.get("μερικώς_παραμεθόριοι_νομοί", {}),
            }
        except Exception:                                            # noqa: BLE001
            pass
    _paramethorios_cache["value"], _paramethorios_cache["ts"] = result, now
    return result


def _check_paramethorios(nomos_key: str, prefix):
    """Επιστρέφει dict {status: 'yes'/'no'/'partial', detail: ...} για το αν
    ο νομός/υποπεριοχή δικαιούται επίδομα παραμεθορίου, βάσει του
    paramethorios.json. status='partial' σημαίνει ότι ΜΕΡΟΣ του νομού
    δικαιούται αλλά η εφαρμογή δεν ξέρει αν η συγκεκριμένη περιοχή είναι
    μέσα σε αυτό το μέρος — χρειάζεται χειροκίνητος έλεγχος."""
    if not nomos_key:
        return {"status": "no", "detail": None}
    data = _load_paramethorios()
    if nomos_key in data["πλήρως"]:
        return {"status": "yes", "detail": None}
    subs = data["υποπεριοχές"].get(nomos_key, [])
    if prefix and prefix in subs:
        return {"status": "yes", "detail": None}
    if nomos_key in data["μερικώς"]:
        return {"status": "partial", "detail": data["μερικώς"][nomos_key]}
    return {"status": "no", "detail": None}


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
  .stats-official {
    background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 20px 24px; margin-bottom: 16px;
  }
  .stats-official-title {
    font-family: Georgia,"Iowan Old Style","Times New Roman",serif; font-size: 15px;
    font-weight: 700; color: var(--ink); margin-bottom: 14px; padding-bottom: 10px;
    border-bottom: 2px solid var(--brass);
  }
  .stats-official-grid { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat-box { flex: 1 1 200px; }
  .stat-label { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }
  .stat-value {
    font-family: Georgia,"Iowan Old Style","Times New Roman",serif; font-size: 30px;
    font-weight: 700; color: var(--brass-dark); line-height: 1.2;
  }
  [data-theme="dark"] .stat-value { color: var(--brass); }
  .stat-sub { font-size: 12px; color: var(--muted-dark); margin-top: 2px; }
  .slip-title { font-family: Georgia,"Iowan Old Style","Times New Roman",serif; font-weight: 700; }
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
  .site-footer {
    margin-top: 44px; padding-top: 16px; border-top: 1px solid var(--line);
    font-size: 11.5px; color: var(--muted); text-align: center; line-height: 1.6;
  }
  .eduai-fab {
    position: fixed; bottom: 22px; right: 22px; z-index: 40;
    background: var(--brass); color: #fff; border: none; padding: 12px 18px;
    border-radius: 999px; font-size: 14px; font-weight: 600; cursor: pointer;
    box-shadow: 0 4px 14px rgba(0,0,0,.22); transition: background .15s, transform .15s;
  }
  .eduai-fab:hover { background: var(--brass-dark); transform: translateY(-1px); }
  .eduai-panel {
    display: none; flex-direction: column; position: fixed; bottom: 78px; right: 22px; z-index: 41;
    width: 340px; max-width: calc(100vw - 32px); height: 440px; max-height: calc(100vh - 120px);
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: 0 10px 34px rgba(0,0,0,.28); overflow: hidden;
  }
  .eduai-panel.open { display: flex; }
  .eduai-header {
    background: var(--ink); color: #fff; padding: 12px 16px; font-weight: 700; font-size: 14px;
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  .eduai-close {
    background: none; border: none; color: #C9D2DE; font-size: 15px; cursor: pointer; padding: 2px 6px;
  }
  .eduai-close:hover { color: #fff; }
  .eduai-messages {
    flex: 1; overflow-y: auto; padding: 14px 14px 6px; display: flex; flex-direction: column; gap: 10px;
  }
  .eduai-msg {
    max-width: 85%; padding: 9px 12px; border-radius: 10px; font-size: 13.5px; line-height: 1.45;
    white-space: pre-wrap; word-break: break-word;
  }
  .eduai-msg-assistant {
    align-self: flex-start; background: var(--paper); color: var(--ink); border: 1px solid var(--line);
  }
  .eduai-msg-user { align-self: flex-end; background: var(--brass); color: #fff; }
  .eduai-msg.eduai-thinking { opacity: .6; font-style: italic; }
  .eduai-input-row {
    display: flex; gap: 8px; padding: 12px 14px; border-top: 1px solid var(--line); flex-shrink: 0;
  }
  .eduai-input-row input {
    flex: 1; padding: 9px 11px; border: 1px solid var(--line); border-radius: 7px;
    font-size: 13.5px; background: var(--input-bg); color: var(--ink);
  }
  .eduai-input-row input:focus { outline: 2px solid var(--brass); outline-offset: 1px; }
  .eduai-send-btn {
    background: var(--brass); color: #fff; border: none; width: 38px; border-radius: 7px;
    font-size: 15px; cursor: pointer; flex-shrink: 0;
  }
  .eduai-send-btn:hover { background: var(--brass-dark); }
  .eduai-send-btn:disabled { opacity: .6; cursor: default; }
  @media (max-width: 480px) {
    .eduai-panel { right: 16px; bottom: 74px; width: calc(100vw - 32px); }
    .eduai-fab { right: 16px; bottom: 16px; }
  }
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
    <a href="{{ url_for('schools') }}" class="{{ 'active' if active=='schools' else '' }}">🏫 Σχολεία</a>
    <a href="{{ url_for('downloads') }}" class="{{ 'active' if active=='downloads' else '' }}">📥 Αρχεία αποτελεσμάτων</a>
    <button type="button" id="theme-toggle" class="theme-toggle-btn">🌙 Σκοτεινό θέμα</button>
    <div class="sidebar-footer">📁 {{ data_dir }}</div>
  </nav>
  <main>
    <h1 class="page-title">{{ title }}</h1>
    <p class="page-sub">{{ subtitle }}</p>
    {{ body|safe }}
    <footer class="site-footer">
      © {{ current_year }} Kopalidis P. Thomas — Πίνακες Αναπληρωτών · Ανεπίσημο εργαλείο ελέγχου, χωρίς καμία σχέση με το
      Υπουργείο Παιδείας ή το ΑΣΕΠ.
    </footer>
  </main>
</div>

<button type="button" id="eduai-toggle" class="eduai-fab" title="Ρώτα το eduAI">💬 eduAI</button>
<div id="eduai-panel" class="eduai-panel">
  <div class="eduai-header">
    <span>🤖 eduAI</span>
    <button type="button" id="eduai-close" class="eduai-close" aria-label="Κλείσιμο">✕</button>
  </div>
  <div id="eduai-messages" class="eduai-messages"></div>
  <div class="eduai-input-row">
    <input id="eduai-input" type="text" placeholder="Ρώτησέ με κάτι..." autocomplete="off">
    <button type="button" id="eduai-send" class="eduai-send-btn">➤</button>
  </div>
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

  // --- eduAI: μικρός βοηθός συνομιλίας, ορατός σε όλες τις σελίδες ---
  (function () {
    var toggle = document.getElementById("eduai-toggle");
    var panel = document.getElementById("eduai-panel");
    var closeBtn = document.getElementById("eduai-close");
    var messagesEl = document.getElementById("eduai-messages");
    var input = document.getElementById("eduai-input");
    var sendBtn = document.getElementById("eduai-send");
    var history = [];

    function addMessage(role, text) {
      var div = document.createElement("div");
      div.className = "eduai-msg eduai-msg-" + role;
      div.textContent = text;
      messagesEl.appendChild(div);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return div;
    }

    toggle.addEventListener("click", function () {
      var isOpen = panel.classList.contains("open");
      panel.classList.toggle("open", !isOpen);
      if (!isOpen) {
        if (!messagesEl.children.length) {
          addMessage("assistant", "Γεια σου! Είμαι το eduAI. Ρώτησέ με για μόρια, φάσεις, ή πώς να χρησιμοποιήσεις την εφαρμογή.");
        }
        input.focus();
      }
    });
    closeBtn.addEventListener("click", function () { panel.classList.remove("open"); });

    function send() {
      var text = input.value.trim();
      if (!text) return;
      addMessage("user", text);
      input.value = "";
      sendBtn.disabled = true;
      var thinking = addMessage("assistant", "...");
      thinking.classList.add("eduai-thinking");

      fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history }),
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          thinking.remove();
          if (!res.ok || res.data.error) {
            addMessage("assistant", "⚠️ " + (res.data.error || "Κάτι πήγε στραβά."));
            return;
          }
          addMessage("assistant", res.data.reply);
          history.push({ role: "user", content: text });
          history.push({ role: "assistant", content: res.data.reply });
        })
        .catch(function () {
          thinking.remove();
          addMessage("assistant", "⚠️ Πρόβλημα σύνδεσης — δοκίμασε ξανά.");
        })
        .finally(function () { sendBtn.disabled = false; });
    }

    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") send();
    });
  })();

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

    var boundaryStyle = { color: "#B98A3D", weight: 2, fillColor: "#B98A3D", fillOpacity: 0.28 };

    // ΠΕΡΙΣΣΟΤΕΡΟΙ ΑΠΟ ΕΝΑΣ δήμοι (π.χ. Δ' Δωδεκανήσου = πολλά ξεχωριστά
    // νησιά μαζί): ΔΕΝ βάζουμε τη γενική κουκκίδα της "περιοχής" — θα έπεφτε
    // σε κάποιο τυχαίο σημείο ανάμεσα στα νησιά, πιθανόν πάνω σε ΕΝΑ από αυτά
    // (μπερδεύοντας το κλικ εκεί, αφού θα έδειχνε ασαφή ετικέτα "Νησί" αντί
    // για το πραγματικό όνομα). Κάθε νησί παίρνει τη ΔΙΚΗ του ακριβή κουκκίδα
    // παρακάτω, οπότε η γενική δεν προσφέρει τίποτα.
    if (!(dimoi && dimoi.length > 1)) {
      var tooltip = kind === "island" ? "Νησί" : kind === "district" ? "Περιοχή μετάθεσης" : "Πρωτεύουσα νομού";
      // bindTooltip = εμφανίζεται περνώντας το ποντίκι· bindPopup = εμφανίζεται
      // με κλικ/άγγιγμα (δουλεύει και σε κινητό, όπου δεν υπάρχει "πέρασμα ποντικιού").
      var mainMarker = L.circleMarker([fallbackLat, fallbackLng], {
        radius: 7, color: "#1D4ED8", weight: 2, fillColor: "#3B82F6", fillOpacity: 0.9,
      }).addTo(map).bindTooltip(tooltip, { direction: "top" }).bindPopup(placeName);
    }

    // Οι "περιοχές μετάθεσης" (π.χ. "Α' Αθήνας", "Α' Θεσ/νίκης") ΔΕΝ
    // αντιστοιχούν οι ίδιες σε μία διοικητική μονάδα στο OpenStreetMap, αλλά
    // ΚΑΘΕ δήμος μέσα τους (Θέρμη, Καλαμαριά κ.λπ.) είναι πραγματικός δήμος
    // και ΘΑ έχει όριο — προχωράμε κανονικά στην αναζήτηση/χρωματισμό
    // παρακάτω. Μπλοκάρουμε μόνο αν δεν έχουμε καθόλου καταγεγραμμένους
    // δήμους για να ψάξουμε (ελλιπές subregions.json).
    if (kind === "district" && !(dimoi && dimoi.length)) {
      return;
    }


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

    // Για μερικά νησιά η αναζήτηση OpenStreetMap έχει αποδειχτεί αναξιόπιστη
    // (λάθος ταίριασμα, π.χ. Πάτμος <-> Αρκοί μπερδεμένα). Για αυτά,
    // χρησιμοποιούμε χειροκίνητα επιβεβαιωμένες συντεταγμένες για ΣΙΓΟΥΡΗ
    // τοποθέτηση της κουκκίδας — η αναζήτηση περιγράμματος γίνεται ΕΠΙΠΛΕΟΝ,
    // προαιρετικά, χωρίς να επηρεάζει πού μπαίνει η ίδια η κουκκίδα.
    var KNOWN_ISLAND_COORDS = {
      "ΠΑΤΜΟΥ": [37.3047, 26.5478],
      "ΑΡΚΟΙ": [37.3833, 26.7333],
      "ΛΕΡΟΥ": [37.1500, 26.8500],
      "ΙΟΥ": [36.7167, 25.2833],
    };

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

        var known = KNOWN_ISLAND_COORDS[dimos];
        if (known) {
          var kb = L.latLngBounds(known, known);
          var kMarker = L.circleMarker(known, {
            radius: 5, color: "#1D4ED8", weight: 1.5, fillColor: "#3B82F6", fillOpacity: 0.85,
          }).addTo(map).bindTooltip(dimos, { direction: "top" }).bindPopup(dimos);
          combinedBounds = combinedBounds ? combinedBounds.extend(kb) : kb;
          searchBoundary("Δήμος " + dimos).then(function (geom) {
            if (geom) {
              var layer = L.geoJSON(geom, { style: boundaryStyle }).addTo(map);
              combinedBounds = combinedBounds.extend(layer.getBounds());
              kMarker.bringToFront();
            }
            setTimeout(nextDimos, 300);
          });
          return;
        }

        // 3 διαδοχικές διατυπώσεις ανά δήμο/νησί — κάποιες φορές μόνο μία
        // από τις τρεις ταιριάζει σωστά στο OpenStreetMap.
        var dimosAttempts = ["Δήμος " + dimos, dimos, "νησί " + dimos];
        var retried = false;
        function tryDimosAttempt(j) {
          if (j >= dimosAttempts.length) {
            if (!retried) {
              // Μία πλήρης επανάληψη όλης της ακολουθίας, μετά από λίγο —
              // καλύπτει παροδικά προβλήματα δικτύου/ρυθμού στο δωρεάν
              // Nominatim, όχι μόνο "δεν βρέθηκε καθόλου".
              retried = true;
              setTimeout(function () { tryDimosAttempt(0); }, 900);
            } else {
              setTimeout(nextDimos, 300);
            }
            return;
          }
          searchPlace(dimosAttempts[j]).then(function (result) {
            if (!result) {
              tryDimosAttempt(j + 1);
              return;
            }
            var b = L.latLngBounds([result.lat, result.lng], [result.lat, result.lng]);
            if (result.geom) {
              var layer = L.geoJSON(result.geom, { style: boundaryStyle }).addTo(map);
              b = layer.getBounds();
            }
            // Μικρό μπλε σημαδάκι πάνω στο ίδιο το νησί, με το όνομά του —
            // προστίθεται ΜΕΤΑ το περίγραμμα και φέρνεται μπροστά, ώστε να
            // ΜΗΝ κρύβεται ποτέ από το χρωματισμένο πολύγωνο.
            var marker = L.circleMarker([result.lat, result.lng], {
              radius: 5, color: "#1D4ED8", weight: 1.5, fillColor: "#3B82F6", fillOpacity: 0.85,
            }).addTo(map).bindTooltip(dimos, { direction: "top" }).bindPopup(dimos);
            marker.bringToFront();
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
          mainMarker.bringToFront();
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
        data_dir=str(core.CFG.DATA_DIR), current_year=datetime.date.today().year,
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
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:10px;">
        📊 Δευτεροβάθμια Γενική Εκπαίδευση — Γυμνάσια 2023/2024 (ΕΛΣΤΑΤ)
      </div>
      <img src="{{ url_for('static', filename='dt_gymnasia_2023_2024.png') }}"
           alt="Στατιστικά ΕΛΣΤΑΤ: Γυμνάσια 2023/2024"
           style="width:100%; border-radius:8px; border:1px solid var(--line); display:block;">
      <div class="hint" style="margin-top:8px;">Πηγή: Ελληνική Στατιστική Αρχή</div>
    </div>
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:10px;">
        📊 Πρωτοβάθμια Εκπαίδευση — Νηπιαγωγεία &amp; Δημοτικά 2022/2023 (ΕΛΣΤΑΤ)
      </div>
      <img src="{{ url_for('static', filename='dt_nipiagogia_dimotika_2022_2023.png') }}"
           alt="Στατιστικά ΕΛΣΤΑΤ: Νηπιαγωγεία και Δημοτικά 2022/2023"
           style="width:100%; border-radius:8px; border:1px solid var(--line); display:block;">
      <div class="hint" style="margin-top:8px;">Πηγή: Ελληνική Στατιστική Αρχή</div>
    </div>
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:10px;">
        📊 Δευτεροβάθμια Γενική Εκπαίδευση — Γενικά Λύκεια 2021/2022 (ΕΛΣΤΑΤ)
      </div>
      <img src="{{ url_for('static', filename='dt_lykeia_2021_2022.png') }}"
           alt="Στατιστικά ΕΛΣΤΑΤ: Γενικά Λύκεια 2021/2022"
           style="width:100%; border-radius:8px; border:1px solid var(--line); display:block;">
      <div class="hint" style="margin-top:8px;">Πηγή: Ελληνική Στατιστική Αρχή</div>
    </div>
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
    {% if mistho_klimakia %}
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:12px;">
        💰 Μισθολογικά κλιμάκια αναπληρωτών (καθαρό ποσό/μήνα)
      </div>
      <table style="width:100%; border-collapse:collapse; font-size:13px;">
        <tr style="border-bottom:2px solid var(--brass);">
          <td style="padding:5px 4px; font-weight:700; font-size:10.5px; text-transform:uppercase; color:var(--muted);">Κλιμάκιο</td>
          <td style="padding:5px 4px; font-weight:700; font-size:10.5px; text-transform:uppercase; color:var(--muted);">Έτη προϋπηρεσίας</td>
          <td style="padding:5px 4px; font-weight:700; font-size:10.5px; text-transform:uppercase; color:var(--muted); text-align:right;">Καθαρό ποσό</td>
        </tr>
        {% for k in mistho_klimakia %}
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:6px 4px;">{{ k.κλιμάκιο }}</td>
          <td style="padding:6px 4px; color:var(--muted-dark);">{{ k.έτη_από }} έως {{ k.έτη_έως }}</td>
          <td style="padding:6px 4px; text-align:right; font-weight:600;">{{ '%.2f'|format(k.καθαρό_ποσό) }} €</td>
        </tr>
        {% endfor %}
      </table>
      <div class="hint" style="margin-top:8px;">
        * Τα παραπάνω ποσά συμπεριλαμβάνουν το επίδομα παραμεθορίου, όπου ισχύει:
        <strong>ΠΛΗΡΕΣ</strong> ωράριο → 100€ μεικτά (περίπου 60€ καθαρά) ·
        <strong>ΜΕΙΩΜΕΝΟ</strong> ωράριο → 50€ μεικτά (περίπου 30€ καθαρά).
      </div>
      {% if paramethorios %}
      <div style="margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); font-size:12.5px; line-height:1.5;">
        {% if paramethorios.status == 'yes' %}
        <span style="color:#1F6B3A; font-weight:600;">✅ * Η περιοχή «{{ map_ctx.nomos }}» είναι παραμεθόριος/προβληματική
        περιοχή Α — ο αναπληρωτής δικαιούται επίδομα παραμεθορίου.</span>
        {% elif paramethorios.status == 'partial' %}
        <span style="color:var(--brass-dark); font-weight:600;">⚠️ * Μέρος του νομού «{{ map_ctx.nomos }}» είναι
        παραμεθόριος ({{ paramethorios.detail }}) — η εφαρμογή δεν μπορεί να ξεχωρίσει αν η συγκεκριμένη περιοχή
        εμπίπτει· χρειάζεται χειροκίνητος έλεγχος.</span>
        {% else %}
        <span style="color:var(--muted-dark);">❌ Ο νομός «{{ map_ctx.nomos }}» δεν είναι παραμεθόριος — δεν δικαιούται
        ο αναπληρωτής επίδομα παραμεθορίου.</span>
        {% endif %}
      </div>
      {% endif %}
    </div>
    {% endif %}
    {% if epidomata %}
    <div class="card">
      <div style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:12px;">
        👨‍👩‍👧 Λοιπά επιδόματα (καθαρό ποσό/μήνα)
      </div>
      <table style="width:100%; border-collapse:collapse; font-size:13px;">
        {% for e in epidomata %}
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:7px 4px;">{{ e.περιγραφή }}</td>
          <td style="padding:7px 4px; text-align:right; font-weight:600; white-space:nowrap;">{{ '%.2f'|format(e.καθαρό_ποσό) }} €</td>
        </tr>
        {% endfor %}
      </table>
    </div>
    {% endif %}
  </div>

</div>

{% if verdict %}
<div class="verdict-banner {{ 'verdict-ok' if verdict.ok else 'verdict-no' }}">
  <span style="font-size:22px;">{{ '✅' if verdict.ok else '❌' }}</span> {{ verdict.text }}
</div>
{% endif %}
{% if stats %}
<div class="stats-official">
  <div class="stats-official-title">Επίσημη σύνοψη θέσης</div>
  <div class="stats-official-grid">
    {% if stats.position %}
    <div class="stat-box">
      <div class="stat-label">Θέση στον τρέχοντα πίνακα</div>
      <div class="stat-value">#{{ '{:,}'.format(stats.position).replace(',', '.') }}</div>
      <div class="stat-sub">από {{ '{:,}'.format(stats.total).replace(',', '.') }} υποψηφίους</div>
    </div>
    {% endif %}
    {% if stats.position_after %}
    <div class="stat-box">
      <div class="stat-label">Θέση μετά την αφαίρεση μονίμων</div>
      <div class="stat-value">#{{ '{:,}'.format(stats.position_after).replace(',', '.') }}</div>
      <div class="stat-sub">από {{ '{:,}'.format(stats.total_after).replace(',', '.') }}
        ({{ stats.removed }} μόνιμοι αφαιρέθηκαν)</div>
    </div>
    {% endif %}
  </div>
</div>
{% endif %}
{% if output %}
<div class="slip slip-official">
  <div class="slip-head">
    <span class="slip-title">📄 Αποτέλεσμα ελέγχου</span>
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
    "ΑΝΑΤΟΛΙΚΗΣ ΑΤΤΙΚΗΣ": {"πρωτεύουσα": "Παλλήνη", "lat": 37.9000, "lng": 23.9500},
}
# Εναλλακτικές γραφές που εμφανίζονται συχνά στις "περιοχές διορισμού"
NOMOI_ALIASES = {
    "ΑΘΗΝΩΝ": "ΑΤΤΙΚΗΣ", "ΑΘΗΝΑΣ": "ΑΤΤΙΚΗΣ", "ΠΕΙΡΑΙΑ": "ΠΕΙΡΑΙΩΣ",
    "ΘΕΣΣΑΛΟΝΙΚΗ": "ΘΕΣΣΑΛΟΝΙΚΗΣ", "ΘΕΣΝΙΚΗΣ": "ΘΕΣΣΑΛΟΝΙΚΗΣ",  # "ΘΕΣ/ΝΙΚΗΣ" μετά την αφαίρεση "/"
}

# Οι 13 επίσημες περιφέρειες της Ελλάδας, με τους νομούς που περιλαμβάνει η
# καθεμία (κλειδιά όπως στο NOMOI παραπάνω). Χρησιμοποιείται στη σελίδα
# "Σχολεία" για το δίδυμο dropdown περιφέρεια -> νομός.
PERIFEREIES = {
    "Αττική": ["ΑΤΤΙΚΗΣ", "ΠΕΙΡΑΙΩΣ", "ΑΝΑΤΟΛΙΚΗΣ ΑΤΤΙΚΗΣ"],
    "Κεντρική Μακεδονία": ["ΘΕΣΣΑΛΟΝΙΚΗΣ", "ΗΜΑΘΙΑΣ", "ΚΙΛΚΙΣ", "ΠΕΛΛΑΣ", "ΠΙΕΡΙΑΣ", "ΣΕΡΡΩΝ", "ΧΑΛΚΙΔΙΚΗΣ"],
    "Ανατολική Μακεδονία και Θράκη": ["ΔΡΑΜΑΣ", "ΚΑΒΑΛΑΣ", "ΞΑΝΘΗΣ", "ΡΟΔΟΠΗΣ", "ΕΒΡΟΥ"],
    "Δυτική Μακεδονία": ["ΚΟΖΑΝΗΣ", "ΚΑΣΤΟΡΙΑΣ", "ΦΛΩΡΙΝΑΣ", "ΓΡΕΒΕΝΩΝ"],
    "Ήπειρος": ["ΙΩΑΝΝΙΝΩΝ", "ΘΕΣΠΡΩΤΙΑΣ", "ΑΡΤΑΣ", "ΠΡΕΒΕΖΑΣ"],
    "Θεσσαλία": ["ΛΑΡΙΣΑΣ", "ΜΑΓΝΗΣΙΑΣ", "ΤΡΙΚΑΛΩΝ", "ΚΑΡΔΙΤΣΑΣ"],
    "Στερεά Ελλάδα": ["ΦΘΙΩΤΙΔΑΣ", "ΒΟΙΩΤΙΑΣ", "ΕΥΒΟΙΑΣ", "ΦΩΚΙΔΑΣ", "ΕΥΡΥΤΑΝΙΑΣ"],
    "Δυτική Ελλάδα": ["ΑΧΑΙΑΣ", "ΑΙΤΩΛΟΑΚΑΡΝΑΝΙΑΣ", "ΗΛΕΙΑΣ"],
    "Πελοπόννησος": ["ΑΡΓΟΛΙΔΑΣ", "ΑΡΚΑΔΙΑΣ", "ΚΟΡΙΝΘΙΑΣ", "ΛΑΚΩΝΙΑΣ", "ΜΕΣΣΗΝΙΑΣ"],
    "Ιόνια Νησιά": ["ΚΕΡΚΥΡΑΣ", "ΛΕΥΚΑΔΑΣ", "ΚΕΦΑΛΛΗΝΙΑΣ", "ΖΑΚΥΝΘΟΥ"],
    "Βόρειο Αιγαίο": ["ΛΕΣΒΟΥ", "ΣΑΜΟΥ", "ΧΙΟΥ"],
    "Νότιο Αιγαίο": ["ΚΥΚΛΑΔΩΝ", "ΔΩΔΕΚΑΝΗΣΟΥ"],
    "Κρήτη": ["ΧΑΝΙΩΝ", "ΡΕΘΥΜΝΟΥ", "ΗΡΑΚΛΕΙΟΥ", "ΛΑΣΙΘΙΟΥ"],
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
    s = s.replace("/", "")                # "ΘΕΣ/ΝΙΚΗΣ" -> "ΘΕΣΝΙΚΗΣ" (συντομογραφίες με "/")
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
                "nomos_key": nomos_key, "prefix": prefix,
            }

    if nomos_key and nomos_key in NOMOI:
        info = NOMOI[nomos_key]
        return {
            "name": nomos_key.capitalize(), "capital": info["πρωτεύουσα"],
            "lat": info["lat"], "lng": info["lng"],
            "kind": "nomos", "search_name": nomos_key.capitalize(), "δήμοι": [],
            "nomos_key": nomos_key, "prefix": prefix,
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


def _extract_predict_stats(output_text: str):
    """Εξάγει τη θέση στον πίνακα (πριν/μετά την αφαίρεση μονίμων) από το
    κείμενο εξόδου του predict, για επίσημη, ευανάγνωστη εμφάνιση. Βασίζεται
    στη ΣΤΑΘΕΡΗ μορφή εκτύπωσης του main.py — αν αλλάξει εκεί, απλά δεν θα
    βρεθεί τίποτα (None), όχι λάθος αριθμοί."""
    if not output_text:
        return None
    stats = {}
    m1 = re.search(r"Στον τρέχοντα πίνακα \((\d+) υποψήφιοι\): περίπου θέση #(\d+)", output_text)
    if m1:
        stats["total"] = int(m1.group(1))
        stats["position"] = int(m1.group(2))
    m2 = re.search(r"Μετά την αφαίρεση (\d+) μονίμων: περίπου θέση #(\d+)\s*από (\d+)", output_text)
    if m2:
        stats["removed"] = int(m2.group(1))
        stats["position_after"] = int(m2.group(2))
        stats["total_after"] = int(m2.group(3))
    return stats or None


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
    paramethorios = None
    if loc:
        map_ctx = {
            "nomos": loc["name"], "capital": loc["capital"], "kind": loc["kind"],
            "search_name": loc["search_name"], "dimoi": loc.get("δήμοι") or [],
            "lat": loc["lat"], "lng": loc["lng"],
            "link_url": _osm_link(loc["lat"], loc["lng"]),
            "sch_url": _sch_embed_url(loc["lat"], loc["lng"]),
        }
        paramethorios = _check_paramethorios(loc.get("nomos_key"), loc.get("prefix"))

    return render_page(
        "home", "Γρήγορος έλεγχος", "Δώσε κλάδο, τοποθεσία-στόχο και τα μόριά σου — θα δεις πού θα έμπαινες φέτος.",
        HOME_TEMPLATE, form=form, output=output, error=error,
        verdict=_extract_verdict(output),
        stats=_extract_predict_stats(output),
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        avg_dates=_average_phase_dates(),
        last_update=_last_data_update(),
        moriodotisi=_load_moriodotisi(),
        mistho_klimakia=_load_mistho_klimakia(),
        epidomata=_load_epidomata(),
        paramethorios=paramethorios,
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
        year_options=_recent_years_only(_available_years(), cutoff=2024),
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
# Καρτέλα: Σχολεία — πραγματικός διαδραστικός χάρτης (περιφέρειες -> νομοί ->
# πρωτεύουσα/πόλεις + χάρτης σχολικών μονάδων ΠΣΔ). Τα όρια είναι πραγματικά
# γεωγραφικά δεδομένα (GeoJSON), όχι σχηματικά.
# ---------------------------------------------------------------------------
SCHOOLS_TEMPLATE = """
<div class="card" style="padding:0; overflow:hidden;">
  <div style="display:flex; flex-wrap:wrap;">
    <div style="flex:3 1 420px; min-width:0; position:relative;">
      <div id="greece-map" style="height:620px;"></div>
      <button type="button" id="map-back-btn" class="copy-btn"
              style="display:none; position:absolute; top:12px; left:12px; z-index:500; background:var(--card);">
        ← Πίσω στις περιφέρειες
      </button>
    </div>
    <div style="flex:1 1 220px; min-width:200px; padding:18px 20px; border-left:1px solid var(--line); max-height:620px; overflow-y:auto;">
      <div id="map-legend-title" style="font-size:11.5px; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; margin-bottom:10px;">Περιφέρειες</div>
      <div id="map-legend"></div>
    </div>
  </div>
</div>

<div id="nomos-info-card" class="card" style="display:none;">
  <div id="nomos-info-title" style="font-family:Georgia,'Iowan Old Style','Times New Roman',serif; font-size:17px; color:var(--ink); margin-bottom:8px;"></div>
  <div id="nomos-info-body" style="font-size:13.5px; line-height:1.6; color:var(--muted-dark);"></div>
</div>

<div id="schools-map-card" class="card" style="display:none;">
  <div id="schools-map-title" style="font-size:12.5px; font-weight:600; color:var(--muted-dark); margin-bottom:10px;"></div>
  <div style="border-radius:8px; overflow:hidden; border:1px solid var(--line);">
    <iframe id="schools-map-iframe" src="" width="100%" height="480" style="border:0; display:block;"
            scrolling="no" loading="lazy" title="Χάρτης σχολικών μονάδων ΠΣΔ"></iframe>
  </div>
  <a id="schools-map-link" href="https://maps.sch.gr/main.html" target="_blank" rel="noopener"
     style="display:inline-block; margin-top:8px; font-size:12px; color:var(--brass);">
    Άνοιγμα πλήρους χάρτη σε νέα καρτέλα ↗
  </a>
</div>

<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
(function () {
  var NOMOS_INFO = {{ nomos_info_json|safe }};
  var NOMOS_CITIES = {{ nomos_cities_json|safe }};
  var PALETTE = ["#B98A3D","#5C8AA6","#7A9B5C","#A65C7A","#8A6FB0","#C77B4E","#4E9B8F",
                 "#B0546F","#6F8FB0","#9B8A4E","#7A5C9B","#4E8F6B","#B06F4E"];

  var map = L.map("greece-map", { scrollWheelZoom: true }).setView([39.0, 22.6], 6);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors", maxZoom: 18,
  }).addTo(map);

  var perifLayer = null, nomosLayer = null;
  var perifData = null, nomoiData = null;
  var backBtn = document.getElementById("map-back-btn");
  var legendEl = document.getElementById("map-legend");
  var legendTitleEl = document.getElementById("map-legend-title");
  var infoCard = document.getElementById("nomos-info-card");
  var schoolsCard = document.getElementById("schools-map-card");

  function titleCase(s) {
    return s.toLowerCase().replace(/(^|\\s|-)\\p{L}/gu, function (c) { return c.toUpperCase(); });
  }

  function fetchGeoJSON(url, label) {
    return fetch(url).then(function (r) {
      if (!r.ok) {
        throw new Error(label + ": HTTP " + r.status + " στο " + url);
      }
      return r.json();
    });
  }

  Promise.all([
    fetchGeoJSON("{{ url_for('static', filename='greece_periphereies.geojson') }}", "Περιφέρειες"),
    fetchGeoJSON("{{ url_for('static', filename='greece_nomoi.geojson') }}", "Νομοί"),
  ]).then(function (results) {
    perifData = results[0];
    nomoiData = results[1];
    renderPerifereies();
  }).catch(function (err) {
    legendEl.innerHTML = "<div style=\\"font-size:12px; color:var(--muted); line-height:1.5;\\">⚠️ Δεν φορτώθηκε ο χάρτης.<br><br>"
      + "<strong>Λεπτομέρεια:</strong> " + (err && err.message ? err.message : String(err))
      + "<br><br>Πιθανότατα λείπουν τα αρχεία .geojson από τον φάκελο static/.</div>";
  });

  function renderPerifereies() {
    infoCard.style.display = "none";
    schoolsCard.style.display = "none";
    backBtn.style.display = "none";
    if (nomosLayer) { map.removeLayer(nomosLayer); nomosLayer = null; }
    if (perifLayer) { map.removeLayer(perifLayer); }

    legendTitleEl.textContent = "Περιφέρειες";
    legendEl.innerHTML = "";
    var colorOf = {};
    perifData.features.forEach(function (f, i) { colorOf[f.properties.name_greek] = PALETTE[i % PALETTE.length]; });

    perifLayer = L.geoJSON(perifData, {
      style: function (f) {
        return { color: "#fff", weight: 1.5, fillColor: colorOf[f.properties.name_greek], fillOpacity: 0.32 };
      },
      onEachFeature: function (f, layer) {
        layer.on("click", function () { selectPerif(f.properties.name_greek); });
        layer.on("mouseover", function () { layer.setStyle({ fillOpacity: 0.5 }); });
        layer.on("mouseout", function () { layer.setStyle({ fillOpacity: 0.32 }); });
      },
    }).addTo(map);
    map.fitBounds(perifLayer.getBounds(), { padding: [10, 10] });

    perifData.features.forEach(function (f, i) {
      var name = f.properties.name_greek;
      var item = document.createElement("div");
      item.style.cssText = "display:flex; align-items:center; gap:8px; padding:7px 0; font-size:12.5px; cursor:pointer; border-bottom:1px solid var(--line);";
      item.innerHTML = "<span style=\\"width:11px; height:11px; border-radius:3px; background:" + colorOf[name]
        + "; flex-shrink:0;\\"></span><span style=\\"color:var(--muted-dark);\\">" + (i + 1) + ".</span> " + titleCase(name);
      item.addEventListener("click", function () { selectPerif(name); });
      legendEl.appendChild(item);
    });
  }

  function selectPerif(perifName) {
    map.removeLayer(perifLayer);
    backBtn.style.display = "inline-block";
    legendTitleEl.textContent = titleCase(perifName);

    var members = nomoiData.features.filter(function (f) {
      var info = NOMOS_INFO[f.properties.name_greek.trim()];
      return info && info.perif === perifName;
    });
    var colorOf = {};
    members.forEach(function (f, i) { colorOf[f.properties.name_greek.trim()] = PALETTE[i % PALETTE.length]; });

    nomosLayer = L.geoJSON({ type: "FeatureCollection", features: members }, {
      style: function (f) {
        return { color: "#fff", weight: 1.5, fillColor: colorOf[f.properties.name_greek.trim()], fillOpacity: 0.38 };
      },
      onEachFeature: function (f, layer) {
        var n = f.properties.name_greek.trim();
        layer.on("click", function () { selectNomos(n, colorOf); });
      },
    }).addTo(map);
    map.fitBounds(nomosLayer.getBounds(), { padding: [16, 16] });

    legendEl.innerHTML = "";
    members.forEach(function (f, i) {
      var n = f.properties.name_greek.trim();
      var info = NOMOS_INFO[n];
      var item = document.createElement("div");
      item.style.cssText = "display:flex; align-items:center; gap:8px; padding:7px 0; font-size:12.5px; cursor:pointer; border-bottom:1px solid var(--line);";
      item.innerHTML = "<span style=\\"width:11px; height:11px; border-radius:3px; background:" + colorOf[n]
        + "; flex-shrink:0;\\"></span><span style=\\"color:var(--muted-dark);\\">" + (i + 1) + ".</span> "
        + titleCase(n) + " <span style=\\"color:var(--muted); font-size:11px;\\">(" + info.capital + ")</span>";
      item.addEventListener("click", function () { selectNomos(n, colorOf); });
      legendEl.appendChild(item);
    });

    infoCard.style.display = "none";
    schoolsCard.style.display = "none";
  }

  function selectNomos(name, colorOf) {
    var ownColor = colorOf[name] || "#B98A3D";
    nomosLayer.eachLayer(function (layer) {
      var n = layer.feature.properties.name_greek.trim();
      if (n === name) {
        layer.setStyle({ fillColor: ownColor, fillOpacity: 0.28, color: ownColor, weight: 3 });
        layer.bringToFront();
        map.fitBounds(layer.getBounds(), { padding: [24, 24] });
      } else {
        layer.setStyle({ fillColor: "#ffffff", fillOpacity: 0.15, color: "#D7DEE6", weight: 1 });
      }
    });

    var info = NOMOS_INFO[name];
    var cities = NOMOS_CITIES[name];

    // Το όνομα/πρωτεύουσα/πόλεις μπαίνουν ΠΑΝΩ-ΠΑΝΩ στο πλαϊνό πάνελ (πάνω από τη
    // λίστα των νομών), ώστε να φαίνονται αμέσως χωρίς να χρειάζεται scroll κάτω
    // από τον (ψηλό) χάρτη.
    var selBody = "<div id=\\"nomos-legend-info\\">"
      + "<div style=\\"font-family:Georgia,'Iowan Old Style','Times New Roman',serif; font-size:16px; color:var(--ink); margin-bottom:6px;\\">"
      + titleCase(name) + "</div>"
      + "<div style=\\"font-size:12.5px; line-height:1.6; color:var(--muted-dark); margin-bottom:14px; padding-bottom:14px; border-bottom:1px solid var(--line);\\">"
      + "<strong>Πρωτεύουσα:</strong> " + info.capital;
    if (cities && cities.length) {
      selBody += "<br><strong>Μεγάλες πόλεις:</strong> " + cities.join(", ");
    }
    selBody += "</div></div>";
    var oldInfo = document.getElementById("nomos-legend-info");
    if (oldInfo) { oldInfo.remove(); }
    legendEl.insertAdjacentHTML("afterbegin", selBody);

    // Κρατάμε και την πιο αναλυτική κάρτα κάτω από τον χάρτη επίσης.
    infoCard.style.display = "block";
    document.getElementById("nomos-info-title").textContent = titleCase(name);
    var body = "<strong>Πρωτεύουσα:</strong> " + info.capital;
    if (cities && cities.length) {
      body += "<br><strong>Μεγάλες πόλεις:</strong> " + cities.join(", ");
    }
    document.getElementById("nomos-info-body").innerHTML = body;

    schoolsCard.style.display = "block";
    document.getElementById("schools-map-title").textContent = "🏫 Σχολικές μονάδες — " + titleCase(name);
    document.getElementById("schools-map-iframe").src =
      "https://maps.sch.gr/embed.html?zoom=10&lat=" + info.lat + "&lng=" + info.lng;
  }

  backBtn.addEventListener("click", renderPerifereies);
})();
</script>
"""


@app.route("/schools")
def schools():
    nomos_info = _load_nomos_units_info()
    nomos_cities = _load_nomos_cities()
    return render_page(
        "schools", "Σχολεία",
        "Κάνε κλικ σε μια περιφέρεια, μετά σε έναν νομό, για να δεις την πρωτεύουσα, τις μεγάλες πόλεις, "
        "και τις σχολικές μονάδες Πρωτοβάθμιας/Δευτεροβάθμιας εκεί.",
        SCHOOLS_TEMPLATE,
        nomos_info_json=json.dumps(nomos_info, ensure_ascii=False),
        nomos_cities_json=json.dumps(nomos_cities, ensure_ascii=False),
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
# eduAI — μικρός βοηθός AI (Google Gemini — έχει πραγματικό δωρεάν επίπεδο,
# χωρίς κάρτα, χωρίς λήξη). Το κλειδί API διαβάζεται ΜΟΝΟ από τη μεταβλητή
# περιβάλλοντος GEMINI_API_KEY — ΠΟΤΕ δεν γράφεται εδώ. Χωρίς αυτήν, η
# διαδρομή απαντάει ευγενικά ότι δεν έχει ρυθμιστεί, χωρίς να ρίχνει τον server.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # δες ai.google.dev/gemini-api/docs/rate-limits αν αλλάξει

_EDUAI_SYSTEM_BASE = (
    "Είσαι το eduAI, ένας σύντομος βοηθός μέσα σε μια ανεπίσημη εφαρμογή ελέγχου "
    "πινάκων κατάταξης αναπληρωτών εκπαιδευτικών στην Ελλάδα (κλάδοι ΠΕ, μόρια, "
    "φάσεις πρόσληψης, περιοχές διορισμού). Βοηθάς τους επισκέπτες να καταλάβουν "
    "πώς λειτουργεί η εφαρμογή και τι σημαίνουν οι όροι. Απαντάς ΣΥΝΤΟΜΑ (2-5 "
    "προτάσεις συνήθως) και καθαρά, στα ελληνικά. ΔΕΝ είσαι επίσημη πηγή — για "
    "νομικά/επίσημα θέματα παραπέμπεις σε ΑΣΕΠ/Υπουργείο Παιδείας. Δεν γνωρίζεις "
    "προσωπικά στοιχεία συγκεκριμένων υποψηφίων. Οι καρτέλες της εφαρμογής είναι: "
    "Γρήγορος έλεγχος, Πλήρης Ανάλυση, Σύνοψη Κλάδου, Βάση Φάσης, Αναβαθμίσεις, "
    "Αρχεία αποτελεσμάτων."
)


def _eduai_system_prompt() -> str:
    """Το system prompt, εμπλουτισμένο δυναμικά με την τρέχουσα μοριοδότηση από
    το moriodotisi.json — έτσι το eduAI απαντάει με τα ΠΡΑΓΜΑΤΙΚΑ, ενημερωμένα
    νούμερα της εφαρμογής, όχι με ό,τι θυμάται από γενική εκπαίδευση."""
    prompt = _EDUAI_SYSTEM_BASE
    try:
        groups = _load_moriodotisi()
        if groups:
            lines = ["\n\nΤρέχουσα μοριοδότηση (για αναφορά, χρησιμοποίησέ τα αν ρωτηθείς):"]
            for g in groups:
                lines.append(f"- {g.get('τίτλος', '')}:")
                for item in g.get("κατηγορίες", []):
                    lines.append(f"    {item.get('κατηγορία', '')}: {item.get('μόρια', '')} "
                                 f"({item.get('μονάδα', '')})")
            prompt += "\n".join(lines)
    except Exception:                                                 # noqa: BLE001
        pass
    return prompt


# Απλό rate-limit ανά IP στη μνήμη της διεργασίας — προστασία από κατάχρηση σε
# δημόσια, πληρωμένη ανά χρήση υπηρεσία. Καθαρίζεται αυτόματα (παλιά ίχνη
# αγνοούνται/αντικαθίστανται) — δεν χρειάζεται ξεχωριστό cleanup thread.
_CHAT_RATE_LIMIT_MAX = 20
_CHAT_RATE_LIMIT_WINDOW = 600  # 10 λεπτά
_chat_rate_hits: dict = {}


def _chat_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _chat_rate_hits.get(ip, []) if now - t < _CHAT_RATE_LIMIT_WINDOW]
    if len(hits) >= _CHAT_RATE_LIMIT_MAX:
        _chat_rate_hits[ip] = hits
        return False
    hits.append(now)
    _chat_rate_hits[ip] = hits
    return True


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if not GEMINI_API_KEY:
        return {"error": "Το eduAI δεν έχει ρυθμιστεί ακόμα σε αυτόν τον server "
                          "(λείπει το κλειδί API)."}, 503

    ip = request.remote_addr or "unknown"
    if not _chat_rate_ok(ip):
        return {"error": "Πάρα πολλά μηνύματα από εσένα σε λίγη ώρα — δοκίμασε ξανά σε λίγα λεπτά."}, 429

    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    history = data.get("history") or []
    if not message:
        return {"error": "Λείπει το μήνυμα."}, 400
    if len(message) > 2000:
        return {"error": "Πολύ μεγάλο μήνυμα (μέγιστο 2000 χαρακτήρες)."}, 400

    # Το Gemini θέλει "contents" (όχι "messages"), με ρόλους "user"/"model"
    # (όχι "assistant"), και ξεχωριστό "systemInstruction" (όχι μήνυμα role=system).
    contents = []
    if isinstance(history, list):
        for h in history[-8:]:                                        # μόνο τα τελευταία λίγα
            if not isinstance(h, dict):
                continue
            role, content = h.get("role"), h.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                gem_role = "model" if role == "assistant" else "user"
                contents.append({"role": gem_role, "parts": [{"text": content[:2000]}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = json.dumps({
        "contents": contents,
        "systemInstruction": {"parts": [{"text": _eduai_system_prompt()}]},
        "generationConfig": {"maxOutputTokens": 500, "temperature": 0.5},
    }).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
           f"?key={GEMINI_API_KEY}")
    req = urllib.request.Request(url, data=payload, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        candidates = result.get("candidates") or []
        reply = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            reply = "".join(p.get("text", "") for p in parts).strip()
        return {"reply": reply or "Δεν έλαβα απάντηση — δοκίμασε ξανά."}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            err = parsed.get("error", parsed)
            detail = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:                                              # noqa: BLE001
            detail = ""
        msg = f"Σφάλμα από το eduAI (κωδικός {exc.code})"
        if detail:
            msg += f": {detail[:300]}"
        return {"error": msg + ". Δοκίμασε ξανά σε λίγο."}, 502
    except Exception:                                                  # noqa: BLE001
        return {"error": "Κάτι πήγε στραβά με το eduAI. Δοκίμασε ξανά σε λίγο."}, 502


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
