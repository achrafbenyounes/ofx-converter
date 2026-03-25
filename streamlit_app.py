"""
OFX Converter Pro — Multi-banques France → Odoo
================================================
Compatible (relevés texte ET scannés / Print-to-PDF) :
  La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale
  CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire
  Qonto · Boursorama / BoursoBank · Revolut · Hello Bank · N26 · Fortuneo…

OCR : Google Cloud Vision API (pas Tesseract)
  → Configurer GCV_API_KEY dans .streamlit/secrets.toml

Stratégies d'extraction :
  1. Parser colonne  (PDF texte natif)  — lit les coordonnées x/y via pdfplumber
  2. Parser texte    (Qonto + fallback) — regex sur blocs DD/MM … montant EUR
  3. OCR Google Vision — pour PDF scannés ou "Print to PDF" sans polices

Format OFX généré :
  OFX 1.x SGML, compatible toutes versions Odoo (14-17+)
  Montants : X.XX (point décimal, JAMAIS virgule, 2 décimales)

─────────────────────────────────────────────────────────────────
requirements.txt :
  streamlit
  pdfplumber
  pandas
  requests
  Pillow
  PyMuPDF          ← OU pdf2image  (rasterisation fallback)

packages.txt (Streamlit Cloud) — si PyMuPDF absent :
  poppler-utils    ← nécessaire pour pdftoppm / pdf2image
─────────────────────────────────────────────────────────────────
"""

import base64
import hashlib
import io
import os
import re
import subprocess
import tempfile
import unicodedata
from datetime import datetime

import pandas as pd
import pdfplumber
import requests
import streamlit as st

st.set_page_config(page_title="OFX Converter Pro", layout="wide")


# =========================================================
# UTILS GÉNÉRAUX
# =========================================================

def sanitize_filename(name: str) -> str:
    """
    Normalise un nom de fichier : supprime accents, espaces → tirets bas,
    retire les caractères non-portables.
    Ex : "BQ QONTO.pdf" → "BQ_QONTO.pdf"
    """
    # Supprimer les accents (NFD → ASCII)
    name = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode("ASCII")
    # Conserver uniquement alphanum, tirets, underscores, points
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^\w\-]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return f"{base}{ext.lower()}"


def clean_amount(text: str) -> float | None:
    """
    Convertit n'importe quel montant français ou anglais en float.
    Gère : espaces / NBSP comme séparateurs de milliers, virgule OU point décimal,
           préfixes +/-/€/¤, suffixe EUR.
    Ex : "1 702,68" → 1702.68 | "+ 46 728,51 ¤" → 46728.51 | "2041.05" → 2041.05
    """
    if not text:
        return None
    text = str(text).replace("\xa0", "").replace(" ", "")
    text = re.sub(r"[€$£¤▶►▸*EUReur]", "", text)
    text = text.replace("+", "")
    text = text.replace(",", ".")
    text = re.sub(r"[^\d.\-]", "", text)
    # Plusieurs points → garder seulement le dernier (ex: "03.02.26")
    parts = text.split(".")
    if len(parts) > 2:
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        v = float(text)
        return round(v, 2) if v != 0 else None
    except Exception:
        return None


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    return re.sub(r"\n+", "\n", text)


def is_amount_word(s: str) -> bool:
    """
    Vrai si la chaîne ressemble à NNN,NN ou NNN.NN (montant décimal bancaire).
    Ex : "6.00", "702,68", "2041.05"
    """
    return bool(re.match(r"^\d{1,4}[,\.]\d{2}$", s.strip()))


def is_leading_digit(s: str) -> bool:
    """
    Vrai si la chaîne est 1 ou 2 chiffres isolés (milliers BP).
    Ex : "1" dans "1" + "702,68" → 1 702,68
    """
    return bool(re.match(r"^\d{1,2}$", s.strip()))


# =========================================================
# DÉTECTION BANQUE
# =========================================================

