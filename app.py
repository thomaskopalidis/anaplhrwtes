# -*- coding: utf-8 -*-
"""
app.py — Γραφική εφαρμογή (GUI) για το ΣΥΣΤΗΜΑ ΑΝΑΛΥΣΗΣ ΠΙΝΑΚΩΝ ΑΝΑΠΛΗΡΩΤΩΝ

ΤΙ ΕΙΝΑΙ: ένα παράθυρο με καρτέλες (μία ανά εντολή: Σύνοψη / Πλήρης Ανάλυση /
Βάση Φάσης / Πρόβλεψη / Αναβαθμίσεις) όπου συμπληρώνεις πεδία και πατάς
«Εκτέλεση» — αντί να γράφεις εντολές στο τερματικό. Από κάτω τρέχει ο ΙΔΙΟΣ
κώδικας του main.py, χωρίς καμία αλλαγή στη λογική ανάλυσης.

ΠΩΣ ΤΟ ΤΡΕΧΕΙΣ:
  1) Βάλε αυτό το αρχείο (app.py) στον ΙΔΙΟ φάκελο με το main.py, config.py,
     loader.py, normalize.py, pipeline.py, audit.py.
  2) Άνοιξε το ίδιο παράθυρο (Anaconda Prompt / PowerShell) που χρησιμοποιείς
     ήδη για το main.py, και γράψε:  python app.py
  3) Ανοίγει ένα παράθυρο εφαρμογής. Καμία γραμμή εντολών δεν χρειάζεται μετά.

ΓΙΑ ΑΝΟΙΓΜΑ ΜΕ ΔΙΠΛΟ ΚΛΙΚ (χωρίς παράθυρο τερματικού):
  Κάνε ένα αντίγραφο αυτού του αρχείου με όνομα app.pyw (ίδιο περιεχόμενο,
  μόνο η κατάληξη αλλάζει) στον ίδιο φάκελο. Τα Windows το ανοίγουν με διπλό
  κλικ μέσω του pythonw.exe, χωρίς μαύρο παράθυρο τερματικού από πίσω.
"""
from __future__ import annotations

import builtins
import io
import os
import queue
import subprocess
import sys
import threading
import traceback
from contextlib import contextmanager, redirect_stdout
from tkinter import BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# Εισαγωγή του κύριου αρχείου ανάλυσης (main.py). Δοκιμάζουμε και main2.py
# σε περίπτωση που έτσι έχει αποθηκευτεί το αρχείο.
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
        "❌ Δεν βρέθηκε το main.py (ή main2.py) στον ίδιο φάκελο με το app.py.\n"
        "   Βάλε το app.py στον ίδιο φάκελο με το main.py, config.py, loader.py,\n"
        "   normalize.py, pipeline.py, audit.py και ξανατρέξε.\n\n"
        "   Λεπτομέρειες:\n   " + "\n   ".join(_import_errors)
    )

# Ίδιο module με αυτό που χρησιμοποιεί το main.py — χρειάζεται ξεχωριστά εδώ
# γιατί εκεί εισάγεται τοπικά μέσα σε συναρτήσεις, όχι σε επίπεδο module.
from loader import DATA_SUFFIXES, is_own_output


# ---------------------------------------------------------------------------
# Ασφάλεια: αν κάποια εσωτερική συνάρτηση ζητήσει input() που δεν το
# περιμέναμε από τη φόρμα, απαντάμε αυτόματα με Enter/κενό (= η προεπιλογή σε
# όλα σχεδόν τα σημεία του main.py) αντί να «κρεμάσει» η εφαρμογή περιμένοντας
# πληκτρολόγηση που δεν μπορεί να έρθει σε ένα GUI.
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


class _QueueWriter(io.TextIOBase):
    """Ό,τι γράφεται με print() πάει σε μια Queue, ώστε να το διαβάζει με
    ασφάλεια το κύριο thread του tkinter (τα widgets δεν είναι thread-safe)."""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Βοηθητικά: λίστες κλάδων / ετών / περιοχών για τα comboboxes
# ---------------------------------------------------------------------------
def _available_klados():
    try:
        pinakes = core._files_of(core.CFG.pinakes_dir(), "base")
        codes = set()
        for f in pinakes:
            codes.update(core.extract_klados_codes(f.stem))
        return sorted(codes)
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
            years.add(y or "(άγνωστο έτος)")
        return sorted(years, key=lambda y: (y == "(άγνωστο έτος)", y))
    except Exception:
        return []


