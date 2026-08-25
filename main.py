# -*- coding: utf-8 -*-
"""
ΣΥΣΤΗΜΑ ΑΝΑΛΥΣΗΣ ΠΙΝΑΚΩΝ ΑΝΑΠΛΗΡΩΤΩΝ

Ο φάκελος δεδομένων ορίζεται στο config.py (DATA_DIR) 

Εντολές:
  python main.py inspect
  python main.py summary --klados ΠΕ01
  python main.py full --klados ΠΕ01 --region ΧΑΝΙΩΝ
  python main.py phase --klados ΠΕ01 --region ΧΑΝΙΩΝ
  python main.py predict --klados ΠΕ01 --region ΧΑΝΙΩΝ --moria 50
  python main.py upgrades --klados ΠΕ01 [--region ΧΑΝΙΩΝ] [--year 2025-2026]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

import config as CFG
from audit import print_checks, run_checks, write_audit
from loader import concat, load_path
from normalize import (classify_orario, extract_klados_codes, extract_phase_year,
                       klados_regex, norm_code_text, norm_key, norm_text, region_matches)
from normalize import to_float
from pipeline import (MORIA_NUM, add_moria, all_regions, enrich_with_current_moria,
                      eparkeia_breakdown, filter_klados, last_in_region,
                      last_in_region_by_orario, rank, remove_monimoi, summarize_klados)

OUT = CFG.output_dir()


def _files_of(path, kind: str):
    """
    Ξεχωρίζει τα αρχεία διορισμών/μονίμων από τους πίνακες κατάταξης όταν όλα
    βρίσκονται στον ίδιο φάκελο (με βάση το όνομα αρχείου).
    """
    p = Path(path)
    if p.is_file():
        return [p]
    from loader import DATA_SUFFIXES, is_own_output
    out_dir = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
    faseis_dir = CFG.faseis_dir()

    def in_faseis(f: Path) -> bool:
        # Ο φάκελος φάσεων είναι ξεχωριστή κατηγορία δεδομένων — δεν πρέπει να
        # μπερδεύεται με τον τρέχοντα πίνακα κατάταξης ή τους μόνιμους,
        # ειδικά όταν δεν υπάρχει δικός τους υποφάκελος 'pinakes'/'monimoi'
        # (οπότε η αναζήτηση πέφτει στον κεντρικό φάκελο και θα τον έβλεπε).
        try:
            return faseis_dir.resolve() in f.resolve().parents
        except OSError:
            return False

    files = [f for f in sorted(p.rglob("*"))
             if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)
             and not in_faseis(f)]

    def is_mon(f):
        up = f.name.upper()
        return any(h in up for h in CFG.MONIMOI_FILENAME_HINTS)
    return [f for f in files if is_mon(f)] if kind == "monimoi" \
        else [f for f in files if not is_mon(f)]


def _load_group(path, kind):
    tables = []
    for f in _files_of(path, kind):
        tables.extend(load_path(f))
    return tables


def _line(char="─", n=78):
    print(char * n)


def _fmt_person(p: dict) -> str:
    name = f"{p.get('ΕΠΩΝΥΜΟ','')} {p.get('ΟΝΟΜΑ','')}".strip()
    pat = p.get("ΠΑΤΡΩΝΥΜΟ") or ""
    reg = p.get("ΠΕΡΙΟΧΗ") or ""
    extra = " ".join(x for x in [f"του {pat}" if pat else "", f"[{reg}]" if reg else ""] if x)
    return f"{name} {extra}".strip()


# Ζητούμενο: προαιρετικό αρχείο data/faseis_dates.json με τις πραγματικές
# ημερομηνίες ανάληψης υπηρεσίας ανά φάση/σχολικό έτος (π.χ. "Α φάση 2024-2025:
# 5-6 Σεπτεμβρίου 2024"). Αν υπάρχει, οι εντολές phase/predict/upgrades δείχνουν
# αυτές τις ημερομηνίες δίπλα σε κάθε ετικέτα φάσης. Αν δεν υπάρχει το αρχείο
# (ή λείπει κάποιο έτος/φάση μέσα του), απλά δεν εμφανίζεται τίποτα παραπάνω —
# καμία επίδραση στην υπόλοιπη λειτουργία.
_FASEIS_DATES_CACHE: dict | None = None


def _load_faseis_dates() -> dict:
    global _FASEIS_DATES_CACHE
    if _FASEIS_DATES_CACHE is not None:
        return _FASEIS_DATES_CACHE
    path = CFG.DATA_DIR / "faseis_dates.json"
    data = {}
    if path.exists():
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                                           # noqa: BLE001
            data = {}
    _FASEIS_DATES_CACHE = data
    return data


def _phase_date_range(year: str | None, phase: str | None) -> str:
    """Επιστρέφει π.χ. '5-6/9/2024' αν βρεθεί στο faseis_dates.json, αλλιώς κενό."""
    if not year or not phase:
        return ""
    info = _load_faseis_dates().get(year, {}).get(phase)
    if not info:
        return ""
    frm, to = info.get("από"), info.get("έως")
    if not frm or not to:
        return ""
    try:
        import datetime
        d1 = datetime.date.fromisoformat(frm)
        d2 = datetime.date.fromisoformat(to)
    except ValueError:
        return ""
    if d1.year == d2.year and d1.month == d2.month:
        return f"{d1.day}-{d2.day}/{d2.month}/{d2.year}"
    if d1.year == d2.year:
        return f"{d1.day}/{d1.month}-{d2.day}/{d2.month}/{d2.year}"
    return f"{d1.day}/{d1.month}/{d1.year}-{d2.day}/{d2.month}/{d2.year}"


def _phase_moria_average() -> dict:
    """Μέσος όρος μορίων ανά φάση, σε όλα τα έτη του faseis_dates.json που έχουν
    καταχωρημένη τιμή 'μόρια'. Επιστρέφει {} αν δεν υπάρχουν καθόλου δεδομένα.
    Γενική/ενδεικτική τιμή — ΔΕΝ είναι ανά κλάδο ή περιοχή."""
    dates = _load_faseis_dates()
    sums: dict = {}
    for year, phases in dates.items():
        if year.startswith("_") or not isinstance(phases, dict):
            continue
        for ph, info in phases.items():
            if not isinstance(info, dict) or info.get("μόρια") is None:
                continue
            try:
                sums.setdefault(ph, []).append(float(info["μόρια"]))
            except (TypeError, ValueError):
                continue
    order = ["Α", "Β", "Γ", "Δ", "Ε", "ΣΤ", "Ζ", "Η"]
    return {ph: sum(sums[ph]) / len(sums[ph]) for ph in order if sums.get(ph)}


# ---------------------------------------------------------------------------
def cmd_inspect(args):
    """Δείχνει τι ΑΚΡΙΒΩΣ διαβάζει το πρόγραμμα από κάθε αρχείο."""
    tables = load_path(args.path)
    if not tables:
        print(f"Δεν βρέθηκαν αρχεία δεδομένων στο '{args.path}'.")
        return 1
    for t in tables:
        _line()
        print(f"📄 {t.path}  [φύλλο: {t.sheet}]")
        print(f"   Επικεφαλίδα στη γραμμή {t.header_row} · {t.n_rows} γραμμές δεδομένων"
              f" · sha256:{t.sha256}")
        print("   Αναγνωρίστηκαν:")
        for orig, canon in t.mapping.items():
            print(f"      {orig!r:<38} → {canon}")
        missing = [c for c in ("ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΜΟΡΙΑ", "ΚΛΑΔΟΣ", "ΠΕΡΙΟΧΗ")
                   if c not in t.mapping.values()]
        if missing:
            print(f"   ⚠️  ΔΕΝ βρέθηκαν: {', '.join(missing)}")
        if t.unmapped:
            print(f"   (αχαρτογράφητες στήλες: {', '.join(map(str, t.unmapped[:12]))})")
        if "ΚΛΑΔΟΣ" in t.df.columns:
            vals = sorted(set(t.df['ΚΛΑΔΟΣ'].dropna()))[:15]
            print(f"   Κλάδοι στο αρχείο: {', '.join(map(str, vals))}")
        if "ΠΕΡΙΟΧΗ" in t.df.columns:
            vals = sorted(set(t.df['ΠΕΡΙΟΧΗ'].dropna()))[:15]
            print(f"   Περιοχές (δείγμα): {', '.join(map(str, vals))}")
    _line()
    print(f"Σύνολο: {len(tables)} πίνακες, {sum(t.n_rows for t in tables)} γραμμές.")
    return 0


# ---------------------------------------------------------------------------
def _load_base(args):
    files = _files_of(args.base, "base")
    code = getattr(args, "klados", None)
    if code and len(files) > 1:
        rx = klados_regex(code, getattr(args, "subcodes", False))
        hit = [f for f in files if rx.search(norm_code_text(f.stem))]
        if len(hit) > 1:
            print(f"\n⚠️  {len(hit)} αρχεία ταιριάζουν στον κλάδο {code}:")
            for f in hit:
                print(f"     • {f.name}")
            print("   Δώσε πιο συγκεκριμένο κωδικό (π.χ. ΠΕ04.01) ή --base <αρχείο>.")
            sys.exit(1)
        if hit:
            files = hit
    tables = []
    for f in files:
        tables.extend(load_path(f))
    if not tables:
        print(f"❌ Δεν βρέθηκε τίποτα στο '{args.base}'.")
        sys.exit(1)
    return tables, concat(tables)


def _print_summary(s):
    print(f"\n📊 ΚΛΑΔΟΣ {s.klados}")
    print(f"   Φιλτράρισμα: {s.how}")
    print(f"   • Πλήθος υποψηφίων        : {s.n_total}")
    print(f"   • Με αναγνώσιμα μόρια     : {s.n_valid_moria}"
          + (f"   ⚠️ {s.n_bad_moria} χωρίς" if s.n_bad_moria else ""))
    if s.first_ranked:
        print(f"   • 1ος του πίνακα  : {s.first_ranked['ΜΟΡΙΑ']:.3f} μόρια — "
              f"{_fmt_person(s.first_ranked)}")
        print(f"   • Τελευταίος πίν. : {s.last_ranked['ΜΟΡΙΑ']:.3f} μόρια — "
              f"{_fmt_person(s.last_ranked)}")
        if s.order_note:
            print(f"     ⚠️  {s.order_note}")
    if s.top:
        print(f"   • Μέγιστα μόρια   : {s.top['ΜΟΡΙΑ']:.3f} μόρια — {_fmt_person(s.top)}"
              + (f"  (+{s.ties_top} ισοβαθμία)" if s.ties_top else ""))
        print(f"       ↳ πηγή: {s.top['ΠΗΓΗ']}")
        print(f"   • Ελάχιστα μόρια  : {s.bottom['ΜΟΡΙΑ']:.3f} μόρια — {_fmt_person(s.bottom)}"
              + (f"  (+{s.ties_bottom} ισοβαθμία)" if s.ties_bottom else ""))
        print(f"       ↳ πηγή: {s.bottom['ΠΗΓΗ']}")


def _print_eparkeia(groups, title="ΚΑΤΑ ΠΑΙΔΑΓΩΓΙΚΗ/ΔΙΔΑΚΤΙΚΗ ΕΠΑΡΚΕΙΑ"):
    if not groups:
        return
    print(f"\n🎓 {title}:")
    for g in groups:
        print(f"   • {g['ΟΜΑΔΑ']}  ({g['ΠΛΗΘΟΣ']} άτομα)")
        if g["top"]:
            print(f"       Μέγιστο : {g['top']['ΜΟΡΙΑ']:.3f} — {_fmt_person(g['top'])}")
            print(f"       Ελάχιστο: {g['bottom']['ΜΟΡΙΑ']:.3f} — {_fmt_person(g['bottom'])}")
        else:
            print("       (καμία εγγραφή με αναγνώσιμα μόρια)")


def cmd_summary(args):
    tables, df = _load_base(args)
    dfk, how = filter_klados(df, args.klados, args.subcodes)
    _print_summary(summarize_klados(dfk, args.klados, how))
    _print_eparkeia(eparkeia_breakdown(dfk))
    return 0


# ---------------------------------------------------------------------------
def cmd_full(args):
    tables, df_all = _load_base(args)
    print("=" * 78)
    print(" ΑΝΑΛΥΣΗ ΠΙΝΑΚΑ ΑΝΑΠΛΗΡΩΤΩΝ")
    print("=" * 78)
    print(f"Φορτώθηκαν {len(tables)} πίνακες / {len(df_all)} γραμμές από '{args.base}'.")

    # --- 1. Κλάδος
    df_k, how = filter_klados(df_all, args.klados, args.subcodes)
    tag = (args.klados or "ΟΛΟΙ").replace(".", "_")
    s_before = summarize_klados(df_k, args.klados, how)
    _print_summary(s_before)
    eparkeia_before = eparkeia_breakdown(df_k)
    _print_eparkeia(eparkeia_before)
    if df_k.empty:
        print("\n❌ Ο κλάδος δεν βρέθηκε. Τρέξε `inspect` για να δεις τι κωδικοί υπάρχουν.")
        return 1

    # --- 2. Αφαίρεση μονίμων
    mon_res = None
    df_work = df_k
    if args.monimoi:
        mon_tables = _load_group(args.monimoi, "monimoi")
        if not mon_tables:
            print(f"\n⚠️  Δεν βρέθηκε αρχείο διορισμών/μονίμων στο '{args.monimoi}'.")
            manual = input("   Δώσε τη διαδρομή του αρχείου (Enter = παράλειψη): ").strip().strip('"')
            if manual and Path(manual).exists():
                args.monimoi = manual
                mon_tables = _load_group(manual, "monimoi")
            if not mon_tables:
                print("   → Συνεχίζω ΧΩΡΙΣ αφαίρεση μονίμων.")
                args.monimoi = None
    if args.monimoi:
        tables = tables + mon_tables
        df_mon = concat(mon_tables)
        if args.klados and "ΚΛΑΔΟΣ" in df_mon.columns:
            df_mon, mhow = filter_klados(df_mon, args.klados, args.subcodes)
        else:
            mhow = "χωρίς φίλτρο κλάδου"

        # Ζητούμενο: αποθήκευση των μονίμων ΤΟΥ ΣΥΓΚΕΚΡΙΜΕΝΟΥ κλάδου, ξεχωριστά.
        if not df_mon.empty:
            mon_cols = [c for c in ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΚΛΑΔΟΣ", "ΠΕΡΙΟΧΗ",
                                    "ΜΟΡΙΑ_ΠΑΛΙΟΥ_ΠΙΝΑΚΑ", "ΜΟΡΙΑ"] + CFG.PROV_COLS
                        if c in df_mon.columns]
            mon_out = OUT / f"ΜΟΝΙΜΟΙ_ΚΛΑΔΟΥ_{(args.klados or 'ΟΛΟΙ').replace('.', '_')}.xlsx"
            df_mon[mon_cols].to_excel(mon_out, index=False)
            print(f"💾 Πίνακας μονίμων του κλάδου ({len(df_mon)} άτομα): {mon_out}")
        try:
            mon_res = remove_monimoi(df_k, df_mon)
        except Exception as exc:                                   # noqa: BLE001
            print(f"\n⚠️  Η αφαίρεση μονίμων απέτυχε ({exc}).")
            print("   → Συνεχίζω ΧΩΡΙΣ αφαίρεση μονίμων. Έλεγξε το αρχείο με `inspect`.")
            mon_res = None
        df_work = mon_res.kept if mon_res is not None else df_k
        pct = 100 * mon_res.n_removed / mon_res.n_before if mon_res.n_before else 0
        print(f"\n🏛️  ΑΦΑΙΡΕΣΗ ΜΟΝΙΜΩΝ  (αρχείο: {args.monimoi} · {mhow})")
        print(f"   • Κλειδί διασταύρωσης : {mon_res.level} ({'+'.join(mon_res.key_cols)})")
        print(f"   • Μόνιμοι στο αρχείο  : {len(df_mon)}")
        print(f"   • Αφαιρέθηκαν         : {mon_res.n_removed} ({pct:.1f}% του κλάδου)")
        print(f"   • Παραμένουν          : {len(df_work)}")
        if len(mon_res.monimoi_unused):
            print(f"   ⚠️  {len(mon_res.monimoi_unused)} μόνιμοι δεν αντιστοιχήθηκαν "
                  f"(δες φύλλο ΜΟΝΙΜΟΙ_ΑΤΑΙΡΙΑΣΤΟΙ στο audit)")

        # Ζητούμενο: αναλυτική λίστα των μονίμων που αφαιρέθηκαν — όνομα, μόρια,
        # και ΘΕΣΗ (κατάταξη) που θα είχαν στον ΑΡΧΙΚΟ πίνακα του κλάδου (πριν
        # την αφαίρεση), όχι μόνο το πλήθος. Ταύτιση με βάση όνομα (όχι index),
        # ώστε να μη σπάει ό,τι κι αν επιστρέφει εσωτερικά η remove_monimoi.
        if mon_res.n_removed:
            try:
                def _name_key3(row):
                    return (norm_key(str(row.get("ΕΠΩΝΥΜΟ", "") or "")),
                            norm_key(str(row.get("ΟΝΟΜΑ", "") or "")),
                            norm_key(str(row.get("ΠΑΤΡΩΝΥΜΟ", "") or "")))

                removed_keys = {_name_key3(r) for _, r in mon_res.removed.iterrows()}
                ranked_before, _ = rank(df_k)
                mask = ranked_before.apply(lambda r: _name_key3(r) in removed_keys, axis=1)
                monimoi_listed = ranked_before[mask]
                sort_col = MORIA_NUM if MORIA_NUM in monimoi_listed.columns else None
                if sort_col:
                    monimoi_listed = monimoi_listed.sort_values(sort_col, ascending=False)
                show_cols = [c for c in ["ΘΕΣΗ", "ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", MORIA_NUM]
                            if c in monimoi_listed.columns]
                if show_cols and not monimoi_listed.empty:
                    print(f"\n🏛️  ΜΟΝΙΜΟΙ ΠΟΥ ΑΦΑΙΡΕΘΗΚΑΝ  (ονόματα, μόρια, θέση στον αρχικό πίνακα):")
                    disp = monimoi_listed[show_cols].rename(columns={MORIA_NUM: "ΜΟΡΙΑ"})
                    print("   " + disp.to_string(index=False).replace("\n", "\n   "))
                    n_unmatched = mon_res.n_removed - len(monimoi_listed)
                    if n_unmatched > 0:
                        print(f"   ⚠️  {n_unmatched} αφαιρεθέντες δεν εμφανίζονται εδώ (δεν ταυτίστηκαν"
                              " ονομαστικά στον αρχικό πίνακα).")
            except Exception as exc:                                  # noqa: BLE001
                print(f"   ⚠️  Δεν μπόρεσα να φτιάξω την αναλυτική λίστα μονίμων ({exc}).")

    # --- 2β. Αφαίρεση όσων ΗΔΗ προσλήφθηκαν φέτος (φάσεις Α, Β, Γ, Δ… ενός ή
    # ΠΕΡΙΣΣΟΤΕΡΩΝ ετών μαζί — π.χ. "2024-2025,2025-2026" για να αφαιρεθούν και
    # τα δύο έτη ταυτόχρονα).
    phase_res = None
    exclude_years = {y.strip() for y in str(getattr(args, "exclude_year", "") or "").split(",") if y.strip()}
    if exclude_years:
        exclude_label = ", ".join(sorted(exclude_years))
        fdir = CFG.faseis_dir()
        if not fdir.exists():
            print(f"\n⚠️  Δεν υπάρχει φάκελος φάσεων ({fdir}) — παραλείπεται.")
        else:
            from loader import DATA_SUFFIXES, is_own_output
            out_dir2 = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
            all_faseis = [f for f in sorted(fdir.rglob("*"))
                         if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir2)]
            year_files = []
            for f in all_faseis:
                rel = f.relative_to(fdir)
                _, y = extract_phase_year(" / ".join(rel.parts))
                if y in exclude_years:
                    year_files.append(f)
            if not year_files:
                avail = sorted({(extract_phase_year(" / ".join(f.relative_to(fdir).parts))[1] or "?")
                                for f in all_faseis})
                print(f"\n⚠️  Δεν βρέθηκαν αρχεία φάσεων για το/τα έτος/η {exclude_label}.")
                print(f"   Διαθέσιμα έτη στο faseis: {', '.join(avail)}")
            else:
                frames = []
                for f in year_files:
                    tabs = load_path(f)
                    if not tabs:
                        continue
                    d = concat(tabs)
                    dk2, _ = filter_klados(d, args.klados, args.subcodes)
                    if not dk2.empty:
                        frames.append(dk2)
                if frames:
                    df_hired = pd.concat(frames, ignore_index=True)
                    try:
                        phase_res = remove_monimoi(df_work, df_hired)
                    except Exception as exc:                          # noqa: BLE001
                        print(f"\n⚠️  Η αφαίρεση ήδη προσληφθέντων απέτυχε ({exc}).")
                        phase_res = None
                    if phase_res is not None:
                        df_work = phase_res.kept
                        pct2 = (100 * phase_res.n_removed / phase_res.n_before
                               if phase_res.n_before else 0)
                        print(f"\n🚫 ΑΦΑΙΡΕΣΗ ΗΔΗ ΠΡΟΣΛΗΦΘΕΝΤΩΝ  (φάσεις {exclude_label}: "
                              f"{', '.join(f.name for f in year_files)})")
                        print(f"   • Εγγραφές στις φάσεις αυτές : {len(df_hired)}")
                        print(f"   • Αφαιρέθηκαν                : {phase_res.n_removed} ({pct2:.1f}%)")
                        print(f"   • Παραμένουν διαθέσιμοι      : {len(df_work)}")
                        if len(phase_res.monimoi_unused):
                            print(f"   ⚠️  {len(phase_res.monimoi_unused)} προσληφθέντες δεν "
                                  "αντιστοιχήθηκαν (πιθανόν άλλος κλάδος/γραφή ονόματος)")
                        hcols = [c for c in ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΚΛΑΔΟΣ", "ΠΕΡΙΟΧΗ",
                                            "ΜΟΡΙΑ"] + CFG.PROV_COLS if c in df_hired.columns]
                        years_tag = "_".join(y.replace("-", "_") for y in sorted(exclude_years))
                        hired_out = OUT / f"ΗΔΗ_ΠΡΟΣΛΗΦΘΕΝΤΕΣ_{tag}_{years_tag}.xlsx"
                        df_hired[hcols].to_excel(hired_out, index=False)
                        print(f"💾 Πίνακας ήδη προσληφθέντων ({len(df_hired)} άτομα): {hired_out}")

    ranked, rank_note = rank(df_work)
    print(f"\n🔢 Κατάταξη: {rank_note}")
    s_after = summarize_klados(df_work, args.klados, "μετά την αφαίρεση μονίμων")
    eparkeia_after = eparkeia_breakdown(df_work)
    if mon_res is not None or phase_res is not None:
        what = " + ".join(x for x in [
            "μονίμων" if mon_res is not None else None,
            "ήδη προσληφθέντων" if phase_res is not None else None] if x)
        print(f"\n📊 ΝΕΟΣ ΠΙΝΑΚΑΣ (ΜΕΤΑ ΤΗΝ ΑΦΑΙΡΕΣΗ {what.upper()}):")
        print(f"   • Νέο πλήθος υποψηφίων : {s_after.n_total}")
        if s_after.top:
            print(f"   • Νέος 1ος (μέγιστα μόρια)   : {s_after.top['ΜΟΡΙΑ']:.3f} — "
                  f"{_fmt_person(s_after.top)}")
            print(f"   • Νέος τελευταίος (ελάχιστα) : {s_after.bottom['ΜΟΡΙΑ']:.3f} — "
                  f"{_fmt_person(s_after.bottom)}")
        if s_after.first_ranked:
            print(f"   • Νέος 1ος επίσημης σειράς   : {s_after.first_ranked['ΜΟΡΙΑ']:.3f} — "
                  f"{_fmt_person(s_after.first_ranked)}")
            print(f"   • Νέος τελευταίος σειράς     : {s_after.last_ranked['ΜΟΡΙΑ']:.3f} — "
                  f"{_fmt_person(s_after.last_ranked)}")
        _print_eparkeia(eparkeia_after, "ΝΕΑ ΚΑΤΑΝΟΜΗ ΚΑΤΑ ΕΠΑΡΚΕΙΑ (μετά την αφαίρεση)")

    # --- 3. Περιοχή διορισμού
    region_res = None
    # Οι περιοχές βρίσκονται στο αρχείο διορισμών, όχι στον πίνακα κατάταξης.
    src = df_work
    src_label = "πίνακας κατάταξης"
    if args.monimoi and "ΠΕΡΙΟΧΗ" in df_mon.columns:
        src, src_label = df_mon, "αρχείο διορισμών"
    if args.hires:
        h_tables = load_path(args.hires)
        tables = tables + h_tables
        df_h = concat(h_tables)
        df_h, _ = filter_klados(df_h, args.klados, args.subcodes)
        src, src_label = df_h, "αρχείο προσλήψεων"
    regions_tbl = all_regions(src)

    if args.region:
        region_res = last_in_region(src, args.region)
        print(f"\n📍 ΠΕΡΙΟΧΗ ΔΙΟΡΙΣΜΟΥ: «{args.region}»  (πηγή: {src_label})")
        if region_res.last:
            print(f"   • Ταιριάζουν οι περιοχές: {', '.join(region_res.regions_found)}")
            print(f"   • Εγγραφές              : {region_res.n_matched}")
            print(f"   • ΒΑΣΗ (τελευταίος)     : {region_res.last['ΜΟΡΙΑ']:.3f} μόρια")
            print(f"     ↳ {_fmt_person(region_res.last)}")
            print(f"     ↳ πηγή: {region_res.last['ΠΗΓΗ']}")
            print("\n   Οι 5 τελευταίοι της περιοχής (για οπτικό έλεγχο):")
            print(region_res.tail.to_string(index=False))
        else:
            print(f"   ⚠️  {region_res.note}")
            if not regions_tbl.empty:
                print("   Διαθέσιμες περιοχές: "
                      + ", ".join(map(str, regions_tbl['ΠΕΡΙΟΧΗ'].head(20))))
    elif not regions_tbl.empty:
        print("\n📍 ΒΑΣΕΙΣ ΑΝΑ ΠΕΡΙΟΧΗ (τελευταίος κάθε περιοχής):")
        print(regions_tbl.head(args.top_regions).to_string(index=False))

    # --- 4. Επαλήθευση
    checks = run_checks(df_all, df_k, mon_res, ranked, region_res)
    if phase_res is not None:
        ok_bal = phase_res.n_before == len(phase_res.kept) + len(phase_res.removed)
        icon = "✅" if ok_bal else "❌"
        print(f" {icon} Ισοζύγιο πλήθους μετά την αφαίρεση ήδη προσληφθέντων\n"
              f"      → {phase_res.n_before} = {len(phase_res.kept)} + {len(phase_res.removed)}")
    all_ok = print_checks(checks)

    summary_rows = [
        {"ΜΕΤΡΙΚΗ": "Κλάδος", "ΤΙΜΗ": args.klados, "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": how},
        {"ΜΕΤΡΙΚΗ": "Υποψήφιοι κλάδου", "ΤΙΜΗ": s_before.n_total,
         "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": "γραμμές βασικού πίνακα μετά το φίλτρο κλάδου"},
        {"ΜΕΤΡΙΚΗ": "Μόρια 1ου", "ΤΙΜΗ": s_before.top.get("ΜΟΡΙΑ"),
         "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": s_before.top.get("ΠΗΓΗ", "")},
        {"ΜΕΤΡΙΚΗ": "Μόρια τελευταίου", "ΤΙΜΗ": s_before.bottom.get("ΜΟΡΙΑ"),
         "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": s_before.bottom.get("ΠΗΓΗ", "")},
    ]
    if mon_res is not None:
        summary_rows += [
            {"ΜΕΤΡΙΚΗ": "Μόνιμοι που αφαιρέθηκαν", "ΤΙΜΗ": mon_res.n_removed,
             "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": f"διασταύρωση με κλειδί {mon_res.level}"},
            {"ΜΕΤΡΙΚΗ": "Παραμένουν στον πίνακα", "ΤΙΜΗ": len(mon_res.kept),
             "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": "σύνολο − μόνιμοι"},
        ]
    if region_res is not None and region_res.last:
        summary_rows.append(
            {"ΜΕΤΡΙΚΗ": f"Βάση περιοχής {args.region}",
             "ΤΙΜΗ": region_res.last["ΜΟΡΙΑ"],
             "ΠΩΣ ΥΠΟΛΟΓΙΣΤΗΚΕ": f"ελάχιστο μορίων · {region_res.last['ΠΗΓΗ']}"})

    ranked_out = OUT / f"ΠΙΝΑΚΑΣ_{tag}.xlsx"
    show = [c for c in ["ΘΕΣΗ", "ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΚΛΑΔΟΣ",
                        "ΠΕΡΙΟΧΗ", MORIA_NUM] + CFG.PROV_COLS if c in ranked.columns]
    ranked[show].rename(columns={MORIA_NUM: "ΜΟΡΙΑ"}).to_excel(ranked_out, index=False)

    audit_out = write_audit(OUT / f"AUDIT_{tag}.xlsx", tables=tables, checks=checks,
                            summary_rows=summary_rows, base_klados=df_k, ranked=ranked,
                            mon_res=mon_res, region_res=region_res, regions_table=regions_tbl,
                            eparkeia_after=eparkeia_after)

    print(f"\n💾 Καθαρός πίνακας : {ranked_out}")
    print(f"💾 Φάκελος αποδείξεων: {audit_out}")
    if not all_ok:
        print("\n⚠️  Υπάρχουν προειδοποιήσεις — άνοιξε το φύλλο ΕΛΕΓΧΟΙ πριν "
              "χρησιμοποιήσεις τα νούμερα.")
    return 0



# ---------------------------------------------------------------------------
# ΔΙΑΔΡΑΣΤΙΚΟ ΜΕΝΟΥ (τρέχει όταν δεν δίνεις εντολή)
# ---------------------------------------------------------------------------
def _describe(path: Path) -> str:
    """Μικρή περιγραφή αρχείου: κλάδοι και πλήθος γραμμών, χωρίς να το ανοίγει ο χρήστης."""
    try:
        tables = load_path(path)
    except Exception as exc:                                   # noqa: BLE001
        return f"[δεν διαβάζεται: {exc}]"
    if not tables:
        return "[δεν αναγνωρίστηκε πίνακας]"
    rows = sum(t.n_rows for t in tables)
    codes = set()
    for t in tables:
        if "ΚΛΑΔΟΣ" in t.df.columns:
            for v in t.df["ΚΛΑΔΟΣ"].dropna().unique():
                codes.update(extract_klados_codes(v))
        codes.update(extract_klados_codes(t.path.stem))
    kl = ", ".join(sorted(codes)[:6]) + ("…" if len(codes) > 6 else "")
    return f"{rows} γραμμές" + (f" · {kl}" if kl else "")


def _pick(title, options, allow_none=False):
    print(f"\n{title}")
    _line("-")
    for i, (label, extra) in enumerate(options, 1):
        print(f"  [{i}] {label}")
        if extra:
            print(f"      {extra}")
    if allow_none:
        print("  [0] (καμία / παράλειψη)")
    _line("-")
    while True:
        raw = input("Επιλογή: ").strip()
        if raw == "0" and allow_none:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("❌ Δώσε έναν από τους αριθμούς της λίστας.")


def cmd_menu(args):
    print("=" * 78)
    print(" ΑΝΑΛΥΣΗ ΠΙΝΑΚΩΝ ΑΝΑΠΛΗΡΩΤΩΝ")
    print("=" * 78)
    print(f"Φάκελος δεδομένων: {CFG.DATA_DIR}")
    if not CFG.DATA_DIR.exists():
        print("❌ Ο φάκελος δεν υπάρχει. Διόρθωσέ τον στο config.py (DATA_DIR).")
        return 1

    pinakes = _files_of(CFG.pinakes_dir(), "base")
    monimoi = _files_of(CFG.monimoi_dir(), "monimoi")
    if not pinakes:
        print("❌ Δεν βρέθηκε κανένας πίνακας κατάταξης στον φάκελο.")
        return 1

    # --- 1) ΚΛΑΔΟΣ: ο χρήστης γράφει τον κωδικό, το πρόγραμμα βρίσκει μόνο του
    #     το σωστό αρχείο — καμία λίστα 40+ αρχείων για να διαλέξει κανείς.
    klados = input("\nΠοιον κλάδο θέλεις; (π.χ. ΠΕ06, ΠΕ04.01): ").strip().upper()
    if not klados:
        print("❌ Χρειάζεται κωδικός κλάδου.")
        return 1

    rx = klados_regex(klados, False)
    hits = [f for f in pinakes if rx.search(norm_code_text(f.stem))]
    if not hits:
        rx_sub = klados_regex(klados, True)
        sub_hits = [f for f in pinakes if rx_sub.search(norm_code_text(f.stem))]
        if sub_hits:
            subs = sorted({c for f in sub_hits for c in extract_klados_codes(f.stem)
                          if c.startswith(klados)})
            print(f"❌ Ο κλάδος {klados} έχει υποκλάδους: {', '.join(subs)}")
            print("   Ξανατρέξε δίνοντας έναν συγκεκριμένο, π.χ. " + subs[0])
        else:
            all_codes = sorted({c for f in pinakes for c in extract_klados_codes(f.stem)})
            print(f"❌ Δεν βρέθηκε πίνακας για {klados}.")
            print(f"   Διαθέσιμοι κλάδοι: {', '.join(all_codes)}")
        return 1
    if len(hits) == 1:
        base = hits[0]
    else:
        i = _pick(f"Βρέθηκαν {len(hits)} αρχεία για {klados} — ποιο;",
                  [(f.name, _describe(f)) for f in hits])
        base = hits[i]

    # --- 2) ΜΟΝΙΜΟΙ: αυτόματα αν υπάρχει μόνο ένα αρχείο, αλλιώς ρωτάει
    mon = None
    if len(monimoi) == 1:
        mon = monimoi[0]
        print(f"\nΑρχείο διορισμών/μονίμων: {mon.name}  (αυτόματα, το μόνο διαθέσιμο)")
    elif len(monimoi) > 1:
        j = _pick("Βρέθηκαν πάνω από ένα αρχεία διορισμών/μονίμων — ποιο;",
                  [(f.name, _describe(f)) for f in monimoi], allow_none=True)
        mon = monimoi[j] if j is not None else None
    else:
        print("\n(Δεν βρέθηκε αρχείο διορισμών/μονίμων — θα παραλειφθεί η αφαίρεση.)")

    # --- 3) ΠΕΡΙΟΧΗ
    region = input("\nΠεριοχή Διορισμού (Enter = όλες οι περιοχές): ").strip()

    args.base, args.monimoi, args.klados = str(base), (str(mon) if mon else None), klados
    args.region = region or None
    args.hires, args.subcodes, args.top_regions = None, False, 25
    print()
    return cmd_full(args)


# ---------------------------------------------------------------------------
def cmd_phase(args):
    """
    Ζητούμενο: για μια περιοχή, πόσα μόρια χρειάστηκε ο τελευταίος για να
    μπει σε συγκεκριμένη φάση/έτος πρόσληψης. Ψάχνει στον υποφάκελο 'faseis'.

    Ένα σχολικό έτος έχει συνήθως πολλές φάσεις (Α΄, Β΄, Γ΄…) — η ΒΑΣΗ ΤΟΥ
    ΕΤΟΥΣ είναι το χαμηλότερο μόριο σε ΟΛΕΣ τις φάσεις μαζί, όχι μόνο σε μία.
    Επίσης, το ίδιο έτος/φάση μπορεί να εκδίδεται σε ξεχωριστά αρχεία ανά
    βαθμίδα (π.χ. ΓΕΝΙΚΗ_ΔΕ / ΓΕΝΙΚΗ_ΠΕ) — αν διαλέξεις παραπάνω από ένα,
    υπολογίζεται και το συνολικό ελάχιστο σε όλα μαζί.
    """
    fdir = CFG.faseis_dir()
    if not fdir.exists():
        print(f"❌ Δεν υπάρχει ακόμα ο φάκελος φάσεων: {fdir}")
        print("   Δημιούργησε τον υποφάκελο 'faseis' μέσα στον κεντρικό φάκελο και")
        print("   βάλε εκεί τα αρχεία (ένα ανά φάση/έτος, ή σε υποφακέλους ανά έτος).")
        return 1

    from loader import DATA_SUFFIXES, is_own_output
    out_dir = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
    all_files = [f for f in sorted(fdir.rglob("*"))
                if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)]
    if not all_files:
        print(f"❌ Δεν βρέθηκε κανένα αρχείο μέσα στο {fdir}.")
        return 1

    def base_label(f: Path) -> str:
        rel = f.relative_to(fdir)
        phase, year = extract_phase_year(" / ".join(rel.parts))
        tag = " ".join(x for x in [f"{phase}΄ ΦΑΣΗ" if phase else None, year] if x)
        dates = _phase_date_range(year, phase)
        if dates:
            tag = f"{tag} ({dates})" if tag else dates
        return tag or f.stem

    def extra_hint(f: Path) -> str:
        """Ό,τι απομένει από το όνομα αφού αφαιρεθούν φάση/έτος — π.χ. 'ΓΕΝΙΚΗ ΔΕ'."""
        rel = f.relative_to(fdir)
        s = norm_text(" / ".join(rel.parts))
        # ΠΡΩΤΑ μετατρέπουμε _/- σε κενό — αλλιώς τα \b στα regex από κάτω
        # μπλοκάρονται, αφού η κάτω παύλα είναι χαρακτήρας λέξης (ίδιο πρόβλημα
        # όπως στο extract_phase_year).
        s = re.sub(r"[_\-/]+", " ", s)
        s = re.sub(r"20\d{2}(?:\s?(20)?\d{2})?", " ", s)
        s = re.sub(r"\b(ΣΤ|Α|Β|Γ|Δ|Ε|Ζ|Η)['΄]?\s*ΦΑΣΗ\b", " ", s)
        s = re.sub(r"\.[A-ZΑ-Ω0-9]+$", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def year_of(f: Path) -> str:
        rel = f.relative_to(fdir)
        _, year = extract_phase_year(" / ".join(rel.parts))
        return year or "(άγνωστο έτος)"

    # Ετικέτα ανά αρχείο — αν δύο αρχεία βγάζουν την ΙΔΙΑ ετικέτα (π.χ. ίδια
    # φάση/έτος αλλά διαφορετική βαθμίδα), προσθέτουμε ό,τι τα ξεχωρίζει.
    base_labels = {f: base_label(f) for f in all_files}
    from collections import Counter
    dupes = {t for t, c in Counter(base_labels.values()).items() if c > 1}
    tag_of = dict(base_labels)
    for f, t in base_labels.items():
        if t in dupes:
            extra = extra_hint(f)
            if extra and extra != t:
                tag_of[f] = f"{t} [{extra}]"

    klados = args.klados or input("Ποιον κλάδο θέλεις; (π.χ. ΠΕ06): ").strip().upper()
    if not klados:
        print("❌ Χρειάζεται κωδικός κλάδου.")
        return 1
    region = args.region or input("Περιοχή Διορισμού: ").strip()
    if not region:
        print("❌ Χρειάζεται περιοχή για αυτόν τον υπολογισμό.")
        return 1

    current_pinakas = _load_current_pinakas(klados, args.subcodes)
    if not current_pinakas.empty:
        current_pinakas_ranked, _ = rank(current_pinakas)
        print(f"\nℹ️  Βρέθηκε τρέχων πίνακας {klados} ({len(current_pinakas)} άτομα) — τα μόρια"
              " ανά φάση θα ενημερωθούν από εκεί όπου υπάρχει ταύτιση ονόματος.")
    else:
        current_pinakas_ranked = current_pinakas
        print(f"\nℹ️  Δεν βρέθηκε τρέχων πίνακας {klados} — θα χρησιμοποιηθούν τα μόρια όπως"
              " είναι μέσα στα ίδια τα αρχεία φάσεων.")

    year_label = None
    if args.file:
        chosen = [Path(args.file)]
    elif args.year:
        groups = {}
        for f in all_files:
            groups.setdefault(year_of(f), []).append(f)
        matches = [y for y in groups if args.year in y]
        if not matches:
            print(f"❌ Δεν βρέθηκε έτος «{args.year}». Διαθέσιμα: {', '.join(sorted(groups))}")
            return 1
        if len(matches) > 1:
            print(f"❌ Το «{args.year}» ταιριάζει σε πάνω από ένα: {', '.join(matches)}."
                  " Δώσε πιο συγκεκριμένο (π.χ. 2024-2025).")
            return 1
        year_label = matches[0]
        chosen = groups[year_label]
    else:
        groups = {}
        for f in all_files:
            groups.setdefault(year_of(f), []).append(f)
        years_sorted = sorted(groups, key=lambda y: (y == "(άγνωστο έτος)", y))
        if len(years_sorted) == 1 and years_sorted[0] != "(άγνωστο έτος)" and len(all_files) == 1:
            year_label = years_sorted[0]
            chosen = groups[year_label]
        else:
            print("\nΔιαθέσιμα αρχεία φάσεων:")
            for i, f in enumerate(all_files, 1):
                print(f"  [{i}] {tag_of[f]:<28} ({f.relative_to(fdir)})")
            print("  [0] ΟΛΑ (συνδυασμός/σύγκριση όλων)")
            raw = input("Επιλογή (Enter = όλα): ").strip()
            if raw in ("", "0"):
                chosen = all_files
                yrs = {year_of(f) for f in all_files}
                year_label = yrs.pop() if len(yrs) == 1 and next(iter(yrs)) != "(άγνωστο έτος)" else None
            elif raw.isdigit() and 1 <= int(raw) <= len(all_files):
                chosen = [all_files[int(raw) - 1]]
                y = year_of(chosen[0])
                year_label = y if y != "(άγνωστο έτος)" else None
            else:
                print("❌ Μη έγκυρη επιλογή.")
                return 1

    print("=" * 78)
    title = f" ΒΑΣΗ — ΚΛΑΔΟΣ {klados} — ΠΕΡΙΟΧΗ «{region}»"
    if year_label:
        title += f" — ΕΤΟΣ {year_label}"
    print(title)
    print("=" * 78)

    rows = []
    combined = []
    detail_rows = []
    for f in chosen:
        tag = tag_of[f]
        tables = load_path(f)
        if not tables:
            print(f"\n📄 {tag}  ({f.name})\n   ⚠️  Το αρχείο δεν διαβάστηκε.")
            continue
        df = concat(tables)
        dfk, how = filter_klados(df, klados, args.subcodes)
        print(f"\n📄 {tag}  ({f.name})")
        if dfk.empty:
            print(f"   ⚠️  Ο κλάδος {klados} δεν βρέθηκε σε αυτό το αρχείο.")
            continue
        if not current_pinakas.empty:
            dfk = enrich_with_current_moria(dfk, current_pinakas_ranked)
            n_ok = int(dfk["_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ"].sum())
            print(f"   (μόρια από τρέχοντα πίνακα: {n_ok}/{len(dfk)} ταυτίστηκαν ονομαστικά)")
        combined.append(dfk)

        # Ζητούμενο: να φαίνονται ΟΛΟΙ όσοι δήλωσαν/προσλήφθηκαν στην περιοχή,
        # με τα ΤΡΕΧΟΝΤΑ μόριά τους — όχι μόνο ο τελευταίος.
        if "ΠΕΡΙΟΧΗ" in dfk.columns:
            people = dfk[dfk["ΠΕΡΙΟΧΗ"].map(lambda v: region_matches(v, region))].copy()
            if not people.empty:
                orario_col = people["ΩΡΑΡΙΟ"] if "ΩΡΑΡΙΟ" in people.columns else [None] * len(people)
                people["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] = [classify_orario(pv, ov) for pv, ov
                                          in zip(people["ΠΕΡΙΟΧΗ"], orario_col)]
                people["_Μ"] = people["ΜΟΡΙΑ"].map(to_float)
                people = people.sort_values("_Μ", ascending=False)
                # Ζητούμενο: ΔΥΟ σειρές — η σειρά με την οποία μπήκε ΤΟΤΕ σε αυτή τη
                # φάση (δική του στήλη ΣΕΙΡΑ του αρχείου) ΚΑΙ η σειρά στον τωρινό
                # πίνακα (δική μας). Μετονομάζουμε αντί να πετάμε τη μία.
                if "ΣΕΙΡΑ" in people.columns:
                    people = people.rename(columns={"ΣΕΙΡΑ": "ΣΕΙΡΑ_ΦΑΣΗΣ"})
                people = people.drop(columns=["ΩΡΑΡΙΟ"], errors="ignore")
                show = [c for c in ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΜΟΡΙΑ", "ΩΡΑΡΙΟ_ΤΥΠΟΣ",
                                    "ΣΕΙΡΑ_ΦΑΣΗΣ", "ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ"] if c in people.columns]
                rename = {"ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ": "ΣΕΙΡΑ_ΤΩΡΑ", "ΩΡΑΡΙΟ_ΤΥΠΟΣ": "ΩΡΑΡΙΟ"}
                people = people.rename(columns=rename)
                show = [rename.get(c, c) for c in show]
                if "_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ" in people.columns:
                    people = people.rename(columns={"_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ": "ΑΠΟ_ΤΡΕΧΟΝΤΑ_ΠΙΝΑΚΑ"})
                    show.append("ΑΠΟ_ΤΡΕΧΟΝΤΑ_ΠΙΝΑΚΑ")
                print(f"\n   Όλοι όσοι δήλωσαν «{region}» εδώ ({len(people)} άτομα), με τρέχοντα μόρια:")
                print("   " + people[show].to_string(index=False).replace("\n", "\n   "))
                if "_ΣΗΜΕΙΩΣΗ_ΤΑΥΤΙΣΗΣ" in dfk.columns:
                    for idx, p in people.iterrows():
                        note = dfk.loc[idx, "_ΣΗΜΕΙΩΣΗ_ΤΑΥΤΙΣΗΣ"] if idx in dfk.index else ""
                        if note:
                            print(f"     ⚠️  {p.get('ΕΠΩΝΥΜΟ','')} {p.get('ΟΝΟΜΑ','')}: {note}")
                for _, p in people.iterrows():
                    detail_rows.append({"ΦΑΣΗ/ΕΤΟΣ": tag, "ΑΡΧΕΙΟ": f.name,
                                        "ΕΠΩΝΥΜΟ": p.get("ΕΠΩΝΥΜΟ", ""), "ΟΝΟΜΑ": p.get("ΟΝΟΜΑ", ""),
                                        "ΠΑΤΡΩΝΥΜΟ": p.get("ΠΑΤΡΩΝΥΜΟ", ""),
                                        "ΜΟΡΙΑ (τρέχοντα)": p.get("ΜΟΡΙΑ", ""),
                                        "ΩΡΑΡΙΟ": p.get("ΩΡΑΡΙΟ", ""),
                                        "ΑΠΟ_ΤΡΕΧΟΝΤΑ_ΠΙΝΑΚΑ": p.get("ΑΠΟ_ΤΡΕΧΟΝΤΑ_ΠΙΝΑΚΑ", "")})

        by_type = last_in_region_by_orario(dfk, region)
        if by_type:
            print()
            for typ in ("ΠΛΗΡΕΣ", "ΜΕΙΩΜΕΝΟ"):
                if typ not in by_type:
                    continue
                rr = by_type[typ]
                print(f"   • {typ}  —  Εγγραφές: {rr.n_matched}  —  Μόρια που χρειάστηκαν: "
                      f"{rr.last['ΜΟΡΙΑ']:.3f}")
                print(f"     ↳ {_fmt_person(rr.last)}")
                print(f"     ↳ πηγή: {rr.last['ΠΗΓΗ']}")
                rows.append({"ΦΑΣΗ/ΕΤΟΣ": tag, "ΑΡΧΕΙΟ": f.name, "ΩΡΑΡΙΟ": typ,
                             "ΕΓΓΡΑΦΕΣ": rr.n_matched, "ΜΟΡΙΑ ΤΕΛΕΥΤΑΙΟΥ": rr.last["ΜΟΡΙΑ"],
                             "ΟΝΟΜΑ": f"{rr.last.get('ΕΠΩΝΥΜΟ','')} {rr.last.get('ΟΝΟΜΑ','')}".strip()})
        else:
            # Ο κλάδος βρέθηκε (π.χ. Ν εγγραφές) αλλά καμία στη ζητούμενη περιοχή —
            # δείξε ποιες περιοχές ΥΠΑΡΧΟΥΝ σε αυτό το αρχείο για να βρεις τη σωστή γραφή.
            print(f"   ⚠️  καμία εγγραφή για την περιοχή αυτή  ({len(dfk)} εγγραφές {klados} σε άλλες περιοχές)")
            if "ΠΕΡΙΟΧΗ" in dfk.columns:
                avail = sorted(set(dfk["ΠΕΡΙΟΧΗ"].dropna()))
                if avail:
                    print(f"       Περιοχές σε αυτό το αρχείο: {', '.join(avail[:15])}"
                          + (" …" if len(avail) > 15 else ""))

    # Ζητούμενο: συνολικό ελάχιστο όταν συνδυάζουμε πάνω από ένα αρχείο
    # (π.χ. πολλές φάσεις του ίδιου έτους, ή ΔΕ+ΠΕ μαζί).
    if len(combined) > 1:
        df_combo = pd.concat(combined, ignore_index=True)
        combo_title = (f"ΒΑΣΗ ΟΛΟΚΛΗΡΟΥ ΤΟΥ ΕΤΟΥΣ {year_label}" if year_label
                       else "ΣΥΝΟΛΙΚΗ ΒΑΣΗ ΕΠΙΛΕΓΜΕΝΩΝ ΑΡΧΕΙΩΝ")
        by_type_c = last_in_region_by_orario(df_combo, region)
        print(f"\n🏁 {combo_title} (χαμηλότερο μόριο σε όλα μαζί):")
        if by_type_c:
            for typ in ("ΠΛΗΡΕΣ", "ΜΕΙΩΜΕΝΟ"):
                if typ not in by_type_c:
                    continue
                rr_c = by_type_c[typ]
                print(f"   • {typ}  —  Σύνολο εγγραφών: {rr_c.n_matched}  —  ΕΛΑΧΙΣΤΑ ΜΟΡΙΑ: "
                      f"{rr_c.last['ΜΟΡΙΑ']:.3f}")
                print(f"     ↳ {_fmt_person(rr_c.last)}")
                print(f"     ↳ πηγή: {rr_c.last['ΠΗΓΗ']}")
                rows.append({"ΦΑΣΗ/ΕΤΟΣ": "★ " + combo_title, "ΑΡΧΕΙΟ": "(συνδυασμός)", "ΩΡΑΡΙΟ": typ,
                             "ΕΓΓΡΑΦΕΣ": rr_c.n_matched, "ΜΟΡΙΑ ΤΕΛΕΥΤΑΙΟΥ": rr_c.last["ΜΟΡΙΑ"],
                             "ΟΝΟΜΑ": f"{rr_c.last.get('ΕΠΩΝΥΜΟ','')} {rr_c.last.get('ΟΝΟΜΑ','')}".strip()})
        else:
            print("   ⚠️  καμία εγγραφή για την περιοχή αυτή σε όλα τα επιλεγμένα αρχεία")

    if len(rows) > 1:
        print("\n📈 ΣΥΓΚΡΙΣΗ:")
        print(pd.DataFrame(rows).to_string(index=False))
    if rows:
        safe_region = norm_key(region).replace(" ", "_")
        tag_file = year_label.replace(" ", "") if year_label else "ΕΠΙΛΟΓΗ"
        out = CFG.output_dir() / f"ΦΑΣΕΙΣ_{klados.replace('.', '_')}_{safe_region}_{tag_file}.xlsx"
        with pd.ExcelWriter(out, engine="openpyxl") as xl:
            pd.DataFrame(rows).to_excel(xl, sheet_name="ΣΥΝΟΨΗ", index=False)
            if detail_rows:
                pd.DataFrame(detail_rows).to_excel(xl, sheet_name="ΑΝΑ_ΑΤΟΜΟ", index=False)
        print(f"\n💾 Αποθηκεύτηκε ({'2' if detail_rows else '1'} φύλλα): {out}")
    return 0



# ---------------------------------------------------------------------------
_PHASE_ORDER_IDX = {L: i for i, L in enumerate(["Α", "Β", "Γ", "Δ", "Ε", "ΣΤ", "Ζ", "Η"])}


def _lookup_person(df: pd.DataFrame, query: str):
    """Ψάχνει με Επώνυμο+Όνομα μέσα στον πίνακα. Επιστρέφει (μόρια, γραμμή) ή (None, None)."""
    parts = query.upper().split()
    if len(parts) < 2:
        print("❌ Γράψε Επώνυμο και Όνομα (π.χ. ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ).")
        return None, None
    mask = (df["ΕΠΩΝΥΜΟ"].str.contains(parts[0], na=False) &
           df["ΟΝΟΜΑ"].str.contains(parts[1], na=False))
    found = df[mask]
    if found.empty:
        print("❌ Δεν βρέθηκε στον πίνακα με αυτό το όνομα.")
        return None, None
    if len(found) > 1:
        print(f"⚠️  Βρέθηκαν {len(found)} υποψήφιοι με αυτά τα στοιχεία — παίρνω τον πρώτο."
              " Αν δεν είναι ο σωστός, ξανατρέξε με --moria και δώσε τα μόρια απευθείας.")
    row = found.iloc[0]
    return to_float(row.get("ΜΟΡΙΑ")), row


def cmd_predict(args):
    """
    Ζητούμενο: «με Χ μόρια, σε ποια περιοχή/φάση θα μπω φέτος;»

    Συνδυάζει: (1) τα μόρια/θέση σου στον τρέχοντα πίνακα, (2) τη νέα θέση
    μετά την αφαίρεση μονίμων, (3) το ιστορικό των φάσεων (Α΄, Β΄, Γ΄…) για
    τη ζητούμενη περιοχή σε προηγούμενα σχολικά έτη, ώστε να βρεθεί η
    ΠΡΩΤΗ φάση στην οποία τα μόριά σου θα αρκούσαν εκεί.

    ΠΡΟΣΟΧΗ: είναι εκτίμηση βασισμένη σε περασμένα χρόνια, όχι εγγύηση — ο
    αριθμός κενών θέσεων και υποψηφίων αλλάζει κάθε χρόνο.
    """
    klados = args.klados or input("Κλάδος (π.χ. ΠΕ06): ").strip().upper()
    if not klados:
        print("❌ Χρειάζεται κλάδος.")
        return 1
    region = args.region or input("Περιοχή-στόχος: ").strip()
    if not region:
        print("❌ Χρειάζεται περιοχή-στόχος.")
        return 1

    # ---- Μόρια/θέση υποψηφίου: απευθείας, ή αναζήτηση με όνομα στον πίνακα ----
    df_k = _load_current_pinakas(klados, args.subcodes)

    moria = args.moria
    if moria is None and args.name and not df_k.empty:
        moria, _ = _lookup_person(df_k, args.name)
    elif moria is None and not df_k.empty:
        name_q = input("Επώνυμο Όνομα (Enter για να δώσεις μόρια απευθείας): ").strip()
        if name_q:
            moria, _ = _lookup_person(df_k, name_q)
    if moria is None:
        raw = input("Πόσα μόρια έχεις στον πίνακα; ").strip().replace(",", ".")
        try:
            moria = float(raw)
        except ValueError:
            print("❌ Μη έγκυρος αριθμός μορίων.")
            return 1

    print("=" * 78)
    print(f" ΠΡΟΒΛΕΨΗ ΤΟΠΟΘΕΤΗΣΗΣ — {klados} — ΣΤΟΧΟΣ: «{region}» — {moria:.3f} μόρια")
    print("=" * 78)

    # ---- Θέση στον τρέχοντα πίνακα, πριν και μετά την αφαίρεση μονίμων ----
    if not df_k.empty:
        d = add_moria(df_k)
        pos = int((d[MORIA_NUM] > moria).sum()) + 1
        print(f"\n📊 Στον τρέχοντα πίνακα ({len(df_k)} υποψήφιοι): περίπου θέση #{pos}")
        monimoi_files = _files_of(CFG.monimoi_dir(), "monimoi")
        if monimoi_files:
            mon_tabs = []
            for f in monimoi_files:
                mon_tabs.extend(load_path(f))
            df_mon = concat(mon_tabs)
            if "ΚΛΑΔΟΣ" in df_mon.columns:
                df_mon, _ = filter_klados(df_mon, klados, args.subcodes)
            try:
                mres = remove_monimoi(df_k, df_mon)
                d2 = add_moria(mres.kept)
                pos2 = int((d2[MORIA_NUM] > moria).sum()) + 1
                print(f"   Μετά την αφαίρεση {mres.n_removed} μονίμων: περίπου θέση #{pos2}"
                      f" από {len(mres.kept)}")
            except Exception:                                       # noqa: BLE001
                pass
        df_k_ranked, _ = rank(df_k)
    else:
        df_k_ranked = df_k
        print("\n(Δεν βρέθηκε ο τρέχων πίνακας του κλάδου — προχωράω μόνο με τα ιστορικά φάσεων.)")

    # ---- Ιστορικό φάσεων για την περιοχή-στόχο ----
    fdir = CFG.faseis_dir()
    if not fdir.exists():
        print("\n⚠️  Δεν υπάρχει φάκελος 'faseis' — χρειάζονται ιστορικά αρχεία φάσεων για πρόβλεψη.")
        print("   Δημιούργησέ τον μέσα στο", CFG.DATA_DIR, "και βάλε τα αρχεία προηγούμενων ετών.")
        return 0
    from loader import DATA_SUFFIXES, is_own_output
    out_dir = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
    files = [f for f in sorted(fdir.rglob("*"))
             if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)]
    if not files:
        print(f"\n⚠️  Ο φάκελος {fdir} είναι άδειος.")
        return 0

    by_year_phase: dict = {}
    for f in files:
        rel = f.relative_to(fdir)
        phase, year = extract_phase_year(" / ".join(rel.parts))
        year = year or "(άγνωστο έτος)"
        by_year_phase.setdefault(year, {}).setdefault(phase or "?", []).append(f)

    predictions = []
    for year in sorted(by_year_phase):
        phases = by_year_phase[year]
        ordered = sorted(phases, key=lambda p: _PHASE_ORDER_IDX.get(p, 99))
        print(f"\n📅 Σχολικό έτος {year}:")
        qualifying = {"ΠΛΗΡΕΣ": None, "ΜΕΙΩΜΕΝΟ": None}
        # Συσσωρευτής για τη συγκεντρωτική λίστα "ποιες περιοχές σε παίρνουν" παρακάτω:
        # για κάθε (περιοχή, ωράριο) κρατάμε το ΧΑΜΗΛΟΤΕΡΟ μόριο ΜΕΧΡΙ ΣΤΙΓΜΗΣ (σωρευτικά
        # ανά φάση) και σε ΠΟΙΑ φάση τα μόριά σου θα αρκούσαν πρώτη φορά.
        region_progress: dict = {}
        for ph in ordered:
            frames = []
            for f in phases[ph]:
                tabs = load_path(f)
                if not tabs:
                    continue
                d = concat(tabs)
                dk, _ = filter_klados(d, klados, args.subcodes)
                if not dk.empty:
                    frames.append(dk)
            if not frames:
                continue
            dcomb = pd.concat(frames, ignore_index=True)
            if not df_k.empty:
                dcomb = enrich_with_current_moria(dcomb, df_k_ranked)
            label = f"{ph}΄ Φάση" if ph != "?" else "Φάση (άγνωστη)"
            dates = _phase_date_range(year, ph)
            if dates:
                label = f"{label} ({dates})"

            if "ΠΕΡΙΟΧΗ" in dcomb.columns:
                oc_all = dcomb["ΩΡΑΡΙΟ"] if "ΩΡΑΡΙΟ" in dcomb.columns else [None] * len(dcomb)
                types_all = [classify_orario(pv, ov) for pv, ov in zip(dcomb["ΠΕΡΙΟΧΗ"], oc_all)]
                moria_all = dcomb["ΜΟΡΙΑ"].map(to_float)
                seira_all = (dcomb["ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ"] if "ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ" in dcomb.columns
                            else [None] * len(dcomb))
                for per, typ, mv, sr in zip(dcomb["ΠΕΡΙΟΧΗ"], types_all, moria_all, seira_all):
                    if pd.isna(mv):
                        continue
                    key = (per, typ)
                    cur = region_progress.setdefault(
                        key, {"best": float("inf"), "qualified_at": None, "seira": None})
                    if mv < cur["best"]:
                        cur["best"] = mv
                        cur["seira"] = sr
                    if cur["qualified_at"] is None and cur["best"] <= moria:
                        cur["qualified_at"] = label

            by_type = last_in_region_by_orario(dcomb, region)
            if not by_type:
                print(f"   • {label:<16} καμία πρόσληψη σε αυτή την περιοχή")
                continue
            print(f"   • {label}:")
            for typ in ("ΠΛΗΡΕΣ", "ΜΕΙΩΜΕΝΟ"):
                if typ not in by_type:
                    continue
                rr = by_type[typ]
                thr = rr.last["ΜΟΡΙΑ"]
                seira_now = rr.last.get("ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ")
                seira_txt = (f", σειρά #{int(seira_now)} στον τωρινό πίνακα"
                            if seira_now not in (None, "") and pd.notna(seira_now) else "")
                ok = moria >= thr
                mark = "✅ ΘΑ ΕΜΠΑΙΝΕΣ" if ok else "❌ όχι ακόμα"
                print(f"       {typ:<9} βάση {thr:>7.3f} μόρια{seira_txt} ({rr.n_matched} θέσεις)  —  {mark}")
                if ok and qualifying.get(typ) is None:
                    qualifying[typ] = (label, thr, rr.n_matched, seira_now)

            # Ζητούμενο: να φαίνονται ΟΛΟΙ όσοι μπήκαν σε αυτή τη φάση/περιοχή, με
            # ΩΡΑΡΙΟ, τρέχοντα μόρια και σειρά στον τωρινό πίνακα — η "απόδειξη".
            if "ΠΕΡΙΟΧΗ" in dcomb.columns:
                people = dcomb[dcomb["ΠΕΡΙΟΧΗ"].map(lambda v: region_matches(v, region))].copy()
                if not people.empty:
                    orario_col = people["ΩΡΑΡΙΟ"] if "ΩΡΑΡΙΟ" in people.columns else [None] * len(people)
                    people["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] = [classify_orario(pv, ov) for pv, ov
                                              in zip(people["ΠΕΡΙΟΧΗ"], orario_col)]
                    people["_Μ"] = people["ΜΟΡΙΑ"].map(to_float)
                    people = people.sort_values("_Μ", ascending=False)
                    # Ζητούμενο: ΔΥΟ σειρές — η ΣΕΙΡΑ ΠΙΝΑΚΑ που είχε ΤΟΤΕ σε αυτή τη
                    # φάση (προτιμάται από το απλό Α/Α της γραμμής, όταν υπάρχει η
                    # στήλη "ΣΕΙΡΑ ΠΙΝΑΚΑ") ΚΑΙ η σειρά στον τωρινό πίνακα (δική μας),
                    # με αυτή τη σειρά εμφάνισης.
                    if "ΣΕΙΡΑ_ΠΙΝΑΚΑ" in people.columns:
                        people = people.rename(columns={"ΣΕΙΡΑ_ΠΙΝΑΚΑ": "ΣΕΙΡΑ_ΦΑΣΗΣ"})
                        people = people.drop(columns=["ΣΕΙΡΑ"], errors="ignore")
                    elif "ΣΕΙΡΑ" in people.columns:
                        people = people.rename(columns={"ΣΕΙΡΑ": "ΣΕΙΡΑ_ΦΑΣΗΣ"})
                    people = people.drop(columns=["ΩΡΑΡΙΟ"], errors="ignore")
                    show = [c for c in ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΜΟΡΙΑ",
                                        "ΩΡΑΡΙΟ_ΤΥΠΟΣ", "ΣΕΙΡΑ_ΦΑΣΗΣ", "ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ",
                                        "_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ"] if c in people.columns]
                    rename = {"ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ": "ΣΕΙΡΑ_ΤΩΡΑ",
                             "_ΤΑΥΤΙΣΤΗΚΕ_ΜΕ_ΠΙΝΑΚΑ": "ΑΠΟ_ΤΡΕΧΟΝΤΑ_ΠΙΝΑΚΑ",
                             "ΩΡΑΡΙΟ_ΤΥΠΟΣ": "ΩΡΑΡΙΟ"}
                    people = people.rename(columns=rename)
                    show = [rename.get(c, c) for c in show]
                    print("     " + people[show].to_string(index=False).replace("\n", "\n     "))
                    if "_ΣΗΜΕΙΩΣΗ_ΤΑΥΤΙΣΗΣ" in dcomb.columns:
                        for _, p in people.iterrows():
                            note = dcomb.loc[p.name, "_ΣΗΜΕΙΩΣΗ_ΤΑΥΤΙΣΗΣ"] if p.name in dcomb.index else ""
                            if note:
                                print(f"       ⚠️  {p.get('ΕΠΩΝΥΜΟ','')} {p.get('ΟΝΟΜΑ','')}: {note}")
        any_hit = False
        for typ in ("ΠΛΗΡΕΣ", "ΜΕΙΩΜΕΝΟ"):
            q = qualifying.get(typ)
            if q:
                label, thr, n, seira_now = q
                seira_txt = (f", σειρά #{int(seira_now)} στον τωρινό πίνακα"
                            if seira_now not in (None, "") and pd.notna(seira_now) else "")
                print(f"   🏁 Πρόβλεψη {year} ({typ}): θα έμπαινες στην {label}"
                      f" (βάση {thr:.3f}{seira_txt})")
                predictions.append((year, typ, label, thr, seira_now))
                any_hit = True
        if not any_hit:
            print(f"   ⚠️  Με {moria:.3f} μόρια δεν θα έφτανες σε καμία φάση του {year} εκεί"
                  " (ούτε πλήρους ούτε μειωμένου ωραρίου, με βάση τα διαθέσιμα αρχεία).")

        # Ζητούμενο: ΟΛΕΣ οι περιοχές που σε παίρνουν φέτος με τα μόριά σου, και σε ποια φάση.
        qualifies_rows = [
            {"ΠΕΡΙΟΧΗ": per, "ΩΡΑΡΙΟ": typ, "ΦΑΣΗ": info["qualified_at"], "ΒΑΣΗ": info["best"],
             "ΣΕΙΡΑ_ΤΩΡΑ": (int(info["seira"]) if info["seira"] not in (None, "")
                            and pd.notna(info["seira"]) else None)}
            for (per, typ), info in region_progress.items() if info["qualified_at"] is not None
        ]
        print(f"\n   🗺️  ΠΕΡΙΟΧΕΣ ΠΟΥ ΣΕ ΠΑΙΡΝΟΥΝ ΤΟ {year} ΜΕ {moria:.3f} ΜΟΡΙΑ:")
        if qualifies_rows:
            qdf = (pd.DataFrame(qualifies_rows)
                  .sort_values(["ΦΑΣΗ", "ΒΑΣΗ"], ascending=[True, False]))
            print("     " + qdf.to_string(index=False).replace("\n", "\n     "))
            print("     (ΦΑΣΗ = η νωρίτερη φάση που θα έφτανε η βάση της περιοχής στα μόριά σου·"
                  " ΒΑΣΗ = το τελικό χαμηλότερο μόριο όλης της χρονιάς εκεί)")
        else:
            print(f"     Με {moria:.3f} μόρια δεν θα έμπαινες σε καμία περιοχή αυτού του κλάδου"
                  f" το {year}, με βάση τα διαθέσιμα αρχεία φάσεων.")

    if predictions:
        print("\n" + "=" * 78)
        print(" ΣΥΝΟΨΗ ΠΡΟΒΛΕΨΗΣ (βάσει ιστορικών ετών — όχι εγγύηση)")
        print("=" * 78)
        for year, typ, label, thr, seira_now in predictions:
            seira_txt = (f", σειρά #{int(seira_now)}" if seira_now not in (None, "")
                        and pd.notna(seira_now) else "")
            print(f"   • {year} ({typ}): {label}  (χρειάστηκαν {thr:.3f} μόρια{seira_txt}"
                  " στον τωρινό πίνακα)")
        print("\n   ⚠️  Η φετινή χρονιά μπορεί να διαφέρει — αλλάζει ο αριθμός κενών/υποψηφίων.")

    avg_moria = _phase_moria_average()
    if avg_moria:
        print("\n" + "=" * 78)
        print(" ΕΝΔΕΙΚΤΙΚΟΣ ΜΕΣΟΣ ΟΡΟΣ ΜΟΡΙΩΝ ΑΝΑ ΦΑΣΗ  (γενικό στοιχείο, όχι ανά κλάδο/περιοχή)")
        print("=" * 78)
        for ph, avg in avg_moria.items():
            print(f"   • {ph}΄ Φάση: {avg:.2f} μόρια  (μέσος όρος διαθέσιμων ετών στο faseis_dates.json)")
        print("\n   ⚠️  Γενική ένδειξη από ιστορικά στοιχεία που μας έχεις δώσει — δεν αφορά")
        print("      ειδικά τον κλάδο ή την περιοχή που ζήτησες παραπάνω.")
    return 0


# ---------------------------------------------------------------------------
# Ζητούμενο: να ξεχωρίζουν στα αποτελέσματα του 'upgrades' οι θέσεις που
# φέρουν την ένδειξη «(Δ.Ε.)» στο όνομα της περιοχής (δηλ. τοποθέτηση σε
# σχολείο Δευτεροβάθμιας Εκπαίδευσης) από τις υπόλοιπες. Δεν αλλάζει τίποτα
# άλλο στη λογική αντιστοίχισης — απλώς προσθέτουμε μια ετικέτα στήλης.
_DE_TAG_RX = re.compile(r"\(\s*Δ\s*\.?\s*Ε\s*\.?\s*\)")


def _region_vathmida(region) -> str:
    """Επιστρέφει 'Δ.Ε.' αν η ΠΕΡΙΟΧΗ φέρει την ένδειξη '(Δ.Ε.)', αλλιώς κενό."""
    if region is None:
        return ""
    return "Δ.Ε." if _DE_TAG_RX.search(str(region)) else ""


def cmd_upgrades(args):
    """
    Ζητούμενο (νέα εντολή):
      1) Ποιοι υποψήφιοι προσλήφθηκαν με ΜΕΙΩΜΕΝΟ ωράριο σε κάθε φάση ενός
         σχολικού έτους — με μόρια, περιοχή και σειρά στον τρέχοντα πίνακα.
      2) Ποιοι από αυτούς αναβαθμίστηκαν σε ΠΛΗΡΕΣ ωράριο στην ΑΜΕΣΩΣ επόμενη
         φάση του ΙΔΙΟΥ σχολικού έτους (π.χ. Α΄ Μειωμένο → Β΄ Πλήρες,
         Β΄ Μειωμένο → Γ΄ Πλήρες κ.ο.κ.).

    Χωρίς --region εξετάζονται ΟΛΕΣ οι περιοχές του κλάδου. Με --region
    περιορίζεται μόνο σε αυτήν. Η ταύτιση ίδιου ατόμου ανάμεσα σε δύο φάσεις
    γίνεται με Επώνυμο+Όνομα+Πατρώνυμο (κανονικοποιημένα) — αν κάπου η
    γραφή του ονόματος διαφέρει αρκετά ανάμεσα στις δύο φάσεις, το άτομο
    μπορεί να μη ταυτιστεί.
    """
    fdir = CFG.faseis_dir()
    if not fdir.exists():
        print(f"❌ Δεν υπάρχει φάκελος φάσεων: {fdir}")
        print("   Χρειάζονται τα αρχεία φάσεων (Α΄, Β΄, Γ΄…) μέσα στο 'faseis'.")
        return 1

    from loader import DATA_SUFFIXES, is_own_output
    out_dir = CFG.DATA_DIR / CFG.OUTPUT_SUBDIR
    all_files = [f for f in sorted(fdir.rglob("*"))
                if f.suffix.lower() in DATA_SUFFIXES and not is_own_output(f, out_dir)]
    if not all_files:
        print(f"❌ Δεν βρέθηκε κανένα αρχείο μέσα στο {fdir}.")
        return 1

    klados = args.klados or input("Ποιον κλάδο θέλεις; (π.χ. ΠΕ04.01): ").strip().upper()
    if not klados:
        print("❌ Χρειάζεται κωδικός κλάδου.")
        return 1
    region = args.region or None

    by_year_phase: dict = {}
    for f in all_files:
        rel = f.relative_to(fdir)
        phase, year = extract_phase_year(" / ".join(rel.parts))
        year = year or "(άγνωστο έτος)"
        by_year_phase.setdefault(year, {}).setdefault(phase or "?", []).append(f)

    if args.year:
        # Ανεκτικό ταίριασμα προς τις δύο κατευθύνσεις — δουλεύει είτε τα αρχεία
        # έχουν ετικέτα "2025" είτε "2025-2026", ό,τι κι αν γράψει ο χρήστης.
        matches = [y for y in by_year_phase if args.year in y or y in args.year]
        if not matches:
            print(f"❌ Δεν βρέθηκε έτος «{args.year}». Διαθέσιμα: {', '.join(sorted(by_year_phase))}")
            return 1
        if len(matches) > 1 and args.year not in matches:
            print(f"❌ Το «{args.year}» ταιριάζει σε πάνω από ένα: {', '.join(matches)}."
                  " Δώσε πιο συγκεκριμένο (π.χ. 2024-2025).")
            return 1
        years = [args.year] if args.year in matches else matches
    else:
        years = sorted(by_year_phase, key=lambda y: (y == "(άγνωστο έτος)", y))

    current_pinakas = _load_current_pinakas(klados, args.subcodes)
    current_pinakas_ranked = rank(current_pinakas)[0] if not current_pinakas.empty else current_pinakas

    def key_of(row):
        return (norm_key(str(row.get("ΕΠΩΝΥΜΟ", "") or "")),
                norm_key(str(row.get("ΟΝΟΜΑ", "") or "")),
                norm_key(str(row.get("ΠΑΤΡΩΝΥΜΟ", "") or "")))

    all_meiomena_rows, all_upgrade_rows = [], []

    for year in years:
        phases = by_year_phase.get(year, {})
        if not phases:
            continue
        ordered = sorted(phases, key=lambda p: _PHASE_ORDER_IDX.get(p, 99))

        print("\n" + "=" * 78)
        title = f" ΜΕΙΩΜΕΝΟ → ΠΛΗΡΕΣ  —  ΚΛΑΔΟΣ {klados}  —  ΕΤΟΣ {year}"
        if region:
            title += f"  —  ΠΕΡΙΟΧΗ «{region}»"
        print(title)
        print("=" * 78)

        # Φορτώνουμε κάθε φάση μία φορά και ταξινομούμε ΩΡΑΡΙΟ_ΤΥΠΟΣ (ΠΛΗΡΕΣ/ΜΕΙΩΜΕΝΟ).
        phase_people = {}
        for ph in ordered:
            frames = []
            for f in phases[ph]:
                tabs = load_path(f)
                if not tabs:
                    continue
                d = concat(tabs)
                dk, _ = filter_klados(d, klados, args.subcodes)
                if not dk.empty:
                    frames.append(dk)
            if not frames:
                continue
            dcomb = pd.concat(frames, ignore_index=True)
            if not current_pinakas.empty:
                dcomb = enrich_with_current_moria(dcomb, current_pinakas_ranked)
            if "ΠΕΡΙΟΧΗ" not in dcomb.columns:
                continue
            dcomb = dcomb.copy()
            orario_col = dcomb["ΩΡΑΡΙΟ"] if "ΩΡΑΡΙΟ" in dcomb.columns else [None] * len(dcomb)
            dcomb["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] = [classify_orario(pv, ov) for pv, ov
                                     in zip(dcomb["ΠΕΡΙΟΧΗ"], orario_col)]
            dcomb["ΒΑΘΜΙΔΑ"] = dcomb["ΠΕΡΙΟΧΗ"].map(_region_vathmida)
            dcomb["_Μ"] = dcomb["ΜΟΡΙΑ"].map(to_float)
            if region:
                dcomb = dcomb[dcomb["ΠΕΡΙΟΧΗ"].map(lambda v: region_matches(v, region))]
            if "ΣΕΙΡΑ_ΠΙΝΑΚΑ" in dcomb.columns:
                dcomb = dcomb.rename(columns={"ΣΕΙΡΑ_ΠΙΝΑΚΑ": "ΣΕΙΡΑ_ΦΑΣΗΣ"})
                dcomb = dcomb.drop(columns=["ΣΕΙΡΑ"], errors="ignore")
            elif "ΣΕΙΡΑ" in dcomb.columns:
                dcomb = dcomb.rename(columns={"ΣΕΙΡΑ": "ΣΕΙΡΑ_ΦΑΣΗΣ"})
            phase_people[ph] = dcomb

        # --- 1) Όλοι όσοι προσλήφθηκαν με ΜΕΙΩΜΕΝΟ, ανά φάση ---
        any_meiomena = False
        for ph in ordered:
            dcomb = phase_people.get(ph)
            if dcomb is None:
                continue
            meiomena = dcomb[dcomb["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] == "ΜΕΙΩΜΕΝΟ"].sort_values("_Μ", ascending=False)
            if meiomena.empty:
                continue
            any_meiomena = True
            n_de = int((meiomena["ΒΑΘΜΙΔΑ"] == "Δ.Ε.").sum())
            n_other = len(meiomena) - n_de
            show = [c for c in ["ΕΠΩΝΥΜΟ", "ΟΝΟΜΑ", "ΠΑΤΡΩΝΥΜΟ", "ΠΕΡΙΟΧΗ", "ΒΑΘΜΙΔΑ", "ΜΟΡΙΑ",
                                "ΣΕΙΡΑ_ΦΑΣΗΣ", "ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ"] if c in meiomena.columns]
            ph_dates = _phase_date_range(year, ph)
            ph_dates_txt = f" ({ph_dates})" if ph_dates else ""
            print(f"\n📄 {ph}΄ Φάση{ph_dates_txt} — ΜΕΙΩΜΕΝΟ ωράριο ({len(meiomena)} άτομα"
                  f" · {n_de} σε θέσεις (Δ.Ε.), {n_other} σε λοιπές):")
            print("   " + meiomena[show].to_string(index=False).replace("\n", "\n   "))
            for _, p in meiomena.iterrows():
                all_meiomena_rows.append({
                    "ΕΤΟΣ": year, "ΦΑΣΗ": f"{ph}΄",
                    "ΕΠΩΝΥΜΟ": p.get("ΕΠΩΝΥΜΟ", ""), "ΟΝΟΜΑ": p.get("ΟΝΟΜΑ", ""),
                    "ΠΑΤΡΩΝΥΜΟ": p.get("ΠΑΤΡΩΝΥΜΟ", ""), "ΠΕΡΙΟΧΗ": p.get("ΠΕΡΙΟΧΗ", ""),
                    "ΒΑΘΜΙΔΑ": p.get("ΒΑΘΜΙΔΑ", ""), "ΜΟΡΙΑ": p.get("ΜΟΡΙΑ", ""),
                    "ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ": p.get("ΣΕΙΡΑ_ΤΩΡΙΝΟΥ_ΠΙΝΑΚΑ", ""),
                })
        if not any_meiomena:
            print("\n   (καμία πρόσληψη με ΜΕΙΩΜΕΝΟ ωράριο βρέθηκε για αυτό το έτος"
                  + (f" στην περιοχή «{region}»" if region else "") + ")")

        # --- 2) Αναβαθμίσεις: ΜΕΙΩΜΕΝΟ σε φάση N -> ΠΛΗΡΕΣ στην ΑΜΕΣΩΣ επόμενη ---
        found_any = False
        for i in range(len(ordered) - 1):
            ph_from, ph_to = ordered[i], ordered[i + 1]
            df_from, df_to = phase_people.get(ph_from), phase_people.get(ph_to)
            if df_from is None or df_to is None:
                continue
            meiomena_from = df_from[df_from["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] == "ΜΕΙΩΜΕΝΟ"]
            plires_to = df_to[df_to["ΩΡΑΡΙΟ_ΤΥΠΟΣ"] == "ΠΛΗΡΕΣ"]
            if meiomena_from.empty or plires_to.empty:
                continue
            plires_by_key = {key_of(r): r for _, r in plires_to.iterrows() if all(key_of(r))}
            for _, p in meiomena_from.iterrows():
                k = key_of(p)
                if k not in plires_by_key:
                    continue
                p2 = plires_by_key[k]
                if not found_any:
                    d_from = _phase_date_range(year, ph_from)
                    d_to = _phase_date_range(year, ph_to)
                    d_from_txt = f" ({d_from})" if d_from else ""
                    d_to_txt = f" ({d_to})" if d_to else ""
                    print(f"\n🚀 ΑΝΑΒΑΘΜΙΣΕΙΣ {ph_from}΄{d_from_txt} (ΜΕΙΩΜΕΝΟ) →"
                          f" {ph_to}΄{d_to_txt} (ΠΛΗΡΕΣ):")
                found_any = True
                m1, m2 = to_float(p.get("ΜΟΡΙΑ")), to_float(p2.get("ΜΟΡΙΑ"))
                pat = p.get("ΠΑΤΡΩΝΥΜΟ") or ""
                de1 = f" [{p.get('ΒΑΘΜΙΔΑ')}]" if p.get("ΒΑΘΜΙΔΑ") else ""
                de2 = f" [{p2.get('ΒΑΘΜΙΔΑ')}]" if p2.get("ΒΑΘΜΙΔΑ") else ""
                print(f"   • {p.get('ΕΠΩΝΥΜΟ','')} {p.get('ΟΝΟΜΑ','')}"
                      + (f" του {pat}" if pat else ""))
                print(f"       {ph_from}΄ ΜΕΙΩΜΕΝΟ στην «{p.get('ΠΕΡΙΟΧΗ','')}»{de1}"
                      + (f" ({m1:.3f} μόρια)" if m1 is not None and pd.notna(m1) else ""))
                print(f"       {ph_to}΄ ΠΛΗΡΕΣ   στην «{p2.get('ΠΕΡΙΟΧΗ','')}»{de2}"
                      + (f" ({m2:.3f} μόρια)" if m2 is not None and pd.notna(m2) else ""))
                all_upgrade_rows.append({
                    "ΕΤΟΣ": year,
                    "ΕΠΩΝΥΜΟ": p.get("ΕΠΩΝΥΜΟ", ""), "ΟΝΟΜΑ": p.get("ΟΝΟΜΑ", ""),
                    "ΠΑΤΡΩΝΥΜΟ": p.get("ΠΑΤΡΩΝΥΜΟ", ""),
                    "ΦΑΣΗ_ΜΕΙΩΜΕΝΟΥ": f"{ph_from}΄", "ΠΕΡΙΟΧΗ_ΜΕΙΩΜΕΝΟΥ": p.get("ΠΕΡΙΟΧΗ", ""),
                    "ΒΑΘΜΙΔΑ_ΜΕΙΩΜΕΝΟΥ": p.get("ΒΑΘΜΙΔΑ", ""), "ΜΟΡΙΑ_ΜΕΙΩΜΕΝΟΥ": p.get("ΜΟΡΙΑ", ""),
                    "ΦΑΣΗ_ΠΛΗΡΟΥΣ": f"{ph_to}΄", "ΠΕΡΙΟΧΗ_ΠΛΗΡΟΥΣ": p2.get("ΠΕΡΙΟΧΗ", ""),
                    "ΒΑΘΜΙΔΑ_ΠΛΗΡΟΥΣ": p2.get("ΒΑΘΜΙΔΑ", ""), "ΜΟΡΙΑ_ΠΛΗΡΟΥΣ": p2.get("ΜΟΡΙΑ", ""),
                })
        if not found_any:
            print("\n   (καμία αναβάθμιση ΜΕΙΩΜΕΝΟ → ΠΛΗΡΕΣ βρέθηκε ανάμεσα σε διαδοχικές φάσεις"
                  " αυτού του έτους)")

    if all_meiomena_rows:
        n_de_total = sum(1 for r in all_meiomena_rows if r.get("ΒΑΘΜΙΔΑ") == "Δ.Ε.")
        print(f"\n📊 Σύνολο ΜΕΙΩΜΕΝΟ (όλα τα έτη): {len(all_meiomena_rows)}"
              f" — {n_de_total} σε θέσεις (Δ.Ε.), {len(all_meiomena_rows) - n_de_total} σε λοιπές.")
    if all_upgrade_rows:
        n_de_from = sum(1 for r in all_upgrade_rows if r.get("ΒΑΘΜΙΔΑ_ΜΕΙΩΜΕΝΟΥ") == "Δ.Ε.")
        n_de_to = sum(1 for r in all_upgrade_rows if r.get("ΒΑΘΜΙΔΑ_ΠΛΗΡΟΥΣ") == "Δ.Ε.")
        print(f"📊 Σύνολο αναβαθμίσεων: {len(all_upgrade_rows)}"
              f" — {n_de_from} ξεκίνησαν από θέση (Δ.Ε.), {n_de_to} κατέληξαν σε θέση (Δ.Ε.).")

    if all_meiomena_rows or all_upgrade_rows:
        tag = klados.replace(".", "_")
        rtag = ("_" + norm_key(region).replace(" ", "_")) if region else ""
        out = CFG.output_dir() / f"ΑΝΑΒΑΘΜΙΣΕΙΣ_{tag}{rtag}.xlsx"
        with pd.ExcelWriter(out, engine="openpyxl") as xl:
            if all_meiomena_rows:
                pd.DataFrame(all_meiomena_rows).to_excel(xl, sheet_name="ΜΕΙΩΜΕΝΑ", index=False)
            if all_upgrade_rows:
                pd.DataFrame(all_upgrade_rows).to_excel(xl, sheet_name="ΑΝΑΒΑΘΜΙΣΕΙΣ", index=False)
        n_sheets = (1 if all_meiomena_rows else 0) + (1 if all_upgrade_rows else 0)
        print(f"\n💾 Αποθηκεύτηκε ({n_sheets} φύλλα): {out}")
    else:
        print("\n(Δεν υπήρχαν δεδομένα προς αποθήκευση.)")
    return 0


def _load_current_pinakas(klados: str, subcodes: bool) -> pd.DataFrame:
    """Φορτώνει τον τρέχοντα πίνακα κατάταξης ενός κλάδου (κενό αν δεν βρεθεί)."""
    pinakes = _files_of(CFG.pinakes_dir(), "base")
    rx = klados_regex(klados, subcodes)
    hit_files = [f for f in pinakes if rx.search(norm_code_text(f.stem))]
    if not hit_files:
        return pd.DataFrame()
    tabs = []
    for f in hit_files:
        tabs.extend(load_path(f))
    if not tabs:
        return pd.DataFrame()
    dfk, _ = filter_klados(concat(tabs), klados, subcodes)
    return dfk

# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Ανάλυση πινάκων αναπληρωτών")
    p.set_defaults(func=cmd_menu)
    sub = p.add_subparsers(dest="cmd")

    i = sub.add_parser("inspect", help="Τι διαβάζει το πρόγραμμα από κάθε αρχείο")
    i.add_argument("--path", default=str(CFG.DATA_DIR))
    i.set_defaults(func=cmd_inspect)

    s = sub.add_parser("summary", help="Πλήθος + μόρια 1ου/τελευταίου ανά κλάδο")
    s.add_argument("--base", default=str(CFG.pinakes_dir()),
                   help=f"Αρχείο ή φάκελος με τον πίνακα (default: {CFG.pinakes_dir()})")
    s.add_argument("--klados", required=True, help="π.χ. ΠΕ70")
    s.add_argument("--subcodes", action="store_true", help="Να περιλαμβάνει π.χ. ΠΕ11.01")
    s.set_defaults(func=cmd_summary)

    f = sub.add_parser("full", help="Πλήρης ανάλυση + αρχείο επαλήθευσης")
    f.add_argument("--base", default=str(CFG.pinakes_dir()))
    f.add_argument("--klados", required=True)
    f.add_argument("--monimoi", default=str(CFG.monimoi_dir()),
                   help="Αρχείο/φάκελος με τους διορισμούς/μόνιμους")
    f.add_argument("--no-monimoi", dest="monimoi", action="store_const", const=None,
                   help="Να μη γίνει αφαίρεση μονίμων")
    f.add_argument("--hires", help="Αρχείο/φάκελος προσλήψεων (αν οι περιοχές είναι εκεί)")
    f.add_argument("--region", help="Περιοχή διορισμού, π.χ. Θεσσαλονίκης")
    f.add_argument("--subcodes", action="store_true")
    f.add_argument("--top-regions", type=int, default=25)
    f.add_argument("--exclude-year", help="Σχολικό έτος (π.χ. 2025-2026): αφαιρεί όσους έχουν "
                   "ήδη προσληφθεί σε φάσεις αυτού του έτους (φάκελος 'faseis')")
    f.set_defaults(func=cmd_full)

    ph = sub.add_parser("phase", help="Πόσα μόρια χρειάστηκε ο τελευταίος σε μια φάση/έτος")
    ph.add_argument("--klados")
    ph.add_argument("--region")
    ph.add_argument("--file", help="Συγκεκριμένο αρχείο φάσης (αλλιώς διαλέγεις από λίστα)")
    ph.add_argument("--year", help="Σχολικό έτος, π.χ. 2024-2025 (ενώνει όλες τις φάσεις του)")
    ph.add_argument("--subcodes", action="store_true")
    ph.set_defaults(func=cmd_phase)

    pr = sub.add_parser("predict", help="Πρόβλεψη περιοχής/φάσης με βάση ιστορικά φάσεων")
    pr.add_argument("--klados")
    pr.add_argument("--region")
    pr.add_argument("--moria", type=float, help="Τα μόριά σου (αν δεν θες αναζήτηση με όνομα)")
    pr.add_argument("--name", help="Επώνυμο Όνομα για αυτόματη αναζήτηση στον πίνακα")
    pr.add_argument("--subcodes", action="store_true")
    pr.set_defaults(func=cmd_predict)

    up = sub.add_parser("upgrades",
                        help="Ποιοι προσλήφθηκαν ΜΕΙΩΜΕΝΟ και αναβαθμίστηκαν σε ΠΛΗΡΕΣ στην επόμενη φάση")
    up.add_argument("--klados")
    up.add_argument("--region", help="Προαιρετικό φίλτρο περιοχής (αλλιώς όλες οι περιοχές)")
    up.add_argument("--year", help="Σχολικό έτος, π.χ. 2024-2025 (αλλιώς όλα τα διαθέσιμα έτη)")
    up.add_argument("--subcodes", action="store_true")
    up.set_defaults(func=cmd_upgrades)
    return p


def _run():
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 40)
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    import datetime
    import traceback
    try:
        sys.exit(_run())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n\nΔιακόπηκε από τον χρήστη.")
        sys.exit(1)
    except Exception:                                              # noqa: BLE001
        # Το πρόγραμμα δεν πρέπει ΠΟΤΕ να τερματίζει με ωμό traceback: γράφουμε
        # την πλήρη λεπτομέρεια σε αρχείο και δείχνουμε στον χρήστη κατανοητό μήνυμα.
        try:
            CFG.output_dir()  # βεβαιώσου ότι υπάρχει
            log_path = CFG.output_dir() / "ERROR_LOG.txt"
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n----- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} -----\n")
                fh.write(f"Εντολή: {' '.join(sys.argv)}\n")
                fh.write(traceback.format_exc())
        except Exception:                                           # noqa: BLE001
            log_path = None
        print("\n" + "=" * 78)
        print("❌ Παρουσιάστηκε απρόβλεπτο σφάλμα και το πρόγραμμα σταμάτησε.")
        print(f"   {traceback.format_exc().strip().splitlines()[-1]}")
        if log_path:
            print(f"   Πλήρης λεπτομέρεια αποθηκεύτηκε στο: {log_path}")
        print("   Πιθανές αιτίες: κατεστραμμένο/κλειδωμένο αρχείο excel, ή αρχείο")
        print("   ανοιχτό στο Excel αυτή τη στιγμή. Τρέξε πρώτα `python main.py inspect`.")
        print("=" * 78)
        sys.exit(1)