def detect_bank(text: str) -> str:
    t = text.upper()
    if "BANQUE POSTALE" in t or "LABANQUEPOSTALE" in t:
        return "BANQUE_POSTALE"
    if "QONTO" in t:
        return "QONTO"
    if "BNP PARIBAS" in t:
        return "BNP_PARIBAS"
    if "CREDIT AGRICOLE" in t or "CRÉDIT AGRICOLE" in t:
        return "CREDIT_AGRICOLE"
    if "SOCIETE GENERALE" in t or "SOCIÉTÉ GÉNÉRALE" in t:
        return "SOCIETE_GENERALE"
    if "CREDIT MUTUEL" in t or "CRÉDIT MUTUEL" in t:
        return "CREDIT_MUTUEL"
    if "CIC " in t and "BANQUE" in t:
        return "CIC"
    if re.search(r"\bLCL\b", t) and ("BANQUE" in t or "RELEVÉ" in t or "RELEVE" in t):
        return "LCL"
    if "CAISSE D'EPARGNE" in t or "CAISSE EPARGNE" in t or "CAISSEEPARGNE" in t:
        return "CAISSE_EPARGNE"
    if "BANQUE POPULAIRE" in t:
        return "BANQUE_POPULAIRE"
    if "BOURSORAMA" in t or "BOURSOBANK" in t:
        return "BOURSORAMA"
    if "REVOLUT" in t:
        return "REVOLUT"
    if "HELLO BANK" in t:
        return "HELLO_BANK"
    if "FORTUNEO" in t:
        return "FORTUNEO"
    if "ORANGE BANK" in t:
        return "ORANGE_BANK"
    if "N26" in t:
        return "N26"
    if "SHINE" in t:
        return "SHINE"
    if "SUMERIA" in t or "LYDIA" in t:
        return "SUMERIA"
    return "GENERIC"


# =========================================================
# IBAN / BIC
# =========================================================

def extract_iban_bic(text: str) -> tuple[str | None, str | None]:
    iban, bic = None, None

    # IBAN France compact ou espacé (ex : FR76 1695 8000 0178 9513 0261 894)
    m = re.search(
        r"\bFR\d{2}"
        r"(?:\s*[0-9A-Z]{4}){5}"
        r"\s*[0-9A-Z]{1,3}\b",
        text,
    )
    if m:
        candidate = re.sub(r"\s+", "", m.group())
        if candidate.startswith("FR"):
            candidate = candidate[:27]
        if 14 <= len(candidate) <= 34:
            iban = candidate

    # BIC (SWIFT) : 4 lettres + FR + 2 alphanum + 3 alphanum optionnels
    m = re.search(r"\b[A-Z]{4}FR[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", text)
    if m:
        bic = m.group()

    return iban, bic


# =========================================================
# SOLDES D'OUVERTURE / CLÔTURE — MULTI-BANQUES
# =========================================================

_AMT = r"([\+\-]?\s*\d[\d\s\xa0]*[,\.]\d{2})"

_OPENING_PATTERNS = [
    # ── La Banque Postale : montant sur la ligne AVANT le label ──
    r"(\d[\d\s\xa0]*,\d{2})\s*\n[Aa]ncien\s+solde",
    r"(\d[\d\s\xa0]*,\d{2})\s+[Aa]ncien\s+solde",
    # ── Banque Postale / générique ──
    r"[Aa]ncien\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Aa]ncien\s+solde\s*:?\s*" + _AMT,
    # ── Qonto : Solde au DD/MM — premier match = ouverture (même ligne ou ligne suivante) ──
    r"[Ss]olde\s+au\s+\d{2}/\d{2}\s+" + _AMT,
    r"[Ss]olde\s+au\s+\d{2}/\d{2}\s*\n\s*" + _AMT,
    # ── Générique ──
    r"[Ss]olde\s+initial\s*:?\s*" + _AMT,
    r"[Ss]olde\s+pr[ée]c[ée]dent\s*:?\s*" + _AMT,
    r"[Ss]olde\s+ant[ée]rieur\s*:?\s*" + _AMT,
    r"[Ss]olde\s+d['']ouverture\s*:?\s*" + _AMT,
    r"[Rr]eport\s+[àa]\s+nouveau\s*:?\s*" + _AMT,
    r"[Rr]eport\s+du\s+mois\s+pr[ée]c[ée]dent\s*:?\s*" + _AMT,
    r"[Ss]olde\s+(?:cr[ée]diteur|d[ée]biteur)\s+au\s+\d{2}[\.\/\-]\d{2}[\.\/\-]\d{4}\s*" + _AMT,
    r"[Ss]olde\s+au\s+\d{2}[\.\/\-]\d{2}[\.\/\-]\d{4}\s*:?\s*" + _AMT,
    r"[Ss]olde\s+comptable\s+au\s+\d{2}[\.\/\-]\d{2}[\.\/\-]\d{4}\s*:?\s*" + _AMT,
    # ── Crédit Agricole / LCL ──
    r"SOLDE\s+ANTERIEUR\s+" + _AMT,
    r"SOLDE\s+PRECEDENT\s+" + _AMT,
    # ── BNP / Société Générale ──
    r"[Vv]otre\s+solde\s+au\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"[Bb]alance?\s+(?:pr[ée]c[ée]dente|initiale|d['']ouverture)\s*:?\s*" + _AMT,
    # ── Caisse d'Épargne / Banque Populaire ──
    r"[Ss]olde\s+[àa]\s+la\s+date\s+du\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"SOLDE\s+EN\s+DEBUT\s+DE\s+PERIODE\s*:?\s*" + _AMT,
    # ── Boursorama / N26 (anglais) ──
    r"[Oo]pening\s+[Bb]alance\s*:?\s*" + _AMT,
]