def _open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                      # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:
        messagebox.showerror("Σφάλμα", f"Δεν μπόρεσα να ανοίξω τον φάκελο:\n{exc}")


# ---------------------------------------------------------------------------
# Πλαίσιο εξόδου + εκτέλεση μιας cmd_* συνάρτησης σε background thread, ώστε
# η εφαρμογή να μη «παγώνει» όσο τρέχει η ανάλυση.
# ---------------------------------------------------------------------------
class RunnerPanel:
    def __init__(self, parent, run_button: ttk.Button):
        self.text = ScrolledText(parent, height=22, wrap="word",
                                  font=("Consolas", 10), state="normal")
        self.run_button = run_button
        self._queue = None

    def widget(self):
        return self.text

    def clear(self):
        self.text.delete("1.0", "end")

    def run(self, cmd_func, args_ns):
        self.clear()
        self.text.insert("end", "⏳ Επεξεργασία… (μπορεί να πάρει λίγα δευτερόλεπτα ανάλογα με το πλήθος αρχείων)\n\n")
        self.run_button.config(state="disabled")
        q: "queue.Queue" = queue.Queue()
        self._queue = q

        def worker():
            writer = _QueueWriter(q)
            try:
                with redirect_stdout(writer), _auto_answer_prompts():
                    cmd_func(args_ns)
            except SystemExit as exc:
                writer.write(f"\n⛔ Διακόπηκε: {exc}\n")
            except Exception:                              # noqa: BLE001
                writer.write("\n❌ Σφάλμα κατά την εκτέλεση:\n")
                writer.write(traceback.format_exc())
            q.put(None)  # sentinel = τέλος

        threading.Thread(target=worker, daemon=True).start()
        self.text.after(80, self._poll)

    def _poll(self):
        q = self._queue
        done = False
        try:
            while True:
                item = q.get_nowait()
                if item is None:
                    done = True
                    break
                self.text.insert("end", item)
                self.text.see("end")
        except queue.Empty:
            pass
        if done:
            self.run_button.config(state="normal")
        else:
            self.text.after(80, self._poll)


# ---------------------------------------------------------------------------
# Βοηθητικά widgets φόρμας
# ---------------------------------------------------------------------------
def _row_label_entry(parent, row, label, default="", width=30):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
    var = StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=1, sticky="w", pady=4)
    return var


def _row_label_combo(parent, row, label, values=(), default="", width=28):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
    var = StringVar(value=default)
    combo = ttk.Combobox(parent, textvariable=var, values=list(values), width=width - 2)
    combo.grid(row=row, column=1, sticky="w", pady=4)
    return var, combo


def _row_checkbox(parent, row, label, default=False):
    var = BooleanVar(value=default)
    ttk.Checkbutton(parent, text=label, variable=var).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=4)
    return var


# ---------------------------------------------------------------------------
# Καρτέλες
# ---------------------------------------------------------------------------
def build_summary_tab(nb):
    frame = ttk.Frame(nb, padding=12)
    form = ttk.Frame(frame)
    form.pack(fill="x")

    klados_var, klados_combo = _row_label_combo(form, 0, "Κλάδος (π.χ. ΠΕ06):",
                                                 values=_available_klados())
    subcodes_var = _row_checkbox(form, 1, "Να περιλαμβάνει υποκλάδους (π.χ. ΠΕ11.01)")

    ttk.Button(form, text="🔄 Ανανέωση λίστας κλάδων",
               command=lambda: klados_combo.config(values=_available_klados())
               ).grid(row=0, column=2, padx=8)

    run_btn = ttk.Button(form, text="▶  Εκτέλεση")
    run_btn.grid(row=2, column=0, columnspan=2, pady=(8, 0), sticky="w")

    panel = RunnerPanel(frame, run_btn)
    panel.widget().pack(fill="both", expand=True, pady=(10, 0))

    def on_run():
        klados = klados_var.get().strip().upper()
        if not klados:
            messagebox.showwarning("Λείπει στοιχείο", "Δώσε κωδικό κλάδου (π.χ. ΠΕ06).")
            return
        args = SimpleNamespace(base=str(core.CFG.pinakes_dir()), klados=klados,
                                subcodes=subcodes_var.get())
        panel.run(core.cmd_summary, args)

    run_btn.config(command=on_run)
    return frame


