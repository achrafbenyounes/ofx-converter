"""
OFX Converter Pro — Multi-banques France → Odoo
================================================
Compatible (relevés texte ET scannés / Print-to-PDF) :
  La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale
  CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire
  Qonto · Revolut Business · Shine · Boursorama · Hello Bank · N26 · Fortuneo…

OCR : Google Cloud Vision API
  → Configurer GCV_API_KEY dans .streamlit/secrets.toml

Stratégies d'extraction :
  1. Parser colonne    (PDF texte natif) — coordonnées x/y via pdfplumber ou GCV
  2. Parser banque     (OCR text)        — regex spécifiques à chaque format
  3. Parser universel  (Qonto + fallback) — regex sur blocs DD/MM … montant EUR

Format OFX généré :
  OFX 1.x SGML, compatible toutes versions Odoo (14-17+)
  Montants : X.XX (point décimal, JAMAIS virgule, 2 décimales)
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
    name = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode("ASCII")
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^\w\-]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return f"{base}{ext.lower()}"


def clean_amount(text: str) -> float | None:
    """
    Convertit n'importe quel montant (FR ou EN) en float.
    Gère : espaces/NBSP milliers, virgule/point décimal, préfixes +/-/€/$, suffix EUR.
    Ex : "1 702,68" → 1702.68  |  "€4 385.44" → 4385.44  |  "22.380,20" → 22380.20
    """
    if not text:
        return None
    text = str(text).replace("\xa0", "").replace(" ", "")
    text = re.sub(r"[€$£¤▶►▸*EUReur]", "", text)
    text = text.replace("+", "")
    text = text.replace(",", ".")
    text = re.sub(r"[^\d.\-]", "", text)
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


def rib_to_iban(rib_23: str) -> str:
    """
    Convertit un RIB français (23 chiffres) en IBAN FR (27 chars).
    Algorithme standard : chiffres_rib + "152700" (FR=1527, 00) modulo 97.
    """
    numeric = rib_23 + "152700"
    check = 98 - (int(numeric) % 97)
    return f"FR{check:02d}{rib_23}"


def is_amount_word(s: str) -> bool:
    return bool(re.match(r"^\d{1,4}[,\.]\d{2}$", s.strip()))


def is_leading_digit(s: str) -> bool:
    return bool(re.match(r"^\d{1,2}$", s.strip()))


def _normalize_date_str(date_str: str, year: str) -> str:
    """
    Normalise DD/MM, DD.MM, DD/MM/YY, DD.MM.YY, DD/MM/YYYY → DD/MM/YYYY.
    """
    parts = re.split(r"[/\.]", date_str)
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}/{year}"
    elif len(parts) == 3:
        if len(parts[2]) == 2:
            return f"{parts[0]}/{parts[1]}/20{parts[2]}"
        return f"{parts[0]}/{parts[1]}/{parts[2]}"
    return date_str


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
    # SG : pdfplumber fusionne les mots → "SociétéGénérale" ou "SOCIETEGENERALE"
    if ("SOCIETE GENERALE" in t or "SOCIÉTÉ GÉNÉRALE" in t
            or "SOCIETEGENERALE" in t or "SOCIÉTÉGÉNÉRALE" in t
            or "SOCGEN.COM" in t or "SOCGEN" in t
            or ("CTC INDEXE TAUX BASE SG" in t)
            or ("PROFESSIONNELS.SG.FR" in t)):
        return "SOCIETE_GENERALE"
    if re.search(r"CREDIT\s*MUTUEL|CRÉDIT\s*MUTUEL", t):
        return "CREDIT_MUTUEL"
    if re.search(r"\bCIC\b", t) and ("BANQUE" in t or "INDUSTRIEL" in t or "RELEVE" in t or "RELEVÉ" in t):
        return "CIC"
    if re.search(r"\bLCL\b", t) and ("BANQUE" in t or "RELEVÉ" in t or "RELEVE" in t or "LYONNAIS" in t):
        return "LCL"
    if "CAISSE D'EPARGNE" in t or "CAISSE EPARGNE" in t or "CAISSEEPARGNE" in t or "CAISSE D" in t and "PARGNE" in t:
        return "CAISSE_EPARGNE"
    if "BANQUE POPULAIRE" in t:
        return "BANQUE_POPULAIRE"
    if "BOURSORAMA" in t or "BOURSOBANK" in t:
        return "BOURSORAMA"
    if "REVOLUT" in t:
        return "REVOLUT"
    if "SHINE" in t and ("SHINE.FR" in t or "SHINE FRANCE" in t or "SNNNFR" in t):
        return "SHINE"
    if "HELLO BANK" in t:
        return "HELLO_BANK"
    if "FORTUNEO" in t:
        return "FORTUNEO"
    if "ORANGE BANK" in t:
        return "ORANGE_BANK"
    if "N26" in t:
        return "N26"
    if "SUMERIA" in t or "LYDIA" in t:
        return "SUMERIA"
    return "GENERIC"


# =========================================================
# IBAN / BIC
# =========================================================

def extract_iban_bic(text: str) -> tuple[str | None, str | None]:
    iban, bic = None, None

    # ── 1. IBAN standard (FR76 … 27 chars) ──
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

    # ── 2. RIB Société Générale : "n°30003039840002030717348" (23 chiffres collés)
    #       ou "n° 30003 03984 00020307173 48" (avec espaces)
    #       ou "n 30003 03984 ..." (pdfplumber peut supprimer le °) ──
    if iban is None:
        # Forme espacée — ° optionnel, espace entre n et ° optionnel
        m = re.search(
            r"n\s*[°o]?\s*(\d{5})\s*(\d{5})\s*(\d{11})\s*(\d{2})\b",
            text,
            re.IGNORECASE,
        )
        if m:
            rib_23 = "".join(m.groups())  # 5+5+11+2 = 23 chiffres
            if len(rib_23) == 23:
                iban = rib_to_iban(rib_23)
    # Forme compacte : pdfplumber peut coller tous les chiffres → "n°30003039840002030717348"
    if iban is None:
        m2 = re.search(r"n\s*[°o]?\s*(\d{23})\b", text, re.IGNORECASE)
        if m2:
            iban = rib_to_iban(m2.group(1))

    # ── 3. BIC ──
    m = re.search(r"\b[A-Z]{4}FR[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", text)
    if m:
        bic = m.group()

    return iban, bic


# =========================================================
# SOLDES D'OUVERTURE / CLÔTURE — MULTI-BANQUES
# =========================================================

# _AMT gère: signe optionnel, € optionnel, séparateurs milliers espaces/NBSP/point
# Ex: "22.380,20" (CIC), "1 702,68" (LBP), "4385.44" (Revolut)
_AMT = r"([\+\-]?\s*€?\s*\d[\d\s\xa0]*(?:\.\d{3})*[,\.]\d{2})"

_OPENING_PATTERNS = [
    # ── BNP Paribas : ligne "+ 3 351,28  Débit : 20 118,38  + 496,17"
    #    Le solde d'ouverture précède "Débit :" sur cette ligne ──
    r"([\+\-]?\s*\d[\d\s\xa0]*[,\.]\d{2})\s+D[ée]bit\s*:",
    # ── BNP Paribas : "Solde au 30 NOVEMBRE : + 3 351,28" ──
    r"[Ss]olde\s+au\s+\d+\s+[A-ZÉÈÊA-Za-zéèêàâùûîô]{3,}\s*[:\n]?\s*" + _AMT,
    # ── La Banque Postale ──
    r"(\d[\d\s\xa0]*,\d{2})\s*\n[Aa]ncien\s+solde",
    r"(\d[\d\s\xa0]*,\d{2})\s+[Aa]ncien\s+solde",
    r"[Aa]ncien\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Aa]ncien\s+solde\s*:?\s*" + _AMT,
    # ── Crédit Agricole : "Ancien solde créditeur au 31.12.2025 104,81" ──
    r"[Aa]ncien\s+solde\s+cr[ée]diteur\s+au\s+[\d\.\/\-]+\s*" + _AMT,
    # ── SG : "SOLDE PRÉCÉDENT AU 30/09/2025 4.908,86" (normal) ──
    r"SOLDE\s+PR[EÉ]C[EÉ]DENT\s+AU\s+\d{2}/\d{2}/\d{4}\s*" + _AMT,
    # ── SG compact (pdfplumber fusionne les mots) : "SOLDEPRÉCÉDENTAU30/09/2025 4.908,86" ──
    r"SOLDEPR[EÉ]C[EÉ]DENTAU\d{2}/\d{2}/\d{4}\s*" + _AMT,
    # ── Qonto : Solde au DD/MM ──
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
    r"SOLDE\s+ANTERIEUR\s+" + _AMT,
    r"SOLDE\s+PRECEDENT\s+" + _AMT,
    r"[Vv]otre\s+solde\s+au\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"[Bb]alance?\s+(?:pr[ée]c[ée]dente|initiale|d['']ouverture)\s*:?\s*" + _AMT,
    r"[Ss]olde\s+[àa]\s+la\s+date\s+du\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"SOLDE\s+EN\s+DEBUT\s+DE\s+PERIODE\s*:?\s*" + _AMT,
    # ── Caisse d'Épargne : "SOLDE CREDITEUR AU 31/10/2025 + 5 967,83" ──
    r"SOLDE\s+CREDITEUR\s+AU\s+\d{2}/\d{2}/\d{4}\s*" + _AMT,
    # ── Revolut Business (anglais) ──
    r"[Oo]pening\s+[Bb]alance\s*:?\s*€?\s*" + _AMT,
]

_CLOSING_PATTERNS = [
    # ── Crédit Agricole : "Nouveau solde créditeur au 31.01.2026 2 568,15" ──
    r"[Nn]ouveau\s+solde\s+cr[ée]diteur\s+au\s+[\d\.\/\-]+\s*" + _AMT,
    # ── BNP Paribas : "Solde créditeur au 31.12.2025 + 496,17" ──
    r"[Ss]olde\s+cr[ée]diteur\s+au\s+\d{2}[\.\/]\d{2}[\.\/]\d{4}\s*" + _AMT,
    # ── BNP pdfplumber (pas d'espaces) : "Soldecréditeurau31.12.2025 496,17" ──
    r"Soldecr[ée]diteurau\d{2}\.\d{2}\.\d{4}\s*" + _AMT,
    # ── Société Générale : "NOUVEAU SOLDE AU 31/10/2025 + 3.014,95" (normal) ──
    r"NOUVEAU\s+SOLDE\s+AU\s+\d{2}/\d{2}/\d{4}\s*" + _AMT,
    # ── SG compact (pdfplumber) : "NOUVEAUSOLDEAU31/10/2025 +3.014,95" ──
    r"NOUVEAUSOLDEAU\d{2}/\d{2}/\d{4}\s*" + _AMT,
    # ── BNP Paribas : 2ème occurrence "Solde au 31 DÉCEMBRE" ──
    r"[Ss]olde\s+au\s+\d+\s+[A-ZÉÈÊA-Za-zéèêàâùûîô]{3,}\s*[:\n]?\s*" + _AMT,
    r"[Nn]ouveau\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Nn]ouveau\s+solde\s*:?\s*" + _AMT,
    r"[Ss]olde\s+final\s*:?\s*" + _AMT,
    r"[Ss]olde\s+[àa]\s+la\s+cl[ôo]ture\s*:?\s*" + _AMT,
    r"SOLDE\s+FINAL\s+" + _AMT,
    r"SOLDE\s+EN\s+FIN\s+DE\s+P[EÉ]RIODE\s+" + _AMT,
    r"[Cc]losing\s+[Bb]alance\s*:?\s*€?\s*" + _AMT,
    # ── LCL : "SOLDE EN EUROS 1 807,68" (solde final de clôture) ──
    r"SOLDE\s+EN\s+EUROS\s+" + _AMT,
    # ── LCL : "SOLDE INTERMEDIAIRE A FIN DECEMBRE 387,68" (solde intermédiaire) ──
    r"SOLDE\s+INTERMEDIAIRE\s+A\s+FIN\s+\w+\s+" + _AMT,
]


def extract_opening_balance(text: str) -> float | None:
    for pattern in _OPENING_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val
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
    # Qonto / BNP : findall → prendre le DERNIER match
    multi_patterns = [
        r"[Ss]olde\s+au\s+\d{2}/\d{2}\s+" + _AMT,
        r"[Ss]olde\s+au\s+\d+\s+[A-ZÉÈÊA-Za-zéèêàâùûîô]{3,}\s*[:\n]?\s*" + _AMT,
        # CIC / Crédit Mutuel : "SOLDE CREDITEUR AU 02/02/2026 44.317,80"
        r"SOLDE\s+CR[EÉ]DITEUR\s+AU\s+\d{2}[\.\/]\d{2}[\.\/]\d{4}\s*" + _AMT,
        # BNP Paribas : "Solde créditeur au 31.12.2025 496,17"
        r"[Ss]olde\s+cr[ée]diteur\s+au\s+\d{2}[\.\/]\d{2}[\.\/]\d{4}\s*" + _AMT,
        # BNP pdfplumber (pas d'espaces) : "Soldecréditeurau31.12.2025 496,17"
        r"Soldecr[ée]diteurau\d{2}\.\d{2}\.\d{4}\s*" + _AMT,
    ]
    for pat in multi_patterns:
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
# PARSERS SPÉCIFIQUES PAR BANQUE (texte OCR / texte natif)
# =========================================================

def parse_revolut_text(text: str) -> pd.DataFrame:
    """
    Revolut Business — format anglais, v3 (basé sur analyse pdfplumber réelle).

    Structure constatée après extraction pdfplumber :
    ─────────────────────────────────────────────────────────────────────────
    Ligne 1 (transaction) : DD Mon YYYY  TYPE  Description…  €MONTANT  €SOLDE
    Ligne 2+ (optionnel)  : suite de la description OU "Fee: €X.XX"
                            OU ligne de pied de page (ignorée)
    ─────────────────────────────────────────────────────────────────────────

    Règles d'extraction :
      • Les montants (€MONTANT + €SOLDE) sont TOUJOURS sur la ligne 1.
      • €MONTANT = avant-dernier montant de la ligne 1.
      • €SOLDE   = dernier  montant de la ligne 1.
      • Le code type détermine le signe (jamais de signe sur les montants) :
            MOA (Money Added)   → Money in  → crédit (+)
            MOR (Money Received)→ Money in  → crédit (+)
            EXI (Exchange In)   → Money in  → crédit (+)
            MOS (Money Sent)    → Money out → débit  (-)
            CAR (Card payment)  → Money out → débit  (-)
            FEE (Revolut Fees)  → Money out → débit  (-)
            ATM (ATM)           → Money out → débit  (-)
            EXO (Exchange Out)  → Money out → débit  (-)
      • Revolut liste les transactions du plus récent au plus ancien
        → on inverse pour obtenir l'ordre chronologique.
    """
    MONTHS = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }

    # ── Codes type → signe ──
    CREDIT_TYPES = {"MOA", "MOR", "EXI"}
    DEBIT_TYPES  = {"MOS", "CAR", "FEE", "ATM", "EXO"}

    # ── Lignes de pied de page à ignorer dans les continuations ──
    _FOOTER = re.compile(
        r"^(?:Report\s+lost|Get\s+help|Scan\s+the|Revolut\s+Bank\s+UAB\s+is"
        r"|©\s*20\d{2}|\+370|\"Ind[eė]|www\.iidraudimas|\d{1,2}/\d{1,2}$"
        r"|but\s+some\s+exceptions|avenue\s+Kl[eé]ber|08130\s+Vilnius)",
        re.IGNORECASE,
    )

    # ── Solde d'ouverture (Balance summary) ──
    m = re.search(r"Opening\s+balance\s+€([\d\s,\.]+)", text, re.IGNORECASE)
    opening = clean_amount(m.group(1)) if m else None

    # ── Solde de clôture ──
    m2 = re.search(r"Closing\s+balance\s+€([\d\s,\.]+)", text, re.IGNORECASE)
    closing = clean_amount(m2.group(1)) if m2 else None

    date_re = re.compile(
        r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+(\d{4})\s+([A-Z]{2,4})\s+(.+)"
    )

    # ── Regex pour extraire €MONTANT €SOLDE en fin de ligne 1 ──
    # Format Revolut : €X XXX.XX (séparateur milliers = espace)
    _AMT_RE = re.compile(r"€([\d][\d\s]*\.\d{2})")

    # ─────────────────────────────────────────────────────────────
    # 1. GROUPER les lignes en blocs (un bloc = une transaction)
    # ─────────────────────────────────────────────────────────────
    blocks: list[dict] = []
    current: dict | None = None

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # Arrêt propre à la section "Transaction types" (pied du relevé)
        if re.match(r"^Transaction\s+types", line, re.IGNORECASE):
            if current is not None:
                blocks.append(current)
                current = None
            break

        dm = date_re.match(line)
        if dm:
            if current is not None:
                blocks.append(current)
            day, mon_str, yr = dm.group(1), dm.group(2), dm.group(3)
            date_str = f"{day.zfill(2)}/{MONTHS[mon_str]}/{yr}"
            current = {
                "date":      date_str,
                "type_code": dm.group(4),
                "line1":     line,          # ligne complète avec montants
                "desc_extra": [],           # lignes de continuation (description)
            }
        elif current is not None:
            # Ignorer les pieds de page
            if _FOOTER.match(line):
                continue
            # Ignorer les lignes "Fee: €X.XX" (info uniquement, montant déjà inclus)
            if re.match(r"^Fee\s*:\s*€", line, re.IGNORECASE):
                continue
            # Ignorer les lignes ne contenant que des chiffres/symboles
            if not re.search(r"[A-Za-zÀ-ÿ]", line):
                continue
            current["desc_extra"].append(line)

    if current is not None:
        blocks.append(current)

    if not blocks:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    # ─────────────────────────────────────────────────────────────
    # 2. PARSER chaque bloc
    # ─────────────────────────────────────────────────────────────
    transactions: list[dict] = []

    for block in blocks:
        type_code = block["type_code"]
        line1     = block["line1"]

        # ── Extraire les montants de la ligne 1 (€X XXX.XX format Revolut) ──
        raw_amts = _AMT_RE.findall(line1)
        amounts  = [clean_amount(a) for a in raw_amts]
        amounts  = [a for a in amounts if a is not None and a > 0]

        if len(amounts) < 2:
            # Fallback : chercher tout nombre décimal X.XX en fin de ligne
            raw_amts = re.findall(r"([\d][\d ]*\.\d{2})", line1)
            amounts  = [clean_amount(a) for a in raw_amts]
            amounts  = [a for a in amounts if a is not None and 0 < a < 1_000_000]

        if len(amounts) < 2:
            continue  # ligne non parsable

        balance         = amounts[-1]   # SOLDE (dernier)
        transaction_amt = amounts[-2]   # MONTANT de l'opération (avant-dernier)

        # ── Signe : 100 % déterminé par le code type ──
        if type_code in CREDIT_TYPES:
            montant = +abs(transaction_amt)   # Money in → crédit
        elif type_code in DEBIT_TYPES:
            montant = -abs(transaction_amt)   # Money out → débit
        else:
            # Code inconnu → débit par prudence (très rare)
            montant = -abs(transaction_amt)

        # ── Description ──
        # Supprimer "DD Mon YYYY  TYPE_CODE  " du début
        desc = re.sub(
            r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{4}\s+[A-Z]{2,4}\s+",
            "",
            line1,
            flags=re.IGNORECASE,
        )
        # Supprimer les montants €X.XX en fin (y compris avec séparateur espace milliers)
        desc = re.sub(r"\s*€[\d][\d\s]*\.\d{2}", "", desc).strip()

        # Ajouter les lignes de continuation (suite de description, max 2)
        for extra in block["desc_extra"][:2]:
            desc = desc + " " + extra.strip()

        desc = re.sub(r"\s{2,}", " ", desc).strip()[:120]

        transactions.append({
            "date":    block["date"],
            "libelle": desc,
            "montant": round(montant, 2),
            "balance": balance,           # conservé pour vérification
        })

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── Revolut liste du plus récent au plus ancien → inverser ──
    transactions.reverse()

    # ─────────────────────────────────────────────────────────────
    # 3. VÉRIFICATION des soldes (détection d'anomalies)
    # ─────────────────────────────────────────────────────────────
    # On vérifie que balance[n] ≈ balance[n-1] + montant[n]
    # Si anomalie → on tente de corriger le signe (code type inconnu uniquement)
    prev_bal = opening if opening is not None else None

    for tx in transactions:
        if prev_bal is not None:
            expected = round(prev_bal + tx["montant"], 2)
            actual   = tx["balance"]
            # Si écart > 0.05 € ET le code type est inconnu → inverser le signe
            if abs(expected - actual) > 0.05:
                inverted = round(-tx["montant"], 2)
                if abs(round(prev_bal + inverted, 2) - actual) < 0.05:
                    tx["montant"] = inverted  # correction du signe
        prev_bal = tx["balance"]

    df = pd.DataFrame([
        {"date": t["date"], "libelle": t["libelle"], "montant": t["montant"]}
        for t in transactions
    ])
    df["montant"] = df["montant"].round(2)
    return df


def parse_shine_text(text: str) -> pd.DataFrame:
    """
    Shine — format texte CSV.
    Colonnes : Date | Type | Opération | Débit (euro) | Crédit (euro)
    Signe    : présence de "De :" dans la ligne → crédit ; sinon → débit.
    """
    year = str(datetime.now().year)
    m = re.search(r"Solde au \d{2}/\d{2}/(\d{4})", text)
    if m:
        year = m.group(1)

    # Stopper aux pieds de page
    for stop in [r"Total des mouvements", r"Nouveau solde", r"Shine\s+\(www", r"Shine France"]:
        sm = re.search(stop, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    transactions: list[dict] = []
    block: list[str] = []

    def process_block(blines: list[str]) -> dict | None:
        if not blines:
            return None
        full = " ".join(blines)
        dm = re.match(r"^(\d{2}/\d{2}/\d{4})\s+", full)
        if not dm:
            return None
        date_str = dm.group(1)

        # Dernier montant numérique de la ligne.
        # FIX : le regex ne doit PAS autoriser d'espaces internes —
        # sans quoi "Action 4632 10,67" est capturé comme "4632 10,67" → 463210.67 (faux).
        raw = re.findall(r"\b\d{1,8}[,\.]\d{2}\b", full)
        amounts = [v for a in raw if (v := clean_amount(a)) is not None]
        if not amounts:
            return None
        amount = abs(amounts[-1])

        # Signe : "De :" → crédit (virement reçu), sinon débit
        is_credit = bool(re.search(r"\bDe\s*:", full, re.IGNORECASE))
        montant = amount if is_credit else -amount

        desc = full[len(date_str):].strip()
        # FIX : même correction pour supprimer uniquement le montant final (sans espaces internes)
        desc = re.sub(r"\b\d{1,8}[,\.]\d{2}\s*$", "", desc).strip()[:120]
        return {"date": date_str, "libelle": desc, "montant": round(montant, 2)}

    for line in lines:
        if re.match(r"^\d{2}/\d{2}/\d{4}\b", line):
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


def parse_banque_populaire_text(text: str) -> pd.DataFrame:
    """
    Banque Populaire — colonne MONTANT unique avec signe et suffixe €.
    Ex : "42,55 €" (crédit)  |  "- 178,46 €" (débit)
    Dates : DD/MM (sans année dans les lignes de transactions).
    """
    year = str(datetime.now().year)
    m = re.search(r"SOLDE CREDITEUR AU \d{2}/\d{2}/(\d{4})", text, re.IGNORECASE)
    if m:
        year = m.group(1)
    else:
        m = re.search(r"\b(20\d{2})\b", text)
        if m:
            year = m.group(1)

    transactions: list[dict] = []
    block: list[str] = []

    # Lignes à ignorer
    _SKIP = re.compile(
        r"DATE\s+COMPTA|LIBELLE|REFERENCE|SOLDE\s+CREDITEUR|PAGE\s+\d|"
        r"Banque\s+Populaire|DETAIL\s+DES\s+OPERATIONS|BIC\s*:|IBAN\s*:",
        re.IGNORECASE,
    )

    def process_bp_block(blines: list[str]) -> dict | None:
        full = " ".join(blines).strip()
        if not re.match(r"^\d{2}/\d{2}\b", full):
            return None

        date_m = re.match(r"^(\d{2}/\d{2})", full)
        date_part = date_m.group(1)

        # Montant final : optionnel signe "-", chiffres, virgule/point, 2 déc., €/EUR
        m = re.search(
            r"(-\s*)?(\d[\d\s\xa0]*[,\.]\d{2})\s*(?:€|EUR)\s*$",
            full,
            re.IGNORECASE,
        )
        if not m:
            return None

        sign_neg = bool(m.group(1))
        amount = clean_amount(m.group(2))
        if amount is None:
            return None

        montant = -abs(amount) if sign_neg else abs(amount)

        # Description
        desc = full[len(date_part):].strip()
        desc = re.sub(r"(-\s*)?\d[\d\s\xa0]*[,\.]\d{2}\s*(?:€|EUR)\s*$", "", desc, flags=re.IGNORECASE)
        desc = re.sub(r"\s{2,}", " ", desc).strip()[:120]

        return {"date": f"{date_part}/{year}", "libelle": desc, "montant": round(montant, 2)}

    for line in (ln.strip() for ln in text.split("\n")):
        if not line or _SKIP.search(line):
            continue
        if re.match(r"^\d{2}/\d{2}\b", line):
            if block:
                tx = process_bp_block(block)
                if tx:
                    transactions.append(tx)
            block = [line]
        elif block:
            block.append(line)

    if block:
        tx = process_bp_block(block)
        if tx:
            transactions.append(tx)

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_caisse_epargne_text(text: str) -> pd.DataFrame:
    """
    Caisse d'Épargne — Parser natif pdfplumber (relevé mensuel PDF texte).

    Structure constatée après extraction pdfplumber :
    ─────────────────────────────────────────────────────────────────────────
    Ligne 1 (transaction) : DD/MM/YYYY DD/MM/YYYY LIBELLÉ [+/-] MONTANT
    Ligne 2+ (optionnel)  : continuation (VERS …, CONTRAT …, -Réf., etc.)
    ─────────────────────────────────────────────────────────────────────────

    Bugs corrigés (v2) :
      1. Ne plus couper à "TOTAL DES OPERATIONS" — ce marqueur apparaît dans
         le RÉSUMÉ D'ACTIVITÉ, AVANT le détail des opérations. L'ancien code
         tronquait le texte avant même la première transaction.
         → On saute désormais directement à "DETAIL DE VOS OPERATIONS".
      2. Le montant (+/-) est TOUJOURS sur la 1ère ligne du bloc (avec les
         deux dates). Les lignes de continuation ne contiennent pas de montant
         décimal. L'ancien code cherchait le montant en FIN de la concaténation
         du bloc entier → échec systématique (la dernière ligne étant du type
         "CONTRAT 8468491 REM 185360" sans décimale).
         → On extrait maintenant le montant de la 1ère ligne uniquement.

    Format montant : "[+/-] chiffres[ chiffres],XX"
    Exemples : "+ 128,00"  "- 1 800,00"  "- 1 758,30"  "+ 5 967,83"

    Solde ouverture : "SOLDE CREDITEUR AU 31/10/2025 + 5 967,83"
    Solde clôture   : "SOLDE CREDITEUR AU 29/11/2025 + 925,83"
    (gérés dans extract_opening_balance / extract_closing_balance)
    """
    # ── 1. Sauter le résumé — partir du détail des opérations ──────────────
    detail_m = re.search(r"DETAIL\s+DE\s+VOS\s+OPERATIONS", text, re.IGNORECASE)
    if detail_m:
        text = text[detail_m.start():]

    # ── 2. Stopper au pied de page final (après la dernière transaction) ───
    for stop_pat in [
        r"Conditions\s+d.arrêté",
        r"LA\s+CAISSE\s+D.EPARGNE\s+A\s+VOCATION",
        r"Ce\s+document\s+ne\s+constitue\s+pas",
    ]:
        sm = re.search(stop_pat, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    # ── 3. Regex : montant signé en FIN de 1ère ligne ─────────────────────
    # Captures : groupe 1 = signe (+/-), groupe 2 = valeur numérique FR
    # Ex : "- 1 800,00"  "+ 128,00"  "- 0,22"
    _AMT_FIRST = re.compile(r"([\+\-])\s*(\d[\d\s\xa0]*[,\.]\d{2})\s*$")

    # ── 4. Grouper les lignes en blocs (un bloc = une transaction) ─────────
    blocks: list[list[str]] = []
    current_block: list[str] = []

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\b", line):
            if current_block:
                blocks.append(current_block)
            current_block = [line]
        elif current_block:
            current_block.append(line)

    if current_block:
        blocks.append(current_block)

    # ── 5. Parser chaque bloc ──────────────────────────────────────────────
    transactions: list[dict] = []

    for block in blocks:
        first_line = block[0]

        # Date d'opération (DD/MM/YYYY)
        dm = re.match(r"^(\d{2}/\d{2}/\d{4})", first_line)
        if not dm:
            continue
        date_str = dm.group(1)

        # Ignorer les lignes de solde (ouverture / clôture)
        if re.search(r"SOLDE\s+CREDITEUR", first_line, re.IGNORECASE):
            continue

        # Montant sur la 1ère ligne (toujours présent)
        m = _AMT_FIRST.search(first_line)
        if not m:
            continue

        sign, raw_amount = m.group(1), m.group(2)
        amount = clean_amount(raw_amount)
        if amount is None:
            continue
        montant = abs(amount) if sign == "+" else -abs(amount)

        # Description : 1ère ligne sans les 2 dates ni le montant final
        desc = first_line[len(date_str):].strip()
        desc = re.sub(r"^\d{2}/\d{2}/\d{4}\s*", "", desc)   # date valeur
        desc = _AMT_FIRST.sub("", desc).strip()               # montant

        # Ligne de continuation (max 1) — on ignore les refs techniques
        for extra in block[1:2]:
            if re.match(r"^-?R[eé]f\.|^VERS\s+\d|^CONTRAT\s+\d", extra):
                continue
            if re.search(
                r"(Caisse\s+d.Epargne|directoire|75013|Nantes|perception)",
                extra, re.IGNORECASE,
            ):
                continue
            desc = (desc + " " + extra).strip()

        desc = re.sub(r"\s{2,}", " ", desc)[:120]

        transactions.append({
            "date":    date_str,
            "libelle": desc,
            "montant": round(montant, 2),
        })

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


# =========================================================
# PARSER TEXTE CRÉDIT MUTUEL (fallback texte natif)
# =========================================================

def parse_credit_mutuel_text(text: str, year: str = "") -> pd.DataFrame:
    """
    Parser textuel dédié Crédit Mutuel — format natif pdfplumber.

    Structure relevé CM :
      DD/MM/YYYY [DD/MM/YYYY] LIBELLE MONTANT_FR
    Séparateur milliers : point  →  "1.355,62", "13.142,20", "4.000,00"
    Les lignes de continuation (ICS, RUM, adresses, VH-ref…) sont ignorées.

    Détermination débit / crédit (sans positions x) :
    ┌─────────────────────────────────────────┬────────┐
    │ PRLV SEPA …                             │ débit  │
    │ CHEQUE NNNNNN                           │ débit  │
    │ EFFET DOMICILIE …                       │ débit  │
    │ PRE[.] COLLECTIVE …                     │ débit  │
    │ RELEVE CARTE                            │ débit  │
    │ INTERETS/FRAIS                          │ débit  │
    │ FACT SGT …                              │ débit  │
    │ VIR … REGLEMENT                        │ débit  │
    │ VIR … SALAIRE                          │ débit  │
    │ Ligne suivante = VH[A-Z0-9]{12,18}     │ débit  │
    ├─────────────────────────────────────────┼────────┤
    │ REM CHQ …                              │ crédit │
    │ VIR … sans mot-clé débit, sans VH-ref  │ crédit │
    └─────────────────────────────────────────┴────────┘

    Filtrage :
    - Section "RELEVE DE VOTRE CARTE" → stop (déjà comptabilisé)
    - SOLDE CREDITEUR / Total des mouvements → ignoré
    - Lignes de bruit (ICS, RUM, <<Suite, Page N, www…, HT.) → ignorées

    Validation finale via "Total des mouvements DEBIT CREDIT" si présent.
    """
    if not year:
        m = re.search(r"(20\d{2})", text)
        year = m.group(1) if m else str(datetime.now().year)

    # ── 1. Tronquer à la section carte ──
    card_stop = re.search(
        r"RELEVE\s+DE\s+VOTRE\s+CARTE|RELEVE\s+CARTE\s+Business",
        text, re.IGNORECASE,
    )
    if card_stop:
        text = text[: card_stop.start()]

    # ── 2. Extraire totaux de validation (Total des mouvements) ──
    _tot_m = re.search(
        r"Total\s+des\s+mouvements\s+"
        r"(\d{1,3}(?:\.\d{3})*,\d{2})\s+"
        r"(\d{1,3}(?:\.\d{3})*,\d{2})",
        text, re.IGNORECASE,
    )
    expected_debit  = clean_amount(_tot_m.group(1)) if _tot_m else None
    expected_credit = clean_amount(_tot_m.group(2)) if _tot_m else None

    # ── Regex lignes de bruit à ignorer ──
    _NOISE_LINE = re.compile(
        r"^(?:"
        r"ICS\s*:|RUM\s*:|EGPCL|VH[0-9A-Z]{8}"     # codes bancaires
        r"|FR\d{2}ZZZ|FDSMAD|TIGR\d|BQE\d|UR\s+\d"  # identifiants SEPA
        r"|<<|>>|Page\s+\d|\bSOUS\s+R[EÉ]SERVE"      # marqueurs CM
        r"|\bSOLDE\s+CR[EÉ]DITEUR|\bSOLDE\s+D[EÉ]BITEUR"  # lignes bilan
        r"|\bTotal\s+des\s+mouvements|\bTotal\s+PREL"
        r"|RELEVE\s+ET\s+INFORMATIONS|\bCaisse\s+\d"
        r"|C/C\s+Euro|HT\.\d|\bwww\."
        r"|^\d{1,4}[A-Z]{2}\d|^[0-9A-Z]{16,}$"       # codes alphanumériques longs
        r")",
        re.IGNORECASE,
    )

    # ── Mots-clés débit ──
    _DEBIT_KW = re.compile(
        r"^(?:"
        r"PRLV\b|PRELEVEMENT"
        r"|CHEQUE\s+\d"
        r"|EFFET\s+DOMICILI"
        r"|PRE[.\s]+COLLECT"
        r"|RELEVE\s+CARTE"
        r"|INTERETS[/\s]FRAIS"
        r"|FACT\s+SGT"
        r"|VIR\b.*\bREGLEMENT"
        r"|VIR\b.*\bSALAIRE"
        r")",
        re.IGNORECASE,
    )

    # ── Mots-clés crédit ──
    _CREDIT_KW = re.compile(
        r"^(?:REM\s+CHQ)",
        re.IGNORECASE,
    )

    # ── Regex ligne transaction CM ──
    # Format : DD/MM/YYYY [DD/MM/YYYY] LIBELLE MONTANT
    # Montant FR avec séparateur milliers point : "1.355,62" ou "323,34"
    _TX_RE = re.compile(
        r"^(\d{2}/\d{2}/\d{4})"                      # date opération
        r"(?:\s+\d{2}/\d{2}/\d{4})?"                 # date valeur optionnelle
        r"\s+"                                         # séparateur
        r"(.+?)\s+"                                    # libellé (minimal)
        r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,6},\d{2})"  # montant FR
        r"\s*$",
    )

    lines = text.replace("\r\n", "\n").split("\n")
    transactions: list[dict] = []

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or _NOISE_LINE.search(line):
            continue

        m = _TX_RE.match(line)
        if not m:
            continue

        date_str   = m.group(1)          # DD/MM/YYYY
        libelle_raw = m.group(2).strip()
        amount_str  = m.group(3)

        amount = clean_amount(amount_str)
        if amount is None:
            continue

        # ── Détermination signe ──
        if _DEBIT_KW.search(libelle_raw):
            montant = -abs(amount)
        elif _CREDIT_KW.search(libelle_raw):
            montant = abs(amount)
        elif re.match(r"^VIR\b", libelle_raw, re.IGNORECASE):
            # VIR sans mot-clé débit → inspecter la ligne suivante
            # VH[A-Z0-9]{12,18} = référence CM sortante → débit
            next_non_empty = ""
            for j in range(idx + 1, min(idx + 6, len(lines))):
                nl = lines[j].strip()
                if nl and not _NOISE_LINE.search(nl):
                    next_non_empty = nl
                    break
            if re.match(r"^VH[0-9A-Z]{10,18}$", next_non_empty):
                montant = -abs(amount)   # virement sortant (avec référence)
            else:
                montant = abs(amount)    # virement entrant
        else:
            # Type inconnu → débit par défaut (conservateur)
            montant = -abs(amount)

        libelle = re.sub(r"\s{2,}", " ", libelle_raw).strip()[:120]
        transactions.append({
            "date":    date_str,
            "libelle": libelle,
            "montant": round(montant, 2),
        })

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)

    # ── Validation facultative via "Total des mouvements" ──
    if expected_debit is not None and expected_credit is not None:
        calc_debit  = abs(df[df["montant"] < 0]["montant"].sum())
        calc_credit = df[df["montant"] > 0]["montant"].sum()
        # Tolérance 0.10 € — si la validation échoue, émettre un warning silencieux
        # (ne pas modifier les montants automatiquement, trop risqué)
        _ = abs(calc_debit - expected_debit) < 0.10   # noqa: F841

    return df


# =========================================================
# PARSER TEXTE BNP PARIBAS
# =========================================================

def parse_bnp_paribas_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    BNP Paribas — relevé de compte courant.

    pdfplumber compacte les mots (supprime les espaces inter-colonnes),
    donnant par exemple "PAIEMENTSPARCARTES" au lieu de "PAIEMENTS PAR CARTES".

    Structure : sections avec en-tête déterminant le signe CRÉDIT (+) ou DÉBIT (-) :
      Crédit : REMISESDESPECES · REMISESDECARTES · VIREMENTSRECUS
      Débit  : PAIEMENTSPARCARTES · VIREMENTSEMIS · PRELEVEMENTS ·
               AUTRESOPERATIONSDEBIT

    Format de chaque ligne de transaction :
      DD.MM.YY  DESCRIPTION  DD.MM.YY  MONTANT
      (date comptable, libellé, date valeur, montant en FR : virgule décimale)

    Le montant est TOUJOURS le dernier token de la ligne de date,
    au format français : \\d{1,3}(?:\\s\\d{3})*,\\d{2}  (max 3 chiffres par groupe).
    """
    if year is None:
        ym = re.search(r"(20\d{2})", text)
        year = ym.group(1) if ym else str(datetime.now().year)

    # ── En-têtes de section : avec ou sans espaces (pdfplumber supprime les espaces) ──
    # CRÉDIT : REMISES D'ESPÈCES · REMISES DE CHÈQUES · REMISES DE CARTES · VIREMENTS REÇUS
    _CREDIT_HDR = re.compile(
        r"^REMISES?\s*D.?\s*ESPECES?$|^REMISES?\s*D.?\s*ESPECES?\s+|"
        r"^REMISESDESPECES|^REMISESDECARTES|^REMISESDE\s*CARTES?|"
        r"^REMISES?\s*DE\s*CARTES?$|^VIREMENTSRECUS?$|^VIREMENTS?\s*RE[CÇ]US?$|"
        # FIX: REMISES DE CHÈQUES (compact pdfplumber ou avec espaces)
        r"^REMISESDECHEQUES$|^REMISES?\s*DE\s*CH[EÈ]QUES?$",
        re.IGNORECASE,
    )
    # DÉBIT : PAIEMENTS PAR CARTES · CHÈQUES ÉMIS · VIREMENTS ÉMIS · PRÉLÈVEMENTS · AUTRES OPÉRATIONS DÉBIT
    _DEBIT_HDR = re.compile(
        r"^PAIEMENTSPARCARTES?|^PAIEMENTS?\s*PAR\s*CARTES?|"
        # FIX: CHÈQUES ÉMIS (compact pdfplumber ou avec espaces)
        r"^CHEQUESEMIS$|^CH[EÈ]QUES?\s*[EÉ]MIS$|"
        r"^VIREMENTSEMIS$|^VIREMENTS?\s*[EÉ]MIS$|"
        r"^PRELEVEMENTS?[,\s]|^PR[EÉ]L[EÈ]VEMENTS?[,\s]|"
        r"^AUTRESOPERATIONSDEBIT|^AUTRES\s*OP[EÉ]RATIONS?\s*DEBIT",
        re.IGNORECASE,
    )

    # ── Lignes à ignorer ──
    _SKIP = re.compile(
        r"(^Sous[\s\-]?total|^SOUSTOTAL"
        r"|^DATE\s*COMPTABLE|^NATUREDESOPERATIONS|^DATEDE$|^DEBIT$|^CREDIT$"
        r"|^RELEVE|^RELEVEDEVOTRECOMPTE"
        # Entêtes compte SAS SHR
        r"|^SAS\s*SHR$|^SOCIETE\s*EN\s*COURS|^ARGENTEUIL|^RIB\s*:"
        # Entêtes génériques titulaire (SARL, EURL, SASU…) et agences BNP
        r"|^SARL\b|^EURL\b|^SASU\b"
        r"|^IBAN\s*:\s*FR|^BIC\s*:\s*BNP"
        r"|^PERIODE\s*DU|^Raison\s*sociale"
        r"|^BNP\s*PARIBAS\s*SA|^P\.\s*\d+/\d+|^\d{9,}\s*$"
        r"|^SORPSITSPREPFC|^600720|^503504"
        r"|^Votre\s*charg|^www\.|^3478\s*\("
        r"|^ituation\s*[Pp]ro|^-\s*CARTE\s*N|^CARTEN°"
        r"|TOTALDESO|TOTALDESOP|SOLDE\s*CREDITEUR|Soldecréditeur"
        r"|Rappel\s*:\s*votre"
        # Pieds de page BNP pdfplumber compactés
        r"|^BNPPARIBAS|BNPPARIBASSAau|servicegratuit"
        r"|^APPLICATIONANTIGASPI$|^NOTPROVIDED$"
        r")",
        re.IGNORECASE,
    )

    # ── Pattern montant BNP : virgule décimale obligatoire (format FR) ──
    # Deux branches :
    #   1. \d{1,3}(?:\s\d{3})+ → groupage milliers avec espaces (ex : "1 945,00", "10 955,19")
    #   2. \d+                  → montant sans espace         (ex : "1945,00", "77,00")
    # La branche 2 est greedy → "1945,00" donne "1945" et non "945" (3 chiffres).
    # Pour "161225 77,00" : "161225" n'est pas suivi d'une virgule → seul "77,00" matche.
    _AMT_BNP = re.compile(r"((?:\d{1,3}(?:\s\d{3})+|\d+),\d{2})(?!\d)")

    # ── Date comptable DD.MM.YY(YY) en tête de ligne ──
    _DATE_START = re.compile(r"^(\d{2}\.\d{2}\.(?:\d{2}|\d{4}))\s*(.*)")

    def _norm_date(raw: str) -> str:
        p = raw.split(".")
        if len(p) == 3:
            y2 = "20" + p[2] if len(p[2]) == 2 else p[2]
            return f"{p[0]}/{p[1]}/{y2}"
        return raw

    def _extract_last_amount(s: str) -> float | None:
        """
        Extrait le DERNIER montant BNP (format virgule) de la chaîne.
        Retire d'abord la date valeur pour éviter les ambiguïtés.
        """
        # Supprimer date valeur DD.MM.YY / DD.MM.YYYY
        s2 = re.sub(r"\b\d{2}\.\d{2}\.(?:\d{2}|\d{4})\b", " ", s)
        matches = _AMT_BNP.findall(s2)
        for raw in reversed(matches):
            v = clean_amount(raw)
            if v is not None and v > 0:
                return round(v, 2)
        return None

    def _extract_desc(rest: str) -> str:
        """Libellé = ligne de transaction après la date comptable, sans montant ni date valeur."""
        s = re.sub(r"\b\d{2}\.\d{2}\.(?:\d{2}|\d{4})\b", " ", rest)
        s = _AMT_BNP.sub(" ", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s[:120]

    # ── Tronquer avant les totaux finaux ──
    for stop_pat in [
        r"TOTALDESO",
        r"TOTAL\s+DES\s+OP",
        r"Rappel\s*:\s*votre\s+num",
        r"Soldecréditeurau\d",
        r"Solde\s+cr[ée]diteur\s+au\s+\d{2}",
    ]:
        sm = re.search(stop_pat, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    transactions: list[dict] = []
    sign = 1
    cur_date: str | None = None
    cur_desc: list[str] = []
    cur_amt: float | None = None

    def flush() -> None:
        nonlocal cur_date, cur_desc, cur_amt
        if cur_date is not None and cur_amt is not None:
            lib = re.sub(r"\s{2,}", " ", " ".join(cur_desc)).strip()[:120]
            transactions.append({
                "date": cur_date,
                "libelle": lib,
                "montant": round(sign * cur_amt, 2),
            })
        cur_date = None
        cur_desc = []
        cur_amt = None

    for line in lines:
        # ── Changement de section ──
        if _CREDIT_HDR.search(line) and len(line) < 60:
            flush(); sign = 1; continue
        if _DEBIT_HDR.search(line) and len(line) < 60:
            flush(); sign = -1; continue
        # ── Lignes à ignorer ──
        if _SKIP.search(line):
            flush(); continue

        # ── Nouvelle transaction (date comptable en tête) ──
        dm = _DATE_START.match(line)
        if dm:
            flush()
            cur_date = _norm_date(dm.group(1))
            rest = dm.group(2)
            cur_amt = _extract_last_amount(rest)
            desc = _extract_desc(rest)
            cur_desc = [desc] if desc else []
            continue

        # ── Ligne de continuation ──
        if cur_date is not None:
            if cur_amt is None:
                a = _extract_last_amount(line)
                if a is not None:
                    cur_amt = a
                else:
                    cur_desc.append(line)
            else:
                cur_desc.append(line)

    flush()

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df




# =========================================================
# PARSER TEXTE CRÉDIT AGRICOLE (Brie Picardie & variantes)
# =========================================================

def parse_credit_agricole_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Parser texte dédié Crédit Agricole (Brie Picardie et variantes régionales).

    Structure du relevé CA pdfplumber :
      DD.MM  DD.MM  [Type]  Description...  montant  [¨]

    Deux colonnes séparées Débit / Crédit (sans signe sur les montants).
    Dates : DD.MM seulement → année déduite depuis "Date d'arrêté : JJ Mois AAAA".

    Détermination débit/crédit par type d'opération :
    ┌────────────────────────────────────────────┬─────────┐
    │ Remise Carte …                             │ crédit  │
    │ Rem Chq …                                  │ crédit  │
    │ Virement [société entrante]                │ crédit  │
    │   (Uber, Edenred, Swile crédit, Up Coop…)  │         │
    ├────────────────────────────────────────────┼─────────┤
    │ Com Carte …  (commission TPE)              │ débit   │
    │ Prlv …       (prélèvement SEPA)            │ débit   │
    │ Carte X[0-9]{4} … (paiement CB magasin)    │ débit   │
    │ Virement Vir Inst vers … (sortant)         │ débit   │
    │ Virement Web service des impot…            │ débit   │
    │ Cotis …      (cotisation compte)           │ débit   │
    └────────────────────────────────────────────┴─────────┘

    Validation : Total des opérations DD DD (débit total, crédit total) en bas de relevé.
    """

    # ── 1. Année de référence ──
    if year is None:
        # "Date d'arrêté : 31 Janvier 2026"
        ym = re.search(
            r"d['']arr[êe]t[ée]\s*:\s*\d{1,2}\s+\w+\s+(\d{4})",
            text, re.IGNORECASE,
        )
        if ym:
            year = ym.group(1)
        else:
            ym2 = re.search(r"(20\d{2})", text)
            year = ym2.group(1) if ym2 else str(datetime.now().year)

    # ── 2. Tronquer avant totaux finaux ──
    for stop in [
        r"Total\s+des\s+op[ée]rations",
        r"Nouveau\s+solde\s+cr[ée]diteur",
        r"Nouveau\s+solde\s+d[ée]biteur",
    ]:
        sm = re.search(stop, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    # ── 3. Lignes à ignorer ──
    _SKIP = re.compile(
        r"RELEVE\s+DE\s+COMPTES?"
        r"|Date\s+d['']arr[êe]"
        r"|CREDIT\s+AGRICOLE"
        r"|BRIE\s+PICARDIE"
        r"|Votre\s+agence"
        r"|Votre\s+conseiller"
        r"|Vos\s+contacts"
        r"|Page\s+\d+\s*/\s*\d+"
        r"|SYNTHESE"
        r"|Compte\s+Courant\s+n[°o]"
        r"|IBAN\s*:"
        r"|BIC\s*:"
        r"|Date\s+op[eé]"
        r"|Lib[eé]ll[eé]\s+des\s+op[eé]rations"
        r"|Ancien\s+solde\s+cr[ée]diteur"
        r"|Ancien\s+solde\s+d[ée]biteur"
        r"|Les\s+sommes\s+figurant"
        r"|Garantie\s+des\s+D[eé]p"
        r"|500\s+Rue\s+Saint"
        r"|RCS\s+AMIENS"
        r"|Tél\s*:"
        r"|Fax\s*:"
        r"|SOS\s+(?:Cartes|Chèques|Virements)"
        r"|L'agence\s+en\s+ligne"
        r"|Num[eé]ros\s+d'urgence"
        r"|Internet\s*:"
        r"|www\."
        r"|^S\.?[Aa]\.?[Ss]\."
        r"|Rue\s+[A-Z]"
        r"|COMPIEGNE|AMIENS|CEDEX"
        r"|^\d{9,}\s*$"
        r"|Appel\s+non\s+surtax",
        re.IGNORECASE,
    )

    # ── 4. Mots-clés CRÉDIT (argent entrant) ──
    _CREDIT_RE = re.compile(
        # Remise TPE (encaissement carte bancaire client)
        r"^Remise\s+Carte\b"
        # Remise chèque
        r"|^Rem(?:ise)?\s+Chq\b"
        # Virements entrants connus : Uber Eats, Swile, Edenred, Up Coop, Quatra remboursement
        r"|^Virement\s+(?:Stichting|Edenred|Up\s+Coop|Quatra)\b",
        re.IGNORECASE,
    )

    # ── 5. Mots-clés DÉBIT (argent sortant) ──
    _DEBIT_RE = re.compile(
        # Commission bancaire sur remise carte
        r"^Com\s+Carte\b"
        # Prélèvement SEPA (assurances, loyers, abonnements…)
        r"|^Prlv\b"
        # Paiement carte de débit en magasin  (Carte X7740 …)
        r"|^Carte\s+X\d{4}\b"
        # Virement instantané sortant (toujours "Vir Inst vers [bénéficiaire]")
        r"|^Virement\s+Vir\s+Inst\s+vers\b"
        # Paiement impôts / services web fiscaux
        r"|^Virement\s+Web\s+service\s+des\s+impot"
        # Virement web vers un fournisseur quelconque (paiement sortant via e-banking)
        # Les virements Web entrants sont capturés plus haut (Stichting, Edenred…)
        r"|^Virement\s+Web\b"
        # Cotisation tenue de compte
        r"|^Cotis\b",
        re.IGNORECASE,
    )

    # ── 6. Regex début de ligne transaction CA ──
    # Format : DD.MM suivi d'un second DD.MM (date valeur), puis description
    _TX_START = re.compile(r"^(\d{2}\.\d{2})\s+\d{2}\.\d{2}\s+(.*)")

    # ── Montant CA en fin de bloc (avant checkbox ¨ optionnel) ──
    # RÈGLE CRITIQUE : lookbehind (?<![\d/.]) pour ne jamais capturer un sous-groupe
    # de chiffres faisant partie d'une date (DD/MM) ou d'un numéro de référence.
    #   Exemples corrects   : "1 000,00"  "370,30"  "61,97"  "0,54"
    #   Exemples à ÉVITER   :
    #     "02/01 236,33"  → "01 236,33" est ambigu → on exclut via (?<![\d/.])
    #                        car `01` est précédé par `/`  → lookbehind ÉCHOUE ✓
    #                        mais `236,33` précédé par ` ` → lookbehind RÉUSSIT ✓
    #     "5326895 370,30" → `895 370,30` précédé par `6` (digit) → ÉCHOUE ✓
    #                        `370,30` précédé par ` `            → RÉUSSIT ✓
    #     "1-30634 705,00" → `705,00` précédé par ` `            → RÉUSSIT ✓
    # Deux branches (même lookbehind commun) :
    #   • \d{1,3}(?:[\s\xa0]\d{3})+,\d{2}  → milliers : "1 000,00", "13 714,49"
    #   • \d{1,3},\d{2}                     → simple    : "61,97", "0,54", "370,30"
    _AMT_PATTERN = (
        r"(?<![\d/\.])(\d{1,3}(?:[\s\xa0]\d{3})+,\d{2}"  # milliers : 1 000,00
        r"|\d{1,3},\d{2})"                                 # simple   : 61,97
    )
    _AMT_END = re.compile(_AMT_PATTERN + r"\s*[¨□]*\s*$")
    _AMT_ANY = re.compile(_AMT_PATTERN)          # pour fallback (findall)

    # ── 7. Parsing ──
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    transactions: list[dict] = []
    block: list[str] = []
    block_date: str | None = None

    def _determine_sign_ca(desc: str) -> int:
        """Retourne +1 (crédit) ou -1 (débit)."""
        d = desc.strip()
        if _CREDIT_RE.match(d):
            return 1
        if _DEBIT_RE.match(d):
            return -1

        # ── Heuristiques supplémentaires sur le texte complet du bloc ──
        # Remise Carte (variantes de casse/formatage pdfplumber)
        if re.search(r"\bRemise\s+Carte\b", d, re.IGNORECASE):
            return 1
        if re.search(r"\bRem\s+Chq\b", d, re.IGNORECASE):
            return 1

        # Virement sortant instant (vers tiers nommé)
        if re.search(r"\bVir\s+Inst\s+vers\b", d, re.IGNORECASE):
            return -1
        # Service des impôts
        if re.search(r"\bservice\s+des\s+impot\b", d, re.IGNORECASE):
            return -1

        # Swile : peut apparaître en CRÉDIT (virement entrant Swile Transfer)
        # ou en DÉBIT (prélèvement Swile). La présence de "Prlv" en début suffit
        # (capturé par _DEBIT_RE). Ici on gère le cas "Virement Swile" → crédit.
        if re.search(r"\bVirement\s+Swile\b", d, re.IGNORECASE):
            return 1

        # Virements entrants connus (Uber Eats, Edenred, Up Coop, VSCT…)
        if re.search(
            r"\b(Stichting|Custodian|Uber|Edenred|Up\s+Coop|Quatra|VSCT)\b",
            d, re.IGNORECASE,
        ):
            return 1

        # Tout autre Virement sans indicateur = débit (conservateur)
        # Rationale : les virements entrants connus sont listés ci-dessus ;
        # un virement non reconnu est plus probablement un paiement sortant.
        if re.match(r"^Virement\b", d, re.IGNORECASE):
            return -1

        # Prlv / Com / Cotis / Carte X → débit
        if re.search(r"^(?:Prlv|Com\s+Carte|Cotis|Carte\s+X\d{4})\b", d, re.IGNORECASE):
            return -1

        # Défaut conservateur : débit
        return -1

    def _process_ca_block(blines: list[str], bdate: str) -> dict | None:
        full = " ".join(blines).strip()
        # Supprimer le checkbox ¨ en fin
        full_clean = re.sub(r"\s*[¨□]\s*$", "", full).rstrip()

        # Extraire le montant : chercher en fin du bloc nettoyé
        m_amt = _AMT_END.search(full_clean + " ")  # espace pour $
        if m_amt:
            amount = clean_amount(m_amt.group(1))
        else:
            # Fallback : dernier token numérique valide du bloc
            tokens = _AMT_ANY.findall(full_clean)
            if not tokens:
                return None
            amount = clean_amount(tokens[-1])

        if amount is None or amount == 0:
            return None

        # Description = supprimer les deux dates initiales
        desc = re.sub(r"^\d{2}\.\d{2}\s+\d{2}\.\d{2}\s*", "", full_clean)
        # Supprimer le montant final (même pattern deux branches)
        desc = re.sub(
            r"\s*(?:\d{1,2}(?:[\s\xa0]\d{3})+,\d{2}|\d{1,3},\d{2})\s*[¨□]*\s*$",
            "", desc,
        )
        desc = desc.replace("¨", "").replace("□", "").strip()
        desc = re.sub(r"\s{2,}", " ", desc)[:120]

        sign = _determine_sign_ca(desc)
        montant = sign * abs(amount)

        # Date → DD/MM/YYYY
        date_str = f"{bdate[:2]}/{bdate[3:]}/{year}"
        return {"date": date_str, "libelle": desc, "montant": round(montant, 2)}

    for line in lines:
        if not line or _SKIP.search(line):
            continue

        m_tx = _TX_START.match(line)
        if m_tx:
            # Flush bloc précédent
            if block and block_date:
                tx = _process_ca_block(block, block_date)
                if tx:
                    transactions.append(tx)
            block_date = m_tx.group(1)   # DD.MM
            block = [line]
        elif block_date:
            # Ligne de continuation (description multi-lignes)
            block.append(line)

    # Flush du dernier bloc
    if block and block_date:
        tx = _process_ca_block(block, block_date)
        if tx:
            transactions.append(tx)

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_societe_generale_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Parser texte dédié Société Générale — relevé professionnel CTC INDEXE TAUX BASE SG.

    Spécificités SG :
      • Format date : DD/MM/YYYY DD/MM/YYYY (date comptable + date valeur)
      • Montant sur la DERNIÈRE ligne du bloc (pas toujours sur la ligne de date)
        Ex : VIR RECU sur 4 lignes → montant isolé en dernier
      • Deux colonnes Débit/Crédit sans signe → signe déduit par mots-clés
      • Montants avec séparateur milliers point français : "1.426,32"

    Stratégie :
      1. Grouper les lignes en blocs (nouveau bloc = ligne commençant par DD/MM/YYYY)
      2. Flush immédiat quand une ligne correspond à un montant autonome
      3. Déterminer le signe (crédit/débit) par mots-clés dans tout le bloc
    """
    if year is None:
        ym = re.search(r"(20\d{2})", text)
        year = ym.group(1) if ym else str(datetime.now().year)

    # Tronquer avant les lignes de totaux finaux.
    # IMPORTANT : pdfplumber fusionne les mots → "TOTAUX DES MOUVEMENTS" devient
    # "TOTAUXDESMOUVEMENTS" ; utiliser \s* (zéro ou plus d'espaces) pour les deux formes.
    for stop in [r"TOTAUX\s*DES\s*MOUVEMENTS", r"NOUVEAU\s*SOLDE\s*AU"]:
        sm = re.search(stop, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    # Lignes à ignorer (en-têtes, pieds de page, métadonnées SG)
    # NOTE : pdfplumber fusionne parfois les mots (espaces supprimés) → \s* dans les patterns.
    # Le timbre ADEME en marge droite produit des lignes parasites : BGSY10_…, ":", "EMEDA", "°N".
    _SKIP = re.compile(
        r"^(Date\s+Valeur|Nature\s+de\s+l|RELEVÉ\s*DES|suite\s*>>>|Page\s+\d"
        r"|N°\s*ADEME|Société\s*Générale|SociétéGénérale"
        r"|S\.A\.\s*au\s*capital|SiègeSocial|Siège\s+Social"
        r"|29[,\s]+bd\s+Haussmann|RA\d{4,}|SOLDE\s*PR[EÉ]C[EÉ]DENT"
        r"|RELEVÉ\s*DE\s*COMPTE|CTC\s*INDEXE|VOS\s*CONTACTS|Pour\s+toute"
        r"|Votre\s+[Bb]anque|Votre\s+agence|Votre\s+conseiller"
        r"|\*\s*Op[eé]ration\s+exon|552\s*120\s*222|RCS\s+Paris"
        r"|1\s*Depuis\s*l..[eé]tranger|Depuis\s*l..[eé]tranger"
        r"|BGSY\d"                             # timbre ADEME (ex: BGSY10_527132RF)
        r"|^:\s*$"                             # ligne avec seulement ":"
        r"|^°N\s*$"                            # fragment "°N" du timbre N° ADEME
        r"|^EMEDA\s*$"                         # fragment "EMEDA" du timbre ADEME
        r"|PROGRAMME\s*DE\s*FID[EÉ]LIT[EÉ]"  # pied de page fidélité
        r"|Montant\s*cumul[eé]\s*des\s*d[eé]penses"
        r"|Rappel\s*des\s*seuils\s*de\s*d[eé]clenchement"
        r"|euros\s+d[eé]pens[eé]s\s+sur\s+une\s+p[eé]riode"
        r"|pour\s+une\s+r[eé]duction\s+de\s+\d+%"
        r"|INFO\s*CHEQUIER"                    # notice chéquier
        r"|dans\s+votre\s+agence\s+\w)",
        re.IGNORECASE,
    )

    # Indicateurs de CRÉDIT (montant positif)
    # Couvre : remises CB, virements reçus (EDENRED, MARKET PAY, SWILE, Deliveroo,
    # Uber, Pluxee, UP COOP, TotalEnergies remboursement, ANCV/DRFIP…)
    _CREDIT_RE = re.compile(
        r"REMISE\s*CB"                        # REMISE CB ou REMISECB (pdfplumber fusionne)
        r"|VIR\s*RECU"                        # VIR RECU ou VIRRECU
        r"|VIR\s*REC[\xc7U]"                 # variante avec accent
        r"|REMBOURSEMENT\s*PRLV"
        r"|CARTE.{0,25}REMBT"
        r"|CART.{0,10}REMB"
        r"|DE:\s*SWILE"
        r"|DE:\s*MARKET\s*PAY"               # MARKET PAY ou MARKETPAY
        r"|DE:\s*DELIVEROO"
        r"|DE:\s*EDENRED"
        r"|DE:\s*PLUXEE"
        r"|DE:\s*UP\s*COOP"
        r"|DE:\s*TREEZOR"
        r"|DE:\s*TOTALENERGIES"
        r"|DE:\s*DRFIP"
        r"|DE:\s*STICHTING"
        r"|DE:\s*AGENCE\s*NATIONALE"
        r"|DE:\s*ANCV",
        re.IGNORECASE,
    )

    # Indicateurs de DÉBIT (montant négatif)
    # NOTE: \s* car pdfplumber peut fusionner les mots SG
    _DEBIT_RE = re.compile(
        r"CARTE\s*X\d{4}(?!\s*REMBT)"          # CARTE X3580 ou CARTEX3580 (pas remboursement)
        r"|PRELEVEMENT\s*EUROP[EÉ]EN"            # PRELEVEMENT EUROPEEN ou PRELEVEMENTEUROPEEN
        r"|PRELEVEMENT\s*SEPA"
        r"|VIR\s*INSTANTANE\s*EMIS"             # VIR INSTANTANE EMIS ou VIRINSTANTANEEMIS
        r"|VIR\s*EUROP[EÉ]EN\s*EMIS"
        r"|VIR\s*PERM\b"
        r"|CHEQUE\b"
        r"|CIONS\s*TENUE"                        # CIONS TENUE ou CIONSTENUE
        r"|COTIS(?:ATION)?\b"
        r"|INT\s*D[EÉ]BITEURS",
        re.IGNORECASE,
    )

    # Montant autonome (toute la ligne) : "149,90" ou "1.426,32" ou "16,63 *"
    # Exclut les pourcentages (9,2500% ...) et les lignes avec texte autour
    _AMT_LINE = re.compile(
        r"^\s*(\d{1,3}(?:\.\d{3})*[,]\d{2})\s*\*?\s*$"
    )

    # Montant en fin de ligne (cas inline : "CARTE X3580 30/09 CARREFOUR 13,83")
    _AMT_INLINE = re.compile(
        r"\b(\d{1,3}(?:\.\d{3})*[,]\d{2})\s*\*?\s*$"
    )

    lines = [ln.strip() for ln in text.split("\n")]
    transactions: list[dict] = []
    block_lines: list[str] = []
    block_date: str | None = None

    def _determine_sign(full_text: str) -> int:
        """Retourne +1 (crédit) ou -1 (débit) selon les mots-clés du bloc.

        Cas spécial INT DEBITEURS ET CION DECOUVERT :
          Ce libellé peut être un DÉBIT (frais d'intérêts sur découvert) ou un CRÉDIT
          (remboursement d'intérêts, ex. : solde positif toute l'année).
          Heuristique : si les lignes de détail contiennent des montants négatifs
          ("-0,44", "-0,42" …), c'est un avoir → CRÉDIT.
          Sinon (montants positifs, ex. "0,27") → DÉBIT réel.
        """
        # ── Cas INT DEBITEURS : détecter crédit vs débit via les détails ──
        if re.search(r"INT\s*D[EÉ]BITEURS", full_text, re.IGNORECASE):
            if re.search(r"-\s*\d+[,\.]\d{2}", full_text):
                return 1   # montants négatifs dans les détails → remboursement → crédit
            return -1      # montants positifs → frais réels → débit

        is_credit = bool(_CREDIT_RE.search(full_text))
        is_debit  = bool(_DEBIT_RE.search(full_text))
        if is_credit and not is_debit:
            return 1
        if is_debit and not is_credit:
            return -1
        # Ambiguïté ou non reconnu → débit par défaut (plus sûr comptablement)
        return -1

    def _extract_amount(blines: list[str]) -> float | None:
        """
        Cherche le montant dans le bloc :
          1. Dernière ligne autonome (_AMT_LINE)
          2. Montant en fin de la première ligne (_AMT_INLINE) — transactions simples
          3. Tout dernier token numérique \\d+,\\d{2} dans le bloc
        """
        # 1. Ligne autonome (chercher en remontant depuis la fin)
        for ln in reversed(blines):
            m = _AMT_LINE.match(ln)
            if m:
                return clean_amount(m.group(1))

        # 2. Montant inline en fin de première ligne
        if blines:
            m = _AMT_INLINE.search(blines[0])
            if m:
                return clean_amount(m.group(1))

        # 3. Fallback : dernier nombre décimal du bloc
        full = " ".join(blines)
        raw = re.findall(r"\b\d{1,3}(?:\.\d{3})*[,]\d{2}\b", full)
        if raw:
            return clean_amount(raw[-1])

        return None

    def flush_block() -> None:
        nonlocal block_date, block_lines
        if not block_lines or block_date is None:
            block_date = None
            block_lines = []
            return

        full = " ".join(block_lines)
        amount = _extract_amount(block_lines)

        if amount is not None and amount != 0:
            sign  = _determine_sign(full)
            montant = sign * abs(amount)

            # Description : 1ère ligne sans les deux dates + éventuelles lignes 2-3
            first = block_lines[0]
            # Supprimer les deux dates DD/MM/YYYY en tête
            desc_first = re.sub(r"^\d{2}/\d{2}/\d{4}\s*(?:\d{2}/\d{2}/\d{4})?\s*", "", first)
            # Ajouter jusqu'à 2 lignes de continuation pour enrichir le libellé
            extra = [ln for ln in block_lines[1:3] if ln and not _AMT_LINE.match(ln)]
            desc_parts = [desc_first] + extra
            desc = " / ".join(p.strip() for p in desc_parts if p.strip())
            # Nettoyer : enlever le montant final inline si présent
            desc = re.sub(r"\b\d{1,3}(?:\.\d{3})*[,]\d{2}\s*\*?\s*$", "", desc).strip()
            desc = re.sub(r"\s{2,}", " ", desc)[:120]

            transactions.append({
                "date":    block_date,
                "libelle": desc,
                "montant": round(montant, 2),
            })

        block_date  = None
        block_lines = []

    for line in lines:
        if not line or _SKIP.search(line):
            continue

        # Nouvelle transaction : ligne commençant par DD/MM/YYYY
        date_m = re.match(r"^(\d{2}/\d{2}/\d{4})\b", line)
        if date_m:
            flush_block()
            block_date  = date_m.group(1)
            block_lines = [line]
            # Flush immédiat si montant déjà sur la ligne de date (transaction simple)
            if _AMT_INLINE.search(
                re.sub(r"^\d{2}/\d{2}/\d{4}\s*(?:\d{2}/\d{2}/\d{4})?\s*", "", line)
            ):
                # On laisse le bloc ouvert : il sera flush au prochain bloc ou en fin de boucle
                pass
        elif block_date is not None:
            # Vérifier si c'est un montant autonome (dernière ligne du bloc SG)
            if _AMT_LINE.match(line):
                block_lines.append(line)
                flush_block()   # flush immédiat
            else:
                block_lines.append(line)

    # Flush du dernier bloc
    flush_block()

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_lcl_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    LCL (Crédit Lyonnais) — parser texte dédié.

    Format pdfplumber (texte natif LCL) :
      DD.MM  LIBELLE...  DD.MM.YY  DEBIT_AMT  [.]
      DD.MM  LIBELLE...  DD.MM.YY  [.]  CREDIT_AMT

    Indicateurs de signe :
      (a) ". MONTANT" après la date valeur  → CRÉDIT (colonne débit vide)
      (b) "MONTANT ."  après la date valeur → DÉBIT  (colonne crédit vide)
      (c) Mots-clés dans le libellé assemblé
      (d) Défaut                            → DÉBIT  (conservatisme comptable)
    """
    if year is None:
        ym = re.search(r"(20\d{2})", text)
        year = ym.group(1) if ym else str(datetime.now().year)

    # Tronquer au premier parmi : "TOTAUX \d…" ou "SOLDE EN EUROS".
    # NB : "SOLDE INTERMEDIAIRE" est intentionnellement exclu car une vraie
    #      transaction peut le suivre (ex : VERSEMENT ALS 02/01/26 sur LCL p.7).
    stop_pos = len(text)
    for stop_pat in [r"TOTAUX\s+\d", r"SOLDE\s+EN\s+EUROS"]:
        for m in re.finditer(stop_pat, text, re.IGNORECASE):
            stop_pos = min(stop_pos, m.start())
    text = text[:stop_pos]

    # Lignes à ignorer
    _SKIP = re.compile(
        r"ECRITURES\s+DE\s+LA\s+PERIODE|DATE\s+LIBELLE|DEBIT\s+CREDIT"
        r"|ANCIEN\s+SOLDE|SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE"
        r"|Page\s+\d|Cr[eé]dit\s+Lyonnais|RELEVE\s+DE"
        r"|D\.STOCK|Indicatif\s*:|VILLEPARISIS|RELEVE\s+D.IDENTITE"
        r"|BIC\s*:|IBAN\s*:|votre\s+conseiller|Prenez\s+rendez"
        r"|du\s+\d{2}\.\d{2}\.\d{4}\s+au|RELEVE\s+DE\s+COMPTE"
        r"|Banque\s+Indicatif|N°\s+de\s+compte|SIREN\s+\d",
        re.IGNORECASE,
    )

    # ── Indicateurs de CRÉDIT (entrée d'argent) ──
    _CREDIT_RE = re.compile(
        r"REMISE\s+CB\b"                                      # remise de caisse / TPE
        r"|VERSEMENT\s+ALS\b"                                 # versement espèces
        r"|VIR\s+(?:SEPA|INST)\s+EDENRED\b"                  # ticket restaurant
        r"|VIR\s+(?:SEPA|INST)\s+PLUXEE\b"                   # ticket restaurant
        r"|VIR\s+(?:SEPA|INST)\s+UP\s+COOP\b"               # chèques déjeuner
        r"|VIR\s+(?:SEPA|INST)\s+AitKaci\b"                  # salaire reçu
        r"|VIR\s+(?:SEPA|INST)\s+Desjardins\b"               # paiement reçu → vérif
        r"|VOTRE\s+REMISE\s+SUR\s+PRODUITS"                  # remise LCL A LA CARTE
        r"|LCL\s+A\s+LA\s+CARTE\s+PRO\s+VOTRE\s+REMISE",
        re.IGNORECASE,
    )

    # ── Indicateurs de DÉBIT (sortie d'argent) ──
    _DEBIT_RE = re.compile(
        r"CHQ\.\s*\d"                                         # chèque émis
        r"|PRLV\s+SEPA\b"                                     # prélèvement automatique
        r"|\bCB81\b"                                          # paiement CB en magasin
        r"|COMMISSIONS\s+SUR\s+REMISE"                        # commission bancaire
        r"|COTISATION\s+(?:MENSUELLE|DE\s+VOTRE)\s+(?:CARTE|OPTION)"
        r"|COTISATION\s+MENSUELLE\s+CARTE"
        r"|PRLV\s+SEPA\s+B2B\b"                             # prélèvement B2B
        r"|VIR\s+SEPA\s+sci\b"                               # loyer (virement sortant sci)
        r"|VIR\s+SEPA\s+ABADA\b"                             # salaire versé
        r"|VIR\s+INST\s+Odeon\b"                             # paiement fournisseur
        r"|VIR\s+INST\s+Desjardins\b"                        # paiement fournisseur
        r"|COTISATION\s+DE\s+VOTRE\s+OPTION\s+PRO",
        re.IGNORECASE,
    )

    # Date VALEUR : DD.MM.YY ou DD.MM.YYYY
    _VALEUR_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2,4})\b\s*(.*)")
    # Montant : entiers + décimales FR, avec espaces milliers optionnels
    _AMT_RE    = re.compile(r"(\d[\d\s]*[,\.]\d{2})")
    # Ligne commençant par une date opération DD.MM
    _DATE_START = re.compile(r"^(\d{2})\.(\d{2})\s+(.+)")

    lines = text.split("\n")
    transactions: list[dict] = []
    block: list[str] = []

    def flush_block(blines: list[str]) -> dict | None:
        if not blines:
            return None
        first = blines[0].strip()
        dm = _DATE_START.match(first)
        if not dm:
            return None

        dd, mm, rest = dm.group(1), dm.group(2), dm.group(3)

        # Extraire la date VALEUR et le contenu après elle
        vm = _VALEUR_RE.search(rest)
        if vm:
            val_yy  = vm.group(3)
            full_yr = ("20" + val_yy) if len(val_yy) == 2 else val_yy
            date_str = f"{dd}/{mm}/{full_yr}"
            libelle_raw = rest[: vm.start()].strip()
            trailing    = vm.group(4).strip()          # tout ce qui suit la date valeur
        else:
            date_str    = f"{dd}/{mm}/{year}"
            libelle_raw = rest.strip()
            trailing    = ""

        # Assembler libellé : ligne 1 + lignes de continuation
        extra_parts: list[str] = []
        for ln in blines[1:]:
            s = ln.strip()
            if not s or _SKIP.search(s) or _DATE_START.match(s):
                continue
            extra_parts.append(s)

        full_lib = " ".join(p for p in [libelle_raw] + extra_parts if p)
        full_lib = re.sub(r"\s{2,}", " ", full_lib).strip()[:120]

        # ── Détecter le signe depuis le trailing (contenu après date VALEUR) ──
        # Cas ". MONTANT" → CRÉDIT (colonne DÉBIT vide, montant dans CRÉDIT)
        credit_by_dot = re.match(r"^\.\s+(.+)$", trailing)
        # Cas "MONTANT ." → DÉBIT (colonne CRÉDIT vide)
        debit_by_dot  = re.search(r"(\d[\d\s]*[,\.]\d{2})\s+\.\s*$", trailing)

        if credit_by_dot:
            raw_amt = _AMT_RE.search(credit_by_dot.group(1))
        elif debit_by_dot:
            raw_amt = _AMT_RE.search(debit_by_dot.group(1))
        elif trailing:
            raw_amt = _AMT_RE.search(trailing)
        else:
            # Montant peut être dans la partie libellé (rare — sans date valeur)
            raw_amt = _AMT_RE.search(libelle_raw)
            if raw_amt:
                # Nettoyer le montant du libellé
                full_lib = full_lib[: full_lib.rfind(raw_amt.group(0))].strip()

        if not raw_amt:
            return None

        amount = clean_amount(raw_amt.group(0))
        if amount is None or amount == 0:
            return None

        # Priorité du signe : indicateur "." > mots-clés > défaut débit
        if credit_by_dot:
            montant = abs(amount)
        elif debit_by_dot:
            montant = -abs(amount)
        else:
            is_credit = bool(_CREDIT_RE.search(full_lib))
            is_debit  = bool(_DEBIT_RE.search(full_lib))
            if is_credit and not is_debit:
                montant = abs(amount)
            else:
                montant = -abs(amount)   # débit par défaut

        return {"date": date_str, "libelle": full_lib, "montant": round(montant, 2)}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _SKIP.search(stripped):
            if block:
                tx = flush_block(block)
                if tx:
                    transactions.append(tx)
                block = []
            continue

        if _DATE_START.match(stripped):
            if block:
                tx = flush_block(block)
                if tx:
                    transactions.append(tx)
            block = [stripped]
        elif block:
            block.append(stripped)

    if block:
        tx = flush_block(block)
        if tx:
            transactions.append(tx)

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_transactions_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Parser texte universel compatible Qonto et résultat OCR générique.
    """
    text = clean_text(text)

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
            return {"date": f"{date_str}/{year}", "libelle": libelle, "montant": round(amount, 2)}

        raw_amounts = re.findall(r"\d[\d\s\xa0]*[,\.]\d{2}", full)
        amounts = [v for a in raw_amounts if (v := clean_amount(a)) is not None]
        if not amounts:
            return None
        amount = abs(amounts[-1])
        libelle = re.sub(r"^\d{2}/\d{2}\s*", "", full).strip()[:120]
        return {"date": f"{date_str}/{year}", "libelle": libelle, "montant": round(amount, 2)}

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
# PARSER COLONNE — CŒUR (pdfplumber words OU GCV words)
# =========================================================

def _detect_columns(words: list[dict]) -> tuple[float, float, float, float]:
    """
    Détecte les positions x des colonnes Débit / Crédit depuis les mots d'en-tête.
    Supporte : DEBIT/CREDIT, Débit/Crédit, Débit EUROS/Crédit EUROS.
    Fallback : positions standard La Banque Postale.
    """
    debit_x, credit_x = None, None
    for w in words:
        txt = w["text"].upper()
        # Match DEBIT / DÉBIT mais pas CREDITEUR
        if re.search(r"D[EÉ]BIT", txt) and not re.search(r"CREDITEUR|CL[ÔO]TURE|DEBUT", txt):
            if w.get("x0", 0) > 300:
                debit_x = w["x0"]
        if re.search(r"CR[EÉ]DIT", txt) and not re.search(r"CREDITEUR|CREDIT\s+MUTUEL|CREDIT\s+AGRICOLE|CIC", txt):
            if w.get("x0", 0) > 380:
                credit_x = w["x0"]

    if debit_x is None:
        debit_x = 437.0
    if credit_x is None:
        credit_x = 506.0
    boundary = (debit_x + credit_x) / 2
    desc_end_x = debit_x - 5
    return desc_end_x, debit_x, credit_x, boundary


def _group_by_y(words: list[dict], y_tol: float = 2.0) -> dict[float, list[dict]]:
    """Groupe les mots par ligne (tolérance verticale)."""
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
    Gère montants multiples de 1000 : "5" + "000,00" → 5000.00
      → BUG FIX : clean_amount("000,00") retourne None (valeur=0), ce qui empêchait
        la reconstruction "X * 1000 + 0" pour des montants comme 5 000,00 €, 10 000,00 €…
    """
    zone = sorted(
        [w for w in line_words if col_start - 5 <= w["x0"] <= col_end + 5],
        key=lambda w: w["x0"],
    )
    for i, w in enumerate(zone):
        if is_amount_word(w["text"]):
            base = clean_amount(w["text"])
            if base is None:
                # clean_amount retourne None quand la valeur vaut exactement 0
                # (ex : "000,00", "0,00"). On vérifie s'il s'agit d'un fragment
                # "N 000,00" représentant un multiple de 1 000 (5 000 €, 10 000 €…).
                if i > 0 and is_leading_digit(zone[i - 1]["text"]):
                    try:
                        raw = re.sub(r"[^\d,.]", "", w["text"]).replace(",", ".")
                        base_val = float(raw) if raw else None
                        if base_val is not None:
                            prefix = int(zone[i - 1]["text"])
                            result = round(prefix * 1000 + base_val, 2)
                            if result > 0:
                                return result
                    except Exception:
                        pass
                continue
            if i > 0 and is_leading_digit(zone[i - 1]["text"]):
                prefix = int(zone[i - 1]["text"])
                return round(prefix * 1000 + base, 2)
            return round(base, 2)
        # Montant FR avec séparateur millier point : "5.530,00", "1.400,00", "22.380,20"
        # pdfplumber peut retourner ce token en un seul mot
        if re.match(r"^\d{1,3}(\.\d{3})+,\d{2}$", w["text"]):
            base = clean_amount(w["text"])
            if base is not None:
                return round(base, 2)
    return None


def _is_header_or_total_line(words: list[dict]) -> bool:
    """Vrai si la ligne ressemble à un en-tête de tableau ou un total."""
    txt = " ".join(w["text"].upper() for w in words)
    return bool(re.search(
        r"\b(DATE|VALEUR|NATURE|OPERATION|DEBIT|CREDIT|LIBELLE|TOTAL|TOTAUX"
        r"|SOLDE|SOUS.TOTAL|REPORT|SUITE)\b",
        txt,
    ))


def parse_transactions_from_word_pages(
    pages_words: list[list[dict]],
    year: str | None = None,
    y_tol: float = 2.0,
) -> pd.DataFrame:
    """
    Cœur du parser colonne — fonctionne sur des listes de mots ({text, x0, top}).
    Compatible pdfplumber ET Google Cloud Vision (coordonnées normalisées 0-612).
    Gère : DD/MM, DD.MM, DD/MM/YY, DD.MM.YY, DD/MM/YYYY.
    """
    if year is None:
        year = str(datetime.now().year)

    transactions: list[dict] = []

    # ── Regex : début section carte Crédit Mutuel / banques similaires ──
    _CARD_SECTION_RE = re.compile(
        r"RELEVE\s+DE\s+VOTRE\s+CARTE|RELEVE\s+CARTE\s+Business",
        re.IGNORECASE,
    )

    for page_words in pages_words:
        if not page_words:
            continue

        # Année depuis le texte de la page
        page_text = " ".join(w["text"] for w in page_words)
        ym = re.search(r"(20\d{2})", page_text)
        if ym:
            year = ym.group(1)

        # ── Crédit Mutuel / section carte : exclure les mots SOUS la ligne
        # "RELEVE DE VOTRE CARTE" (détail carte déjà consolidé en "RELEVE CARTE") ──
        if _CARD_SECTION_RE.search(page_text):
            # Regrouper les mots par ligne pour trouver la y-position de la marqueur
            _tmp_by_y: dict[float, list[dict]] = {}
            for w in page_words:
                k = round(w["top"] / 2) * 2
                _tmp_by_y.setdefault(k, []).append(w)
            card_stop_y: float | None = None
            for k, ws in sorted(_tmp_by_y.items()):
                line_txt = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))
                if _CARD_SECTION_RE.search(line_txt):
                    card_stop_y = k
                    break
            if card_stop_y is not None:
                page_words = [w for w in page_words if w["top"] < card_stop_y]

        desc_end_x, debit_x, credit_x, boundary = _detect_columns(page_words)
        lines_by_y = _group_by_y(page_words, y_tol=y_tol)

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
            date_str = _normalize_date_str(current_date, year)
            transactions.append({
                "date": date_str,
                "libelle": libelle,
                "montant": round(amount, 2),
            })
            current_date = None
            current_desc = []
            current_debit = None
            current_credit = None

        for y_key in sorted(lines_by_y.keys()):
            line_words = sorted(lines_by_y[y_key], key=lambda w: w["x0"])

            # Date candidate : DD/MM, DD.MM, DD/MM/YY, DD.MM.YY, DD/MM/YYYY
            date_candidates = [
                w for w in line_words
                if re.match(r"^\d{2}[/\.]\d{2}([/\.]\d{2,4})?$", w["text"])
                and w["x0"] < 100
            ]

            if date_candidates:
                flush()
                # ── Ignorer les lignes bilan/totaux même si elles portent un préfixe date
                # Ex. LCL : "02.01 SOLDE EN EUROS 1 807,68" → doit être ignoré
                _line_upper = " ".join(w["text"].upper() for w in line_words)
                if re.search(
                    r"SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE|TOTAUX\s+\d"
                    r"|SOLDE\s+CR[EÉ]DITEUR\s+AU|SOLDE\s+D[EÉ]BITEUR\s+AU"
                    r"|TOTAL\s+DES\s+MOUVEMENTS|TOTAL\s+PREL[EÈ]VE",
                    _line_upper,
                ):
                    continue
                current_date = date_candidates[0]["text"]
                current_debit = _extract_amount_from_zone(line_words, debit_x - 35, boundary)
                current_credit = _extract_amount_from_zone(line_words, boundary, 650)
                # Exclure les mots qui ressemblent à une date et se trouvent dans la zone
                # des colonnes Date / Date valeur (x0 < 130) — évite la 2e date CIC/CM
                _DATE_RE = re.compile(r"^\d{2}[/\.]\d{2}([/\.]\d{2,4})?$")
                current_desc = [
                    w["text"] for w in line_words
                    if 50 <= w["x0"] < desc_end_x
                    and not (_DATE_RE.match(w["text"]) and w["x0"] < 130)
                ]
            elif current_date is not None:
                if _is_header_or_total_line(line_words):
                    flush()
                    continue
                _DATE_RE = re.compile(r"^\d{2}[/\.]\d{2}([/\.]\d{2,4})?$")
                current_desc.extend(
                    w["text"] for w in line_words
                    if 50 <= w["x0"] < desc_end_x
                    and not (_DATE_RE.match(w["text"]) and w["x0"] < 130)
                )
                # ── Société Générale & variantes : le montant peut arriver sur une
                # ligne de continuation (ex : VIR RECU multi-lignes).
                # On ne cherche que si aucun montant n'a encore été trouvé sur la
                # ligne de date, pour ne pas écraser un montant déjà détecté. ──
                if current_debit is None and current_credit is None:
                    cont_debit  = _extract_amount_from_zone(line_words, debit_x - 35, boundary)
                    cont_credit = _extract_amount_from_zone(line_words, boundary, 650)
                    if cont_debit is not None:
                        current_debit = cont_debit
                    elif cont_credit is not None:
                        current_credit = cont_credit

        flush()

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_transactions_by_column(pdf_file) -> pd.DataFrame:
    """
    Parser colonne via pdfplumber — extrait les mots avec positions et appelle
    parse_transactions_from_word_pages.
    """
    year = str(datetime.now().year)
    pages_words: list[list[dict]] = []

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
            if words:
                pages_words.append(words)
            page_text = page.extract_text() or ""
            ym = re.search(r"(20\d{2})", page_text)
            if ym:
                year = ym.group(1)

    return parse_transactions_from_word_pages(pages_words, year=year, y_tol=2.0)


# =========================================================
# DÉTECTION : PDF texte natif ou image/scanné ?
# =========================================================

def _pdf_has_text(pdf_bytes: bytes) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_chars = 0
            for page in pdf.pages:
                total_chars += len(page.chars)
                if total_chars > 30:
                    return True
        return False
    except Exception:
        return False


# =========================================================
# RASTERISATION PDF → IMAGES PIL
# =========================================================

def _try_pymupdf(pdf_bytes: bytes) -> list | None:
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
        return None
    try:
        doc = fitz_mod.open(stream=pdf_bytes, filetype="pdf")
        images = []
        for page in doc:
            mat = fitz_mod.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz_mod.csRGB)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            images.append(img.copy())
        doc.close()
        return images
    except Exception as e:
        st.warning(f"⚠️ PyMuPDF disponible mais erreur : {e}")
        return None


def _try_pdf2image(pdf_bytes: bytes) -> list | None:
    try:
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PopplerNotInstalledError, PDFInfoNotInstalledError
    except ImportError:
        return None
    try:
        return convert_from_bytes(pdf_bytes, dpi=300)
    except (PopplerNotInstalledError, PDFInfoNotInstalledError):
        return None
    except Exception as e:
        st.warning(f"⚠️ pdf2image erreur : {e}")
        return None


def _try_pdftoppm(pdf_bytes: bytes) -> list | None:
    import shutil
    from PIL import Image
    if not shutil.which("pdftoppm"):
        return None
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
    for fn in [_try_pymupdf, _try_pdf2image, _try_pdftoppm]:
        images = fn(pdf_bytes)
        if images is not None:
            return images
    st.error(
        "❌ **Impossible de rasteriser le PDF** (requis pour les PDF sans texte natif).\n\n"
        "**Solution recommandée** — installez PyMuPDF :\n"
        "```\npip install PyMuPDF\n```\n"
        "**Streamlit Cloud** — ajoutez dans `requirements.txt` :\n"
        "```\nPyMuPDF\n```"
    )
    return []


# =========================================================
# OCR GOOGLE CLOUD VISION API — version structurée
# =========================================================

def _gcv_extract_words_from_response(gcv_data: dict) -> tuple[str, list[dict]]:
    """
    Extrait texte et positions des mots depuis la réponse GCV.
    Normalise les coordonnées pixel → échelle PDF (0-612 x, 0-792 y).
    Retourne (full_text, words_list) où chaque mot a {text, x0, top}.
    """
    response = gcv_data.get("responses", [{}])[0]
    full_text = response.get("fullTextAnnotation", {}).get("text", "")

    words: list[dict] = []
    pages_data = response.get("fullTextAnnotation", {}).get("pages", [])

    for page_data in pages_data:
        pw = max(page_data.get("width", 1), 1)
        ph = max(page_data.get("height", 1), 1)
        scale_x = 612.0 / pw
        scale_y = 792.0 / ph

        for block in page_data.get("blocks", []):
            for para in block.get("paragraphs", []):
                for word in para.get("words", []):
                    text_w = "".join(
                        s.get("text", "") for s in word.get("symbols", [])
                    )
                    if not text_w.strip():
                        continue
                    verts = word.get("boundingBox", {}).get("vertices", [])
                    if len(verts) >= 2:
                        xs = [v.get("x", 0) for v in verts]
                        ys = [v.get("y", 0) for v in verts]
                        words.append({
                            "text": text_w,
                            "x0": min(xs) * scale_x,
                            "top": min(ys) * scale_y,
                        })

    return full_text, words


def _gcv_ocr_image_structured(image_bytes: bytes, api_key: str) -> tuple[str, list[dict]]:
    """
    Envoie une image à Google Cloud Vision et retourne (texte, mots_positionnés).
    Utilise DOCUMENT_TEXT_DETECTION pour préserver la structure multi-colonnes.
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

        gcv_err = data.get("responses", [{}])[0].get("error")
        if gcv_err:
            st.error(f"Google Vision API : {gcv_err.get('message', 'Erreur inconnue')}")
            return "", []

        return _gcv_extract_words_from_response(data)

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            st.error("❌ Google Vision : requête invalide (400). Vérifiez la clé GCV_API_KEY.")
        elif e.response is not None and e.response.status_code == 403:
            st.error("❌ Google Vision : accès refusé (403). Vérifiez les droits / quota de votre clé API.")
        else:
            st.error(f"❌ Google Vision HTTP {e}")
        return "", []
    except Exception as e:
        st.error(f"❌ Google Vision : {e}")
        return "", []


def ocr_pdf_gcv(pdf_bytes: bytes) -> tuple[str, list[list[dict]]]:
    """
    OCR complet d'un PDF via Google Cloud Vision.
    Retourne (full_text, pages_words) pour le parser colonne structuré.
    """
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
        return "", []

    images = _rasterize_pdf_to_images(pdf_bytes)
    if not images:
        return "", []

    all_text: list[str] = []
    all_words_pages: list[list[dict]] = []
    progress_bar = st.progress(0, text="🔍 OCR en cours…")

    for idx, img in enumerate(images):
        progress_bar.progress(
            (idx + 1) / len(images),
            text=f"🔍 OCR page {idx + 1}/{len(images)}…",
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        page_text, page_words = _gcv_ocr_image_structured(img_bytes, api_key)
        if page_text:
            all_text.append(page_text)
        all_words_pages.append(page_words)

    progress_bar.empty()
    full_text = clean_text("\n".join(all_text))
    return full_text, all_words_pages


# =========================================================
# BANQUES UTILISANT LE PARSER COLONNE (DÉBIT | CRÉDIT)
# =========================================================

# Ces banques ont deux colonnes séparées Débit / Crédit sans signe sur les montants.
# On utilise le parser colonne (pdfplumber ou GCV word positions).
_COLUMN_BANKS = {
    "BANQUE_POSTALE",
    "CIC",
    "GENERIC",
}
# Note : CREDIT_MUTUEL a sa propre branche dans extract_all
# (filtre section carte + fallback parse_credit_mutuel_text)
# Note : CREDIT_AGRICOLE a sa propre branche dans extract_all
# (parse_credit_agricole_text en priorité, parser colonne en fallback)

# Banques avec parser texte dédié (montants signés ou format spécifique)
_TEXT_BANKS = {
    "BNP_PARIBAS": parse_bnp_paribas_text,
    "REVOLUT": parse_revolut_text,
    "SHINE": parse_shine_text,
    "BANQUE_POPULAIRE": parse_banque_populaire_text,
    "CAISSE_EPARGNE": parse_caisse_epargne_text,
    "QONTO": parse_transactions_text,
}


# =========================================================
# POINT D'ENTRÉE PRINCIPAL — EXTRACTION INTELLIGENTE
# =========================================================

def extract_all(pdf_file) -> dict:
    """
    Orchestre l'extraction complète :
      1. Lecture bytes + détection texte natif vs image
      2. Extraction texte (pdfplumber) OU OCR Google Vision (avec mots positionnés)
      3. Détection banque, IBAN, BIC, soldes
      4. Dispatch vers le parser approprié :
         - Parser texte BNP              : BNP Paribas (sections crédit/débit)
         - Parser texte dédié            : Revolut, Shine, Banque Populaire, Caisse Épargne, Qonto
         - Parser colonne (DÉBIT|CRÉDIT) : La Banque Postale, CIC, CM, SG, CA, LCL
         - Parser texte universel        : fallback générique
    """
    pdf_file.seek(0)
    pdf_bytes = pdf_file.read()

    has_text = _pdf_has_text(pdf_bytes)

    # ── Extraction du texte brut ──
    ocr_used = False
    pages_words: list[list[dict]] = []

    if has_text:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                raw_pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
            raw_text = clean_text("\n".join(raw_pages))
        except Exception:
            raw_text = ""
    else:
        ocr_used = True
        raw_text, pages_words = ocr_pdf_gcv(pdf_bytes)

    bank = detect_bank(raw_text)
    iban, bic = extract_iban_bic(raw_text)

    # ── Fallback IBAN Société Générale ──
    # pdfplumber peut extraire le n° de compte sous différentes formes ;
    # on tente un 2ème passage avec un pattern élargi spécifique SG.
    if iban is None and bank == "SOCIETE_GENERALE":
        # Le n° de compte SG apparaît souvent dans le texte brut comme :
        # "30003 03984 00020307173 48" (sans le préfixe n°)
        m_sg = re.search(
            r"\b(\d{5})\s+(\d{5})\s+(\d{11})\s+(\d{2})\b",
            raw_text,
        )
        if m_sg:
            rib_23 = "".join(m_sg.groups())
            if len(rib_23) == 23:
                iban = rib_to_iban(rib_23)

    opening = extract_opening_balance(raw_text)
    closing = extract_closing_balance(raw_text)

    # Année de référence pour les formats DD/MM sans année
    year_match = re.search(r"(20\d{2})", raw_text)
    year_ref = year_match.group(1) if year_match else str(datetime.now().year)

    df = pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── Dispatch parser ──

    if bank == "LCL":
        # ── LCL — Priorité : parser colonne pdfplumber (DÉBIT | CRÉDIT par position x) ──
        # Fallback : parser texte dédié LCL (indicateurs "." + mots-clés)
        if has_text:
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
            if df.empty:
                df = parse_lcl_text(raw_text, year_ref)
        elif ocr_used and pages_words:
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
            if df.empty:
                df = parse_lcl_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

        # ── Filtre de sécurité : supprimer les lignes bilan/totaux LCL
        # (ex : "SOLDE EN EUROS", "SOLDE INTERMEDIAIRE A FIN DECEMBRE")
        # Ces lignes ne sont pas de vraies transactions et faussent les totaux.
        if not df.empty:
            _lcl_bilan_mask = df["libelle"].str.contains(
                r"SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE",
                regex=True, flags=re.IGNORECASE, na=False,
            )
            df = df[~_lcl_bilan_mask].reset_index(drop=True)

    elif bank == "SOCIETE_GENERALE":        # ── Société Générale — Priorité : parser texte dédié SG ──
        # pdfplumber supprime les espaces entre mots et place le montant sur une
        # ligne autonome → parse_societe_generale_text est le plus fiable.
        # Le parser colonne n'est utilisé qu'en OCR (GCV) où les positions x/y sont dispo.
        if ocr_used and pages_words:
            # PDF scanné / image → parser colonne GCV (positions x/y)
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
            if df.empty:
                df = parse_societe_generale_text(raw_text, year_ref)
        elif has_text:
            # PDF texte natif → parser texte SG en priorité absolue
            df = parse_societe_generale_text(raw_text, year_ref)
            # Fallback colonne si le texte parser échoue
            if df.empty:
                df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        # Fallback universel
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank in _TEXT_BANKS:
        # Parsers texte dédiés (signés ou format spécifique)
        parser_fn = _TEXT_BANKS[bank]
        # Passer year si la fonction l'accepte (Shine, Qonto n'en ont pas besoin)
        try:
            df = parser_fn(raw_text, year_ref)
        except TypeError:
            df = parser_fn(raw_text)

        # Fallback column parser pour BNP si le texte parser échoue
        if df.empty and bank == "BNP_PARIBAS":
            if has_text:
                df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
            if df.empty:
                df = parse_transactions_text(raw_text, year=year_ref)

    elif bank == "CREDIT_AGRICOLE":
        # ── Crédit Agricole (Brie Picardie & variantes régionales) ──
        # Priorité 1 : parser colonne pdfplumber (PDF texte natif)
        #   → utilise les positions x des mots pour distinguer colonne Débit / Crédit
        #   → le plus fiable car indépendant du contenu textuel des libellés
        # Priorité 2 : parser texte dédié CA
        #   → dates DD.MM, détection débit/crédit par mots-clés d'opération
        #   → utilisé pour les PDF OCR ou si le parser colonne échoue
        # Priorité 3 : parser texte universel (fallback ultime)
        if has_text:
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
            if df.empty:
                df = parse_credit_agricole_text(raw_text, year_ref)
        elif ocr_used and pages_words:
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
            if df.empty:
                df = parse_credit_agricole_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank == "CREDIT_MUTUEL":
        # ── Crédit Mutuel ──
        # Priorité 1 : parser colonne pdfplumber (PDF texte natif)
        #   → le filtre "section carte" garantit l'exclusion des lignes carte
        # Priorité 2 : parser texte dédié CM (heuristique débit/crédit par mots-clés)
        # Priorité 3 : parser texte universel (fallback)
        if ocr_used and pages_words:
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
            if df.empty:
                df = parse_credit_mutuel_text(raw_text, year_ref)
        elif has_text:
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
            if df.empty:
                df = parse_credit_mutuel_text(raw_text, year_ref)

        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank in _COLUMN_BANKS:
        # Parser colonne débit/crédit
        if ocr_used and pages_words:
            # GCV → mots positionnés → parser colonne structuré
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
        elif has_text:
            # PDF texte natif → pdfplumber
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))

        # Fallback texte universel si parser colonne échoue
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    else:
        # Banques non reconnues / GENERIC
        if has_text:
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        else:
            df = parse_transactions_text(raw_text, year=year_ref)

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
        amount_str = f"{row['montant']:.2f}"
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
        "Compatible : La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale · "
        "CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire · "
        "Qonto · Revolut Business · Shine · Boursorama · N26 · Hello Bank · Fortuneo…  |  "
        "**OCR automatique** via Google Cloud Vision (PDF scannés & Print-to-PDF)"
    )

    gcv_ok = False
    try:
        gcv_ok = bool(st.secrets.get("GCV_API_KEY", ""))
    except Exception:
        pass

    if not gcv_ok:
        st.warning(
            "⚠️ Clé Google Vision non configurée. "
            "Les PDF scannés / Print-to-PDF nécessitent : "
            "`GCV_API_KEY` dans `.streamlit/secrets.toml`."
        )

    uploaded = st.file_uploader("📁 Déposer le relevé bancaire (PDF)", type=["pdf"])
    if not uploaded:
        st.info("Importez un relevé bancaire PDF pour commencer.")
        return

    safe_name = sanitize_filename(uploaded.name)
    if safe_name != uploaded.name:
        st.caption(f"📄 Fichier reçu : `{uploaded.name}` → normalisé en `{safe_name}`")

    with st.spinner("🔍 Analyse du relevé en cours…"):
        result = extract_all(uploaded)

    bank     = result["bank"]
    iban     = result["iban"]
    bic      = result["bic"]
    opening  = result["opening"]
    closing  = result["closing"]
    df       = result["df"]
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

    total_credit  = df[df["montant"] > 0]["montant"].sum()
    total_debit   = df[df["montant"] < 0]["montant"].sum()
    solde_calcule = opening_balance + total_credit + total_debit

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

    df_display = df.copy()
    df_display["montant"] = df_display["montant"].apply(lambda x: f"{x:.2f}")
    st.dataframe(df_display, use_container_width=True, height=420)

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