_CLOSING_PATTERNS = [
    r"[Nn]ouveau\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Nn]ouveau\s+solde\s*:?\s*" + _AMT,
    r"[Ss]olde\s+final\s*:?\s*" + _AMT,
    r"[Ss]olde\s+[àa]\s+la\s+cl[ôo]ture\s*:?\s*" + _AMT,
    r"SOLDE\s+FINAL\s+" + _AMT,
    r"SOLDE\s+EN\s+FIN\s+DE\s+P[EÉ]RIODE\s+" + _AMT,
    r"[Cc]losing\s+[Bb]alance\s*:?\s*" + _AMT,
]


def extract_opening_balance(text: str) -> float | None:
    for pattern in _OPENING_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val
    # Fallback : dernier montant avant la 1ère transaction
    first_tx = re.search(r"\d{2}/\d{2}", text)
    if first_tx:
        before = text[: first_tx.start()]
        amounts = re.findall(r"\d[\d\s\xa0]*[,\.]\d{2}", before)
        if amounts:
            val = clean_amount(amounts[-1])
            if val is not None:
                return val
    return None


def extract_closing_balance(text: str) -> float | None:
    # Qonto : findall → prendre le DERNIER "Solde au DD/MM"
    qonto_patterns = [
        r"[Ss]olde\s+au\s+\d{2}/\d{2}\s+" + _AMT,
        r"[Ss]olde\s+au\s+\d{2}/\d{2}\s*\n\s*" + _AMT,
    ]
    for pat in qonto_patterns:
        matches = re.findall(pat, text, re.IGNORECASE | re.MULTILINE)
        if len(matches) >= 2:
            val = clean_amount(matches[-1])
            if val is not None:
                return val

    for pattern in _CLOSING_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val
    return None


# =========================================================
# PARSER TEXTE UNIVERSEL — Qonto + fallback OCR
# =========================================================