def build_full_tab(nb):
    frame = ttk.Frame(nb, padding=12)
    form = ttk.Frame(frame)
    form.pack(fill="x")

    klados_var, klados_combo = _row_label_combo(form, 0, "Κλάδος (π.χ. ΠΕ06):",
                                                 values=_available_klados())
    region_var = _row_label_entry(form, 1, "Περιοχή διορισμού (κενό = όλες):")
    subcodes_var = _row_checkbox(form, 2, "Να περιλαμβάνει υποκλάδους")
    monimoi_var = _row_checkbox(form, 3, "Αφαίρεση μονίμων/διορισμένων", default=True)
    year_var, year_combo = _row_label_combo(
        form, 4, "Εξαίρεση ήδη προσληφθέντων στο έτος (προαιρετικό):",
        values=_available_years())
    top_regions_var = _row_label_entry(form, 5, "Πλήθος περιοχών στη λίστα:", default="25", width=8)

    ttk.Button(form, text="🔄 Ανανέωση κλάδων/ετών",
               command=lambda: (klados_combo.config(values=_available_klados()),
                                 year_combo.config(values=_available_years()))
               ).grid(row=0, column=2, padx=8)

    run_btn = ttk.Button(form, text="▶  Εκτέλεση")
    run_btn.grid(row=6, column=0, columnspan=2, pady=(8, 0), sticky="w")

    panel = RunnerPanel(frame, run_btn)
    panel.widget().pack(fill="both", expand=True, pady=(10, 0))

    def on_run():
        klados = klados_var.get().strip().upper()
        if not klados:
            messagebox.showwarning("Λείπει στοιχείο", "Δώσε κωδικό κλάδου.")
            return
        try:
            top_regions = int(top_regions_var.get().strip() or 25)
        except ValueError:
            messagebox.showwarning("Μη έγκυρη τιμή", "Το πλήθος περιοχών πρέπει να είναι αριθμός.")
            return
        year = year_var.get().strip()
        if year == "(άγνωστο έτος)":
            year = ""
        args = SimpleNamespace(
            base=str(core.CFG.pinakes_dir()),
            klados=klados,
            monimoi=str(core.CFG.monimoi_dir()) if monimoi_var.get() else None,
            hires=None,
            region=region_var.get().strip() or None,
            subcodes=subcodes_var.get(),
            top_regions=top_regions,
            exclude_year=year or None,
        )
        panel.run(core.cmd_full, args)

    run_btn.config(command=on_run)
    return frame


def build_phase_tab(nb):
    frame = ttk.Frame(nb, padding=12)
    form = ttk.Frame(frame)
    form.pack(fill="x")

    klados_var, klados_combo = _row_label_combo(form, 0, "Κλάδος (π.χ. ΠΕ06):",
                                                 values=_available_klados())
    region_var = _row_label_entry(form, 1, "Περιοχή διορισμού:")
    year_var, year_combo = _row_label_combo(
        form, 2, "Σχολικό έτος (κενό = συνδυασμός όλων):", values=_available_years())
    subcodes_var = _row_checkbox(form, 3, "Να περιλαμβάνει υποκλάδους")

    ttk.Button(form, text="🔄 Ανανέωση κλάδων/ετών",
               command=lambda: (klados_combo.config(values=_available_klados()),
                                 year_combo.config(values=_available_years()))
               ).grid(row=0, column=2, padx=8)

    run_btn = ttk.Button(form, text="▶  Εκτέλεση")
    run_btn.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="w")

    panel = RunnerPanel(frame, run_btn)
    panel.widget().pack(fill="both", expand=True, pady=(10, 0))

    def on_run():
        klados = klados_var.get().strip().upper()
        region = region_var.get().strip()
        if not klados or not region:
            messagebox.showwarning("Λείπουν στοιχεία", "Χρειάζονται κλάδος ΚΑΙ περιοχή.")
            return
        year = year_var.get().strip()
        if year == "(άγνωστο έτος)":
            year = ""
        args = SimpleNamespace(klados=klados, region=region, file=None,
                                year=year or None, subcodes=subcodes_var.get())
        panel.run(core.cmd_phase, args)

    run_btn.config(command=on_run)
    return frame


