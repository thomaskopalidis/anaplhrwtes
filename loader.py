# -*- coding: utf-8 -*-
"""
Φόρτωση αρχείων Excel/CSV με:
  * αυτόματη ανίχνευση της γραμμής-επικεφαλίδας,
  * χαρτογράφηση στηλών σε σταθερά ονόματα (config.COLUMN_ALIASES),
  * διατήρηση της ΠΡΟΕΛΕΥΣΗΣ κάθε γραμμής (αρχείο / φύλλο / γραμμή Excel).

Η προέλευση είναι το θεμέλιο της επαλήθευσης: κάθε νούμερο που βγάζει το
πρόγραμμα μπορεί να γυρίσει πίσω σε συγκεκριμένο κελί συγκεκριμένου αρχείου.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import re

import config as CFG
from normalize import norm_key, norm_text

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb"}
DATA_SUFFIXES = EXCEL_SUFFIXES | {".csv", ".tsv"}

# Ό,τι παράγει το ίδιο το πρόγραμμα ΔΕΝ πρέπει ποτέ να ξαναδιαβαστεί σαν είσοδος
# (ισχύει σε ΚΑΘΕ σημείο που διαβάζουμε αρχεία, όχι μόνο στο main.py).
OWN_OUTPUT_PREFIXES = ("AUDIT_", "ΠΙΝΑΚΑΣ_", "ΜΟΝΙΜΟΙ_ΚΛΑΔΟΥ_", "ΦΑΣΕΙΣ_")


def is_own_output(path: Path, output_dir: Path | None = None) -> bool:
    """True αν το αρχείο είναι δικό μας αποτέλεσμα (όνομα ή θέση μέσα στο output/)."""
    if path.name.startswith("~$"):
        return True
    if path.name.upper().startswith(tuple(p.upper() for p in OWN_OUTPUT_PREFIXES)):
        return True
    if output_dir is not None:
        try:
            return output_dir.resolve() in path.resolve().parents
        except OSError:
            return False
    return False


@dataclass
class LoadedTable:
    df: pd.DataFrame
    path: Path
    sheet: str
    header_row: int                     # 1-based γραμμή Excel της επικεφαλίδας
    mapping: dict = field(default_factory=dict)   # αρχική στήλη -> κανονική
    unmapped: list = field(default_factory=list)
    sha256: str = ""
    n_rows: int = 0


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _score_header(values) -> tuple[int, dict]:
    """Πόσες από τις τιμές μιας γραμμής μοιάζουν με γνωστές επικεφαλίδες."""
    mapping = _map_columns([norm_text(v) for v in values])
    core = {"ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ"}
    found = set(mapping.values())
    score = len(found)
    if core.issubset(found):
        score += 5
    return score, mapping


def _map_columns(cols) -> dict:
    """Επιστρέφει {αρχική_στήλη: κανονικό_όνομα} για όσες αναγνωρίζονται."""
    mapping = {}
    taken = set()

    # 1) ακριβής αντιστοίχιση
    for canonical, aliases in CFG.COLUMN_ALIASES.items():
        if canonical in taken:
            continue
        alias_set = {norm_key(a) for a in aliases}
        for col in cols:
            if col in mapping:
                continue
            if norm_key(col) in alias_set:
                mapping[col] = canonical
                taken.add(canonical)
                break

    # 2) χαλαροί κανόνες
    for canonical, musts, forbid in CFG.CONTAINS_RULES:
        if canonical in taken:
            continue
        for col in cols:
            if col in mapping:
                continue
            c = norm_text(col).replace(" ", "")
            if all(m in c for m in musts) and not any(f in c for f in forbid):
                mapping[col] = canonical
                taken.add(canonical)
                break
    return mapping


def _legend_map(df0) -> dict:
    """
    Οι πίνακες του ΑΣΕΠ έχουν στήλες με ονόματα '1'…'35' και από κάτω από τα
    δεδομένα ένα υπόμνημα τύπου '4:ΑΡΙΘΜΟΣ ΑΥΤΟΤΕΛΩΝ ΜΕΤΑΠΤΥΧΙΑΚΩΝ ΤΙΤΛΩΝ'.
    Το διαβάζουμε ώστε οι στήλες να πάρουν το πραγματικό τους όνομα.
    """
    out = {}
    col0 = df0.iloc[:, 0].map(lambda v: "" if v is None else str(v))
    for val in col0:
        m = re.fullmatch(r"\s*(\d{1,3})\s*:\s*(.+?)\s*", val)
        if m:
            out[m.group(1)] = norm_text(m.group(2))
    return out


def _dedupe(cols: list[str]) -> list[str]:
    seen, out = {}, []
    for c in cols:
        c = c if c else "ΑΝΩΝΥΜΗ"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def load_one(path: Path, sheet=None) -> list[LoadedTable]:
    """Διαβάζει ΟΛΑ τα φύλλα ενός αρχείου (ή ένα συγκεκριμένο)."""
    path = Path(path)
    out: list[LoadedTable] = []
    digest = file_sha256(path)

    if path.suffix.lower() in (".csv", ".tsv"):
        sep = "\t" if path.suffix.lower() == ".tsv" else None
        raw = {"CSV": pd.read_csv(path, header=None, dtype=str, sep=sep, engine="python")}
    else:
        try:
            book = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
        except Exception as exc:                      # noqa: BLE001
            print(f"   [ΣΦΑΛΜΑ ΑΝΑΓΝΩΣΗΣ] {path.name}: {exc}")
            return out
        raw = book if isinstance(book, dict) else {str(sheet or 0): book}

    for sheet_name, df0 in raw.items():
        if df0 is None or df0.empty:
            continue

        best_score, best_idx, best_map = -1, None, {}
        for i in range(min(CFG.MAX_HEADER_SCAN, len(df0))):
            score, mapping = _score_header(df0.iloc[i].tolist())
            if score > best_score:
                best_score, best_idx, best_map = score, i, mapping
        if best_idx is None or best_score < 2:
            continue

        header_vals = [norm_text(v) for v in df0.iloc[best_idx].tolist()]
        df = df0.iloc[best_idx + 1:].copy()
        df.columns = _dedupe(header_vals)

        # Η γραμμή Excel μπαίνει ΠΡΙΝ από κάθε φιλτράρισμα, αλλιώς χαλάει η αρίθμηση
        df[CFG.PROV_FILE] = path.name
        df[CFG.PROV_SHEET] = str(sheet_name)
        df[CFG.PROV_ROW] = [i + 1 for i in df.index]

        # Υπόμνημα κριτηρίων -> πραγματικά ονόματα στηλών
        legend = _legend_map(df0)
        if legend:
            df = df.rename(columns={c: legend[c] for c in df.columns if c in legend})
            df.columns = _dedupe(list(df.columns))

        mapping = _map_columns(list(df.columns))
        df = df.rename(columns=mapping)

        # Κόβει ό,τι δεν είναι εγγραφή υποψηφίου: κενές γραμμές και το υπόμνημα
        # κριτηρίων που οι πίνακες του ΑΣΕΠ έχουν κάτω από τα δεδομένα.
        df = df.dropna(how="all")
        if "ΕΠΩΝΥΜΟ" in df.columns:
            df = df[df["ΕΠΩΝΥΜΟ"].map(norm_text) != ""]
        df = df.reset_index(drop=True)
        if df.empty:
            continue
        df = df.loc[:, ~df.columns.duplicated()]

        # Κανονικοποίηση τιμών (συμβατό με pandas 1.x/2.x/3.x — στην 3.x το dtype
        # των κειμένων είναι 'str' και όχι 'object')
        for col in df.columns:
            if col in CFG.PROV_COLS:
                continue
            if not pd.api.types.is_numeric_dtype(df[col]) and \
               not pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].map(norm_text)

        out.append(LoadedTable(
            df=df, path=path, sheet=str(sheet_name), header_row=best_idx + 1,
            mapping=mapping, sha256=digest, n_rows=len(df),
            unmapped=[c for c in df.columns
                      if c not in CFG.COLUMN_ALIASES and c not in CFG.PROV_COLS],
        ))
    return out


def load_path(path) -> list[LoadedTable]:
    """Δέχεται αρχείο Ή φάκελο (αναδρομικά). Αγνοεί ~$ και τα δικά μας outputs."""
    p = Path(path)
    out_dir = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
    if p.is_file():
        return [] if is_own_output(p, out_dir) else load_one(p)
    tables: list[LoadedTable] = []
    for f in sorted(p.rglob("*")):
        if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir):
            tables.extend(load_one(f))
    return tables


def concat(tables: list[LoadedTable]) -> pd.DataFrame:
    if not tables:
        return pd.DataFrame()
    return pd.concat([t.df for t in tables], ignore_index=True)