def parse_transactions_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Parser texte universel compatible Qonto et résultat OCR.
    Détecte les blocs commençant par DD/MM et extrait [+/-] montant EUR.
    """
    text = clean_text(text)

    # Stopper aux pieds de page / totaux
    for stop_pat in [
        r"[Tt]outes\s+les\s+cartes",
        r"[Tt]otal\s+des\s+op[ée]rations",
        r"Qonto\s+SA",
        r"Qonto,\s+une\s+marque",
        r"OLINDA\s+SAS",
    ]:
        m = re.search(stop_pat, text)
        if m:
            text = text[: m.start()]

    # Année : chercher dans le texte ou utiliser l'année courante
    if year is None:
        ym = re.search(r"20\d{2}", text)
        year = ym.group(0) if ym else str(datetime.now().year)

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    transactions: list[dict] = []
    block: list[str] = []

    def process_block(blines: list[str]) -> dict | None:
        if not blines:
            return None
        full = " ".join(blines)

        dm = re.match(r"(\d{2}/\d{2})\b", full)
        if not dm:
            return None
        date_str = dm.group(1)

        # Chercher [+/-] montant EUR (format Qonto / générique)
        m = re.search(
            r"([\+\-])\s*([\d\s\xa0]*[\d,\.]+\d{2})\s*EUR",
            full,
            re.IGNORECASE,
        )
        if m:
            sign = m.group(1)
            amount = clean_amount(m.group(2))
            if amount is None:
                return None
            amount = -abs(amount) if sign == "-" else abs(amount)
            libelle = full[: m.start()].strip()
            libelle = re.sub(r"^\d{2}/\d{2}\s*", "", libelle).strip()
            libelle = re.sub(r"\s{2,}", " ", libelle)[:120]
            return {
                "date": f"{date_str}/{year}",
                "libelle": libelle,
                "montant": round(amount, 2),
            }

        # Fallback : n'importe quel montant dans le bloc
        raw_amounts = re.findall(r"\d[\d\s\xa0]*[,\.]\d{2}", full)
        amounts = [v for a in raw_amounts if (v := clean_amount(a)) is not None]
        if not amounts:
            return None
        amount = abs(amounts[-1])
        libelle = re.sub(r"^\d{2}/\d{2}\s*", "", full).strip()[:120]
        return {
            "date": f"{date_str}/{year}",
            "libelle": libelle,
            "montant": round(amount, 2),
        }

    for line in lines:
        if re.match(r"^\d{2}/\d{2}\b", line):
            if block:
                tx = process_block(block)
                if tx:
                    transactions.append(tx)
            block = [line]
        elif block:
            block.append(line)

    if block:
        tx = process_block(block)
        if tx:
            transactions.append(tx)

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


# =========================================================
# PARSER COLONNE — PDF texte structuré natif
# =========================================================

def _detect_columns(words: list[dict]) -> tuple[float, float, float, float]:
    """Détecte les positions x des colonnes Débit / Crédit."""
    debit_x, credit_x = None, None
    for w in words:
        txt = w["text"].upper()
        if re.search(r"D[EÉ]BIT", txt) and w["x0"] > 350:
            debit_x = w["x0"]
        if re.search(r"CR[EÉ]DIT", txt) and w["x0"] > 430:
            credit_x = w["x0"]
    if debit_x is None:
        debit_x = 437.0
    if credit_x is None:
        credit_x = 506.0
    boundary = (debit_x + credit_x) / 2
    desc_end_x = debit_x - 5
    return desc_end_x, debit_x, credit_x, boundary


def _group_by_y(words: list[dict], y_tol: float = 2.0) -> dict[float, list[dict]]:
    """Groupe les mots par ligne (tolérance verticale ±2pt)."""
    lines: dict[float, list[dict]] = {}
    for w in words:
        y = round(w["top"] / y_tol) * y_tol
        lines.setdefault(y, []).append(w)
    return lines


def _extract_amount_from_zone(
    line_words: list[dict], col_start: float, col_end: float
) -> float | None:
    """
    Extrait un montant de la zone [col_start, col_end].
    Gère montants fractionnés La Banque Postale : "1" + "702,68" → 1702.68
    """
    zone = sorted(
        [w for w in line_words if col_start - 5 <= w["x0"] <= col_end + 5],
        key=lambda w: w["x0"],
    )
    for i, w in enumerate(zone):
        if is_amount_word(w["text"]):
            base = clean_amount(w["text"])
            if base is None:
                continue
            if i > 0 and is_leading_digit(zone[i - 1]["text"]):
                prefix = int(zone[i - 1]["text"])
                return round(prefix * 1000 + base, 2)
            return round(base, 2)
    return None


def parse_transactions_by_column(pdf_file) -> pd.DataFrame:
    """
    Parser colonne — coordonnées x/y pdfplumber.
    Compatible La Banque Postale, PDF texte natif structuré.
    """
    year = str(datetime.now().year)
    transactions: list[dict] = []

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
            if not words:
                continue

            desc_end_x, debit_x, credit_x, boundary = _detect_columns(words)

            page_text = page.extract_text() or ""
            ym = re.search(r"(20\d{2})", page_text)
            if ym:
                year = ym.group(1)

            lines_by_y = _group_by_y(words)
            current_date: str | None = None
            current_desc: list[str] = []
            current_debit: float | None = None
            current_credit: float | None = None

            def flush() -> None:
                nonlocal current_date, current_desc, current_debit, current_credit
                if current_date is None:
                    return
                if current_debit is not None and current_debit > 0:
                    amount = -abs(current_debit)
                elif current_credit is not None and current_credit > 0:
                    amount = abs(current_credit)
                else:
                    current_date = None
                    current_desc = []
                    current_debit = None
                    current_credit = None
                    return
                libelle = re.sub(r"\s{2,}", " ", " ".join(current_desc)).strip()[:120]
                transactions.append(
                    {
                        "date": f"{current_date}/{year}",
                        "libelle": libelle,
                        "montant": round(amount, 2),
                    }
                )
                current_date = None
                current_desc = []
                current_debit = None
                current_credit = None

            for y in sorted(lines_by_y.keys()):
                line_words = sorted(lines_by_y[y], key=lambda w: w["x0"])
                date_candidates = [
                    w
                    for w in line_words
                    if re.match(r"^\d{2}/\d{2}$", w["text"]) and w["x0"] < 80
                ]
                if date_candidates:
                    flush()
                    current_date = date_candidates[0]["text"]
                    current_debit = _extract_amount_from_zone(
                        line_words, debit_x - 35, boundary
                    )
                    current_credit = _extract_amount_from_zone(
                        line_words, boundary, 630
                    )
                    current_desc = [
                        w["text"] for w in line_words if 85 <= w["x0"] < desc_end_x
                    ]
                elif current_date is not None:
                    current_desc.extend(
                        w["text"] for w in line_words if 85 <= w["x0"] < desc_end_x
                    )

            flush()

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


# =========================================================
# DÉTECTION : PDF texte natif ou image/scanné ?
# =========================================================

def _pdf_has_text(pdf_bytes: bytes) -> bool:
    """
    Retourne True si le PDF contient du texte extractible (> seuil minimal).
    False pour les PDF scannés ET les PDF 'Print to PDF' sans polices incorporées.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_chars = 0
            for page in pdf.pages:
                chars = page.chars
                total_chars += len(chars)
                if total_chars > 30:
                    return True
        return False
    except Exception:
        return False


