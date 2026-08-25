# -*- coding: utf-8 -*-
"""Η λογική των 4 ζητούμενων: στατιστικά κλάδου, αφαίρεση μονίμων, τελευταίος ανά περιοχή."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import config as CFG
from normalize import (classify_orario, klados_regex, norm_code_text, norm_digits,
                       norm_key, region_matches, to_float)

KEY_COL = "_ΚΛΕΙΔΙ"
MORIA_NUM = "_ΜΟΡΙΑ_ΑΡΙΘ"


# ---------------------------------------------------------------------------
# ΚΛΕΙΔΙΑ ΤΑΥΤΟΠΟΙΗΣΗΣ
# ---------------------------------------------------------------------------
def choose_identity(df_a: pd.DataFrame, df_b: pd.DataFrame) -> tuple[str, list[str]]:
    """Διαλέγει το ισχυρότερο κλειδί που υπάρχει ΚΑΙ στα δύο tables."""
    for level, cols in CFG.IDENTITY_LEVELS:
        if all(c in df_a.columns and c in df_b.columns for c in cols):
            return level, cols
    raise ValueError(
        "Δεν βρέθηκαν κοινές στήλες ταυτοποίησης (ΑΦΜ/ΑΜ ή ΕΠΩΝΥΜΟ+ΟΝΟΜΑ). "
        "Τρέξε `inspect` και συμπλήρωσε τα aliases στο config.py."
    )


def add_key(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    if cols in (["ΑΦΜ"], ["ΑΜ"]):
        df[KEY_COL] = df[cols[0]].map(norm_digits)
    else:
        df[KEY_COL] = df[cols].apply(lambda r: "|".join(norm_key(v) for v in r), axis=1)
    df.loc[df[KEY_COL].str.replace("|", "", regex=False).str.strip() == "", KEY_COL] = ""
    return df


def add_moria(df: pd.DataFrame, col: str = "ΜΟΡΙΑ") -> pd.DataFrame:
    df = df.copy()
    df[MORIA_NUM] = df[col].map(to_float) if col in df.columns else float("nan")
    return df


# ---------------------------------------------------------------------------
# 1. ΦΙΛΤΡΟ ΚΛΑΔΟΥ + ΣΤΑΤΙΣΤΙΚΑ
# ---------------------------------------------------------------------------
def filter_klados(df: pd.DataFrame, code: str, include_subcodes: bool = False):
    """
    Επιστρέφει (φιλτραρισμένο_df, τρόπος_φιλτραρίσματος).
    Αν δεν υπάρχει στήλη ΚΛΑΔΟΣ, δεν φιλτράρει (υποθέτει αρχείο ενός κλάδου).
    """
    if not code:
        return df.copy(), "χωρίς φίλτρο (δεν ζητήθηκε κλάδος)"
    rx = klados_regex(code, include_subcodes)
    if "ΚΛΑΔΟΣ" not in df.columns:
        # Οι πίνακες κατάταξης είναι ένα αρχείο ανά κλάδο: ο κωδικός είναι στο όνομα.
        if CFG.PROV_FILE in df.columns:
            mask = df[CFG.PROV_FILE].fillna("").map(
                lambda v: bool(rx.search(norm_code_text(v))))
            if mask.any():
                return df[mask].copy(), f"όνομα αρχείου ~ /{rx.pattern}/"
            return df.iloc[0:0].copy(), (
                f"ΚΑΝΕΝΑ ΑΡΧΕΙΟ για {code} — ούτε στήλη ΚΛΑΔΟΣ ούτε κωδικός στο όνομα")
        return df.copy(), "ΧΩΡΙΣ ΦΙΛΤΡΟ — δεν υπάρχει στήλη ΚΛΑΔΟΣ/ΕΙΔΙΚΟΤΗΤΑ στο αρχείο"
    mask = df["ΚΛΑΔΟΣ"].fillna("").map(lambda v: bool(rx.search(norm_code_text(v))))
    return df[mask].copy(), f"στήλη ΚΛΑΔΟΣ ~ /{rx.pattern}/"


@dataclass
class KladosSummary:
    klados: str
    how: str
    n_total: int
    n_valid_moria: int
    n_bad_moria: int
    top: dict = field(default_factory=dict)
    bottom: dict = field(default_factory=dict)
    ties_top: int = 0
    ties_bottom: int = 0
    first_ranked: dict = field(default_factory=dict)   # κατά επίσημη ΣΕΙΡΑ
    last_ranked: dict = field(default_factory=dict)
    order_note: str = ""


def _person(row: pd.Series) -> dict:
    out = {k: row.get(k, "") for k in
           ("ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΠΕΡΙΟΧΗ", "ΚΛΑΔΟΣ")}
    out["ΜΟΡΙΑ"] = row.get(MORIA_NUM, float("nan"))
    # Αν η γραμμή έχει περάσει από enrich_with_current_moria, κουβαλάει και τη
    # σειρά στον τρέχοντα πίνακα — τη μεταφέρουμε, ώστε να φαίνεται δίπλα στο
    # μόριο του "τελευταίου" σε κάθε αποτέλεσμα.
    out["ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ"] = row.get("ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ", None)
    out["ΠΗΓΗ"] = (f"{row.get(CFG.PROV_FILE, '?')} / {row.get(CFG.PROV_SHEET, '?')} "
                   f"/ γρ.{row.get(CFG.PROV_ROW, '?')}")
    return out


def summarize_klados(df: pd.DataFrame, code: str, how: str) -> KladosSummary:
    d = add_moria(df)
    valid = d.dropna(subset=[MORIA_NUM])
    s = KladosSummary(
        klados=code or "(όλοι)", how=how, n_total=len(d),
        n_valid_moria=len(valid), n_bad_moria=len(d) - len(valid),
    )
    if not valid.empty:
        top_val, bot_val = valid[MORIA_NUM].max(), valid[MORIA_NUM].min()
        s.top = _person(valid[valid[MORIA_NUM] == top_val].iloc[0])
        s.bottom = _person(valid[valid[MORIA_NUM] == bot_val].iloc[0])
        s.ties_top = int((valid[MORIA_NUM] == top_val).sum()) - 1
        s.ties_bottom = int((valid[MORIA_NUM] == bot_val).sum()) - 1

    # Αν ο πίνακας έχει επίσημη σειρά κατάταξης, τη σεβόμαστε: η σειρά ΔΕΝ
    # προκύπτει μόνο από τα μόρια (π.χ. προτάσσονται όσοι έχουν παιδαγωγική επάρκεια).
    if "ΣΕΙΡΑ" in d.columns:
        order = pd.to_numeric(d["ΣΕΙΡΑ"], errors="coerce")
        ok = order.notna()
        if ok.any():
            dd = d[ok].assign(_ord=order[ok])
            s.first_ranked = _person(dd.loc[dd["_ord"].idxmin()])
            s.last_ranked = _person(dd.loc[dd["_ord"].idxmax()])
            if s.bottom and s.last_ranked.get("ΜΟΡΙΑ") != s.bottom.get("ΜΟΡΙΑ"):
                s.order_note = ("η επίσημη σειρά δεν ακολουθεί μόνο τα μόρια "
                                "(π.χ. πρόταξη λόγω παιδαγωγικής επάρκειας)")
    return s


OLD_ORDER = "ΠΑΛΙΑ_ΣΕΙΡΑ"


def rank(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """
    Νέα κατάταξη. Αν ο πίνακας έχει επίσημη σειρά (στήλη Α/Α), τη ΔΙΑΤΗΡΕΙ και
    απλώς ξαναριθμεί όσους έμειναν — γιατί η επίσημη σειρά δεν προκύπτει μόνο
    από τα μόρια (προτάσσονται όσοι έχουν παιδαγωγική επάρκεια).
    Αλλιώς ταξινομεί κατά φθίνοντα μόρια.
    """
    d = add_moria(df)
    if "ΣΕΙΡΑ" in d.columns:
        order = pd.to_numeric(d["ΣΕΙΡΑ"], errors="coerce")
        if order.notna().any():
            d[OLD_ORDER] = order
            d = d.sort_values(OLD_ORDER, na_position="last").reset_index(drop=True)
            d["ΘΕΣΗ"] = range(1, len(d) + 1)
            return d, "διατηρήθηκε η επίσημη σειρά του πίνακα, με νέα αρίθμηση"
    d = d.sort_values(MORIA_NUM, ascending=False, na_position="last").reset_index(drop=True)
    d["ΘΕΣΗ"] = d[MORIA_NUM].rank(method="min", ascending=False).astype("Int64")
    return d, "ταξινόμηση κατά φθίνοντα μόρια"


# ---------------------------------------------------------------------------
# ΑΝΑΛΥΣΗ ΜΕ/ΧΩΡΙΣ ΠΑΙΔΑΓΩΓΙΚΗ ΕΠΑΡΚΕΙΑ
# ---------------------------------------------------------------------------
_YES_VALUES = {"ΝΑΙ", "YES", "TRUE", "1"}


def eparkeia_breakdown(df: pd.DataFrame) -> list[dict]:
    """
    Μέγιστο/ελάχιστο μόριο ξεχωριστά για όσους ΕΧΟΥΝ και όσους ΔΕΝ ΕΧΟΥΝ
    παιδαγωγική/διδακτική επάρκεια — γιατί οι πίνακες του ΑΣΕΠ κατατάσσουν
    πρώτα όλους τους «με επάρκεια» (ανεξαρτήτως μορίων) και μετά τους «χωρίς».
    Επιστρέφει [] αν ο πίνακας δεν έχει καθόλου τη σχετική στήλη.
    """
    if "ΕΠΑΡΚΕΙΑ" not in df.columns:
        return []
    d = add_moria(df)
    d = d.assign(_ΕΠ=d["ΕΠΑΡΚΕΙΑ"].map(lambda v: norm_key(v) in _YES_VALUES))

    out = []
    for label, flag in [("ΜΕ παιδαγωγική/διδακτική επάρκεια", True),
                        ("ΧΩΡΙΣ παιδαγωγική/διδακτική επάρκεια", False)]:
        sub = d[d["_ΕΠ"] == flag]
        row = {"ΟΜΑΔΑ": label, "ΠΛΗΘΟΣ": len(sub), "top": {}, "bottom": {}}
        valid = sub.dropna(subset=[MORIA_NUM])
        if not valid.empty:
            top_val, bot_val = valid[MORIA_NUM].max(), valid[MORIA_NUM].min()
            row["top"] = _person(valid[valid[MORIA_NUM] == top_val].iloc[0])
            row["bottom"] = _person(valid[valid[MORIA_NUM] == bot_val].iloc[0])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 2. ΑΦΑΙΡΕΣΗ ΜΟΝΙΜΩΝ
# ---------------------------------------------------------------------------
@dataclass
class MonimoiResult:
    kept: pd.DataFrame
    removed: pd.DataFrame
    level: str
    key_cols: list
    n_before: int
    n_removed: int
    dup_keys_base: pd.DataFrame
    empty_keys: int
    monimoi_unused: pd.DataFrame          # μόνιμοι που ΔΕΝ βρέθηκαν στον πίνακα
    ambiguous: pd.DataFrame               # ίδιο ονοματεπώνυμο, ασύμβατο πατρώνυμο
    moria_conflicts: pd.DataFrame         # νέα μόρια < παλιά μόρια (ύποπτη ταύτιση)


def _pat_compatible(a: str, b: str) -> bool:
    """
    Τα πατρώνυμα στα αρχεία διορισμών είναι συχνά κομμένα ('ΓΕΩΡΓ' αντί 'ΓΕΩΡΓΙΟΣ').
    Θεωρούμε συμβατά όσα το ένα είναι αρχή του άλλου (>=3 χαρακτήρες).
    """
    a, b = norm_key(a), norm_key(b)
    if not a or not b:
        return True                       # άγνωστο -> δεν το χρησιμοποιούμε για απόρριψη
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long_.startswith(short)


def remove_monimoi(df_base: pd.DataFrame, df_mon: pd.DataFrame) -> MonimoiResult:
    level, cols = choose_identity(df_base, df_mon)
    base = add_key(df_base, cols)
    mon = add_key(df_mon, cols)

    use_pat = (level == "ΟΝΟΜΑΤΕΠΩΝΥΜΟ" or level == "ΠΛΗΡΕΣ") and \
              "ΠΑΤΡΩΝΥΜΟ" in df_base.columns and "ΠΑΤΡΩΝΥΜΟ" in df_mon.columns

    if use_pat:
        # Το κλειδί γίνεται ΕΠΩΝΥΜΟ|ΟΝΟΜΑ και το πατρώνυμο συγκρίνεται με πρόθεμα,
        # γιατί στα αρχεία διορισμών είναι κομμένο.
        level = "ΕΠΩΝΥΜΟ+ΟΝΟΜΑ+ΠΑΤΡΩΝΥΜΟ (πρόθεμα)"
        cols = ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ"]
        base = add_key(df_base, ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ"])
        mon = add_key(df_mon, ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ"])

    mon_pats: dict[str, list] = {}
    for k, pat in zip(mon[KEY_COL], mon.get("ΠΑΤΡΩΝΥΜΟ", pd.Series([""] * len(mon)))):
        if k:
            mon_pats.setdefault(k, []).append(pat)

    hit, amb_rows = [], []
    base_pats = base.get("ΠΑΤΡΩΝΥΜΟ", pd.Series([""] * len(base), index=base.index))
    for idx, k in base[KEY_COL].items():
        cands = mon_pats.get(k) if k else None
        if not cands:
            hit.append(False)
            continue
        if not use_pat:
            hit.append(True)
            continue
        ok = [c for c in cands if _pat_compatible(base_pats.loc[idx], c)]
        hit.append(bool(ok))
        if not ok:
            amb_rows.append({"ΚΛΕΙΔΙ": k,
                             "ΠΑΤΡΩΝΥΜΟ ΠΙΝΑΚΑ": base_pats.loc[idx],
                             "ΠΑΤΡΩΝΥΜΑ ΔΙΟΡΙΣΜΩΝ": ", ".join(map(str, cands)),
                             "ΓΡΑΜΜΗ": base.loc[idx, CFG.PROV_ROW]})
    hit = pd.Series(hit, index=base.index)

    # Οι διπλοεγγραφές μετρώνται με το πλήρες κλειδί (μαζί με πατρώνυμο)
    dup_base = add_key(df_base, cols) if use_pat else base
    dup = (dup_base[dup_base[KEY_COL] != ""]
           .groupby(KEY_COL).size().reset_index(name="ΠΛΗΘΟΣ"))
    dup = dup[dup["ΠΛΗΘΟΣ"] > 1]

    used = set(base.loc[hit, KEY_COL])
    unused = mon[(mon[KEY_COL] != "") & (~mon[KEY_COL].isin(used))]

    # Διασταύρωση μορίων: τα μόρια του νέου πίνακα δεν μπορούν να είναι μικρότερα
    # από τα μόρια του παλιού (η προϋπηρεσία μόνο προστίθεται).
    removed = base[hit].copy()
    conflicts = pd.DataFrame()
    # Λογικός έλεγχος «νέα μόρια ≥ παλιά μόρια» — προαιρετικός στολισμός, ποτέ
    # δεν πρέπει να ρίξει το πρόγραμμα αν τα αρχεία έχουν παράξενη/διπλή στήλη.
    try:
        mon_nodup = mon.loc[:, ~mon.columns.duplicated()]
        old_col = next((c for c in ("ΜΟΡΙΑ_ΠΑΛΙΟΥ_ΠΙΝΑΚΑ", "ΜΟΡΙΑ")
                        if c in mon_nodup.columns), None)
        if old_col and "ΜΟΡΙΑ" in removed.columns and KEY_COL in mon_nodup.columns:
            old = (mon_nodup[[KEY_COL, old_col]].rename(columns={old_col: "_ΠΑΛΙΑ_RAW"})
                   .drop_duplicates(subset=[KEY_COL]))
            removed = removed.merge(old, on=KEY_COL, how="left")
            if "_ΠΑΛΙΑ_RAW" in removed.columns:
                removed["_ΠΑΛΙΑ"] = removed["_ΠΑΛΙΑ_RAW"].map(to_float)
                removed["_ΝΕΑ"] = removed["ΜΟΡΙΑ"].map(to_float)
                removed["_ΔΙΑΦΟΡΑ"] = removed["_ΝΕΑ"] - removed["_ΠΑΛΙΑ"]
                conflicts = removed[removed["_ΔΙΑΦΟΡΑ"] < -0.001]
    except Exception as exc:                                      # noqa: BLE001
        print(f"   (παραλείπεται ο έλεγχος παλιών/νέων μορίων: {exc})")

    return MonimoiResult(
        kept=base[~hit].copy(), removed=removed,
        level=level, key_cols=cols, n_before=len(base), n_removed=int(hit.sum()),
        dup_keys_base=dup, empty_keys=int((base[KEY_COL] == "").sum()),
        monimoi_unused=unused, ambiguous=pd.DataFrame(amb_rows),
        moria_conflicts=conflicts,
    )



# ---------------------------------------------------------------------------
# ΕΝΗΜΕΡΩΣΗ ΜΟΡΙΩΝ ΦΑΣΗΣ ΑΠΟ ΤΟΝ ΤΡΕΧΟΝΤΑ ΒΑΣΙΚΟ ΠΙΝΑΚΑ
# ---------------------------------------------------------------------------
def enrich_with_current_moria(df_phase: pd.DataFrame, df_current: pd.DataFrame) -> pd.DataFrame:
    """
    Οι φάσεις δείχνουν ΠΟΙΟΣ πήγε ΠΟΥ· τα ΜΟΡΙΑ τα εμπιστευόμαστε από τον
    τρέχοντα (ενημερωμένο) βασικό πίνακα, όχι από το ίδιο το αρχείο φάσης
    (που μπορεί να έχει παλιά/διαφορετική στιγμιαία τιμή). Ταυτίζει άτομα με
    ΕΠΩΝΥΜΟ+ΟΝΟΜΑ(+ΠΑΤΡΩΝΥΜΟ ως πρόθεμα) — ίδια ανθεκτική λογική με τους
    μόνιμους. Όπου δεν βρεθεί ταύτιση, κρατά το μόριο του αρχείου φάσης (αν
    υπάρχει) και το σημειώνει ως αταύτιστο, ώστε να φαίνεται στο αποτέλεσμα.
    """
    out = df_phase.copy()
    if df_phase.empty or df_current.empty or "ΕΠΩΝΥΜΟ" not in df_current.columns:
        out["_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ"] = False
        return out

    has_pat = "ΠΑΤΡΩΝΥΜΟ" in df_phase.columns and "ΠΑΤΡΩΝΥΜΟ" in df_current.columns
    has_thesi = "ΘΕΣΗ" in df_current.columns
    key_cols = ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ"]
    a = add_key(df_phase, key_cols)
    b = add_key(df_current, key_cols)
    b = add_moria(b)

    cur_map: dict[str, list] = {}
    for idx, k in b[KEY_COL].items():
        if k:
            cur_map.setdefault(k, []).append(idx)

    # Διαγνωστικό ευρετήριο ΜΟΝΟ με επώνυμο, για όταν αποτυγχάνει η πλήρης ταύτιση:
    # δείχνει αν το άτομο μάλλον λείπει εντελώς από τον πίνακα ή απλώς γράφεται αλλιώς.
    surname_map: dict[str, list] = {}
    if "ΕΠΩΝΥΜΟ" in df_current.columns:
        for idx in df_current.index:
            sk = norm_key(df_current.loc[idx, "ΕΠΩΝΥΜΟ"])
            if sk:
                surname_map.setdefault(sk, []).append(idx)

    new_moria, matched, new_thesi, notes = [], [], [], []
    for idx, row in a.iterrows():
        cands = cur_map.get(row[KEY_COL], [])
        val, ok, thesi = float("nan"), False, None
        if cands:
            good = cands
            if has_pat:
                pat_a = row.get("ΠΑΤΡΩΝΥΜΟ", "")
                good = [i for i in cands if _pat_compatible(pat_a, b.loc[i, "ΠΑΤΡΩΝΥΜΟ"])]
            if good:
                val, ok = b.loc[good[0], MORIA_NUM], True
                if has_thesi:
                    thesi = b.loc[good[0], "ΘΕΣΗ"]
        note = ""
        if not ok:
            val = to_float(row.get("ΜΟΡΙΑ")) if "ΜΟΡΙΑ" in row else float("nan")
            sk = norm_key(row.get("ΕΠΩΝΥΜΟ", ""))
            near = surname_map.get(sk, [])
            if near:
                names = "· ".join(
                    f"{df_current.loc[i, 'ΕΠΩΝΥΜΟ']} {df_current.loc[i, 'ΟΝΟΜΑ']}"
                    for i in near[:3])
                note = f"το επώνυμο υπάρχει στον πίνακα με άλλο όνομα/πατρώνυμο: {names}"
            else:
                note = "το επώνυμο δεν υπάρχει καθόλου στον τρέχοντα πίνακα"
        new_moria.append(val)
        matched.append(ok)
        new_thesi.append(thesi)
        notes.append(note)

    out["ΜΟΡΙΑ"] = new_moria
    out["_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ"] = matched
    out["_ΣΗΜΕΙΩΣΗ_ΤΑΥΤΙΣΗΣ"] = notes
    if has_thesi:
        # Ξεχωριστό όνομα (όχι "ΘΕΣΗ") ώστε να μη συγκρούεται με τυχόν στήλη
        # που ήδη έχει το ίδιο το αρχείο φάσης.
        out["ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ"] = new_thesi
    return out

# ---------------------------------------------------------------------------
# 3. ΤΕΛΕΥΤΑΙΟΣ ΠΟΥ ΠΕΡΑΣΕ ΑΝΑ ΠΕΡΙΟΧΗ ΔΙΟΡΙΣΜΟΥ
# ---------------------------------------------------------------------------
@dataclass
class RegionResult:
    region_query: str
    n_matched: int
    regions_found: list
    last: dict = field(default_factory=dict)
    tail: pd.DataFrame = field(default_factory=pd.DataFrame)
    note: str = ""


def last_in_region(df_hires: pd.DataFrame, region: str, tail_n: int = 5) -> RegionResult:
    """
    Ο «τελευταίος που πέρασε» = αυτός με τα ΧΑΜΗΛΟΤΕΡΑ μόρια ανάμεσα σε όσους
    προσλήφθηκαν/διορίστηκαν στη ζητούμενη περιοχή. Επιστρέφει και τους
    τελευταίους N για οπτικό έλεγχο.
    """
    if "ΠΕΡΙΟΧΗ" not in df_hires.columns:
        return RegionResult(region, 0, [], note="Δεν υπάρχει στήλη ΠΕΡΙΟΧΗ στα αρχεία.")

    d = add_moria(df_hires)
    mask = d["ΠΕΡΙΟΧΗ"].map(lambda v: region_matches(v, region))
    sel = d[mask]
    found = sorted(set(sel["ΠΕΡΙΟΧΗ"].dropna()))
    if sel.empty:
        return RegionResult(region, 0, [], note="Καμία εγγραφή για την περιοχή αυτή.")

    valid = sel.dropna(subset=[MORIA_NUM])
    if valid.empty:
        return RegionResult(region, len(sel), found,
                            note="Βρέθηκαν εγγραφές αλλά χωρίς αναγνώσιμα μόρια.")

    valid = valid.sort_values(MORIA_NUM, ascending=False)
    res = RegionResult(region, len(sel), found, last=_person(valid.iloc[-1]))
    show = ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΠΕΡΙΟΧΗ", MORIA_NUM] + CFG.PROV_COLS
    res.tail = valid[[c for c in show if c in valid.columns]].tail(tail_n)
    if len(sel) != len(valid):
        res.note = f"{len(sel) - len(valid)} εγγραφές αγνοήθηκαν (μη αναγνώσιμα μόρια)."
    return res


def last_in_region_by_orario(df: pd.DataFrame, region: str) -> dict:
    """
    Ίδιο με last_in_region, αλλά χωρισμένο σε ΠΛΗΡΕΣ/ΜΕΙΩΜΕΝΟ ωράριο — έτσι
    η βάση μιας θέσης μειωμένου ωραρίου (συνήθως χαμηλότερη) δεν φαίνεται σαν
    να ήταν η βάση για κανονική/πλήρη θέση. Επιστρέφει dict {"ΠΛΗΡΕΣ": ...,
    "ΜΕΙΩΜΕΝΟ": ...} — μόνο για τους τύπους που έχουν έστω μία εγγραφή.
    """
    if df.empty or "ΠΕΡΙΟΧΗ" not in df.columns:
        return {}
    orario_col = df["ΩΡΑΡΙΟ"] if "ΩΡΑΡΙΟ" in df.columns else [None] * len(df)
    types = [classify_orario(pv, ov) for pv, ov in zip(df["ΠΕΡΙΟΧΗ"], orario_col)]
    d = df.assign(_ΩΡΑΡΙΟ_ΤΥΠΟΣ=types)
    out = {}
    for typ in ("ΠΛΗΡΕΣ", "ΜΕΙΩΜΕΝΟ"):
        sub = d[d["_ΩΡΑΡΙΟ_ΤΥΠΟΣ"] == typ]
        if sub.empty:
            continue
        res = last_in_region(sub, region)
        # ΣΗΜΑΝΤΙΚΟ: μπορεί να υπάρχουν εγγραφές αυτού του τύπου ωραρίου
        # ΑΛΛΟΥ (όχι στη ζητούμενη περιοχή) — τότε res.last μένει κενό {}.
        # Το προσθέτουμε μόνο αν βρέθηκε πραγματικό ταίριασμα περιοχής,
        # αλλιώς μια αδειανή εγγραφή θα έσκαγε παρακάτω σε rr.last["ΜΟΡΙΑ"].
        if res.last:
            out[typ] = res
    return out



def all_regions(df_hires: pd.DataFrame) -> pd.DataFrame:
    """Πίνακας: περιοχή -> πλήθος, μέγιστο, ελάχιστο (βάση) + ποιος είναι ο τελευταίος."""
    if "ΠΕΡΙΟΧΗ" not in df_hires.columns:
        return pd.DataFrame()
    d = add_moria(df_hires).dropna(subset=[MORIA_NUM])
    if d.empty:
        return pd.DataFrame()
    rows = []
    for reg, grp in d.groupby("ΠΕΡΙΟΧΗ"):
        last = grp.loc[grp[MORIA_NUM].idxmin()]
        rows.append({
            "ΠΕΡΙΟΧΗ": reg,
            "ΠΛΗΘΟΣ": len(grp),
            "ΜΕΓΙΣΤΟ": round(grp[MORIA_NUM].max(), 3),
            "ΒΑΣΗ (ΕΛΑΧΙΣΤΟ)": round(grp[MORIA_NUM].min(), 3),
            "ΤΕΛΕΥΤΑΙΟΣ": f"{last.get('ΕΠΩΝΥΜΟ','')} {last.get('ΟΝΟΜΑ','')}".strip(),
            "ΠΗΓΗ": f"{last.get(CFG.PROV_FILE,'?')}/γρ.{last.get(CFG.PROV_ROW,'?')}",
        })
    return pd.DataFrame(rows).sort_values("ΒΑΣΗ (ΕΛΑΧΙΣΤΟ)")