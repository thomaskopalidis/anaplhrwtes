# -*- coding: utf-8 -*-
"""
ΖΗΤΟΥΜΕΝΟ 4: «πώς ξέρω ότι αυτά που μου δίνεις είναι σωστά;»

Δύο μηχανισμοί:

A) ΑΥΤΟΜΑΤΟΙ ΕΛΕΓΧΟΙ (checks) — λογικές συνθήκες που ΠΡΕΠΕΙ να ισχύουν.
   Αν σπάσει έστω μία, το πρόγραμμα το φωνάζει αντί να βγάλει ωραίο αλλά λάθος νούμερο.

B) ΦΑΚΕΛΟΣ ΑΠΟΔΕΙΞΕΩΝ (audit workbook) — κάθε νούμερο συνοδεύεται από τις
   γραμμές των πρωτότυπων αρχείων που το παρήγαγαν, ώστε να το ελέγξεις με το χέρι.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import config as CFG
from pipeline import MORIA_NUM


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    @property
    def icon(self) -> str:
        return "✅" if self.ok else "❌"


def run_checks(base_raw, base_klados, mon_res, ranked, region_res=None) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    # 1. Ισοζύγιο πλήθους: αρχικοί = όσοι έμειναν + όσοι αφαιρέθηκαν
    if mon_res is not None:
        ok = mon_res.n_before == len(mon_res.kept) + len(mon_res.removed)
        add(Check("Ισοζύγιο πλήθους μετά την αφαίρεση μονίμων",
                  ok, f"{mon_res.n_before} = {len(mon_res.kept)} + {len(mon_res.removed)}"))

    # 2. Καμία γραμμή δεν χάθηκε/διπλασιάστηκε στο φιλτράρισμα κλάδου
    add(Check("Το φίλτρο κλάδου δεν δημιούργησε γραμμές",
              len(base_klados) <= len(base_raw),
              f"σύνολο αρχείου {len(base_raw)} → κλάδος {len(base_klados)}"))

    # 3. Διπλοεγγραφές ίδιου προσώπου στον βασικό πίνακα
    if mon_res is not None:
        n_dup = len(mon_res.dup_keys_base)
        add(Check("Μοναδικότητα υποψηφίων στον βασικό πίνακα",
                  n_dup == 0,
                  "καμία διπλοεγγραφή" if n_dup == 0
                  else f"{n_dup} κλειδιά εμφανίζονται >1 φορά — δες φύλλο ΔΙΠΛΟΕΓΓΡΑΦΕΣ"))

        add(Check("Κενά κλειδιά ταυτοποίησης", mon_res.empty_keys == 0,
                  f"{mon_res.empty_keys} γραμμές χωρίς όνομα/ΑΦΜ"))

        add(Check("Ισχύς κλειδιού διασταύρωσης",
                  mon_res.level.startswith(("ΑΦΜ", "ΑΔΤ", "ΑΜ", "ΠΛΗΡΕΣ", "ΕΠΩΝΥΜΟ+ΟΝΟΜΑ+ΠΑΤΡ")),
                  f"επίπεδο: {mon_res.level} ({'+'.join(mon_res.key_cols)})"
                  + ("" if mon_res.level != "ΟΝΟΜΑΤΕΠΩΝΥΜΟ"
                     else " — ΠΡΟΣΟΧΗ: χωρίς πατρώνυμο υπάρχει κίνδυνος συνωνυμίας")))

    # 4. Ποσοστό μη αναγνώσιμων μορίων
    if MORIA_NUM in ranked.columns and len(ranked):
        bad = int(ranked[MORIA_NUM].isna().sum())
        pct = 100 * bad / len(ranked)
        add(Check("Ανάγνωση στήλης μορίων", pct < 1.0,
                  f"{bad}/{len(ranked)} γραμμές ({pct:.1f}%) χωρίς αριθμητικά μόρια"))

    # 5. Η κατάταξη είναι συνεπής
    if "ΠΑΛΙΑ_ΣΕΙΡΑ" in ranked.columns:
        old = ranked["ΠΑΛΙΑ_ΣΕΙΡΑ"].dropna().tolist()
        pos = ranked["ΘΕΣΗ"].tolist()
        add(Check("Η νέα αρίθμηση σέβεται την επίσημη σειρά",
                  all(a < b for a, b in zip(old, old[1:]))
                  and pos == list(range(1, len(pos) + 1)),
                  f"θέσεις 1…{len(pos)} με αύξουσα αρχική σειρά"))
    elif MORIA_NUM in ranked.columns:
        vals = ranked[MORIA_NUM].dropna().tolist()
        add(Check("Η κατάταξη είναι φθίνουσα",
                  all(a >= b for a, b in zip(vals, vals[1:])),
                  f"{len(vals)} έγκυρες τιμές"))

    # 6α. Ασύμβατα πατρώνυμα σε ίδιο ονοματεπώνυμο
    if mon_res is not None and len(getattr(mon_res, "ambiguous", [])) :
        n = len(mon_res.ambiguous)
        add(Check("Συμφωνία πατρωνύμου στις ταυτίσεις", False,
                  f"{n} περιπτώσεις ίδιου ονοματεπωνύμου με ασύμβατο πατρώνυμο — "
                  f"ΔΕΝ αφαιρέθηκαν, δες φύλλο ΑΜΦΙΒΟΛΕΣ_ΤΑΥΤΙΣΕΙΣ"))
    elif mon_res is not None:
        add(Check("Συμφωνία πατρωνύμου στις ταυτίσεις", True, "καμία ασυμφωνία"))

    # 6β. Λογικός έλεγχος μορίων: νέα ≥ παλιά
    if mon_res is not None and getattr(mon_res, "moria_conflicts", None) is not None:
        n = len(mon_res.moria_conflicts)
        add(Check("Νέα μόρια ≥ μόρια παλιού πίνακα", n == 0,
                  "συνεπές" if n == 0 else
                  f"{n} διορισμένοι έχουν ΜΙΚΡΟΤΕΡΑ μόρια στον νέο πίνακα — "
                  f"πιθανή λάθος ταύτιση, δες φύλλο ΣΥΓΚΡΟΥΣΕΙΣ_ΜΟΡΙΩΝ"))

    # 6. Οι μόνιμοι που δηλώθηκαν βρέθηκαν όντως στον πίνακα
    if mon_res is not None:
        n_un = len(mon_res.monimoi_unused)
        add(Check("Αντιστοίχιση αρχείου μονίμων", n_un == 0,
                  "όλοι οι μόνιμοι εντοπίστηκαν στον πίνακα" if n_un == 0
                  else f"{n_un} μόνιμοι ΔΕΝ βρέθηκαν (άλλος κλάδος ή διαφορετική γραφή "
                       f"ονόματος) — δες φύλλο ΜΟΝΙΜΟΙ_ΑΤΑΙΡΙΑΣΤΟΙ"))

    # 7. Η βάση περιοχής ανήκει στο εύρος του πίνακα
    if region_res is not None and region_res.last:
        m = region_res.last.get("ΜΟΡΙΑ")
        lo = ranked[MORIA_NUM].min() if len(ranked) else None
        hi = ranked[MORIA_NUM].max() if len(ranked) else None
        ok = m is not None and lo is not None and lo <= m <= hi
        add(Check("Η βάση της περιοχής είναι εντός εύρους πίνακα", bool(ok),
                  f"{m} ∈ [{lo}, {hi}]"))

    return checks


def print_checks(checks: list[Check]) -> bool:
    print("\n🔍 ΑΥΤΟΜΑΤΟΙ ΕΛΕΓΧΟΙ ΕΓΚΥΡΟΤΗΤΑΣ")
    print("-" * 78)
    for c in checks:
        print(f" {c.icon} {c.name}\n      → {c.detail}")
    failed = [c for c in checks if not c.ok]
    print("-" * 78)
    print(f" Σύνολο: {len(checks) - len(failed)}/{len(checks)} πέρασαν"
          + ("" if not failed else f"  ⚠️  {len(failed)} προειδοποιήσεις"))
    return not failed


def _sample(df: pd.DataFrame, n=20, seed=0) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [c for c in ["ΘΕΣΗ", "ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΜΟΡΙΑ",
                        MORIA_NUM, "ΠΕΡΙΟΧΗ"] + CFG.PROV_COLS if c in df.columns]
    return df.sample(min(n, len(df)), random_state=seed)[cols]


def write_audit(path: Path, *, tables, checks, summary_rows,
                base_klados, ranked, mon_res=None, region_res=None,
                regions_table=None, eparkeia_after=None) -> Path:
    """Γράφει το βιβλίο αποδείξεων."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    files_df = pd.DataFrame([{
        "ΑΡΧΕΙΟ": t.path.name,
        "ΔΙΑΔΡΟΜΗ": str(t.path),
        "ΦΥΛΛΟ": t.sheet,
        "SHA256(16)": t.sha256,
        "ΓΡΑΜΜΗ ΕΠΙΚΕΦΑΛΙΔΑΣ": t.header_row,
        "ΓΡΑΜΜΕΣ ΔΕΔΟΜΕΝΩΝ": t.n_rows,
        "ΣΤΗΛΕΣ ΠΟΥ ΑΝΑΓΝΩΡΙΣΤΗΚΑΝ": ", ".join(f"{k}→{v}" for k, v in t.mapping.items()),
    } for t in tables])

    checks_df = pd.DataFrame([{"ΕΛΕΓΧΟΣ": c.name,
                               "ΑΠΟΤΕΛΕΣΜΑ": "OK" if c.ok else "ΠΡΟΣΟΧΗ",
                               "ΛΕΠΤΟΜΕΡΕΙΑ": c.detail} for c in checks])

    sheets = {
        "ΣΥΝΟΨΗ": pd.DataFrame(summary_rows),
        "ΕΛΕΓΧΟΙ": checks_df,
        "ΑΡΧΕΙΑ_ΠΡΟΕΛΕΥΣΗΣ": files_df,
        "ΔΕΙΓΜΑ_ΓΙΑ_ΧΕΙΡΟΚΙΝΗΤΟ_ΕΛΕΓΧΟ": _sample(ranked),
        "ΤΕΛΙΚΟΣ_ΠΙΝΑΚΑΣ": ranked,
    }
    if mon_res is not None:
        sheets["ΑΦΑΙΡΕΘΕΝΤΕΣ_ΜΟΝΙΜΟΙ"] = mon_res.removed
        sheets["ΔΙΠΛΟΕΓΓΡΑΦΕΣ"] = mon_res.dup_keys_base
        sheets["ΜΟΝΙΜΟΙ_ΑΤΑΙΡΙΑΣΤΟΙ"] = mon_res.monimoi_unused
        if len(getattr(mon_res, "ambiguous", [])):
            sheets["ΑΜΦΙΒΟΛΕΣ_ΤΑΥΤΙΣΕΙΣ"] = mon_res.ambiguous
        if getattr(mon_res, "moria_conflicts", None) is not None and len(mon_res.moria_conflicts):
            sheets["ΣΥΓΚΡΟΥΣΕΙΣ_ΜΟΡΙΩΝ"] = mon_res.moria_conflicts
    if region_res is not None and not region_res.tail.empty:
        sheets["ΟΥΡΑ_ΠΕΡΙΟΧΗΣ"] = region_res.tail
    if regions_table is not None and not regions_table.empty:
        sheets["ΒΑΣΕΙΣ_ΑΝΑ_ΠΕΡΙΟΧΗ"] = regions_table
    if eparkeia_after:
        rows = []
        for g in eparkeia_after:
            for tag, p in (("ΜΕΓΙΣΤΟ", g["top"]), ("ΕΛΑΧΙΣΤΟ", g["bottom"])):
                rows.append({
                    "ΟΜΑΔΑ": g["ΟΜΑΔΑ"], "ΠΛΗΘΟΣ ΟΜΑΔΑΣ": g["ΠΛΗΘΟΣ"], "ΤΥΠΟΣ": tag,
                    "ΜΟΡΙΑ": p.get("ΜΟΡΙΑ"), "ΕΠΩΝΥΜΟ": p.get("ΕΠΩΝΥΜΟ"),
                    "ΟΝΟΜΑ": p.get("ΟΝΟΜΑ"), "ΠΑΤΡΩΝΥΜΟ": p.get("ΠΑΤΡΩΝΥΜΟ"),
                    "ΠΗΓΗ": p.get("ΠΗΓΗ"),
                })
        sheets["ΑΝΑ_ΕΠΑΡΚΕΙΑ"] = pd.DataFrame(rows)

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, df in sheets.items():
            (df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)) \
                .to_excel(xl, sheet_name=name[:31], index=False)
    return path