# =========================================================
# RASTERISATION PDF → IMAGES PIL
# =========================================================

def _try_pymupdf(pdf_bytes: bytes) -> list | None:
    """
    Rasterise via PyMuPDF (pure Python, aucune dépendance système).
    Essaie les deux noms d'import : 'pymupdf' (≥1.24) puis 'fitz' (<1.24).
    Retourne une liste d'images PIL ou None si PyMuPDF n'est pas installé.
    """
    from PIL import Image

    fitz_mod = None
    for mod_name in ("pymupdf", "fitz"):
        try:
            import importlib
            fitz_mod = importlib.import_module(mod_name)
            break
        except ImportError:
            continue

    if fitz_mod is None:
        return None  # PyMuPDF non installé — essayer la méthode suivante

    try:
        doc = fitz_mod.open(stream=pdf_bytes, filetype="pdf")
        images = []
        for page in doc:
            mat = fitz_mod.Matrix(300 / 72, 300 / 72)  # 300 DPI
            pix = page.get_pixmap(matrix=mat, colorspace=fitz_mod.csRGB)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            images.append(img.copy())
        doc.close()
        return images
    except Exception as e:
        st.warning(f"⚠️ PyMuPDF disponible mais erreur : {e}")
        return None


def _try_pdf2image(pdf_bytes: bytes) -> list | None:
    """
    Rasterise via pdf2image + poppler-utils.
    Retourne une liste d'images PIL ou None si non disponible.
    Gère explicitement PopplerNotInstalledError (poppler absent).
    """
    try:
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PopplerNotInstalledError, PDFInfoNotInstalledError
    except ImportError:
        return None  # pdf2image non installé

    try:
        return convert_from_bytes(pdf_bytes, dpi=300)
    except (PopplerNotInstalledError, PDFInfoNotInstalledError):
        # poppler-utils absent sur le système — ne pas afficher d'erreur,
        # on passera à la méthode suivante
        return None
    except Exception as e:
        st.warning(f"⚠️ pdf2image erreur : {e}")
        return None


def _try_pdftoppm(pdf_bytes: bytes) -> list | None:
    """
    Rasterise via subprocess pdftoppm (poppler-utils CLI).
    Retourne une liste d'images PIL ou None si pdftoppm absent.
    """
    import shutil
    from PIL import Image

    if not shutil.which("pdftoppm"):
        return None  # pdftoppm absent — ne pas afficher d'erreur ici

    tmp_pdf_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
            tmp_pdf.write(pdf_bytes)
            tmp_pdf_path = tmp_pdf.name

        with tempfile.TemporaryDirectory() as tmp_dir:
            prefix = os.path.join(tmp_dir, "page")
            result = subprocess.run(
                ["pdftoppm", "-png", "-r", "300", tmp_pdf_path, prefix],
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0:
                st.warning(f"⚠️ pdftoppm : {result.stderr.decode()[:200]}")
                return None

            images = []
            for fname in sorted(os.listdir(tmp_dir)):
                if fname.endswith(".png"):
                    with open(os.path.join(tmp_dir, fname), "rb") as f:
                        img = Image.open(io.BytesIO(f.read()))
                        images.append(img.copy())
            return images if images else None

    except Exception as e:
        st.warning(f"⚠️ pdftoppm erreur : {e}")
        return None
    finally:
        if tmp_pdf_path:
            try:
                os.unlink(tmp_pdf_path)
            except Exception:
                pass


def _rasterize_pdf_to_images(pdf_bytes: bytes) -> list:
    """
    Convertit chaque page du PDF en image PIL (RGB, 300 DPI).
    Cascade de méthodes — s'arrête à la première qui fonctionne :
      1. PyMuPDF  — pur Python, aucune dépendance système (recommandé)
      2. pdf2image — requiert poppler-utils installé sur le système
      3. pdftoppm  — requiert poppler-utils dans le PATH
    Si aucune méthode ne fonctionne, affiche un message d'aide clair.
    """
    # Méthode 1 : PyMuPDF (préféré — pur Python)
    images = _try_pymupdf(pdf_bytes)
    if images is not None:
        return images

    # Méthode 2 : pdf2image
    images = _try_pdf2image(pdf_bytes)
    if images is not None:
        return images

    # Méthode 3 : pdftoppm CLI
    images = _try_pdftoppm(pdf_bytes)
    if images is not None:
        return images

    # Aucune méthode disponible → message d'aide actionnable
    st.error(
        "❌ **Impossible de rasteriser le PDF** (requis pour les PDF sans texte natif).\n\n"
        "**Solution recommandée** — installez PyMuPDF (pur Python, sans dépendance système) :\n"
        "```\npip install PyMuPDF\n```\n"
        "**Streamlit Cloud** — ajoutez dans `requirements.txt` :\n"
        "```\nPyMuPDF\n```\n"
        "**Alternative** — installez poppler-utils :\n"
        "```\n# Linux / Streamlit Cloud (packages.txt)\npoppler-utils\n\n"
        "# macOS\nbrew install poppler\n\n"
        "# Windows\nchoco install poppler\n```"
    )
    return []


# =========================================================
# OCR GOOGLE CLOUD VISION API
# =========================================================

def _gcv_ocr_image(image_bytes: bytes, api_key: str) -> str:
    """
    Envoie une image PNG/JPEG à Google Cloud Vision et retourne le texte OCR.
    Utilise DOCUMENT_TEXT_DETECTION (optimal pour documents multi-colonnes).
    """
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("utf-8")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
                "imageContext": {"languageHints": ["fr", "en"]},
            }
        ]
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        # Vérifier les erreurs Google Vision
        gcv_err = data.get("responses", [{}])[0].get("error")
        if gcv_err:
            st.error(f"Google Vision API : {gcv_err.get('message', 'Erreur inconnue')}")
            return ""

        return (
            data.get("responses", [{}])[0]
            .get("fullTextAnnotation", {})
            .get("text", "")
        )
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            st.error(
                "❌ Google Vision : requête invalide (400). "
                "Vérifiez que la clé API GCV_API_KEY est correcte et activée."
            )
        elif e.response is not None and e.response.status_code == 403:
            st.error(
                "❌ Google Vision : accès refusé (403). "
                "Vérifiez les droits / quota de votre clé API."
            )
        else:
            st.error(f"❌ Google Vision HTTP {e}")
        return ""
    except Exception as e:
        st.error(f"❌ Google Vision : {e}")
        return ""


