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
import threading
import traceback
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


def _available_klados():
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
        return sorted(real)
    except Exception:
        return []


def _available_years():
    try:
        fdir = core.CFG.faseis_dir()
        if not fdir.exists():
            return []
        out_dir = core.CFG.DATA_DIR / core.CFG.OUTPUT_SUBDIR
        files = [f for f in sorted(fdir.rglob("*"))
                 if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)]
        years = set()
        for f in files:
            rel = f.relative_to(fdir)
            _, y = core.extract_phase_year(" / ".join(rel.parts))
            if y:
                years.add(y)
        return sorted(years)
    except Exception:
        return []


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
<style>
  :root {
    --ink: #16233B; --paper: #EEF2F6; --card: #FFFFFF;
    --brass: #B98A3D; --brass-dark: #8F6A2C; --line: #D7DEE6;
    --danger: #B23A3A; --radius: 10px;
  }
  * { box-sizing: border-box; }
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
  main { flex: 1; padding: 36px 44px; max-width: 980px; }
  h1.page-title {
    font-family: Georgia,"Iowan Old Style","Times New Roman",serif;
    font-size: 27px; margin: 0 0 6px;
  }
  p.page-sub { color: #55617A; margin: 0 0 26px; font-size: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 24px 26px; margin-bottom: 22px;
  }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 12.5px; font-weight: 600; color: #3C4A63; margin-bottom: 6px; }
  .field input[type=text], .field input[type=number], .field select {
    width: 100%; max-width: 340px; padding: 9px 11px; border: 1px solid var(--line);
    border-radius: 6px; font-size: 14px; background: #FBFCFE; color: var(--ink);
  }
  .field input:focus, .field select:focus {
    outline: 2px solid var(--brass); outline-offset: 1px; border-color: var(--brass);
  }
  .field .hint { font-size: 11.5px; color: #7C879A; margin-top: 4px; }
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
    padding: 13px 22px 12px; border-bottom: 1px dashed var(--line); font-size: 13px; color: #55617A;
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
    <div class="sidebar-footer">📁 {{ data_dir }}</div>
  </nav>
  <main>
    <h1 class="page-title">{{ title }}</h1>
    <p class="page-sub">{{ subtitle }}</p>
    {{ body|safe }}
  </main>
</div>
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
{% if avg_dates %}
<div class="card">
  <div style="font-size:12.5px; font-weight:600; color:#3C4A63; margin-bottom:10px;">
    📅 Μέσος όρος ημερομηνιών ανά φάση (βάσει ιστορικού — ενδεικτικό, όχι εγγύηση)
  </div>
  <table style="width:100%; border-collapse:collapse; font-size:13.5px;">
    {% for phase, info in avg_dates.items() %}
    <tr style="border-bottom:1px solid var(--line);">
      <td style="padding:7px 0; font-weight:600; width:90px;">{{ phase }}΄ Φάση</td>
      <td style="padding:7px 0; text-align:right; color:#55617A;">
        {{ info.text }} <span style="opacity:.65;">({{ info.years }})</span>
      </td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}
{% if output %}
<div class="slip">
  <div class="slip-head"><span>Αποτέλεσμα ελέγχου</span><span class="tag">{{ form.klados }}</span></div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


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
    return render_page(
        "home", "Γρήγορος έλεγχος", "Δώσε κλάδο, τοποθεσία-στόχο και τα μόριά σου — θα δεις πού θα έμπαινες φέτος.",
        HOME_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        avg_dates=_average_phase_dates(),
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
  <div class="slip-head"><span>Σύνοψη κλάδου</span><span class="tag">{{ form.klados }}</span></div>
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
      <div class="field">
        <label for="exclude_year">Εξαίρεση ήδη προσληφθέντων στο έτος (προαιρετικό)</label>
        <select id="exclude_year" name="exclude_year">
          <option value="" {{ 'selected' if not form.exclude_year }}>— καμία εξαίρεση —</option>
          {% for y in year_options %}
          <option value="{{ y }}" {{ 'selected' if form.exclude_year==y }}>{{ y }}</option>
          {% endfor %}
        </select>
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
  <div class="slip-head"><span>Πλήρης ανάλυση</span><span class="tag">{{ form.klados }}</span></div>
  <pre>{{ output }}</pre>
</div>
{% endif %}
"""


@app.route("/full", methods=["GET", "POST"])
def full():
    form = {"klados": "", "region": "", "subcodes": False, "monimoi": True,
            "exclude_year": "", "top_regions": "25"}
    output, error = None, None
    if request.method == "POST":
        form["klados"] = request.form.get("klados", "").strip().upper()
        form["region"] = request.form.get("region", "").strip()
        form["subcodes"] = bool(request.form.get("subcodes"))
        form["monimoi"] = bool(request.form.get("monimoi"))
        form["exclude_year"] = request.form.get("exclude_year", "").strip()
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
                    exclude_year=form["exclude_year"] or None,
                )
                output = _run_capture(core.cmd_full, args)
    return render_page(
        "full", "Πλήρης ανάλυση",
        "Κλάδος, αφαίρεση μονίμων, περιοχή διορισμού και αποθήκευση αρχείου επαλήθευσης.",
        FULL_TEMPLATE, form=form, output=output, error=error,
        klados_datalist=_klados_datalist("klados-list", _available_klados()),
        year_options=_available_years(),
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
  <div class="slip-head"><span>Βάση φάσης</span><span class="tag">{{ form.klados }}</span></div>
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
  <div class="slip-head"><span>Μειωμένο → Πλήρες</span><span class="tag">{{ form.klados }}</span></div>
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
  <p style="margin:0; color:#7C879A;">Δεν έχουν δημιουργηθεί ακόμα αρχεία. Τρέξε μια Πλήρη Ανάλυση, Βάση
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
def _open_browser():
    webbrowser.open("http://127.0.0.1:5000/")


if __name__ == "__main__":
    threading.Timer(1.0, _open_browser).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
