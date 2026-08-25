# -*- coding: utf-8 -*-
"""
Κανονικοποίηση κειμένου και αριθμών.

Γιατί υπάρχει: τα excel του υπουργείου έχουν τόνους, διπλά κενά, λατινικά
γράμματα που μοιάζουν με ελληνικά (A αντί για Α), κόμματα αντί για τελείες.
Χωρίς αυστηρή κανονικοποίηση οι διασταυρώσεις ονομάτων βγάζουν λάθος νούμερα.
"""
from __future__ import annotations

import math
import re
import unicodedata

# Λατινικά που μοιάζουν οπτικά με ελληνικά -> ελληνικά
LATIN_TO_GREEK = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "Z": "Ζ", "H": "Η", "I": "Ι", "K": "Κ",
    "M": "Μ", "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "Y": "Υ", "X": "Χ",
})

_EMPTY = {"", "NAN", "NONE", "NAT", "-", "--", "ΝΑΝ"}


def strip_accents(text) -> str:
    """Αφαιρεί τόνους/διαλυτικά."""
    if text is None:
        return ""
    nfkd = unicodedata.normalize("NFD", str(text))
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def norm_text(value) -> str:
    """ΚΕΦΑΛΑΙΑ, χωρίς τόνους, χωρίς διπλά κενά. Για επικεφαλίδες & γενική χρήση."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    s = strip_accents(value).upper().replace("\n", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s in _EMPTY else s


def norm_key(value) -> str:
    """
    Πιο επιθετική κανονικοποίηση, ΜΟΝΟ για κλειδιά ταυτοποίησης.
    Μετατρέπει λατινικά ομοιογραφικά σε ελληνικά και πετάει σημεία στίξης.
    """
    s = norm_text(value)
    if not s:
        return ""
    s = s.translate(LATIN_TO_GREEK)
    s = re.sub(r"[^Α-ΩA-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_digits(value) -> str:
    """Κρατά μόνο ψηφία (για ΑΦΜ / ΑΜ)."""
    s = re.sub(r"\D", "", str(value or ""))
    return s.lstrip("0") if s else ""


def to_float(value) -> float:
    """
    Μετατροπή σε αριθμό με ανοχή στα ελληνικά formats:
    '12,5' -> 12.5   |   '1.234,50' -> 1234.5   |   '1,234.50' -> 1234.5
    Επιστρέφει nan αν δεν διαβάζεται (ΔΕΝ γίνεται σιωπηλά 0).
    """
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float("nan") if (isinstance(value, float) and math.isnan(value)) else float(value)
    s = str(value).strip()
    if not s or s.upper() in _EMPTY:
        return float("nan")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return float("nan")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # 1.234,50
            s = s.replace(".", "").replace(",", ".")
        else:                                 # 1,234.50
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def norm_code_text(value) -> str:
    """
    Κανονικοποίηση για αναζήτηση κωδικού κλάδου: κρατά τις τελείες (ΠΕ04.01) και
    μετατρέπει κάθε άλλο σημείο (_ , - , /) σε κενό, ώστε το 'ΠΕ01' μέσα σε
    '1_ΚΑΤ_ΠΕ01_ΘΕΟΛΟΓΟΙ.xlsx' να αναγνωρίζεται ως αυτοτελής κωδικός.
    """
    s = norm_text(value).translate(LATIN_TO_GREEK)
    s = re.sub(r"[^Α-ΩA-Z0-9. ]", " ", s)
    s = re.sub(r"\b(ΠΕ|ΤΕ|ΔΕ)\s+(\d)", r"\1\2", s)   # 'ΠΕ 70' -> 'ΠΕ70'
    return re.sub(r"\s+", " ", s).strip()


def klados_regex(code: str, include_subcodes: bool = False) -> re.Pattern:
    """
    Φτιάχνει regex που πιάνει ΜΟΝΟ τον συγκεκριμένο κλάδο.
    Το 'ΠΕ7' ΔΕΝ πιάνει 'ΠΕ70'. Το 'ΠΕ11' πιάνει 'ΠΕ11.01' μόνο με include_subcodes.
    """
    c = norm_code_text(code).replace(" ", "")
    tail = r"(?:\.\d+)?" if include_subcodes else ""
    return re.compile(rf"(?<![Α-Ω0-9.]){re.escape(c)}{tail}(?![\d.])")


def extract_klados_codes(text: str) -> list[str]:
    """Βρίσκει κωδικούς τύπου ΠΕ70, ΤΕ01.20, ΔΕ01 μέσα σε ένα string."""
    return re.findall(r"(?:ΠΕ|ΤΕ|ΔΕ)\d+(?:\.\d+)?", norm_code_text(text))


_PHASE_LETTERS = ["Α", "Β", "Γ", "Δ", "Ε", "ΣΤ", "Ζ", "Η"]
_ORDINAL_TO_LETTER = {str(i + 1): L for i, L in enumerate(_PHASE_LETTERS)}
_ENGLISH_TO_LETTER = {c: L for c, L in zip("ABCDEFGH", _PHASE_LETTERS)}


def extract_phase_year(text) -> tuple[str | None, str | None]:
    """
    Βρίσκει φάση (Α, Β, Γ…) και σχολικό έτος (2024-2025) μέσα σε ένα
    όνομα αρχείου ή διαδρομή φακέλων, ανεξάρτητα από τη σύμβαση ονομασίας:
    'Α ΦΑΣΗ 2024-2025', '1η ΦΑΣΗ 2024_2025', 'a_fash/2024-25', κ.λπ.
    Επιστρέφει (None, None) για ό,τι δεν αναγνωρίζεται — δεν σκάει ποτέ.
    """
    # norm_text (ΟΧΙ norm_key): πρέπει να κρατήσει τα -/_ ώστε να διαβαστεί
    # σωστά το εύρος έτους '2024-2025', που το norm_key θα το κατέστρεφε.
    s = norm_text(text)

    year = None
    m = re.search(r"20\d{2}(?:[-_/]\s?(20)?\d{2})?", s)
    if m:
        raw = re.sub(r"[\s_/]+", "-", m.group(0))
        if "-" in raw:
            a, b = raw.split("-", 1)
            if len(b) == 2:
                raw = f"{a}-{a[:2]}{b}"
        year = raw

    # Οι κάτω παύλες/ενωτικά είναι \w χαρακτήρες, οπότε "_Α_ΦΑΣΗ_" δεν έχει
    # πραγματικό όριο λέξης γύρω από "Α" — για την αναγνώριση φάσης τα
    # αντικαθιστούμε με κενό (μόνο εδώ, το έτος έχει ήδη διαβαστεί από το s).
    sp = re.sub(r"[_\-/]+", " ", s)

    phase = None
    m = re.search(r"\b(ΣΤ|Α|Β|Γ|Δ|Ε|Ζ|Η)['΄]?\s*ΦΑΣΗ\b", sp)
    if m:
        phase = m.group(1)
    else:
        m = re.search(r"\b([1-8])[ΗΟΣ]{0,2}\s*ΦΑΣΗ\b", sp)
        if m:
            phase = _ORDINAL_TO_LETTER.get(m.group(1))
        else:
            m = re.search(r"\b([A-H])\s?FASH\b", sp)
            if m:
                phase = _ENGLISH_TO_LETTER.get(m.group(1))
    return phase, year


def classify_orario(periochi_text, orario_value=None) -> str:
    """
    Ταξινομεί μια εγγραφή πρόσληψης σε 'ΠΛΗΡΕΣ' ή 'ΜΕΙΩΜΕΝΟ' ωράριο.
    Ελέγχει πρώτα ρητή στήλη ΩΡΑΡΙΟ (ΑΠΩ=πλήρες, ΑΜΩ=μειωμένο) αν υπάρχει,
    αλλιώς ψάχνει λέξη-κλειδί μέσα στο ίδιο το κείμενο της περιοχής — οι
    πραγματικοί πίνακες φάσεων συχνά γράφουν κατευθείαν μέσα στην ΠΕΡΙΟΧΗ
    κάτι σαν 'ΠΕΛΛΑΣ (Π.Ε.) - ΜΕΙΩΜΕΝΟΥ ΩΡΑΡΙΟΥ'.
    Σύμβαση: αν δεν βρεθεί καμία ένδειξη μειωμένου, θεωρείται ΠΛΗΡΕΣ — έτσι
    δηλώνονται οι θέσεις στα πραγματικά αρχεία (η εξαίρεση επισημαίνεται,
    ο κανόνας όχι).
    """
    ov = norm_key(orario_value) if orario_value else ""
    if ov in ("ΑΠΩ", "ΠΛΗΡΕΣ", "ΠΛΗΡΟΥΣ", "FULL", "FULLTIME"):
        return "ΠΛΗΡΕΣ"
    if ov in ("ΑΜΩ", "ΜΕΙΩΜΕΝΟ", "ΜΕΙΩΜΕΝΟΥ", "PART", "PARTTIME"):
        return "ΜΕΙΩΜΕΝΟ"
    pv = norm_key(periochi_text)
    if "ΜΕΙΩΜΕΝ" in pv:
        return "ΜΕΙΩΜΕΝΟ"
    return "ΠΛΗΡΕΣ"


def region_matches(value, wanted: str) -> bool:
    """
    Σύγκριση περιοχής με ανοχή σε πτώσεις:
    'ΘΕΣΣΑΛΟΝΙΚΗΣ Α' matches 'Θεσσαλονίκη'.
    Κόβει τις 3 τελευταίες καταλήξεις για να πιάσει ΗΣ/Η/ΑΣ/Α/ΟΥ/Ο.
    """
    v = norm_key(value)
    w = norm_key(wanted)
    if not v or not w:
        return False
    stem = w[:-2] if len(w) > 5 else w
    return stem in v