def ocr_pdf_gcv(pdf_bytes: bytes) -> str:
    """
    OCR complet d'un PDF via Google Cloud Vision API.
    Rasterise chaque page puis appelle l'API Vision.
    """
    # Récupérer la clé API depuis les secrets Streamlit
    api_key = ""
    try:
        api_key = st.secrets.get("GCV_API_KEY", "")
    except Exception:
        pass

    if not api_key:
        st.error(
            "❌ Clé **GCV_API_KEY** absente dans `.streamlit/secrets.toml`.\n\n"
            "Ajoutez : `GCV_API_KEY = \"votre-clé\"`"
        )
        return ""

    images = _rasterize_pdf_to_images(pdf_bytes)
    if not images:
        return ""

    ocr_pages: list[str] = []
    progress_bar = st.progress(0, text="🔍 OCR en cours…")

    for idx, img in enumerate(images):
        progress_bar.progress(
            (idx + 1) / len(images),
            text=f"🔍 OCR page {idx + 1}/{len(images)}…",
        )
        # Convertir en PNG bytes
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        page_text = _gcv_ocr_image(img_bytes, api_key)
        if page_text:
            ocr_pages.append(page_text)

    progress_bar.empty()

    full_text = clean_text("\n".join(ocr_pages))
    return full_text


# =========================================================
# POINT D'ENTRÉE PRINCIPAL — EXTRACTION INTELLIGENTE
# =========================================================

def extract_all(pdf_file) -> dict:
    """
    Orchestre l'extraction complète :
      1. Lit le PDF en bytes (pour accès multiple)
      2. Détecte si le PDF contient du texte natif ou non
         → PDF "Print to PDF" sans police = pas de texte → OCR
      3. Extrait banque, IBAN, BIC, soldes, transactions
    Retourne un dict : raw_text, bank, iban, bic, opening, closing, df, ocr_used
    """
    pdf_file.seek(0)
    pdf_bytes = pdf_file.read()

    has_text = _pdf_has_text(pdf_bytes)

    # ── Extraction du texte brut ──
    ocr_used = False
    if has_text:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                raw_pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
            raw_text = clean_text("\n".join(raw_pages))
        except Exception:
            raw_text = ""
    else:
        # PDF scanné ou "Print to PDF" sans polices → OCR Google Vision
        ocr_used = True
        raw_text = ocr_pdf_gcv(pdf_bytes)

    bank = detect_bank(raw_text)
    iban, bic = extract_iban_bic(raw_text)
    opening = extract_opening_balance(raw_text)
    closing = extract_closing_balance(raw_text)

    # ── Choix du parser transactions ──
    if not has_text:
        # PDF scanné/image → parser texte sur résultat OCR
        df = parse_transactions_text(raw_text)
    else:
        # PDF texte natif → parser colonne en priorité
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        # Fallback sur parser texte si colonne ne donne rien
        if df.empty:
            df = parse_transactions_text(raw_text)

    return {
        "raw_text": raw_text,
        "bank": bank,
        "iban": iban,
        "bic": bic,
        "opening": opening,
        "closing": closing,
        "df": df,
        "ocr_used": ocr_used,
    }


