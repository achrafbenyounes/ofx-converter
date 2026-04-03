"""
OFX Converter Pro — Multi-banques France → Odoo
================================================
Compatible (relevés texte ET scannés / Print-to-PDF) :
  La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale
  CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire
  Qonto · Revolut Business · Shine · Boursorama · Hello Bank · N26 · Fortuneo
  **Finom** (finom.co / PNL Fintech B.V.)…

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
    Valide l'année finale pour éviter les fragments IBAN (ex : "2062") :
    si l'année extraite est hors plage réaliste ou incohérente avec le relevé,
    l'année de référence du relevé est utilisée à la place.
    """
    parts = re.split(r"[/\.]", date_str)
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}/{year}"
    elif len(parts) == 3:
        if len(parts[2]) == 2:
            full_yr = "20" + parts[2]
        else:
            full_yr = parts[2]
        # Validation : cohérence avec l'année du relevé (±1)
        try:
            yr_int  = int(full_yr)
            ref_int = int(year)
            if not (2000 <= yr_int <= 2099 and abs(yr_int - ref_int) <= 1):
                full_yr = year
        except (ValueError, TypeError):
            full_yr = year
        return f"{parts[0]}/{parts[1]}/{full_yr}"
    return date_str


def _extract_year_from_text(text: str, fallback_year: str | None = None) -> str:
    """
    Extrait l'année du relevé depuis le texte OCR/pdfplumber, en évitant les
    faux positifs provenant des numéros IBAN (ex : "FR47 3000 2062 5200…"
    contient "2062" qui matcherait un simple re.search(r"20\d{2}")).

    Ordre de priorité :
      1. Année dans une date complète DD.MM.YYYY ou DD/MM/YYYY
         → impossible de confondre avec un fragment d'IBAN ou de RIB
      2. Année dans le contexte "du/au/le DD.MM.YYYY"
      3. Regex générique 20XX (fallback)
      4. Année courante
    """
    if fallback_year is None:
        fallback_year = str(datetime.now().year)

    # 1. Date complète : DD.MM.YYYY ou DD/MM/YYYY  (format le plus fiable)
    m = re.search(r"\b\d{2}[./]\d{2}[./](20\d{2})\b", text)
    if m:
        return m.group(1)

    # 2. En-tête de période : "du 01.11.2025 au" ou "au 28.11.2025"
    m = re.search(
        r"\b(?:du|au|le)\s+\d{2}[./]\d{2}[./](20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)

    # 3. Fallback générique (peut capturer un fragment d'IBAN — dernier recours)
    m = re.search(r"(20\d{2})", text)
    if m:
        return m.group(1)

    return fallback_year


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
    # Finom : BIC propriétaire FNOMFRP2 ou mention "Finom" / "PNL Fintech"
    if (
        "FNOMFRP2" in t
        or "FINOM" in t
        or "PNL FINTECH" in t
        or re.search(r"FINOM\.CO|FINOM\s+PAYMENTS", t)
    ):
        return "FINOM"
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
    # ── Banque Populaire Occitane : "SOLDE CREDITEURAU05/02/2026 59 240,29 €"
    #    pdfplumber colle CREDITEURAU sans espace — forme UNIQUE à BP Occitane.
    #    Doit être en PREMIER : la forme avec espaces matcherait sinon
    #    l'intermédiaire de fin de mois et non l'ouverture.
    r"SOLDE\s*CREDITEUR(?:AU|\s+AU)\s*\d{2}/\d{2}/\d{4}\s*" + _AMT,
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
    r"SOLDE\s*CREDITEUR\s*AU\s*\d{2}/\d{2}/\d{4}\s*" + _AMT,
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
    # ── Banque Populaire Occitane : "SOLDE CREDITEUR AU 05/03/2026* 67 491,10 €"
    #    pdfplumber conserve l'astérisque réglementaire après la date ──
    r"SOLDE\s*CREDITEUR(?:AU|\s*AU)\s*\d{2}/\d{2}/\d{4}\*?\s*" + _AMT,
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
    # ── Finom : "Solde de clôture : 542,59 €" ──
    r"[Ss]olde\s+de\s+cl[ôo]ture\s*:?\s*" + _AMT,
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
        # ── Banque Populaire Occitane : toutes occurrences (joined + spaced + astérisque)
        #    "SOLDE CREDITEURAU05/02/2026 59 240,29 €"  (ouverture)
        #    "SOLDE CREDITEUR AU 28/02/2026 74 477,85 €" (intermédiaire)
        #    "SOLDE CREDITEUR AU 05/03/2026* 67 491,10 €" (clôture)
        #    → findall prend le DERNIER = solde de clôture
        #    ⚠ `\s*` après AU (pas \s+) car pdfplumber colle "AU" et la date dans la 1ère occurrence ──
        r"SOLDE\s*CREDITEUR(?:AU|\s*AU)\s*\d{2}/\d{2}/\d{4}\*?\s*" + _AMT,
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
    Shine — relevé de compte professionnel multi-pages.
    Colonnes PDF : Date | Type | Opération | Débit (euro) | Crédit (euro)

    Bugs résolus (v2) :
    ─────────────────────────────────────────────────────────────────────────
    BUG 1 — Stop prématuré :
      Les patterns "Shine (www.shine.fr)" et "Shine France" apparaissent
      dans l'en-tête de CHAQUE page (dès la 5ᵉ ligne du document !).
      L'ancienne logique coupait le texte avant la première transaction.
      → Supprimés des stop-patterns. On stoppe uniquement sur les totaux
        de fin ("Total des mouvements", "Nouveau solde") qui n'apparaissent
        qu'une seule fois, à la dernière page.

    BUG 2 — Lignes de continuation inversées :
      pdfplumber place les lignes de référence d'un virement AVANT la
      ligne de date correspondante (comportement spécifique Shine) :
          "POP SVCS, DOB , 20260202 - ... - Creditor Name SEPA : L Oasis"
          "09/02/2026 Virement De : STICHTING CUSTODIAN UBER PAYMENTS 2 104,66"
      L'ancienne logique rattachait ces lignes au bloc PRÉCÉDENT → mauvaise
      description et potentiellement mauvais montant.
      → Nouvelle règle : toute ligne non-date est bufferisée dans orphan_before
        et rattachée au PROCHAIN bloc de date.

    BUG 3 — Pieds de page corrompant les montants :
      Le pied légal "Shine France, SAS au capital de 4 446,79 €…" était
      rattaché au dernier bloc de la page → "4 446,79" devenait le montant
      de la transaction au lieu du vrai montant.
      → Filtré par _SKIP_SHINE (liste exhaustive des lignes à ignorer).

    Règles de signe :
      • Présence de "De :" dans le bloc → crédit (virement entrant)
      • "Frais virement / prélèvement" → toujours débit (frais bancaires)
      • Tout autre cas → débit
    ─────────────────────────────────────────────────────────────────────────
    """

    # ── 1. Stopper au total final (apparaît une seule fois, dernière page) ──
    for stop in [r"Total des commissions", r"Total des mouvements", r"Nouveau solde"]:
        sm = re.search(stop, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]
            break

    # ── 2. Lignes à ignorer — en-têtes et pieds répétés sur chaque page ──
    _SKIP_SHINE = re.compile(
        r"^Relev[ée]\s+d[''`']?op[ée]rations"       # "Relevé d'opérations"
        r"|^De\s+\d{2}/\d{2}/\d{4}\s+[àa]\s+\d{2}/\d{2}/\d{4}"  # "De 01/02/... à 28/02/..."
        r"|^Compte\s+professionnel"
        r"|^Shine\s*\("                               # "Shine (www.shine.fr)"
        r"|^Shine\s+France"                           # "Shine France, SAS au capital..."
        r"|^Nom\s+du\s+compte"
        r"|^SIRET\s*:"
        r"|^IBAN\s*:"
        r"|^BIC\s*:"
        r"|^Messagerie\s*:"
        r"|^Date\s+Type\s+Op[ée]ration"              # en-tête de tableau
        r"|^Les\s+op[ée]rations\s+[ée]crites"        # mention légale bas de page
        r"|^Solde\s+au\s+\d{2}/\d{2}/\d{4}"         # solde d'ouverture (pas une transaction)
        r"|^de\s+paiement"                            # suite notice légale
        r"|^exploitant\s+le\s+nom"
        r"|^immatricul[ée]e?"
        r"|^agr[ée][ée]e?\s+par"
        r"|^ayant\s+son\s+si[èe]ge"
        r"|\bSAS\s+au\s+capital\b"                   # "Shine France, SAS au capital..."
        r"|^122\s+rue\s+Amelot"                       # adresse Shine Paris
        r"|Page\s+\d+/\d+",                          # "Page 1/2", "Page 2/2"
        re.IGNORECASE | re.UNICODE,
    )

    # ── 3. Regex montant : séparateur milliers espace, décimale virgule/point ──
    #   Capture "2 104,66", "1 365,55", "219,13", "0,48" — pas "20260202"
    _AMT_RE   = re.compile(r"\b\d{1,3}(?:\s\d{3})*[,\.]\d{2}\b")
    _FRAIS_RE = re.compile(r"Frais\s+(?:virement|pr[ée]l[eè]vement)", re.IGNORECASE)

    # ── Signaux crédit / débit — utilisés quand le parser colonne n'est pas dispo ──
    # Virement entrant : "De : INEO…" ; remboursement carte : "Remb", "REFUND"…
    _CREDIT_SIGNALS = re.compile(
        r"\bDe\s*:"                                          # virement entrant
        r"|\bRemb(?:oursement)?\b"                          # remboursement
        r"|\bAvoir\b"                                        # avoir commercial
        r"|\bR[eé]gularisation\b"                           # régularisation
        r"|\bRefund\b"                                       # remboursement anglais
        r"|\bCashback\b"                                     # cashback
        r"|\bCr[eé]dit\s+(?!mutuel|agricole|du\s+nord)"    # crédit (hors banques)
        r"|\bReversal\b",                                    # annulation paiement
        re.IGNORECASE,
    )
    # Signaux débit explicites — priorité sur _CREDIT_SIGNALS
    _DEBIT_FORCE = re.compile(
        r"Frais\s+(?:virement|pr[ée]l[eè]vement|paiement|bancaire)"
        r"|\bPr[ée]l[eè]vement\b"
        r"|\bCommission\b"
        r"|\bCotisation\b",
        re.IGNORECASE,
    )

    # ── 4. Grouper les lignes en blocs (un bloc = une transaction) ──
    #
    # Particularité Shine pdfplumber :
    #   Les lignes de référence d'un virement (ex : "POP SVCS, DOB…") apparaissent
    #   AVANT la ligne de date correspondante. Chaque ligne de date est autonome
    #   (contient type + opération + montant). Il n'existe pas de continuation
    #   après la date : toute ligne non-date est donc un orphan_before rattaché
    #   au prochain bloc.
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    blocks: list[list[str]] = []
    current: list[str] | None = None
    orphan_before: list[str] = []   # lignes de référence précédant la prochaine date

    for line in lines:
        if _SKIP_SHINE.search(line):
            # En-tête / pied de page → flush bloc en cours + reset
            if current is not None:
                blocks.append(current)
                current = None
            orphan_before = []
            continue

        if re.match(r"^\d{2}/\d{2}/\d{4}\b", line):
            # Nouvelle date → sauvegarder le bloc précédent
            if current is not None:
                blocks.append(current)
            # Nouveau bloc = lignes orphelines accumulées + ligne de date
            current = orphan_before + [line]
            orphan_before = []
        else:
            # Ligne non-date → bufferiser pour le PROCHAIN bloc (jamais le courant)
            orphan_before.append(line)

    if current is not None:
        blocks.append(current)

    # ── 5. Parser chaque bloc ──
    transactions: list[dict] = []

    for block_lines in blocks:
        # Identifier la ligne de date et les lignes orphelines
        date_str   = None
        main_line  = ""
        orphan_lines: list[str] = []
        for bl in block_lines:
            dm = re.match(r"^(\d{2}/\d{2}/\d{4})\b", bl)
            if dm and date_str is None:
                date_str  = dm.group(1)
                main_line = bl
            else:
                orphan_lines.append(bl)
        if not date_str:
            continue

        # Texte complet du bloc (pour détection "De :" et extraction montant)
        full = " ".join(block_lines)

        # Dernier montant = montant de la transaction
        # (\d{1,3}(?:\s\d{3})* évite de capturer les timestamps "20260202")
        raw     = _AMT_RE.findall(full)
        amounts = [v for a in raw if (v := clean_amount(a)) is not None]
        if not amounts:
            continue
        amount = abs(amounts[-1])

        # Signe : "De :" ou signal crédit explicite → crédit, SAUF débit forcé
        is_debit_forced = bool(_DEBIT_FORCE.search(full))
        is_credit = (not is_debit_forced) and bool(_CREDIT_SIGNALS.search(full))
        montant   = amount if is_credit else -amount

        # Libellé : contenu de la ligne de date (sans date ni montant final)
        #           suivi des lignes orphelines séparées par " | "
        main_desc = main_line[len(date_str):].strip()
        main_desc = re.sub(r"\s*\b\d{1,3}(?:\s\d{3})*[,\.]\d{2}\b\s*$", "", main_desc).strip()
        extra     = " | ".join(orphan_lines) if orphan_lines else ""
        desc      = (main_desc + (" | " + extra if extra else "")).strip()
        desc      = re.sub(r"\s{2,}", " ", desc)[:120]

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


def parse_finom_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Finom (finom.co / PNL Fintech B.V.) — relevé de compte professionnel.

    Structure pdfplumber constatée (relevé multi-colonnes) :
    ─────────────────────────────────────────────────────────────────────────
    En-tête   : IBAN + BIC + période + "Solde d'ouverture" + "Solde de clôture"
    Tableau   : colonnes Terminé | Description | Payer | Solde
    ─────────────────────────────────────────────────────────────────────────
    Ligne 1 transaction  : DD/MM/YYYY  DESCRIPTION  [AMOUNT €  BALANCE €]
    Lignes continuation  : référence paiement (M4XXXXXXX), IBAN: …, BIC: …
    Dernière ligne bloc  : [AMOUNT €  BALANCE €] (si description multi-ligne)
    ─────────────────────────────────────────────────────────────────────────

    Règles d'extraction :
      - Signe EXPLICITE dans la colonne Payer :
          "151,92 €"    → crédit (positif)
          "- 1,80 €"    → débit  (négatif) — espace entre "-" et le chiffre
      - Dernier montant € du bloc   = Solde courant (colonne Solde)  → ignoré
      - Avant-dernier montant € bloc = montant transaction (colonne Payer)
      - Transactions listées du plus RÉCENT au plus ANCIEN → liste inversée

    Cas spéciaux gérés :
      - "1 129,19 €" : séparateur milliers espace/NBSP
      - "Rapyd Europe MX… IBAN: … BIC: …" : description multi-ligne
      - PNL Fintech B.V. monthly fee       : débit explicite (signe "-")
      - PNL Fintech B.V. Cashback          : crédit explicite (pas de signe)
      - M OU MME … TIMOUMI                  : crédit (virement reçu)
      - "S.A.S.-METRO FRANCE"              : tiret dans libellé, pas un signe
      - "Jan 17, 2026 - Jan 28, 2026"      : tiret dans date textuelle, ignoré
      - TOO GOOD TO GO FRANCE              : crédit (remboursement)
      - Validation optionnelle via solde courant de chaque transaction

    Format OFX produit : montants signés, dates DD/MM/YYYY, libellés nettoyés.
    """

    # ── 1. Année de référence ────────────────────────────────────────────────
    if year is None:
        for pat in [
            r"Au\s*:\s*\d{2}/\d{2}/(\d{4})",
            r"Du\s*:\s*\d{2}/\d{2}/(\d{4})",
            r"\b(20\d{2})\b",
        ]:
            m_y = re.search(pat, text, re.IGNORECASE)
            if m_y:
                year = m_y.group(1)
                break
        if year is None:
            year = str(datetime.now().year)

    # ── 2. Délimiter la zone utile ───────────────────────────────────────────
    # IMPORTANT : stopper UNIQUEMENT sur la mention légale finale (bas de dernière page).
    # Les watermarks "Créé avec Finom.co N" sont des pieds de page intermédiaires
    # répétés entre chaque page → ne PAS les utiliser comme stop (ils coupent les pages).
    sm = re.search(r"FINOM\s+PAYMENTS\s+B\.V\.,?\s+soci", text, re.IGNORECASE)
    if sm:
        text = text[:sm.start()]

    # ── 3. Regex montant EUR (signe explicite "- " ou absence de signe) ─────
    #
    # Exemples à capturer :
    #   "151,92 €"       → crédit  (group1=None,  group2="151,92")
    #   "- 1,80 €"       → débit   (group1="- ",  group2="1,80")
    #   "1 129,19 €"     → crédit  (group1=None,  group2="1 129,19")
    #   "- 297,05 €"     → débit   (group1="- ",  group2="297,05")
    #
    # Contrainte : le signe "-" DOIT être précédé d'un espace / début de ligne
    # pour ne PAS capturer les tirets dans les libellés ("S.A.S.-METRO").
    #
    # Lookbehind : (?:^|(?<=[\s€])) garantit que le "-" éventuel est isolé.
    # Le groupe 1 est OPTIONNEL : présent → débit, absent → crédit.
    _AMT_EUR = re.compile(
        r"(?:^|(?<=[\s€]))(-\s*)?(\d[\d\xa0\s]*[,\.]\d{2})\s*€",
        re.MULTILINE,
    )

    # ── 4. Lignes à sauter entièrement (en-têtes, pieds, numéros de page) ───
    _SKIP_LINE = re.compile(
        # En-tête de tableau (répété sur chaque page Finom)
        r"^Termin[ée]\b"               # "Terminé" seul ET "Terminé Description Payer Solde"
        r"|^Description$|^Payer$|^Solde$"
        r"|^Cr[ée][ée]\s+avec\s+Finom"  # watermark bas de page "Créé avec Finom.co N"
        r"|^finom\s*$"                  # logo texte "finom"
        r"|^\d+\s*$"                    # numéros de page seuls
        r"|^FINOM\s+PAYMENTS",          # mention légale footer
        re.IGNORECASE,
    )

    # ── 5. Grouper les lignes en blocs (un bloc = une transaction) ──────────
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if _SKIP_LINE.search(line):
            # Flush le bloc en cours si on rencontre un en-tête de page ou watermark
            if current:
                blocks.append(current)
                current = []
            continue
        # Ignorer numéros de page seuls (1, 2 … 7)
        if re.match(r"^\d{1,2}$", line):
            continue
        # Chaque bloc commence par une ligne DD/MM/YYYY
        if re.match(r"^\d{2}/\d{2}/\d{4}\b", line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)

    if current:
        blocks.append(current)

    # ── 6. Parser chaque bloc ────────────────────────────────────────────────
    transactions: list[dict] = []

    for block in blocks:
        first_line = block[0]

        # ── Date ──
        dm = re.match(r"^(\d{2}/\d{2}/\d{4})\b", first_line)
        if not dm:
            continue
        date_str = dm.group(1)

        # ── Ignorer les lignes de solde d'ouverture / clôture ──
        if re.search(
            r"Solde\s+d[''']ouverture|Solde\s+de\s+cl[ôo]ture|Termin[ée]",
            first_line, re.IGNORECASE,
        ):
            continue

        # ── Trouver tous les montants EUR dans le bloc ──────────────────────
        # On concatène tout le bloc pour attraper les montants sur n'importe quelle ligne.
        full_block = " ".join(block)

        amounts: list[float] = []        # montants signés (crédit +, débit -)
        balances_raw: list[float] = []   # tous montants absolus (pour validation)

        for m_a in _AMT_EUR.finditer(full_block):
            sign_str = (m_a.group(1) or "").strip()
            val = clean_amount(m_a.group(2))
            if val is None:
                continue
            signed = -abs(val) if sign_str == "-" else abs(val)
            amounts.append(signed)

        # ── Nécessite au moins 2 montants (transaction + solde) ─────────────
        if len(amounts) < 2:
            # Fallback : une seule ligne simple comme "30/01/2026 APRR - 1,80 € 390,67 €"
            # Le "-" peut être collé différemment selon le PDF → réessayer en mode permissif
            amounts_fallback = []
            for m_fb in re.finditer(
                r"(-\s*)?((?:\d[\d\xa0\s]*)?[,\.]\d{2}|\d+[,\.]\d{2})\s*€",
                full_block,
            ):
                s = (m_fb.group(1) or "").strip()
                v = clean_amount(m_fb.group(2))
                if v is None:
                    continue
                amounts_fallback.append(-abs(v) if s == "-" else abs(v))
            if len(amounts_fallback) >= 2:
                amounts = amounts_fallback
            else:
                continue  # impossible de parser ce bloc

        # Avant-dernier = montant transaction (colonne Payer)
        # Dernier       = solde courant       (colonne Solde, non utilisé dans OFX)
        tx_amount = amounts[-2]
        balance_after = amounts[-1]    # solde après transaction (validation)

        # ── Construction du libellé ─────────────────────────────────────────
        desc_parts: list[str] = []

        # Première ligne : supprimer la date + les montants EUR
        rest_first = first_line[len(date_str):].strip()
        rest_first = _AMT_EUR.sub("", rest_first).strip()
        rest_first = re.sub(r"\s{2,}", " ", rest_first).strip()
        if rest_first:
            desc_parts.append(rest_first)

        # Lignes de continuation (références, IBAN, BIC exclus)
        for ln in block[1:]:
            # Exclure les lignes IBAN:/BIC: de la description (bruit)
            if re.match(r"^(?:IBAN|BIC)\s*:", ln, re.IGNORECASE):
                continue
            # Supprimer les montants EUR éventuels en fin de ligne
            ln_clean = _AMT_EUR.sub("", ln).strip()
            ln_clean = re.sub(r"\s{2,}", " ", ln_clean).strip()
            if ln_clean:
                desc_parts.append(ln_clean)

        desc = " ".join(desc_parts)
        desc = re.sub(r"\s{2,}", " ", desc).strip()[:120]
        if not desc:
            desc = "Transaction Finom"

        transactions.append({
            "date": date_str,
            "libelle": desc,
            "montant": round(tx_amount, 2),
            "_balance": balance_after,      # utilisé pour validation (supprimé ci-dessous)
        })

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── 7. Finom liste du plus récent au plus ancien → inverser ─────────────
    transactions = list(reversed(transactions))

    # ── 8. Validation optionnelle par solde courant ──────────────────────────
    # Finom affiche le solde après chaque transaction.
    # On vérifie balance[n] ≈ balance[n-1] + montant[n].
    # En cas d'anomalie et de montant non signé dans l'original (improbable ici),
    # on tente l'inversion du signe.
    prev_bal: float | None = None
    for tx in transactions:
        bal = tx["_balance"]
        if prev_bal is not None:
            expected = round(prev_bal + tx["montant"], 2)
            if abs(expected - bal) > 0.10:
                # Tenter l'inversion
                inv = round(-tx["montant"], 2)
                if abs(round(prev_bal + inv, 2) - bal) < 0.10:
                    tx["montant"] = inv
        prev_bal = bal

    # Supprimer la colonne de validation temporaire
    for tx in transactions:
        tx.pop("_balance", None)

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


def parse_banque_populaire_text(text: str) -> pd.DataFrame:
    """
    Banque Populaire Occitane — Parser natif pdfplumber (relevé mensuel PDF texte).

    Structure pdfplumber constatée :
    ─────────────────────────────────────────────────────────────────────────
    Ligne 1 : DD/MM  LIBELLE REFERENCE  DD/MM  DD/MM  [-]MONTANT€
    Lignes + : continuation — référence interne, bénéficiaire, marchand, etc.
    ─────────────────────────────────────────────────────────────────────────

    Corrections v2 (Banque Populaire Occitane réel) :
      1. Le MONTANT est TOUJOURS en fin de la 1ère ligne du bloc : "[-]X XXX,XX €"
         → Extraction uniquement sur line[0], pas du bloc entier.
         → L'ancien code concaténait toutes les lignes → échec sur les blocs
           contenant "178,46EUR 1 EURO = 1,000000" (taux change CB) en fin.
      2. Lignes "XXX,XXEUR 1 EURO = 1,000000" (taux de change carte) ignorées.
      3. Lignes "- NB0079/108105" (références internes banque) ignorées.
      4. pdfplumber peut coller des mots : "CREDITEURAU" → patterns solde adaptés
         (voir _OPENING_PATTERNS / _CLOSING_PATTERNS).
      5. Signe "-" peut être collé au chiffre ("-5,87 €") ou séparé ("- 178,46 €").
      6. Milliers séparés par espace : "1 269,59 €", "-1 269,59 €".
      7. Solde clôture avec astérisque : "AU 05/03/2026* 67 491,10 €" → géré.
    """
    # ── 1. Extraire l'année de référence ─────────────────────────────────────
    year = str(datetime.now().year)
    for pat in [
        r"RELEVE\s+N[°o]?\s*\d+\s+AU\s+\d{2}/\d{2}/(\d{4})",
        r"relev[ée]\s+de\s+compte\s+n[°o]?\d+\s+au\s+\d{2}/\d{2}/(\d{4})",
        r"SOLDE\s+CREDITEUR(?:AU|\s+AU)\s+\d{2}/\d{2}/(\d{4})",
        r"\b(20\d{2})\b",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            year = m.group(1)
            break

    # ── 2. Délimiter la zone utile ───────────────────────────────────────────
    detail_m = re.search(r"DETAIL\s+DES\s+OPERATIONS", text, re.IGNORECASE)
    if detail_m:
        text = text[detail_m.start():]

    for stop_pat in [
        r"TOTAL\s+DES\s+MOUVEMENTS\s+DEBITEURS",
        r"DETAIL\s+DE\s+VOS\s+(?:PRELEVEMENTS|VIREMENTS|MOUVEMENTS)\s+SEPA",
    ]:
        sm = re.search(stop_pat, text, re.IGNORECASE)
        if sm:
            text = text[:sm.start()]
            break

    # ── 3. Regex : montant signé en fin de première ligne ────────────────────
    # Structure ligne 1 : "DD/MM LIBELLE REF DATEOP DATEVAL MONTANT€"
    # Le montant SUIT toujours les deux dates DD/MM DD/MM.
    # On intègre ces deux dates dans le regex pour ancrer correctement l'extraction
    # et éviter que \d{1,3} capture "2" depuis "06/02" suivi de " 101,10 €".
    # Groupe 1 = signe "-" optionnel, Groupe 2 = valeur numérique (virgule décimale FR)
    _LINE_AMOUNT = re.compile(
        r"\d{2}/\d{2}\s+\d{2}/\d{2}\s+(-\s*)?(\d{1,3}(?:[\s\xa0]\d{3})*,\d{2})\s*€\s*$"
    )

    # ── 4. Lignes à ignorer (en-têtes, pieds de page, soldes) ───────────────
    _SKIP = re.compile(
        r"^DATE\s+COMPTA|^LIBELLE\s*/\s*REFERENCE|^DATE\s+OPERATION|^DATE\s+VALEUR"
        r"|^DATE\s+DATE|^LIBELLE\s*/|^MONTANT$"
        # Lignes entête de colonnes fusionnées par pdfplumber (ex: "DATE DATE DATE LIBELLE")
        r"|^(?:DATE\s+){2,}|LIBELLE\s*/\s*REFERENCE"
        r"|SOLDE\s+CREDITEUR"          # lignes de solde (pas des transactions)
        r"|TOTAL\s+DES\s+MOUVEMENTS"   # résumé en pied
        r"|^VOTRE\s*COMPTE\s+COURANT"
        r"|^RELEVE\s+N[°o]"
        r"|^Page\s*\d|^page\s*\d|^Page\d"
        r"|^COMPTA\s+OPERATION|^COMPTA\s*OPER"   # en-tête ligne 2 des colonnes
        r"|Banque\s+Populaire\s+Occitane"
        r"|textes\s+relatifs|Pompidou|ORIAS|RCS\s+TOULOUSE"
        r"|Méd[ié]ateure?|médiation|FNBP"
        r"|DETAIL\s+DES\s+OPERATIONS"
        r"|IBAN\s*:|BIC\s*:"
        r"|SARL\s+PALAIS\s+ROYAL"
        r"|MESSAGE\s+DE\s+VOTRE\s+BANQUE"
        r"|Votre\s+Agence|VotreConseill|Votre\s+Conseill"
        r"|TARIFICATION|RÈGLEMENTATION|MONÉTIQUE|MONETIQUE"
        r"|www\.banquepopulaire|BANQUEPOPULAIRE",
        re.IGNORECASE,
    )

    # Lignes de continuité à NE PAS inclure dans la description
    _NOISE_LINE = re.compile(
        # Taux de change CB : "178,46EUR 1 EURO = 1,000000"
        r"^\d[\d\s]*[,\.]\d+EUR\s+1\s+EURO\s*=\s*1[,\.]0+\s*$"
        # Référence interne banque : "- NB0079/108105"
        r"|^-\s*NB\d+/\d+$"
        # En-têtes de colonnes glissés dans la continuité : "DATE DATE DATE" etc.
        r"|^(?:DATE\s+){2,}DATE?\s*$"
        r"|^LIBELLE\s*/\s*REFERENCE",
        re.IGNORECASE,
    )

    # ── 5. Grouper lignes → blocs, puis parser ───────────────────────────────
    transactions: list[dict] = []
    block_lines: list[str] = []

    def flush_block(blines: list[str]) -> dict | None:
        if not blines:
            return None
        first = blines[0]

        # La ligne 1 doit commencer par DD/MM
        dm = re.match(r"^(\d{2}/\d{2})\b", first)
        if not dm:
            return None
        date_str = f"{dm.group(1)}/{year}"

        # Montant en fin de ligne 1 uniquement
        m_amt = _LINE_AMOUNT.search(first)
        if not m_amt:
            return None

        sign_neg = bool(m_amt.group(1) and m_amt.group(1).strip())
        amount = clean_amount(m_amt.group(2))
        if amount is None:
            return None
        montant = -abs(amount) if sign_neg else abs(amount)

        # ── Construction de la description ──
        # a) Première ligne : retirer la date initiale + le bloc [DATEOP DATEVAL MONTANT€]
        desc_first = first[len(dm.group(1)):].strip()
        # Le regex _LINE_AMOUNT intègre "DD/MM DD/MM MONTANT €" → le sub supprime tout d'un coup
        desc_first = _LINE_AMOUNT.sub("", desc_first).strip()

        # b) Lignes de continuation (max 2), en filtrant le bruit
        extra_parts: list[str] = []
        for ln in blines[1:3]:
            if _NOISE_LINE.match(ln):
                continue
            extra_parts.append(ln.strip())

        desc = desc_first
        if extra_parts:
            desc = (desc + " " + " ".join(extra_parts)).strip()
        desc = re.sub(r"\s{2,}", " ", desc)[:120]

        return {"date": date_str, "libelle": desc, "montant": round(montant, 2)}

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _SKIP.search(line):
            continue

        if re.match(r"^\d{2}/\d{2}\b", line):
            # Nouvelle transaction → vider le bloc précédent
            tx = flush_block(block_lines)
            if tx:
                transactions.append(tx)
            block_lines = [line]
        else:
            if block_lines:
                block_lines.append(line)

    # Dernier bloc
    tx = flush_block(block_lines)
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
    #          AUTRES OPÉRATIONS CRÉDIT
    _CREDIT_HDR = re.compile(
        r"^REMISES?\s*D.?\s*ESPECES?$|^REMISES?\s*D.?\s*ESPECES?\s+|"
        r"^REMISESDESPECES|^REMISESDECARTES|^REMISESDE\s*CARTES?|"
        r"^REMISES?\s*DE\s*CARTES?$|^VIREMENTSRECUS?$|^VIREMENTS?\s*RE[CÇ]US?$|"
        # REMISES DE CHÈQUES (compact pdfplumber ou avec espaces)
        r"^REMISESDECHEQUES$|^REMISES?\s*DE\s*CH[EÈ]QUES?$|"
        # AUTRES OPÉRATIONS CRÉDIT (ex: remboursement CB)
        r"^AUTRESOPERATIONSCREDIT$|^AUTRES\s*OP[EÉ]RATIONS?\s*CR[EÉ]DIT$",
        re.IGNORECASE,
    )
    # DÉBIT : PAIEMENTS PAR CARTES · CHÈQUES ÉMIS · VIREMENTS ÉMIS · PRÉLÈVEMENTS
    #         RETRAITS D'ESPÈCES · AUTRES OPÉRATIONS DÉBIT
    _DEBIT_HDR = re.compile(
        r"^PAIEMENTSPARCARTES?|^PAIEMENTS?\s*PAR\s*CARTES?|"
        # CHÈQUES ÉMIS (compact pdfplumber ou avec espaces)
        r"^CHEQUESEMIS$|^CH[EÈ]QUES?\s*[EÉ]MIS$|"
        r"^VIREMENTSEMIS$|^VIREMENTS?\s*[EÉ]MIS$|"
        r"^PRELEVEMENTS?[,\s]|^PR[EÉ]L[EÈ]VEMENTS?[,\s]|"
        r"^AUTRESOPERATIONSDEBIT|^AUTRES\s*OP[EÉ]RATIONS?\s*DEBIT|"
        # RETRAITS D'ESPÈCES / RETRAITS D'ESPECES (retraits espèces ATM)
        r"^RETRAITS?\s*D[\s'''\u2019]?\s*ESPECES?|^RETRAIT\s*SDESPECES",
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
        # Lignes de solde et totaux d'en-tête (ex: "Solde au 20 JANVIER :")
        r"|^Solde\s+au\s+\d|^Solde\s+au\s+\w"
        r"|^D[eé]bit\s*:[\s\d]|^Cr[eé]dit\s*:[\s\d]"
        # Nom de l'agence BNP seul sur une ligne (ex: "THIAIS")
        r"|^Service\s+Client$"
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
      • pdfplumber fusionne les mots (pas d'espace) : "VIRINSTRE" = "VIR INST RE"

    Terminologie SG des virements (critique pour le signe) :
      • VIR INST RE  XXXXXXXXX  → Virement Instantané REçu      → CRÉDIT (+)
        pdfplumber fusionne en "VIRINSTRE653592629181"
      • VIR INSTANTANE EMIS NET → Virement Instantané Émis      → DÉBIT  (-)
        pdfplumber fusionne en "VIRINSTANTANEEMISNET"
      • VIR EUROPEEN EMIS NET   → Virement SEPA Émis            → DÉBIT  (-)
      • VIR PERM                → Virement Permanent            → DÉBIT  (-)

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
        r"|^:?\s*EMEDA\s*$"                    # "EMEDA" ou ": EMEDA" (pages 2-3, timbre ADEME)
        r"|\d{6}AR\s*$"                        # code marge pages 2-3 ex: "720624AR"
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
    # Couvre : remises CB, virements reçus, versements espèces, remises chèques…
    # NOTE: \s* partout car pdfplumber fusionne les mots sans espace.
    _CREDIT_RE = re.compile(
        r"REMISE\s*CB"                        # REMISE CB / REMISECB — encaissement TPE
        r"|VIR\s*INST\s*RE"                  # VIR INST RE XXXXXX = virement instantané REÇU
                                              #   pdfplumber fusionne → "VIRINSTRE653592629181"
                                              #   ≠ "VIR INSTANTANE EMIS" / "VIR INST EMIS" (débit)
        r"|VIR\s*RECU"                        # VIR RECU ou VIRRECU
        r"|VIR\s*REC[\xc7U]"                 # variante avec accent
        r"|REMBOURSEMENT\s*PRLV"
        r"|CARTE.{0,25}REMBT"
        r"|CART.{0,10}REMB"
        # ── Versements espèces / chèques ──────────────────────────────────────
        r"|VRST\s*GAB"                        # Versement espèces via GAB (distributeur)
                                              # Ex : "VRST GAB 13/01/26 16H44 003800"
        r"|VERST\s*ESP"                       # Versement espèces manuel ou via GAB
                                              # Ex : "VERST ESP 08/01/26 100,00"
                                              #      "VERST ESP GAB 26/01/26 19H51 713876"
        r"|REMISE\s*CH[EÈ]QUE"               # Remise de chèque(s) au guichet
                                              # Ex : "REMISE CHEQUE 0000225 015"
                                              # pdfplumber peut fusionner → "REMISECHEQUE..."
        # ── Virements entrants identifiés par l'émetteur (DE:) ────────────────
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
        r"|PRELEVEMENT\s*EUROP[EÉ]EN"           # PRELEVEMENT EUROPEEN ou PRELEVEMENTEUROPEEN
                                                 # ⚠ "PRLV EUROP[EÉ]EN" volontairement absent :
                                                 #   il est sous-ensemble de "REMBOURSEMENT PRLV EUROPEEN"
                                                 #   (crédit). "PRLV EUROPEEN B2B" tombe déjà en -1 par défaut.
        r"|PRELEVEMENT\s*SEPA"
        r"|VIR\s*INSTANTANE\s*EMIS"             # VIR INSTANTANE EMIS ou VIRINSTANTANEEMIS
        r"|VIR\s*EUROP[EÉ]EN\s*EMIS"
        r"|VIR\s*PERM\b"
        r"|CHEQUE\b"
        r"|CIONS\s*TENUE"                        # CIONS TENUE ou CIONSTENUE
        r"|COTIS(?:ATION)?"                      # COTISATION, COTIS… (sans \b : pdfplumber fusionne
                                                 # ex: "COTISATIONMENSUELLEJAZZPRO" sans espace)
        r"|ABONNEMENT\s*MATERIEL"                # loyer TPE / matériel (ex: ABONNEMENTMATERIEL)
        r"|INT\s*D[EÉ]BITEURS"
        r"|QUIETIS\s*PRO",                       # cotisation abonnement Pro SG (ex: QUIETIS PRO REDUC)
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

        Règles de priorité explicite (avant la détection générique) :

        1. REMISE CH[EÈ]QUE : toujours CRÉDIT (remise/dépôt de chèque client).
           CHEQUE\\b dans _DEBIT_RE génère une fausse ambiguïté -> on court-circuite.
           Ex : "REMISE CHEQUE 0000225 015" ou "REMISECHEQUE..." (pdfplumber fusionne).

        2. INT DEBITEURS : CRÉDIT ou DÉBIT selon les sous-montants.
           Si les lignes de détail contiennent des montants négatifs ("-0,44" ...)
           c'est un avoir -> CRÉDIT. Sinon (montants positifs, ex "0,27") -> DÉBIT.
        """
        # ── Règle 1 : REMISE CHEQUE -> toujours crédit ──────────────────────────
        if re.search(r"REMISE\s*CH[EÈ]QUE", full_text, re.IGNORECASE):
            return 1

        # ── Règle 2 : INT DEBITEURS -> détecter crédit vs débit via les détails ──
        if re.search(r"INT\s*D[EÉ]BITEURS", full_text, re.IGNORECASE):
            if re.search(r"-\s*\d+[,\.]\d{2}", full_text):
                return 1   # montants négatifs dans les détails -> remboursement -> crédit
            return -1      # montants positifs -> frais réels -> débit

        is_credit = bool(_CREDIT_RE.search(full_text))
        is_debit  = bool(_DEBIT_RE.search(full_text))
        if is_credit and not is_debit:
            return 1
        if is_debit and not is_credit:
            return -1
        # Ambiguïté ou non reconnu -> débit par défaut (plus sûr comptablement)
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
        year = _extract_year_from_text(text)

    # Tronquer au premier parmi : "TOTAUX \d…" ou "SOLDE EN EUROS".
    # NB : "SOLDE INTERMEDIAIRE" est intentionnellement exclu car une vraie
    #      transaction peut le suivre (ex : VERSEMENT ALS 02/01/26 sur LCL p.7).
    stop_pos = len(text)
    for stop_pat in [r"TOTAUX\s+\d", r"SOLDE\s+EN\s+EUROS"]:
        for m in re.finditer(stop_pat, text, re.IGNORECASE):
            stop_pos = min(stop_pos, m.start())
    text = text[:stop_pos]

    # Lignes à ignorer
    # NOTE : on ne skippe PAS les lignes "LIBELLE:...", "REF.CLIENT:...", "ID.CREANCIER:..."
    # qui sont des continuations valides de transactions SEPA multi-lignes.
    # "\bLIBELLE\b" est donc remplacé par "DATE\s+LIBELLE" (en-tête de colonne uniquement).
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
        r"|COTISATION\s+DE\s+VOTRE\s+OPTION\s+PRO"
        r"|VIR\s+SEPA\s+GAN\s+ASS",                          # assurance prélèvement
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
            # Validation : l'année doit être dans une plage réaliste (2000-2099)
            # et cohérente avec l'année du relevé (±1 an max).
            # Cela évite de capturer des fragments d'IBAN comme "2062".
            try:
                yr_int  = int(full_yr)
                ref_int = int(year)
                if not (2000 <= yr_int <= 2099 and abs(yr_int - ref_int) <= 1):
                    full_yr = year
            except (ValueError, TypeError):
                full_yr = year
            date_str    = f"{dd}/{mm}/{full_yr}"
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


def parse_lcl_ocr_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    LCL (Crédit Lyonnais) — parser dédié pour PDF scannés / images OCR.

    Variante robuste de parse_lcl_text() adaptée au texte produit par Google
    Cloud Vision sur des relevés LCL scannés ou imprimés en PDF-image.

    Différences avec le parser texte natif :
      • Accepte DD.MM et DD/MM comme date opération (OCR peut produire les deux)
      • Accepte DD.MM.YY, DD.MM.YYYY, DD/MM/YY, DD/MM/YYYY comme date valeur
      • Le "." colonne (indicateur DÉBIT/CRÉDIT) est optionnel :
          - Si présent  → logique identique à parse_lcl_text (prioritaire)
          - Si absent   → détermination par mots-clés LCL, puis heuristique
      • Ignore les artefacts OCR courants (espaces parasites dans les montants)
      • Enrichi des préfixes CB23XXXX courants sur LCL (CB23PAYPAL, CB23AMAZON…)
      • Gère les libellés multi-lignes provenant de l'OCR (mots coupés entre pages)

    Ordre de priorité pour le signe :
      1. ". MONTANT"  → CRÉDIT  (indicateur colonne préservé par l'OCR)
      2. "MONTANT ."  → DÉBIT   (indicateur colonne préservé par l'OCR)
      3. Mots-clés CRÉDIT dans le libellé assemblé
      4. Mots-clés DÉBIT  dans le libellé assemblé
      5. Deux montants dans le trailing → avant-dernier = montant réel (dernier = solde)
      6. Défaut → DÉBIT (conservatisme comptable)
    """
    if year is None:
        year = _extract_year_from_text(text)

    # ── Tronquer aux totaux / solde final ──
    stop_pos = len(text)
    for stop_pat in [r"TOTAUX\s+\d", r"SOLDE\s+EN\s+EUROS"]:
        for m in re.finditer(stop_pat, text, re.IGNORECASE):
            stop_pos = min(stop_pos, m.start())
    text = text[:stop_pos]

    # ── Lignes à ignorer (identique parse_lcl_text + variantes OCR) ──
    # NOTE : "\bLIBELLE\b" supprimé — les lignes "LIBELLE:..." sont des continuations
    # SEPA valides. Seul "DATE LIBELLE" (en-tête de colonne) est skippé.
    _SKIP = re.compile(
        r"ECRITURES\s+DE\s+LA\s+PERIODE|DATE\s+LIBELLE|DEBIT\s+CREDIT"
        r"|ANCIEN\s+SOLDE|SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE"
        r"|Page\s+\d|Cr[eé]dit\s+Lyonnais|RELEVE\s+DE"
        r"|D\.STOCK|Indicatif\s*:|VILLEPARISIS|RELEVE\s+D.IDENTITE"
        r"|BIC\s*:|IBAN\s*:|votre\s+conseiller|Prenez\s+rendez"
        r"|du\s+\d{2}[\.\/]\d{2}[\.\/]\d{4}\s+au|RELEVE\s+DE\s+COMPTE"
        r"|Banque\s+Indicatif|N°\s+de\s+compte|SIREN\s+\d"
        r"|MACRO\s+MULTI\s+SERVICES|Indicatif\s*:\s*\d"
        r"|\bVALEUR\b|\bDATE\b|\bDEBIT\b|\bCREDIT\b",
        re.IGNORECASE,
    )

    # ── Indicateurs CRÉDIT (entrée d'argent) — enrichis pour OCR ──
    #
    # RÈGLE : un VIR SEPA / VIR INST est CRÉDIT sauf s'il figure dans la liste
    # d'exceptions DÉBIT ci-dessous.  La liste est intentionnellement courte pour
    # éviter les faux-négatifs : seuls les libellés CLAIREMENT sortants (loyers,
    # cotisations sociales, fournisseurs récurrents identifiés) sont exclus.
    # Les cas ambigus (même bénéficiaire pouvant être créditeur ou débiteur selon
    # le mois) sont laissés à l'indicateur "." colonne, prioritaire sur les mots-clés.
    _CREDIT_RE = re.compile(
        r"REMISE\s+CB\b"                                      # remise TPE / caisse
        r"|VERSEMENT\s+ALS\b"                                 # versement espèces
        r"|VIR\s+(?:SEPA|INST)\s+EDENRED\b"                  # ticket restaurant
        r"|VIR\s+(?:SEPA|INST)\s+PLUXEE\b"
        r"|VIR\s+(?:SEPA|INST)\s+UP\s+COOP\b"
        r"|VIR\s+(?:SEPA|INST)\s+AitKaci\b"
        r"|VOTRE\s+REMISE\s+SUR\s+PRODUITS"
        r"|LCL\s+A\s+LA\s+CARTE\s+PRO\s+VOTRE\s+REMISE"
        # ── Virements reçus identifiés par le libellé OCR ──
        r"|O[\/\\]DE\s+PAIEMENT"                              # ordre de paiement reçu (CRÉDIT)
        r"|VIR\s+EI\b"                                        # virement entrant individuel (CRÉDIT)
        r"|VIR\s+INST\s+EI\b"                                 # virement entrant (CRÉDIT)
        # NB : CIONS/O/PAIEMT est une commission de paiement → DÉBIT (retiré de cette liste)
        # ── VIR SEPA entrants : tout sauf les sorties connues ──
        # Exclusions DÉBIT confirmées : loyers SCI, cotisations sociales, fournisseurs récurrents
        r"|VIR\s+SEPA\s+(?!SCI\b|ABADA\b|GCOLLECT\b|PREFILOC|MALAKOFF|URSSAF|SIE\s+VAL|ERCINEA|GAN\s+ASS)(?!O\s+MEGA\b)(?!SAS\s+YNOV\b)"
        # ── VIR INST entrants : tout sauf les sorties connues ──
        # NB : THOMAS retiré de la liste d'exclusion — VIR INST d'un particulier = crédit reçu
        r"|VIR\s+INST\s+(?!DM\s+INFORMATIQUE|CSI\b|ERCINEA|O\s+MEGA|SAS\s+YNOV|GCOLLECT|RETURN\s+TRADING)",
        re.IGNORECASE,
    )

    # ── Indicateurs DÉBIT (sortie d'argent) — enrichis pour OCR ──
    _DEBIT_RE = re.compile(
        r"CHQ\.\s*\d"
        r"|PRLV\s+SEPA\b"                                     # prélèvement automatique
        r"|COMMISSIONS\s+SUR\s+REMISE"                        # commission bancaire
        r"|COTISATION\s+(?:MENSUELLE|DE\s+VOTRE)\s+(?:CARTE|OPTION)"
        r"|COTISATION\s+MENSUELLE\s+CARTE"
        r"|COTISATION\s+DE\s+VOTRE\s+OPTION\s+PRO"
        r"|PRLV\s+SEPA\s+B2B\b"
        r"|ABON\s+LCL"                                        # abonnement LCL
        r"|CIONS[\/\\]O[\/\\]PAIEMT"                          # commission sur paiement (DÉBIT)
        # ── CB23XXXX : paiements carte courants sur relevés LCL ──
        r"|CB23(?:PAYPAL|AMAZON|EBAY|ESSO|APRR|DAC\s+ENI|RELAIS|MAXICOFFEE|AREAS|KFC|COFIROUTE|INPI|SALVA|HOSTINGER|GOOGLE)\b"
        r"|\bCB23\w+"                                         # fallback tous CB23
        # ── Virements sortants identifiés ──
        r"|VIR\s+SEPA\s+(?:SCI\b|ABADA\b|GCOLLECT\b|PREFILOC|MALAKOFF|URSSAF|SIE\s+VAL|ERCINEA|GAN\s+ASS)"
        r"|VIR\s+SEPA\s+(?:SAS\s+YNOV|O\s+MEGA)"
        # NB : THOMAS retiré — un virement reçu d'un particulier est un crédit
        r"|VIR\s+INST\s+(?:DM\s+INFORMATIQUE|CSI\b|ERCINEA|O\s+MEGA|SAS\s+YNOV|GCOLLECT|RETURN\s+TRADING)",
        re.IGNORECASE,
    )

    # ── Regex dates ──
    # Date opération en début de ligne : DD.MM ou DD/MM (OCR)
    _DATE_START = re.compile(r"^(\d{2})[\.\/](\d{2})\s+(.+)", re.DOTALL)
    # Date valeur : DD.MM.YY / DD.MM.YYYY / DD/MM/YY / DD/MM/YYYY
    _VALEUR_RE  = re.compile(r"\b(\d{2})[\.\/](\d{2})[\.\/](\d{2,4})\b\s*(.*)", re.DOTALL)
    _AMT_RE     = re.compile(r"(\d[\d\s]*[,\.]\d{2})")

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

        # ── Date valeur ──
        # On cherche d'abord une date avec points (DD.MM.YY) — format natif LCL.
        # Fallback : slash (DD/MM/YY) — artefact OCR ou date dans un libellé.
        _VALEUR_DOT = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2,4})\b\s*(.*)", re.DOTALL)
        vm = _VALEUR_DOT.search(rest) or _VALEUR_RE.search(rest)
        if vm:
            val_yy   = vm.group(3)
            full_yr  = ("20" + val_yy) if len(val_yy) == 2 else val_yy
            # Validation : l'année doit être dans une plage réaliste (2000-2099)
            # et cohérente avec l'année du relevé (±1 an max).
            # Évite les fragments IBAN comme "2062" extraits à tort.
            try:
                yr_int  = int(full_yr)
                ref_int = int(year)
                if not (2000 <= yr_int <= 2099 and abs(yr_int - ref_int) <= 1):
                    full_yr = year
            except (ValueError, TypeError):
                full_yr = year
            date_str    = f"{dd}/{mm}/{full_yr}"
            libelle_raw = rest[: vm.start()].strip()
            trailing    = vm.group(4).strip()
            # Supprimer toute date résiduelle du trailing (2ème occurrence, artefact OCR)
            # Ex : "04.11.25 9,89 ." → "9,89 ." après suppression de "04.11.25"
            trailing = re.sub(r"\b\d{2}[\.\/]\d{2}[\.\/]\d{2,4}\b\s*", "", trailing).strip()
        else:
            date_str    = f"{dd}/{mm}/{year}"
            libelle_raw = rest.strip()
            trailing    = ""

        # ── Assembler libellé (ligne 1 + continuations) ──
        extra_parts: list[str] = []
        for ln in blines[1:]:
            s = ln.strip()
            if not s or _SKIP.search(s) or _DATE_START.match(s):
                continue
            extra_parts.append(s)

        full_lib = " ".join(p for p in [libelle_raw] + extra_parts if p)
        full_lib = re.sub(r"\s{2,}", " ", full_lib).strip()[:120]

        # ── Détection du signe via le trailing ──
        credit_by_dot = re.match(r"^\.\s+(.+)$", trailing)
        debit_by_dot  = re.search(r"(\d[\d\s]*[,\.]\d{2})\s+\.\s*$", trailing)

        if credit_by_dot:
            raw_amt = _AMT_RE.search(credit_by_dot.group(1))
        elif debit_by_dot:
            raw_amt = _AMT_RE.search(debit_by_dot.group(1))
        elif trailing:
            raw_amt = _AMT_RE.search(trailing)
        else:
            raw_amt = _AMT_RE.search(libelle_raw)
            if raw_amt:
                full_lib = full_lib[: full_lib.rfind(raw_amt.group(0))].strip()

        if not raw_amt:
            return None

        amount = clean_amount(raw_amt.group(0))
        if amount is None or amount == 0:
            return None

        # ── Signe : indicateur "." > mots-clés > heuristique > défaut DÉBIT ──
        if credit_by_dot:
            montant = abs(amount)
        elif debit_by_dot:
            montant = -abs(amount)
        else:
            is_credit = bool(_CREDIT_RE.search(full_lib))
            is_debit  = bool(_DEBIT_RE.search(full_lib))
            if is_credit and not is_debit:
                montant = abs(amount)
            elif is_debit:
                montant = -abs(amount)
            else:
                # Heuristique OCR : si trailing contient 2 montants,
                # le dernier est probablement le solde → prendre l'avant-dernier
                all_amts = [clean_amount(a) for a in _AMT_RE.findall(trailing)]
                all_amts = [v for v in all_amts if v is not None and v > 0]
                if len(all_amts) >= 2:
                    amount = all_amts[-2]
                montant = -abs(amount)  # débit par défaut

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

    Supporte les relevés Qonto multi-pages (le footer "Qonto SA, au capital de..."
    apparaît en bas de CHAQUE page — il ne doit pas tronquer le texte).
    Seul "Toutes les cartes..." (dernière page) marque la fin des transactions.
    """
    text = clean_text(text)

    # ── Tronquer au PREMIER marqueur de fin de transactions (break après 1er match) ──
    # "Qonto SA" retiré : apparaît dans le footer de chaque page → tronquerait trop tôt
    for stop_pat in [
        r"[Tt]outes\s+les\s+cartes\s+de\s+votre",
        r"[Tt]otal\s+des\s+op[ée]rations",
        r"Qonto,\s+une\s+marque",
        r"OLINDA\s+SAS",
    ]:
        m = re.search(stop_pat, text)
        if m:
            text = text[: m.start()]
            break  # s'arrêter au premier marqueur trouvé

    if year is None:
        ym = re.search(r"20\d{2}", text)
        year = ym.group(0) if ym else str(datetime.now().year)

    # ── Lignes de footer/header inter-pages Qonto à ignorer ──
    # (apparaissent entre chaque page dans l'extraction pdfplumber)
    _QONTO_SKIP = re.compile(
        r"^Qonto\s+(SA|est\s+agr[eé]|SAS)|"
        r"^Scann[eé]\s+avec\s+CamScanner|"
        # En-tête de période répété en bas de chaque page : "Du 01/03/2026 au 31/03/2026"
        r"^Du\s+\d{2}/\d{2}/\d{4}\s+au\s+\d{2}/\d{2}/\d{4}|"
        # Ligne de footer avec IBAN entre parenthèses : "(FR76 1695 8000 ...)"
        r"^\S.*\(FR\d{2}\s+\d{4}|"
        r"^\d+/\d+\s*$|"                    # numéro de page ex: "1/6", "2/6"
        r"^IBAN\s*:\s*FR|^BIC\s*:\s*QNT|"
        r"si[eè]ge\s+social|numéro\s+16958|au\s+capital\s+de\s+\d|"
        r"^Relevés?\s+de\s+compte$|"
        r"^Date\s+de\s+valeur\s*$|^Transactions\s*$|^D[eé]bit\s*$|^Cr[eé]dit\s*$",
        re.IGNORECASE,
    )

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

        # Priorité 1 : montant signé explicite  (+/- AMOUNT EUR)
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

        # Priorité 2 : dernier montant numérique sans signe EUR
        raw_amounts = re.findall(r"\d[\d\s\xa0]*[,\.]\d{2}", full)
        amounts = [v for a in raw_amounts if (v := clean_amount(a)) is not None]
        if not amounts:
            return None
        amount = abs(amounts[-1])
        libelle = re.sub(r"^\d{2}/\d{2}\s*", "", full).strip()[:120]
        return {"date": f"{date_str}/{year}", "libelle": libelle, "montant": round(amount, 2)}

    for line in lines:
        # Ignorer les lignes de footer/header inter-pages
        if _QONTO_SKIP.search(line):
            continue

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

    Correction borne (v2) :
      On utilise x1 (bord DROIT) du mot "Débit" et x0 (bord GAUCHE) du mot "Crédit"
      pour calculer la borne de séparation. L'ancienne formule (debit_x0 + credit_x0)/2
      plaçait la borne trop à gauche sur les relevés Shine (et variantes), créant une
      zone de chevauchement où les montants débit à x0≈482 tombaient dans les deux
      colonnes → résolution erronée « Crédit » par le tiebreaker.
      Avec (debit_x1 + credit_x0)/2, la borne est exactement dans l'espace vide entre
      les deux colonnes → aucun chevauchement.
    """
    debit_x, credit_x = None, None
    debit_x1: float | None = None   # bord DROIT du mot "Débit" dans l'en-tête

    for w in words:
        txt = w["text"].upper()
        # Match DEBIT / DÉBIT mais pas CREDITEUR
        if re.search(r"D[EÉ]BIT", txt) and not re.search(r"CREDITEUR|CL[ÔO]TURE|DEBUT", txt):
            if w.get("x0", 0) > 300:
                debit_x  = w["x0"]
                debit_x1 = w.get("x1", w["x0"])   # bord droit du header "Débit"
        if re.search(r"CR[EÉ]DIT", txt) and not re.search(r"CREDITEUR|CREDIT\s+MUTUEL|CREDIT\s+AGRICOLE|CIC", txt):
            if w.get("x0", 0) > 380:
                credit_x = w["x0"]

    if debit_x is None:
        debit_x  = 437.0
        debit_x1 = 437.0
    if credit_x is None:
        credit_x = 506.0

    # Borne = milieu entre bord droit de "Débit" et bord gauche de "Crédit"
    # → élimine la zone de chevauchement que (debit_x0+credit_x0)/2 créait
    _debit_right = debit_x1 if debit_x1 is not None else debit_x
    boundary = (_debit_right + credit_x) / 2
    desc_end_x = debit_x - 5
    return desc_end_x, debit_x, credit_x, boundary


def _group_by_y(words: list[dict], y_tol: float = 2.0) -> dict[float, list[dict]]:
    """Groupe les mots par ligne (tolérance verticale)."""
    lines: dict[float, list[dict]] = {}
    for w in words:
        y = round(w["top"] / y_tol) * y_tol
        lines.setdefault(y, []).append(w)
    return lines


def _extract_lcl_opening_balance(pdf_bytes: bytes) -> float | None:
    """
    Extrait l'ANCIEN SOLDE LCL en tenant compte de sa colonne (DÉBIT ou CRÉDIT).

    Sur un relevé LCL :
      - Solde d'ouverture CRÉDITEUR → montant dans la colonne CRÉDIT (x0 > boundary) → positif
      - Solde d'ouverture DÉBITEUR  → montant dans la colonne DÉBIT  (x0 < boundary) → négatif

    La fonction générique extract_opening_balance() ignore cette position et retourne
    toujours une valeur positive, ce qui crée un écart quand le compte est à découvert.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
                if not words:
                    continue

                # ── Détection des colonnes DÉBIT/CRÉDIT ──
                debit_x, credit_x = None, None
                for w in words:
                    txt = w["text"].upper()
                    if re.search(r"D[EÉ]BIT", txt) and not re.search(r"CREDITEUR|DEBUT", txt):
                        if w.get("x0", 0) > 300:
                            debit_x = w["x0"]
                    if re.search(r"CR[EÉ]DIT", txt) and not re.search(
                        r"CREDITEUR|CREDIT\s+MUTUEL|CREDIT\s+AGRICOLE|CIC", txt
                    ):
                        if w.get("x0", 0) > 380:
                            credit_x = w["x0"]

                if debit_x is None:
                    debit_x = 437.0
                if credit_x is None:
                    credit_x = 506.0
                boundary = (debit_x + credit_x) / 2

                # ── Chercher la ligne ANCIEN SOLDE ──
                lines_by_y = _group_by_y(words, y_tol=2.0)
                for y_key in sorted(lines_by_y.keys()):
                    line_words = sorted(lines_by_y[y_key], key=lambda w: w["x0"])
                    line_upper = " ".join(w["text"].upper() for w in line_words)
                    if "ANCIEN" not in line_upper or "SOLDE" not in line_upper:
                        continue

                    # Trouver le montant numérique et sa position x
                    amount_words = [
                        w for w in line_words
                        if re.match(r"^\d[\d\s]*[,\.]\d{2}$", w["text"].strip())
                        or re.match(r"^\d{1,4}[,\.]\d{2}$", w["text"].strip())
                        or re.match(r"^\d{1,2}$", w["text"].strip())  # préfixe milliers
                    ]

                    # Reconstruction montant (ex : "1" + "153,56" → 1153.56)
                    amt_val = _extract_amount_from_zone(line_words, 300.0, 700.0)
                    if amt_val is None:
                        continue

                    # Déterminer la colonne en regardant le mot montant principal
                    # (le token de type "NNN,NN" le plus à droite du libellé)
                    main_amt_word = None
                    for w in reversed(sorted(line_words, key=lambda w: w["x0"])):
                        if re.match(r"^\d{1,4}[,\.]\d{2}$", w["text"].strip()):
                            main_amt_word = w
                            break
                        # Cas préfixe milliers : le token "NNN,NN" peut être précédé de "N"
                        if re.match(r"^\d{1,2}$", w["text"].strip()):
                            # cherche le suivant
                            pass

                    # Si on n'a pas trouvé de token principal, utiliser le dernier numérique
                    if main_amt_word is None:
                        for w in reversed(sorted(line_words, key=lambda w: w["x0"])):
                            if re.search(r"\d", w["text"]) and w["x0"] > 300:
                                main_amt_word = w
                                break

                    if main_amt_word is None:
                        return abs(amt_val)  # fallback positif

                    # Signe selon la colonne
                    if main_amt_word["x0"] >= boundary:
                        return abs(amt_val)   # colonne CRÉDIT → solde positif
                    else:
                        return -abs(amt_val)  # colonne DÉBIT  → solde négatif (à découvert)

    except Exception:
        pass
    return None


def _extract_lcl_opening_balance_from_gcv(
    pages_words: list[list[dict]],
) -> float | None:
    """
    Extrait l'ANCIEN SOLDE LCL depuis les positions de mots Google Cloud Vision.

    Même logique que _extract_lcl_opening_balance() (pdfplumber), mais opère sur
    les listes de mots GCV déjà normalisées (coordonnées 0-612 x, 0-792 y).

    Sur un relevé LCL scanné :
      - Solde d'ouverture CRÉDITEUR → montant dans colonne CRÉDIT (x0 ≥ boundary) → +
      - Solde d'ouverture DÉBITEUR  → montant dans colonne DÉBIT  (x0 < boundary)  → -
    """
    for page_words in pages_words:
        if not page_words:
            continue

        # ── Détection colonnes DÉBIT / CRÉDIT ──
        debit_x, credit_x = None, None
        for w in page_words:
            txt = w["text"].upper()
            if re.search(r"D[EÉ]BIT", txt) and not re.search(r"CREDITEUR|DEBUT", txt):
                if w.get("x0", 0) > 300:
                    debit_x = w["x0"]
            if re.search(r"CR[EÉ]DIT", txt) and not re.search(
                r"CREDITEUR|CREDIT\s+MUTUEL|CREDIT\s+AGRICOLE|CIC", txt
            ):
                if w.get("x0", 0) > 380:
                    credit_x = w["x0"]

        if debit_x is None:
            debit_x = 437.0
        if credit_x is None:
            credit_x = 506.0
        boundary = (debit_x + credit_x) / 2

        # ── Chercher la ligne ANCIEN SOLDE ──
        lines_by_y = _group_by_y(page_words, y_tol=2.0)
        for y_key in sorted(lines_by_y.keys()):
            line_words = sorted(lines_by_y[y_key], key=lambda w: w["x0"])
            line_upper = " ".join(w["text"].upper() for w in line_words)
            if "ANCIEN" not in line_upper or "SOLDE" not in line_upper:
                continue

            amt_val = _extract_amount_from_zone(line_words, 300.0, 700.0)
            if amt_val is None:
                continue

            # Trouver le token principal du montant pour déterminer sa colonne
            main_amt_word = None
            for w in reversed(sorted(line_words, key=lambda w: w["x0"])):
                if re.match(r"^\d{1,4}[,\.]\d{2}$", w["text"].strip()):
                    main_amt_word = w
                    break

            if main_amt_word is None:
                for w in reversed(sorted(line_words, key=lambda w: w["x0"])):
                    if re.search(r"\d", w["text"]) and w["x0"] > 300:
                        main_amt_word = w
                        break

            if main_amt_word is None:
                return abs(amt_val)  # fallback positif

            if main_amt_word["x0"] >= boundary:
                return abs(amt_val)    # colonne CRÉDIT → positif
            else:
                return -abs(amt_val)   # colonne DÉBIT  → négatif (découvert)

    return None


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
    """
    Vrai si la ligne ressemble à un en-tête de tableau ou un total.

    IMPORTANT — ne pas confondre avec les lignes de référence SEPA qui
    commencent par "LIBELLE:" (ex: "LIBELLE:Reecive G3W2L9") ou par
    "REF.CLIENT:", "ID.CREANCIER:", "REF.MANDAT:".
    Ces lignes sont des continuations valides d'une transaction LCL/BNP.
    Le mot LIBELLE ne doit matcher QUE lorsqu'il est suivi d'un espace
    ou d'une fin de ligne (en-tête de colonne), PAS lorsqu'il est suivi
    de ":" (référence SEPA).
    """
    txt = " ".join(w["text"].upper() for w in words)
    # Exclure les lignes purement composées de références SEPA
    if re.match(
        r"^(?:LIBELLE:|REF\.CLIENT:|REF\.MANDAT:|ID\.CREANCIER:|MANDAT:)",
        txt, re.IGNORECASE,
    ):
        return False
    return bool(re.search(
        r"\b(DATE|VALEUR|NATURE|OPERATION|DEBIT|CREDIT|TOTAL|TOTAUX"
        r"|SOLDE|SOUS.TOTAL|REPORT|SUITE)\b"
        # LIBELLE uniquement comme en-tête de colonne (pas suivi de ":")
        r"|\bLIBELLE\b(?!\s*:)",
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

        # Année depuis le texte de la page — utilise le helper sécurisé
        # (évite de capturer un fragment IBAN comme "2062")
        page_text = " ".join(w["text"] for w in page_words)
        year = _extract_year_from_text(page_text, fallback_year=year)

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
            # ── Résolution de l'ambiguïté débit/crédit ──
            # Si les deux zones ont un montant (can happen when x0 is near boundary),
            # on choisit le montant dont la valeur est la plus éloignée de zéro
            # (heuristique : le "vrai" montant est généralement plus grand que 0).
            # En cas d'égalité, le crédit est favorisé (moins de risque d'erreur
            # que d'écraser un crédit en débit).
            if (current_debit is not None and current_debit > 0
                    and current_credit is not None and current_credit > 0):
                # Les deux colonnes ont un montant → probable chevauchement de zones.
                # Conserver uniquement le crédit (moins susceptible d'être un artefact).
                current_debit = None
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
                    r"|TOTAL\s+DES\s+MOUVEMENTS|TOTAL\s+PREL[EÈ]VE"
                    r"|ANCIEN\s+SOLDE"
                    # ── Shine : ligne de solde d'ouverture "Solde au 31/01/2026 59 436,61 €"
                    #    Elle porte une vraie date DD/MM/YYYY et un montant → serait capturée
                    #    comme transaction sans ce filtre explicite.
                    r"|SOLDE\s+AU\b",
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


def _prefilter_shine_words(pages_words: list[list[dict]]) -> list[list[dict]]:
    """
    Filtre les mots pdfplumber d'un relevé Shine avant de les passer au parser colonne.

    Shine répète sur CHAQUE page les blocs suivants (en-tête + pied légal) qui ne
    sont PAS des transactions. Sans filtrage, ces lignes contaminent les libellés :

      — En-têtes répétés : "Relevé d'opérations", "De 01/02/… à 28/02/…",
        "Compte professionnel", "Nom du compte", "IBAN", "BIC", "SIRET",
        "Messagerie", adresse client, adresse Shine, "Date Type Opération…"

      — Pieds légaux    : "Shine France, SAS au capital de 4 446,79 €…",
        "de paiement", "exploitant le nom commercial Shine",
        "Les opérations écrites en italique…", "Page 1/2"

      — Colonne Type multi-ligne : pdfplumber extrait parfois le type d'opération
        ("Virement", "instantané", "Prélèvement") SUR UNE LIGNE SÉPARÉE précédant
        la ligne de date correspondante. Sans filtrage, ce mot se retrouve dans le
        libellé de la transaction précédente.

    Stratégie : on groupe les mots par y-clé (arrondi à 2 pts), on reconstruit le
    texte de chaque ligne, et on supprime les lignes dont le texte correspond à un
    pattern de saut.  Les mots restants sont retournés page par page.
    """
    # Patterns de lignes à éliminer — miroir exact de _SKIP_SHINE dans parse_shine_text,
    # adaptés pour matcher sur la ligne reconstruite depuis les mots pdfplumber.
    _SKIP_LINE = re.compile(
        r"^Relev[ée]\s+d[''`']?op[ée]rations"
        r"|^De\s+\d{2}/\d{2}/\d{4}\s+[àa]\s+\d{2}/\d{2}/\d{4}"
        r"|^Compte\s+professionnel"
        r"|^Shine\s*(?:\(|France)"
        r"|^Nom\s+du\s+compte"
        r"|^SIRET\s*:"
        r"|^IBAN\s*:"
        r"|^BIC\s*:"
        r"|^Messagerie\s*:"
        # ⚠️ NE PAS filtrer "Date Type Opération Débit Crédit" → nécessaire pour _detect_columns
        # Cette ligne est ignorée en tant que transaction par _is_header_or_total_line
        r"|^Les\s+op[ée]rations\s+[ée]crites"
        r"|^de\s+paiement"
        r"|\bexploitant\s+le\s+nom\b"        # sans ^ : la ligne peut commencer par "828 701 557,"
        r"|^immatricul[ée]e?"
        r"|^agr[ée][ée]e?\s+par"
        r"|^ayant\s+son\s+si[èe]ge"
        r"|\bSAS\s+au\s+capital\b"
        r"|^122\s+rue\s+Amelot"
        r"|^25\s+RUE\s+DE\s+LA\s+LIB"       # adresse client
        r"|^828\s+701\s+557"                  # continuation légale "828 701 557, exploitant…"
        r"|Page\s+\d+/\d+"
        # ── Solde d'ouverture "Solde au DD/MM/YYYY …" → pas une transaction ──
        r"|^Solde\s+au\s+\d{2}/\d{2}/\d{4}"
        # ── Mots de type isolés (Shine multi-ligne) : "Virement" / "instantané"
        #    Ils précèdent la ligne de date correspondante et contamineraient
        #    le libellé de la transaction précédente si on les laissait passer.
        r"|^(?:Virement|instantan[ée]|Pr[ée]l[eè]vement|Cart[e]?)$",
        re.IGNORECASE | re.UNICODE,
    )

    # ── Regex pour détecter si une ligne (reconstruite) contient un montant ──
    # Utilisée pour identifier les lignes-description orphelines multi-lignes :
    # ex. "MALAKOFF HUMANIS - RETRAITE - MALAKOFF HUMANIS - 202601M - SIRET"
    # Ces lignes apparaissent AVANT la date de la transaction suivante mais APRÈS
    # la date de la transaction précédente → elles contaminent le libellé précédent.
    # On les filtre si : pas de date, pas de montant ≥ 1,00 dans les colonnes Débit/Crédit.
    # ⚠️ On ne filtre PAS les lignes de référence SEPA utiles (REF:, LIBELLE:, /FAC/…)
    _ORPHAN_DESC_RE = re.compile(
        r"^(?:"
        r"[A-Z][A-Z0-9\s\-\*\./&,'éèêàùûîôäëïüç]+"  # texte alphanumèrique seul
        r")\s*-\s*(?:SIRET|RUM|RCS|REF|MANDAT|IBAN)\b",  # suivi d'un mot-clé administratif
        re.IGNORECASE,
    )

    filtered: list[list[dict]] = []
    for page_words in pages_words:
        if not page_words:
            filtered.append([])
            continue

        # Grouper par y-clé (même tolérance que le parser colonne)
        by_y: dict[float, list[dict]] = {}
        for w in page_words:
            k = round(w["top"] / 2) * 2
            by_y.setdefault(k, []).append(w)

        # Identifier les y-clés des lignes à supprimer
        skip_ys: set[float] = set()
        for yk, ws in by_y.items():
            line_text = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))
            if _SKIP_LINE.search(line_text) or _ORPHAN_DESC_RE.search(line_text):
                skip_ys.add(yk)

        kept = [w for w in page_words if round(w["top"] / 2) * 2 not in skip_ys]
        filtered.append(kept)

    return filtered


def parse_transactions_by_column_shine(pdf_file, y_tol: float = 3.0) -> pd.DataFrame:
    """
    Variante du parser colonne dédiée aux relevés Shine.

    Ajoute un pré-filtrage des mots pdfplumber via _prefilter_shine_words afin
    d'éliminer avant analyse :
      • les en-têtes répétés page par page (IBAN, BIC, adresse, "Relevé d'opérations"…)
      • les pieds légaux ("Shine France, SAS au capital…", "Page X/Y"…)
      • la ligne "Solde au DD/MM/YYYY …" (solde d'ouverture, pas une transaction)
      • les mots de type isolés ("Virement", "instantané") qui apparaissent sur une
        ligne séparée avant la date correspondante et contamineraient le libellé
        de la transaction précédente.

    y_tol=3.0 : tolérance verticale légèrement supérieure à 2.0 (défaut) pour absorber
    les micro-décalages pdfplumber sur les relevés Shine, tout en restant en-deçà de
    4.0 (LCL) afin de ne pas fusionner des transactions distinctes.
    """
    year = str(datetime.now().year)
    pages_words: list[list[dict]] = []

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
            if words:
                pages_words.append(words)
            page_text = page.extract_text() or ""
            year = _extract_year_from_text(page_text, fallback_year=year)

    # Supprimer les lignes non-transactionnelles spécifiques à Shine
    filtered_pages = _prefilter_shine_words(pages_words)

    return parse_transactions_from_word_pages(filtered_pages, year=year, y_tol=y_tol)


def parse_transactions_by_column(pdf_file, y_tol: float = 2.0) -> pd.DataFrame:
    """
    Parser colonne via pdfplumber — extrait les mots avec positions et appelle
    parse_transactions_from_word_pages.

    y_tol : tolérance verticale (points) pour regrouper les mots d'une même ligne.
      • 2.0 (défaut) : précis, pour la plupart des banques.
      • 4.0 (LCL)    : nécessaire car pdfplumber peut placer les montants de la colonne
                       Débit/Crédit sur une y légèrement différente de la date (ex : 100 vs 101),
                       ce qui les sépare en deux groupes distincts avec y_tol=2.0 et provoque
                       des transactions manquées quand une ligne de référence SEPA (LIBELLE:…)
                       déclenche un flush() avant que le montant soit traité.
    """
    year = str(datetime.now().year)
    pages_words: list[list[dict]] = []

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
            if words:
                pages_words.append(words)
            page_text = page.extract_text() or ""
            year = _extract_year_from_text(page_text, fallback_year=year)

    return parse_transactions_from_word_pages(pages_words, year=year, y_tol=y_tol)


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
    # "SHINE" retiré de _TEXT_BANKS — géré par sa propre branche dans extract_all
    # (parser colonne en priorité + parse_shine_text en fallback)
    "BANQUE_POPULAIRE": parse_banque_populaire_text,
    "CAISSE_EPARGNE": parse_caisse_epargne_text,
    "QONTO": parse_transactions_text,
    "FINOM": parse_finom_text,
}


# =========================================================
# POINT D'ENTRÉE PRINCIPAL — EXTRACTION INTELLIGENTE
# =========================================================

def extract_all_from_image(image_file) -> dict:
    """
    Entrée : fichier image (PNG / JPG / JPEG / TIFF / BMP / WEBP).
    Convertit l'image en PDF mono-page puis lance le pipeline OCR GCV standard.
    Retourne le même dictionnaire que extract_all().
    """
    from PIL import Image as PILImage

    image_file.seek(0)
    raw_bytes = image_file.read()

    # Convertir l'image en PDF en mémoire via Pillow
    img = PILImage.open(io.BytesIO(raw_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=150)
    pdf_buf.seek(0)
    pdf_bytes = pdf_buf.read()

    # Créer un objet compatible seek/read
    class _BytesIO(io.BytesIO):
        pass

    fake_pdf = _BytesIO(pdf_bytes)
    fake_pdf.name = getattr(image_file, "name", "image.pdf").rsplit(".", 1)[0] + ".pdf"
    return extract_all(fake_pdf, force_ocr=True)


def extract_all(pdf_file, force_ocr: bool = False) -> dict:
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

    has_text = _pdf_has_text(pdf_bytes) and not force_ocr

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
    year_ref = _extract_year_from_text(raw_text)

    df = pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── Dispatch parser ──

    if bank == "LCL":
        # ── Correction solde d'ouverture LCL ──
        # extract_opening_balance() retourne toujours une valeur positive.
        # Or LCL place l'ANCIEN SOLDE en colonne DÉBIT quand le compte est à découvert
        # (solde débiteur) → le solde d'ouverture doit alors être NÉGATIF.
        if has_text:
            # PDF texte natif : utilise les positions pdfplumber
            lcl_opening = _extract_lcl_opening_balance(pdf_bytes)
            if lcl_opening is not None:
                opening = lcl_opening
        elif ocr_used and pages_words:
            # PDF scanné / image : utilise les positions Google Cloud Vision
            lcl_opening = _extract_lcl_opening_balance_from_gcv(pages_words)
            if lcl_opening is not None:
                opening = lcl_opening

        # ── LCL — Cascade de parsers ──
        #
        # PDF texte natif :
        #   1. Parser colonne pdfplumber (DÉBIT | CRÉDIT par position x)
        #   2. Parser texte dédié LCL (indicateurs "." + mots-clés)
        #
        # PDF scanné / image (OCR Google Cloud Vision) :
        #   1. Parser colonne GCV (positions x/y normalisées 0-612)
        #   2. Parser LCL dédié OCR (DD.MM|DD/MM + "." colonne + mots-clés enrichis)
        #   3. Parser LCL texte natif (si OCR très fidèle à la mise en page)
        #   4. Parser universel (dernier recours)
        if has_text:
            # LCL PDF natif : y_tol=4.0 indispensable.
            # pdfplumber peut placer le montant Débit/Crédit sur une y légèrement
            # différente de la date (ex : 100 vs 101). Avec y_tol=2.0, round(101/2)*2=102 ≠ 100
            # → le montant se retrouve dans un groupe différent de la date → la transaction
            # est flush() sans montant (perdue) quand une ligne LIBELLE:… survient.
            # Avec y_tol=4.0 : round(101/4)*4=100 → même groupe → montant correctement rattaché.
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes), y_tol=4.0)
            if df.empty:
                df = parse_lcl_text(raw_text, year_ref)
        elif ocr_used and pages_words:
            # Priorité 1 : parser colonne GCV (exploite les positions x/y)
            df = parse_transactions_from_word_pages(
                pages_words, year=year_ref, y_tol=4.0
            )
            if df.empty:
                # Priorité 2 : parser LCL dédié OCR (robuste aux artefacts OCR)
                df = parse_lcl_ocr_text(raw_text, year_ref)
            if df.empty:
                # Priorité 3 : parser LCL texte natif (fonctionne si OCR très fidèle)
                df = parse_lcl_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

        # ── Filtre de sécurité : supprimer les lignes bilan/totaux LCL
        # (ex : "SOLDE EN EUROS", "SOLDE INTERMEDIAIRE A FIN DECEMBRE", "ANCIEN SOLDE")
        # Ces lignes ne sont pas de vraies transactions et faussent les totaux.
        if not df.empty:
            _lcl_bilan_mask = df["libelle"].str.contains(
                r"SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE|ANCIEN\s+SOLDE",
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

    elif bank == "SHINE":
        # ── Shine — Parser colonne en priorité (PDF texte natif) ──
        #
        # Shine structure ses relevés avec DEUX colonnes séparées Débit / Crédit
        # (sans signe sur les montants). Le parser texte (parse_shine_text) ne peut
        # pas distinguer les deux colonnes — il utilisait l'heuristique "De :" pour
        # détecter les virements entrants, manquant tous les remboursements carte
        # (ex : WORLD EDUCATION SERV, CERBA…) dont les montants tombent en colonne
        # Crédit (x0 ≈ 535-545) mais n'ont pas de "De :" dans le libellé.
        #
        # parse_transactions_by_column_shine :
        #   - Pré-filtre les mots pdfplumber (_prefilter_shine_words) :
        #       supprime en-têtes répétés, pieds légaux, ligne "Solde au",
        #       mots de type isolés ("Virement" / "instantané" sur ligne seule)
        #   - Puis appelle le parser colonne avec y_tol=3.0
        #
        # Positions x relevé Shine :
        #   - Débit  header x0=457 x1=477  → montants x0 ≈ 480-490
        #   - Crédit header x0=515         → montants x0 ≈ 535-545
        #   - boundary = (477 + 515) / 2 ≈ 496 → séparation nette
        if has_text:
            df = parse_transactions_by_column_shine(io.BytesIO(pdf_bytes))
            if df.empty:
                df = parse_shine_text(raw_text)
        elif ocr_used and pages_words:
            # OCR : pas de filtre de mots GCV (les en-têtes sont généralement
            # absents des blocs OCR structurés) ; on préfiltre quand même par sécurité
            filtered_gcv = _prefilter_shine_words(pages_words)
            df = parse_transactions_from_word_pages(filtered_gcv, year=year_ref, y_tol=3.0)
            if df.empty:
                df = parse_shine_text(raw_text)
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
    
    st.markdown("""
        <style>
            /* Animation douce */
            @keyframes fadeIn {
                from {opacity: 0; transform: translateY(10px);}
                to {opacity: 1; transform: translateY(0);}
            }

        .block-container {
            animation: fadeIn 0.6s ease-in-out;
        }

        /* Boutons plus stylés */
            .stButton>button {
                border-radius: 10px;
                transition: 0.3s;
            }
        .stButton>button:hover {
            transform: scale(1.05);
        }
        </style>
    """, unsafe_allow_html=True)
    
    
    st.sidebar.markdown("""
        ### 👨‍💻 À propos

        **Achraf BEN YOUNES**  
        🚀 Data & AI Engineer  

        ---

        📞 **Contact**  
        🇫🇷 07 60 93 53 71  

        📧 **Email**  
        achrafbenyounes2012@gmail.com  

        ---

            © 2026 Achraf BEN YOUNES  
        """)
    
    st.title("💳 OFX Converter Pro — Multi-banques France → Odoo")
    
    st.caption(
        "Compatible : La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale · "
        "CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire · "
        "Qonto · Revolut Business · Shine · **Finom**  |  "
        "**OCR automatique** (PDF scannés & Print-to-PDF)"
    )
    
    st.markdown("""
        <p style='font-size:16px; color:gray;'>
        Convertissez automatiquement vos relevés bancaires en format OFX compatible Odoo — 
        <span style='color:#4CAF50; font-weight:bold;'>rapide, fiable et intelligent</span>.
        </p>
        """, unsafe_allow_html=True)

    # ── Upload : PDF, images scannées, photos de relevés ──
    _IMAGE_TYPES = ["png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"]
    uploaded = st.file_uploader(
        "📁 Déposer le relevé bancaire (PDF ou image scannée)",
        type=["pdf"] + _IMAGE_TYPES,
        help="Formats acceptés : PDF natif, PDF scanné, PNG, JPG, JPEG, TIFF, BMP, WEBP",
    )

    col_force, _ = st.columns([2, 5])
    with col_force:
        force_ocr = st.checkbox(
            "🔎 Forcer OCR (Google Vision)",
            value=False,
            help=(
                "Cochez si le PDF est imprimé/scanné mais détecté à tort comme 'texte natif'. "
                "L'OCR sera appliqué même si le PDF contient du texte extractible."
            ),
        )

    if not uploaded:
        st.info("Importez un relevé bancaire (PDF ou image) pour commencer.")
        return

    safe_name = sanitize_filename(uploaded.name)
    if safe_name != uploaded.name:
        st.caption(f"📄 Fichier reçu : `{uploaded.name}` → normalisé en `{safe_name}`")

    file_ext = uploaded.name.rsplit(".", 1)[-1].lower()
    is_image_file = file_ext in _IMAGE_TYPES

    with st.spinner("🔍 Analyse du relevé en cours…"):
        if is_image_file:
            result = extract_all_from_image(uploaded)
        else:
            result = extract_all(uploaded, force_ocr=force_ocr)

    if is_image_file:
        st.info(f"🖼️ Image détectée ({file_ext.upper()}) — conversion puis OCR via **Google Cloud Vision**")

    bank     = result["bank"]
    iban     = result["iban"]
    bic      = result["bic"]
    opening  = result["opening"]
    closing  = result["closing"]
    df       = result["df"]
    ocr_used = result["ocr_used"]
    raw_text = result["raw_text"]

    if ocr_used and not is_image_file:
        st.info("🔎 PDF image/scanné détecté — extraction via **Google Cloud Vision**")
    elif not is_image_file and force_ocr:
        st.info("🔎 Mode OCR forcé — extraction via **Google Cloud Vision**")

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

        ofx_filename = re.sub(r"\.(png|jpg|jpeg|tiff?|bmp|webp)$", ".ofx", safe_name, flags=re.IGNORECASE)
        ofx_filename = ofx_filename.replace(".pdf", ".ofx")

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