def build_predict_tab(nb):
    frame = ttk.Frame(nb, padding=12)
    form = ttk.Frame(frame)
    form.pack(fill="x")

    klados_var, klados_combo = _row_label_combo(form, 0, "Κλάδος (π.χ. ΠΕ06):",
                                                 values=_available_klados())
    region_var = _row_label_entry(form, 1, "Περιοχή-στόχος:")
    moria_var = _row_label_entry(form, 2, "Τα μόριά σου:", width=12)
    subcodes_var = _row_checkbox(form, 3, "Να περιλαμβάνει υποκλάδους")

    ttk.Button(form, text="🔄 Ανανέωση κλάδων",
               command=lambda: klados_combo.config(values=_available_klados())
               ).grid(row=0, column=2, padx=8)

    run_btn = ttk.Button(form, text="▶  Εκτέλεση")
    run_btn.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="w")

    panel = RunnerPanel(frame, run_btn)
    panel.widget().pack(fill="both", expand=True, pady=(10, 0))

    def on_run():
        klados = klados_var.get().strip().upper()
        region = region_var.get().strip()
        if not klados or not region:
            messagebox.showwarning("Λείπουν στοιχεία", "Χρειάζονται κλάδος ΚΑΙ περιοχή-στόχος.")
            return
        try:
            moria = float(moria_var.get().strip().replace(",", "."))
        except ValueError:
            messagebox.showwarning("Μη έγκυρη τιμή", "Δώσε τα μόριά σου σε αριθμό (π.χ. 50.8).")
            return
        args = SimpleNamespace(klados=klados, region=region, moria=moria, name=None,
                                subcodes=subcodes_var.get())
        panel.run(core.cmd_predict, args)

    run_btn.config(command=on_run)
    return frame


def build_upgrades_tab(nb):
    frame = ttk.Frame(nb, padding=12)
    form = ttk.Frame(frame)
    form.pack(fill="x")

    klados_var, klados_combo = _row_label_combo(form, 0, "Κλάδος (π.χ. ΠΕ06):",
                                                 values=_available_klados())
    region_var = _row_label_entry(form, 1, "Περιοχή (κενό = όλες):")
    year_var, year_combo = _row_label_combo(
        form, 2, "Σχολικό έτος (κενό = όλα τα διαθέσιμα):", values=_available_years())
    subcodes_var = _row_checkbox(form, 3, "Να περιλαμβάνει υποκλάδους")

    ttk.Button(form, text="🔄 Ανανέωση κλάδων/ετών",
               command=lambda: (klados_combo.config(values=_available_klados()),
                                 year_combo.config(values=_available_years()))
               ).grid(row=0, column=2, padx=8)

    run_btn = ttk.Button(form, text="▶  Εκτέλεση")
    run_btn.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="w")

    panel = RunnerPanel(frame, run_btn)
    panel.widget().pack(fill="both", expand=True, pady=(10, 0))

    def on_run():
        klados = klados_var.get().strip().upper()
        if not klados:
            messagebox.showwarning("Λείπει στοιχείο", "Δώσε κωδικό κλάδου.")
            return
        year = year_var.get().strip()
        if year == "(άγνωστο έτος)":
            year = ""
        args = SimpleNamespace(klados=klados, region=region_var.get().strip() or None,
                                year=year or None, subcodes=subcodes_var.get())
        panel.run(core.cmd_upgrades, args)

    run_btn.config(command=on_run)
    return frame


# ---------------------------------------------------------------------------
def main_gui():
    root = Tk()
    root.title("Ανάλυση Πινάκων Αναπληρωτών")
    root.geometry("980x720")
    root.minsize(760, 560)
    try:
        ttk.Style().theme_use("vista")
    except Exception:                                          # noqa: BLE001
        pass

    top = ttk.Frame(root, padding=(12, 10, 12, 0))
    top.pack(fill="x")
    ttk.Label(top, text=f"📁 Φάκελος δεδομένων: {core.CFG.DATA_DIR}",
              font=("Segoe UI", 9)).pack(side="left")
    ttk.Button(top, text="📂 Άνοιγμα φακέλου αποτελεσμάτων",
               command=lambda: _open_folder(core.CFG.output_dir())).pack(side="right")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=12)

    nb.add(build_summary_tab(nb), text="Σύνοψη Κλάδου")
    nb.add(build_full_tab(nb), text="Πλήρης Ανάλυση")
    nb.add(build_phase_tab(nb), text="Βάση Φάσης")
    nb.add(build_predict_tab(nb), text="Πρόβλεψη")
    nb.add(build_upgrades_tab(nb), text="Αναβαθμίσεις Μειωμένου")

    root.mainloop()


if __name__ == "__main__":
    main_gui()