# =========================================================
# GÉNÉRATION OFX — COMPATIBLE TOUTES VERSIONS ODOO
# =========================================================

def generate_ofx(
    df: pd.DataFrame,
    bank_id: str,
    acc_id: str,
    opening_balance: float,
) -> str:
    """
    OFX 1.x SGML — compatible Odoo 14, 15, 16, 17+.
    Règles critiques :
      - TRNAMT   : X.XX  (point décimal, SANS virgule, 2 décimales)
      - TRNTYPE  : CREDIT / DEBIT
      - DTPOSTED : YYYYMMDD
      - FITID    : hash MD5 unique
    """
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    closing = opening_balance + df["montant"].sum()

    lines = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONE",
        "ENCODING:UTF-8",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "<OFX>",
        "<BANKMSGSRSV1>",
        "<STMTTRNRS>",
        "<TRNUID>1001",
        "<STATUS>",
        "<CODE>0",
        "<SEVERITY>INFO",
        "</STATUS>",
        "<STMTRS>",
        "<CURDEF>EUR",
        "<BANKACCTFROM>",
        f"<BANKID>{bank_id}",
        f"<ACCTID>{acc_id}",
        "<ACCTTYPE>CHECKING",
        "</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{now[:8]}",
        f"<DTEND>{now[:8]}",
    ]

    for i, row in df.iterrows():
        try:
            dt = pd.to_datetime(row["date"], dayfirst=True).strftime("%Y%m%d")
        except Exception:
            dt = datetime.now().strftime("%Y%m%d")

        fitid = hashlib.md5(f"{dt}{row['montant']:.2f}{i}".encode()).hexdigest()
        amount_str = f"{row['montant']:.2f}"  # point décimal garanti
        libelle_clean = re.sub(r"[<>&\"'\\]", " ", str(row["libelle"]))
        libelle_clean = re.sub(r"\s{2,}", " ", libelle_clean).strip()[:60]
        trntype = "CREDIT" if row["montant"] >= 0 else "DEBIT"

        lines += [
            "<STMTTRN>",
            f"<TRNTYPE>{trntype}",
            f"<DTPOSTED>{dt}",
            f"<TRNAMT>{amount_str}",
            f"<FITID>{fitid}",
            f"<NAME>{libelle_clean}",
            "</STMTTRN>",
        ]

    lines += [
        "</BANKTRANLIST>",
        "<LEDGERBAL>",
        f"<BALAMT>{closing:.2f}",
        f"<DTASOF>{now[:8]}",
        "</LEDGERBAL>",
        "<AVAILBAL>",
        f"<BALAMT>{closing:.2f}",
        f"<DTASOF>{now[:8]}",
        "</AVAILBAL>",
        "</STMTRS>",
        "</STMTTRNRS>",
        "</BANKMSGSRSV1>",
        "</OFX>",
    ]

    return "\n".join(lines)


# =========================================================
# INTERFACE STREAMLIT
# =========================================================

def main():
    st.title("💳 OFX Converter Pro — Multi-banques France → Odoo")
    st.caption(
        "Compatible : La Banque Postale · BNP · Crédit Agricole · Société Générale · "
        "CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire · "
        "Qonto · Boursorama · Revolut · N26 · Hello Bank · Fortuneo…  |  "
        "**OCR automatique** via Google Cloud Vision (PDF scannés & Print-to-PDF)"
    )

    # ── Vérification clé GCV ──
    gcv_ok = False
    try:
        gcv_ok = bool(st.secrets.get("GCV_API_KEY", ""))
    except Exception:
        pass

    if not gcv_ok:
        st.warning(
            "⚠️ Clé Google Vision non configurée. "
            "Les PDF scannés / Print-to-PDF (Qonto, etc.) nécessitent : "
            "`GCV_API_KEY` dans `.streamlit/secrets.toml`."
        )

    uploaded = st.file_uploader("📁 Déposer le relevé bancaire (PDF)", type=["pdf"])
    if not uploaded:
        st.info("Importez un relevé bancaire PDF pour commencer.")
        return

    # ── Nom de fichier sécurisé ──
    safe_name = sanitize_filename(uploaded.name)
    if safe_name != uploaded.name:
        st.caption(f"📄 Fichier reçu : `{uploaded.name}` → normalisé en `{safe_name}`")

    with st.spinner("🔍 Analyse du relevé en cours…"):
        result = extract_all(uploaded)

    bank    = result["bank"]
    iban    = result["iban"]
    bic     = result["bic"]
    opening = result["opening"]
    closing = result["closing"]
    df      = result["df"]
    ocr_used = result["ocr_used"]
    raw_text = result["raw_text"]

    if ocr_used:
        st.info("🔎 PDF image/scanné détecté — extraction via **Google Cloud Vision**")

    # ══════════ SECTION 1 : INFORMATIONS COMPTE ══════════
    st.subheader("🏦 Informations du compte")
    c1, c2, c3 = st.columns(3)
    c1.info(f"**Banque détectée**\n\n{bank}")
    c2.info(f"**IBAN**\n\n`{iban or 'Non détecté'}`")
    c3.info(f"**BIC**\n\n`{bic or 'Non détecté'}`")

    # ══════════ SECTION 2 : SOLDES ══════════
    st.subheader("💰 Soldes")
    col_o, col_c = st.columns(2)
    with col_o:
        opening_balance = st.number_input(
            "Solde d'ouverture (€)",
            value=float(opening) if opening is not None else 0.0,
            format="%.2f",
            step=0.01,
            help="Extrait automatiquement du relevé. Modifiable si nécessaire.",
        )
    with col_c:
        if closing is not None:
            st.metric("Solde de clôture extrait (€)", f"{closing:.2f}")
        else:
            st.warning("Solde de clôture non détecté dans le PDF")

    # ══════════ SECTION 3 : TRANSACTIONS ══════════
    if df.empty:
        st.error("❌ Aucune transaction détectée. Vérifiez le format du PDF.")

        if raw_text:
            with st.expander("🔍 Texte brut extrait (debug)"):
                st.text(raw_text[:3000])
        return

    total_credit   = df[df["montant"] > 0]["montant"].sum()
    total_debit    = df[df["montant"] < 0]["montant"].sum()
    solde_calcule  = opening_balance + total_credit + total_debit

    st.subheader(f"📊 Transactions — {len(df)} opérations")

    m1, m2, m3, m4 = st.columns(4)
    m1.success(f"✅ **Total Crédit**\n\n**{total_credit:.2f} €**")
    m2.error(f"🔻 **Total Débit**\n\n**{abs(total_debit):.2f} €**")
    m3.info(f"📌 **Solde calculé**\n\n**{solde_calcule:.2f} €**")
    if closing is not None:
        delta = abs(solde_calcule - closing)
        icon  = "✅" if delta < 0.05 else "⚠️"
        msg   = f"{icon} **Écart vs relevé**\n\n**{delta:.2f} €**"
        (m4.success if delta < 0.05 else m4.warning)(msg)

    # Tableau d'affichage avec montants formatés X.XX
    df_display = df.copy()
    df_display["montant"] = df_display["montant"].apply(lambda x: f"{x:.2f}")
    st.dataframe(df_display, use_container_width=True, height=420)

    # Texte brut OCR (debug optionnel)
    if ocr_used and raw_text:
        with st.expander("🔍 Texte OCR brut (debug)"):
            st.text(raw_text[:4000])

    # ══════════ SECTION 4 : EXPORT OFX ══════════
    st.subheader("⚙️ Paramètres export OFX")
    ci, cb = st.columns(2)
    with ci:
        iban_input = st.text_input("IBAN", value=iban or "")
    with cb:
        bic_input = st.text_input("BIC", value=bic or "")

    if st.button("🚀 Générer le fichier OFX", type="primary"):
        ofx_content = generate_ofx(
            df,
            bank_id=bic_input or bic or "UNKNOWN",
            acc_id=iban_input or iban or "UNKNOWN",
            opening_balance=opening_balance,
        )

        # Nom de fichier OFX sécurisé (sans espaces ni accents)
        ofx_filename = safe_name.replace(".pdf", ".ofx")

        st.download_button(
            label="⬇️ Télécharger le fichier OFX",
            data=ofx_content.encode("utf-8"),
            file_name=ofx_filename,
            mime="text/plain",
        )

        with st.expander("👁️ Aperçu OFX (80 premières lignes)"):
            st.code("\n".join(ofx_content.split("\n")[:80]), language="xml")

        closing_final = opening_balance + df["montant"].sum()
        st.success(
            f"✅ OFX généré — **{len(df)} transactions** | "
            f"Clôture : **{closing_final:.2f} €** | "
            f"Fichier : `{ofx_filename}`"
        )


if __name__ == "__main__":
    main()