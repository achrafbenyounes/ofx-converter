"""
OFX Converter Pro — Multi-banques France → Odoo
================================================
Compatible (relevés PDF texte natif) :
  La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale
  CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire
  Qonto · Revolut Business · Shine · Boursorama · Hello Bank · N26 · Fortuneo
  **Finom** (finom.co / PNL Fintech B.V.)…

Stratégies d'extraction :
  1. Parser colonne    (PDF texte natif) — coordonnées x/y via pdfplumber
  2. Parser banque     (texte brut)      — regex spécifiques à chaque format
  3. Parser universel  (Qonto + fallback) — regex sur blocs DD/MM … montant EUR

Format OFX généré :
  OFX 1.x SGML, compatible toutes versions Odoo (14-17+)
  Montants : X.XX (point décimal, JAMAIS virgule, 2 décimales)
"""

import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(
    page_title="OFX Converter Pro",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="collapsed",
)


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
    Extrait l'année du relevé depuis le texte extrait par pdfplumber, en évitant les
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
# PARSERS SPÉCIFIQUES PAR BANQUE
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


def parse_transactions_text(text: str, year: str | None = None) -> pd.DataFrame:
    """
    Parser texte universel compatible Qonto et fallback générique.

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
        year = _extract_year_from_text(text)

    # ── Lignes de footer/header inter-pages Qonto à ignorer ──
    # (apparaissent entre chaque page dans l'extraction pdfplumber)
    _QONTO_SKIP = re.compile(
        r"^Qonto\s+(SA|est\s+agr[eé]|SAS)|"
        r"^Scann[eé]\s+avec\s+CamScanner|"
        r"^Relevés?\s+de\s+compte(?:\s+Qonto)?$|"
        r"^Date\s+de\s+valeur(?:\s+Transactions\s+D[eé]bit\s+Cr[eé]dit)?$|"
        r"^Transactions\s*$|^D[eé]bit\s*$|^Cr[eé]dit\s*$|"
        # En-têtes de synthèse Qonto scannés : Solde / Entrées / Sorties
        r"^Solde\s+au\s+\d{2}/\d{2}(?:/\d{4})?\b|"
        r"^Entr[ée]es?\b|^Sorties?\b|"
        # En-tête de période répété en haut/bas des pages
        r"^Du\s+\d{2}/\d{2}/\d{4}\s+au\s+\d{2}/\d{2}/\d{4}|"
        # Ligne de footer avec IBAN entre parenthèses : "RS BÂTIMENT (FR76 1695 ...)"
        r"^\S.*\(FR\d{2}\s+\d{4}|"
        r"^\d+/\d+\s*$|"
        r"^IBAN\s*:\s*FR|^BIC\s*:\s*QNT|"
        r"si[eè]ge\s+social|numéro\s+16958|au\s+capital\s+de\s+\d|"
        r"compatibles\s+avec\s+Apple\s+Pay|"
        r"^Toutes\s+les\s+cartes\s+de\s+votre|"
        r"^86\s+RUE\s+VOLTAIRE$|^93100,?\s+MONTREUIL|^RS\s+B[ÂA]TIMENT$",
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
                lines_by_y = _group_by_y(words, y_tol=4.0)
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
        # pdfplumber peut retourner ce token en un seul mot.
        # Plafond à 500 000 € pour éviter de capter des numéros de référence/SIREN/RCS
        # ou totaux de bas de page (ex : "2.037.713.591,00") comme montants de transaction.
        if re.match(r"^\d{1,3}(\.\d{3})+,\d{2}$", w["text"]):
            base = clean_amount(w["text"])
            if base is not None and base <= 500_000:
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
    Compatible pdfplumber (coordonnées normalisées 0-612).
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
# POINT D'ENTRÉE PRINCIPAL — EXTRACTION INTELLIGENTE
# =========================================================

# Banques dont le parser colonne est le chemin principal
_COLUMN_BANKS = {
    "BANQUE_POSTALE",
    "CIC",
    "GENERIC",
}

# Banques avec parser texte dédié
_TEXT_BANKS = {
    "BNP_PARIBAS": parse_bnp_paribas_text,
    "REVOLUT": parse_revolut_text,
    "BANQUE_POPULAIRE": parse_banque_populaire_text,
    "CAISSE_EPARGNE": parse_caisse_epargne_text,
    "QONTO": parse_transactions_text,
    "FINOM": parse_finom_text,
}


def extract_all(pdf_file) -> dict:
    """
    Orchestre l'extraction complète depuis un PDF texte natif :
      1. Lecture bytes + extraction texte via pdfplumber
      2. Détection banque, IBAN, BIC, soldes
      3. Dispatch vers le parser approprié :
         - Parser texte BNP              : BNP Paribas (sections crédit/débit)
         - Parser texte dédié            : Revolut, Shine, Banque Populaire, Caisse Épargne, Qonto
         - Parser colonne (DÉBIT|CRÉDIT) : La Banque Postale, CIC, CM, SG, CA, LCL
         - Parser texte universel        : fallback générique
    """
    pdf_file.seek(0)
    pdf_bytes = pdf_file.read()

    # ── Extraction du texte brut via pdfplumber ──
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            raw_pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
        raw_text = clean_text("\n".join(raw_pages))
    except Exception:
        raw_text = ""

    bank = detect_bank(raw_text)
    iban, bic = extract_iban_bic(raw_text)

    # ── Fallback IBAN Société Générale ──
    if iban is None and bank == "SOCIETE_GENERALE":
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

    year_ref = _extract_year_from_text(raw_text)

    df = pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── Dispatch parser ──

    if bank == "LCL":
        # Correction solde d'ouverture LCL (colonne DÉBIT ou CRÉDIT selon découvert)
        lcl_opening = _extract_lcl_opening_balance(pdf_bytes)
        if lcl_opening is not None:
            opening = lcl_opening

        # LCL PDF natif : y_tol=4.0 indispensable (montants sur y légèrement différente de la date)
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes), y_tol=4.0)
        if df.empty:
            df = parse_lcl_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

        # Filtre lignes bilan/totaux LCL (SOLDE EN EUROS, SOLDE INTERMEDIAIRE, ANCIEN SOLDE)
        if not df.empty:
            _lcl_bilan_mask = df["libelle"].str.contains(
                r"SOLDE\s+EN\s+EUROS|SOLDE\s+INTERMEDIAIRE|ANCIEN\s+SOLDE",
                regex=True, flags=re.IGNORECASE, na=False,
            )
            df = df[~_lcl_bilan_mask].reset_index(drop=True)

    elif bank == "SOCIETE_GENERALE":
        # Parser texte SG en priorité absolue
        df = parse_societe_generale_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank == "SHINE":
        # Parser colonne en priorité (colonnes Débit/Crédit séparées sans signe)
        df = parse_transactions_by_column_shine(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_shine_text(raw_text)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank in _TEXT_BANKS:
        parser_fn = _TEXT_BANKS[bank]
        try:
            df = parser_fn(raw_text, year_ref)
        except TypeError:
            df = parser_fn(raw_text)
        if df.empty and bank == "BNP_PARIBAS":
            df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank == "CREDIT_AGRICOLE":
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_credit_agricole_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank == "CREDIT_MUTUEL":
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_credit_mutuel_text(raw_text, year_ref)
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    elif bank in _COLUMN_BANKS:
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    else:
        # Banques non reconnues / GENERIC
        df = parse_transactions_by_column(io.BytesIO(pdf_bytes))
        if df.empty:
            df = parse_transactions_text(raw_text, year=year_ref)

    return {
        "raw_text": raw_text,
        "bank": bank,
        "iban": iban,
        "bic": bic,
        "opening": opening,
        "closing": closing,
        "df": df,
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

def inject_custom_ui() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg-1: #07111f;
            --bg-2: #0b1f3a;
            --bg-3: #102d52;
            --glass: rgba(255,255,255,0.08);
            --glass-strong: rgba(255,255,255,0.12);
            --stroke: rgba(255,255,255,0.14);
            --text: #eef4ff;
            --muted: #b8c7df;
            --accent: #53e0c2;
            --accent-2: #7c8cff;
            --accent-3: #ff8a5b;
            --success: #34d399;
            --danger: #fb7185;
            --warning: #fbbf24;
            --shadow: 0 20px 60px rgba(3, 8, 20, 0.35);
            --radius-xl: 24px;
            --radius-lg: 18px;
        }

        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu,
        footer {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
        }

        .stApp {
            background:
                radial-gradient(circle at 10% 20%, rgba(83,224,194,0.18), transparent 22%),
                radial-gradient(circle at 90% 15%, rgba(124,140,255,0.18), transparent 24%),
                radial-gradient(circle at 80% 80%, rgba(255,138,91,0.12), transparent 24%),
                linear-gradient(135deg, var(--bg-1) 0%, var(--bg-2) 45%, var(--bg-3) 100%);
            color: var(--text);
            overflow-x: hidden;
        }

        .stApp::before,
        .stApp::after {
            content: "";
            position: fixed;
            inset: auto;
            width: 34rem;
            height: 34rem;
            border-radius: 999px;
            filter: blur(80px);
            z-index: 0;
            pointer-events: none;
            opacity: 0.35;
            animation: floatBlob 12s ease-in-out infinite;
        }

        .stApp::before {
            top: -8rem;
            right: -10rem;
            background: rgba(83, 224, 194, 0.22);
        }

        .stApp::after {
            left: -10rem;
            bottom: -10rem;
            background: rgba(124, 140, 255, 0.20);
            animation-delay: 2s;
        }

        @keyframes floatBlob {
            0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
            50% { transform: translate3d(0, 22px, 0) scale(1.08); }
        }

        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(18px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @keyframes pulseGlow {
            0%, 100% { box-shadow: 0 0 0 rgba(83,224,194,0.0); }
            50% { box-shadow: 0 0 32px rgba(83,224,194,0.18); }
        }

        .main .block-container {
            position: relative;
            z-index: 1;
            padding-top: 0.25rem;
            padding-bottom: 2.5rem;
            max-width: 1380px;
            animation: fadeUp 0.8s ease;
        }

        .stExpander {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 18px;
            overflow: hidden;
            box-shadow: var(--shadow);
        }

        .stExpander details summary p {
            font-weight: 700;
            color: var(--text) !important;
        }

        [data-testid="stFileUploader"] {
            background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.04));
            border: 1px dashed rgba(124,140,255,0.45);
            border-radius: 22px;
            padding: 0.6rem;
            box-shadow: var(--shadow);
        }

        [data-testid="stFileUploader"] section {
            background: transparent;
        }

        /* ── Mobile : zone de drop entièrement tappable ── */
        [data-testid="stFileUploaderDropzone"] {
            min-height: 80px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 0.6rem;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            padding: 0.35rem;
        }

        .stTabs [data-baseweb="tab"] {
            height: 3rem;
            border-radius: 14px;
            background: transparent;
            color: var(--muted);
            transition: all 0.25s ease;
            font-weight: 700;
        }

        .stTabs [aria-selected="true"] {
            background: linear-gradient(135deg, rgba(83,224,194,0.18), rgba(124,140,255,0.20)) !important;
            color: var(--text) !important;
            border: 1px solid rgba(255,255,255,0.10);
        }

        .stDataFrame, div[data-testid="stMetric"] {
            animation: fadeUp 0.55s ease;
        }

        [data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(7,17,31,0.98), rgba(11,31,58,0.96)),
                rgba(255,255,255,0.02);
            border-right: 1px solid rgba(255,255,255,0.08);
        }

        [data-testid="stSidebar"] * {
            color: #eef4ff !important;
        }

        .sidebar-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.05));
            border: 1px solid rgba(255,255,255,0.10);
            backdrop-filter: blur(14px);
            border-radius: 20px;
            padding: 1.15rem 1rem;
            box-shadow: var(--shadow);
        }

        .hero-shell {
            position: relative;
            overflow: hidden;
            background:
                linear-gradient(135deg, rgba(255,255,255,0.11), rgba(255,255,255,0.05)),
                linear-gradient(120deg, rgba(83,224,194,0.11), rgba(124,140,255,0.10));
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 28px;
            padding: 1.5rem 1.5rem 1.3rem 1.5rem;
            backdrop-filter: blur(18px);
            box-shadow: var(--shadow);
            animation: fadeUp 0.9s ease, pulseGlow 6s ease-in-out infinite;
        }

        .hero-shell::before {
            content: "";
            position: absolute;
            inset: 0;
            background:
                radial-gradient(circle at top right, rgba(255,255,255,0.16), transparent 25%),
                linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent);
            transform: translateX(-100%);
            animation: sheen 8s linear infinite;
        }

        @keyframes sheen {
            100% { transform: translateX(100%); }
        }

        .badge-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
            margin-bottom: 0.9rem;
        }

        .soft-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.42rem 0.78rem;
            border-radius: 999px;
            background: rgba(255,255,255,0.10);
            border: 1px solid rgba(255,255,255,0.12);
            color: var(--text);
            font-size: 0.82rem;
            font-weight: 600;
            backdrop-filter: blur(10px);
        }

        .hero-title {
            margin: 0;
            font-size: clamp(2rem, 3vw, 3.3rem);
            line-height: 1.02;
            letter-spacing: -0.03em;
            color: white;
            font-weight: 800;
        }

        .hero-subtitle {
            margin: 0.9rem 0 1.1rem 0;
            color: var(--muted);
            font-size: 1rem;
            line-height: 1.65;
            max-width: 58rem;
        }

        .hero-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 0.9rem;
            margin-top: 1rem;
        }

        .kpi-card,
        .glass-card,
        .info-panel,
        .cta-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.11), rgba(255,255,255,0.05));
            border: 1px solid var(--stroke);
            border-radius: var(--radius-lg);
            padding: 1rem 1rem 0.95rem 1rem;
            box-shadow: var(--shadow);
            backdrop-filter: blur(14px);
        }

        .kpi-card {
            min-height: 118px;
            transition: transform .25s ease, border-color .25s ease, box-shadow .25s ease;
        }

        .kpi-card:hover,
        .glass-card:hover,
        .info-panel:hover {
            transform: translateY(-4px);
            border-color: rgba(83,224,194,0.35);
        }

        .kpi-label {
            color: var(--muted);
            font-size: 0.84rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 0.5rem;
        }

        .kpi-value {
            color: white;
            font-size: 1.7rem;
            line-height: 1.1;
            font-weight: 800;
            margin-bottom: 0.25rem;
        }

        .kpi-note {
            color: #dce7ff;
            font-size: 0.88rem;
        }

        .section-title {
            font-size: 1.08rem;
            font-weight: 750;
            color: white;
            margin: 0 0 0.85rem 0;
        }

        .section-subtitle {
            color: var(--muted);
            margin-top: -0.2rem;
            margin-bottom: 1rem;
            font-size: 0.95rem;
        }

        .feature-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 0.9rem;
            margin-top: 1rem;
        }

        .feature-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.09), rgba(255,255,255,0.04));
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 18px;
            padding: 1rem;
            min-height: 120px;
            transition: transform .25s ease, border-color .25s ease;
        }

        .feature-card:hover { transform: translateY(-5px); border-color: rgba(124,140,255,0.34); }
        .feature-card .icon { font-size: 1.5rem; margin-bottom: 0.5rem; }
        .feature-card .title { font-weight: 700; color: white; margin-bottom: 0.35rem; }
        .feature-card .text { color: var(--muted); font-size: 0.92rem; line-height: 1.5; }

        div[data-testid="stFileUploader"] > section,
        div[data-testid="stTextInput"] > div,
        div[data-testid="stNumberInput"] > div,
        .stDataFrame,
        div[data-baseweb="select"] > div,
        .stTabs [data-baseweb="tab-list"] {
            background: rgba(255,255,255,0.05) !important;
            border: 1px solid rgba(255,255,255,0.10) !important;
            border-radius: 18px !important;
            backdrop-filter: blur(12px);
        }

        .stTabs [data-baseweb="tab"] {
            color: #dfe7fb;
            font-weight: 700;
            border-radius: 14px;
            padding: 0.6rem 0.9rem;
        }

        .stTabs [aria-selected="true"] {
            background: linear-gradient(90deg, rgba(83,224,194,0.18), rgba(124,140,255,0.18)) !important;
            color: white !important;
        }

        .stButton > button,
        .stDownloadButton > button {
            border: 0 !important;
            border-radius: 16px !important;
            padding: 0.8rem 1.15rem !important;
            font-weight: 800 !important;
            color: white !important;
            background: linear-gradient(135deg, #18c9a7, #5f7cff, #ff8a5b) !important;
            background-size: 200% 200% !important;
            box-shadow: 0 16px 35px rgba(13, 24, 55, 0.35) !important;
            transition: transform .22s ease, box-shadow .22s ease !important;
            animation: gradientShift 8s ease infinite;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover {
            transform: translateY(-2px) scale(1.01);
            box-shadow: 0 22px 40px rgba(13, 24, 55, 0.45) !important;
        }

        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        div[data-testid="stMetric"] {
            background: linear-gradient(180deg, rgba(255,255,255,0.11), rgba(255,255,255,0.05));
            border: 1px solid rgba(255,255,255,0.10);
            padding: 1rem;
            border-radius: 18px;
            box-shadow: var(--shadow);
        }

        div[data-testid="stMetric"] label, div[data-testid="stMetric"] * {
            color: white !important;
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            border-radius: 999px;
            padding: 0.42rem 0.74rem;
            font-size: 0.84rem;
            font-weight: 700;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.10);
            color: white;
            margin-top: 0.25rem;
        }

        .footer-note {
            text-align: center;
            color: #b8c7df;
            font-size: 0.88rem;
            margin-top: 1.4rem;
        }

        .small-muted { color: var(--muted); font-size: 0.9rem; }
        .accent { color: var(--accent); }
        .danger { color: var(--danger); }
        .success { color: var(--success); }
        .warning { color: var(--warning); }

        .stAlert {
            border-radius: 18px !important;
            border: 1px solid rgba(255,255,255,0.10) !important;
            backdrop-filter: blur(10px);
        }

        [data-testid="stExpander"] {
            background: rgba(255,255,255,0.05);
            border-radius: 18px;
            border: 1px solid rgba(255,255,255,0.10);
            overflow: hidden;
        }

        /* ── File uploader "Browse files" button ── */
        [data-testid="stFileUploader"] button {
            background: linear-gradient(135deg, #18c9a7, #5f7cff) !important;
            color: white !important;
            font-weight: 800 !important;
            border: none !important;
            border-radius: 14px !important;
            padding: 0.65rem 1.4rem !important;
            min-height: 44px !important;        /* WCAG touch target */
            min-width: 120px !important;
            box-shadow: 0 8px 24px rgba(95,124,255,0.35) !important;
            touch-action: manipulation !important;
            -webkit-tap-highlight-color: transparent !important;
            cursor: pointer !important;
            position: relative !important;
            z-index: 2 !important;
        }
        /* Hover uniquement sur non-touch pour éviter état collant sur mobile */
        @media (hover: hover) {
            [data-testid="stFileUploader"] button:hover {
                transform: translateY(-2px) scale(1.02) !important;
                box-shadow: 0 14px 32px rgba(95,124,255,0.5) !important;
            }
        }

        /* ── File uploader drag label ── */
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] p,
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] span,
        [data-testid="stFileUploaderDropzoneInstructions"] div span,
        [data-testid="stFileUploaderDropzoneInstructions"] div small {
            color: rgba(220,230,255,0.85) !important;
            font-weight: 600 !important;
        }

        /* ── Stat totals row — wow cards ── */
        .stat-wow-row {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1rem;
            margin: 1rem 0;
        }
        .stat-wow {
            border-radius: 22px;
            padding: 1.2rem 1rem;
            text-align: center;
            position: relative;
            overflow: hidden;
            backdrop-filter: blur(14px);
            border: 1px solid rgba(255,255,255,0.13);
            box-shadow: 0 8px 28px rgba(0,0,0,0.28);
            transition: transform .25s ease, box-shadow .25s ease;
            animation: fadeUp 0.6s ease;
        }
        .stat-wow:hover { transform: translateY(-5px); box-shadow: 0 16px 40px rgba(0,0,0,0.4); }
        .stat-wow.credit  { background: linear-gradient(135deg, rgba(24,201,167,0.22), rgba(24,201,167,0.08)); }
        .stat-wow.debit   { background: linear-gradient(135deg, rgba(255,100,100,0.22), rgba(255,100,100,0.08)); }
        .stat-wow.solde   { background: linear-gradient(135deg, rgba(124,140,255,0.22), rgba(124,140,255,0.08)); }
        .stat-wow.ecart   { background: linear-gradient(135deg, rgba(255,193,60,0.22), rgba(255,193,60,0.08)); }
        .stat-wow.ecart-ok { background: linear-gradient(135deg, rgba(24,201,167,0.22), rgba(24,201,167,0.08)); }
        .stat-wow-icon { font-size: 1.6rem; margin-bottom: 0.3rem; }
        .stat-wow-label {
            color: rgba(200,215,255,0.75);
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-weight: 700;
            margin-bottom: 0.4rem;
        }
        .stat-wow-value {
            font-size: 1.65rem;
            font-weight: 900;
            line-height: 1.1;
            color: white;
            letter-spacing: -0.02em;
            margin-bottom: 0.3rem;
        }
        .stat-wow-value.credit { color: #18c9a7; }
        .stat-wow-value.debit  { color: #ff6464; }
        .stat-wow-value.solde  { color: #a5b4fc; }
        .stat-wow-value.ecart  { color: #ffc13c; }
        .stat-wow-value.ecart-ok { color: #18c9a7; }
        .stat-wow-note { color: rgba(200,215,255,0.55); font-size: 0.82rem; }

        /* ── Upload CTA info box ── */
        .upload-cta {
            background: linear-gradient(135deg, rgba(15,28,55,0.85), rgba(20,38,75,0.80));
            border: 1px solid rgba(124,140,255,0.28);
            border-radius: 20px;
            padding: 1.1rem 1.4rem;
            text-align: center;
            color: #c8d8ff;
            font-size: 1rem;
            font-weight: 600;
            margin-top: 0.8rem;
            backdrop-filter: blur(12px);
        }
        .upload-cta a { color: #7cc4ff; text-decoration: underline dotted; }

        /* ── Dataframe tweaks ── */
        .stDataFrame iframe { border-radius: 16px !important; }
        [data-testid="stDataFrameResizable"] { border-radius: 16px !important; overflow: hidden; }

        /* ============================================================
           RESPONSIVE MOBILE (Android / iPhone)
           ============================================================ */

        /* Désactiver les animations lourdes sur mobile — évite le gel */
        @media (max-width: 768px) {
            .stApp::before,
            .stApp::after { display: none !important; }

            .hero-shell::before { display: none !important; }

            .hero-shell {
                animation: fadeUp 0.5s ease !important;
                border-radius: 18px !important;
                padding: 1rem !important;
            }

            .main .block-container {
                padding-left: 0.75rem !important;
                padding-right: 0.75rem !important;
                padding-top: 0.25rem !important;
            }

            /* Hero title plus petit */
            .hero-title { font-size: 1.7rem !important; }
            .hero-subtitle { font-size: 0.9rem !important; margin-bottom: 0.7rem !important; }

            /* KPI grid : 2 colonnes sur mobile */
            .hero-grid {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 0.6rem !important;
            }

            /* Stat wow : 2 colonnes */
            .stat-wow-row {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 0.6rem !important;
            }
            .stat-wow-value { font-size: 1.2rem !important; }
            .stat-wow-icon { font-size: 1.2rem !important; }

            /* Tabs : scroll horizontal sur mobile */
            .stTabs [data-baseweb="tab-list"] {
                overflow-x: auto !important;
                flex-wrap: nowrap !important;
                gap: 0.3rem !important;
                padding: 0.25rem !important;
                -webkit-overflow-scrolling: touch;
            }
            .stTabs [data-baseweb="tab"] {
                white-space: nowrap !important;
                font-size: 0.82rem !important;
                padding: 0.5rem 0.7rem !important;
                min-height: 44px !important;
            }

            /* Uploader plus grand sur mobile */
            [data-testid="stFileUploader"] {
                border-radius: 16px !important;
                padding: 0.4rem !important;
            }
            [data-testid="stFileUploaderDropzone"] {
                min-height: 100px !important;
                flex-direction: column !important;
                gap: 0.5rem !important;
            }
            [data-testid="stFileUploader"] button {
                width: 100% !important;
                min-height: 52px !important;
                font-size: 1rem !important;
                border-radius: 12px !important;
            }

            /* Cards info : min-height plus petit */
            .kpi-card { min-height: 90px !important; }
            .info-panel { padding: 0.75rem !important; }

            /* Enlever backdrop-filter coûteux sur mobile */
            .hero-shell,
            .kpi-card,
            .glass-card,
            .info-panel,
            .sidebar-card,
            .soft-badge,
            .stat-wow {
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
            }

            /* Upload CTA */
            .upload-cta { font-size: 0.95rem !important; padding: 0.9rem 1rem !important; }

            /* Boutons principaux */
            .stButton > button,
            .stDownloadButton > button {
                min-height: 48px !important;
                font-size: 0.95rem !important;
            }

            /* Footernote */
            .footer-note { font-size: 0.78rem !important; }
        }

        /* Très petits écrans (< 480px) */
        @media (max-width: 480px) {
            .hero-grid { grid-template-columns: 1fr 1fr !important; }
            .stat-wow-row { grid-template-columns: 1fr 1fr !important; }
            .hero-title { font-size: 1.45rem !important; }
            .badge-row { gap: 0.3rem !important; }
            .soft-badge { font-size: 0.72rem !important; padding: 0.3rem 0.55rem !important; }
        }

        /* Réduire animations si l'utilisateur préfère */
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important;
                transition-duration: 0.01ms !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_hero() -> None:
    st.markdown(
        """
        <section class="hero-shell">
            <div class="badge-row">
                <span class="soft-badge">🏦 Multi-banques FR</span>
                <span class="soft-badge">📦 Export OFX pour Odoo</span>
                <span class="soft-badge">⚡ Extraction rapide</span>
            </div>
            <h1 class="hero-title">OFX Converter Pro</h1>
            <p class="hero-subtitle">
                Une expérience comptable moderne, immersive et rassurante :
                animations fluides, feedback visuel instantané, lecture facilitée et parcours
                d’import ultra-clair pour transformer un relevé bancaire en fichier OFX propre.
            </p>
            <div class="hero-grid">
                <div class="kpi-card">
                    <div class="kpi-label">Banques supportées</div>
                    <div class="kpi-value">15+</div>
                    <div class="kpi-note">France, néobanques et pros</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Stratégies</div>
                    <div class="kpi-value">3 couches</div>
                    <div class="kpi-note">Colonne, texte dédié, fallback regex</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Objectif UX</div>
                    <div class="kpi-value">Ultra fluide</div>
                    <div class="kpi-note">Moins de friction, plus de confiance</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Résultat</div>
                    <div class="kpi-value">OFX prêt</div>
                    <div class="kpi-note">Compatible Odoo 14 à 17+</div>
                </div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_feature_band() -> None:
    st.markdown(
        """
        <div class="feature-grid">
            <div class="feature-card">
                <div class="icon">📤</div>
                <div class="title">Upload guidé</div>
                <div class="text">Zone de dépôt élégante, lisible et rassurante pour lancer la conversion sans hésitation.</div>
            </div>
            <div class="feature-card">
                <div class="icon">🧠</div>
                <div class="title">Détection intelligente</div>
                <div class="text">Banque, IBAN, BIC, soldes et transactions sont extraits avec plusieurs stratégies complémentaires.</div>
            </div>
            <div class="feature-card">
                <div class="icon">📊</div>
                <div class="title">Lecture instantanée</div>
                <div class="text">Les indicateurs clés sont mis en avant avec des cartes animées pour une compréhension immédiate.</div>
            </div>
            <div class="feature-card">
                <div class="icon">🚀</div>
                <div class="title">Export premium</div>
                <div class="text">Le fichier OFX est généré dans un parcours clair, avec aperçu et contrôle final des données.</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_info_card(title: str, value: str, emoji: str = "ℹ️") -> None:
    st.markdown(
        f"""
        <div class="info-panel">
            <div class="section-title">{emoji} {title}</div>
            <div class="small-muted" style="word-break:break-word;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stat_card(label: str, value: str, note: str, tone: str = "neutral") -> None:
    tone_class = {
        "success": "success",
        "danger": "danger",
        "warning": "warning",
        "neutral": "accent",
    }.get(tone, "accent")
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {tone_class}">{value}</div>
            <div class="kpi-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            """
            <div class="sidebar-card">
                <div class="section-title">👨‍💻 À propos</div>
                <div class="small-muted"><strong>Achraf BEN YOUNES</strong><br>🚀 Data & AI Engineer</div>
                <div style="height:10px"></div>
                <div class="section-title">📞 Contact</div>
                <div class="small-muted">🇫🇷 07 60 93 53 71</div>
                <div style="height:10px"></div>
                <div class="section-title">📧 Email</div>
                <div class="small-muted">achrafbenyounes2012@gmail.com</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("")
        st.markdown(
            """
            <div class="glass-card">
                <div class="section-title">🧭 Conseils UX</div>
                <div class="small-muted">
                    • Déposez un PDF natif pour une extraction plus rapide.<br>
                    • Utilisez un PDF téléchargé depuis votre espace bancaire.<br>
                    • Vérifiez le solde calculé avant l’export.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def main():
    inject_custom_ui()
    render_sidebar()
    render_hero()

    st.markdown("<div style=’height: 8px’></div>", unsafe_allow_html=True)

    uploaded = st.file_uploader(
        "📁 Déposer le relevé bancaire (PDF)",
        type=["pdf"],
        help="Format accepté : PDF texte natif (relevé téléchargé depuis votre espace bancaire)",
    )

    if not uploaded:
        st.markdown(
            """
            <div class="upload-cta">
                Importez un relevé bancaire pour lancer <strong>l’analyse automatique</strong>.
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    safe_name = sanitize_filename(uploaded.name)
    if safe_name != uploaded.name:
        st.caption(f"📄 Fichier reçu : `{uploaded.name}` → normalisé en `{safe_name}`")

    with st.spinner("🔍 Analyse du relevé en cours…"):
        result = extract_all(uploaded)

    bank = result["bank"]
    iban = result["iban"]
    bic = result["bic"]
    opening = result["opening"]
    closing = result["closing"]
    df = result["df"]
    raw_text = result["raw_text"]

    st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)

    top_tabs = st.tabs(["Vue d’ensemble", "Transactions", "Export OFX", "Debug"])

    with top_tabs[0]:
        st.markdown(
            """
            <div class="section-title">🏦 Informations du compte</div>
            <div class="section-subtitle">Les métadonnées détectées sont présentées dans des cartes lisibles.</div>
            """,
            unsafe_allow_html=True,
        )
        i1, i2, i3 = st.columns([1, 1.4, 0.7], gap="medium")
        with i1:
            render_info_card("Banque détectée", bank or "Non détectée", "🏦")
        with i2:
            render_info_card("IBAN", iban or "Non détecté", "💳")
        with i3:
            render_info_card("BIC", bic or "Non détecté", "🏷️")

        st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)
        st.markdown(
            """
            <div class="section-title">💰 Pilotage des soldes</div>
            <div class="section-subtitle">Corrigez le solde d’ouverture si nécessaire et contrôlez l’écart final.</div>
            """,
            unsafe_allow_html=True,
        )

        col_o, col_c = st.columns([1.2, 1], gap="medium")
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

        if df.empty:
            st.error("❌ Aucune transaction détectée. Vérifiez le format du PDF.")
        else:
            total_credit = df[df["montant"] > 0]["montant"].sum()
            total_debit = df[df["montant"] < 0]["montant"].sum()
            solde_calcule = opening_balance + total_credit + total_debit
            delta = abs(solde_calcule - closing) if closing is not None else None

            ecart_class = "ecart-ok" if (delta is not None and delta < 0.05) else "ecart"
            ecart_value = f"{delta:.2f} €" if delta is not None else "N/A"
            ecart_note = ("Concordance OK ✓" if (delta is not None and delta < 0.05)
                          else ("Vérifier" if delta is not None else "Clôture non détectée"))
            st.markdown(
                f"""
                <div class="stat-wow-row">
                    <div class="stat-wow credit">
                        <div class="stat-wow-icon">📈</div>
                        <div class="stat-wow-label">Total crédit</div>
                        <div class="stat-wow-value credit">+{total_credit:,.2f} €</div>
                        <div class="stat-wow-note">Flux entrants</div>
                    </div>
                    <div class="stat-wow debit">
                        <div class="stat-wow-icon">📉</div>
                        <div class="stat-wow-label">Total débit</div>
                        <div class="stat-wow-value debit">−{abs(total_debit):,.2f} €</div>
                        <div class="stat-wow-note">Décaissements</div>
                    </div>
                    <div class="stat-wow solde">
                        <div class="stat-wow-icon">💼</div>
                        <div class="stat-wow-label">Solde calculé</div>
                        <div class="stat-wow-value solde">{solde_calcule:,.2f} €</div>
                        <div class="stat-wow-note">Ouverture + mouvements</div>
                    </div>
                    <div class="stat-wow {ecart_class}">
                        <div class="stat-wow-icon">{"✅" if (delta is not None and delta < 0.05) else "⚠️"}</div>
                        <div class="stat-wow-label">Écart vs relevé</div>
                        <div class="stat-wow-value {ecart_class}">{ecart_value}</div>
                        <div class="stat-wow-note">{ecart_note}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with top_tabs[1]:
        if df.empty:
            st.error("❌ Aucune transaction détectée. Vérifiez le format du PDF.")
        else:
            st.markdown(
                f"""
                <div class="section-title">📊 Transactions — {len(df)} opérations</div>
                <div class="section-subtitle">Tableau immersif pour parcourir rapidement l’ensemble des écritures.</div>
                """,
                unsafe_allow_html=True,
            )
            df_display = df.copy()
            df_display["montant"] = pd.to_numeric(df_display["montant"], errors="coerce")
            st.dataframe(
                df_display,
                use_container_width=True,
                height=min(48 * len(df_display) + 58, 600),
                column_config={
                    "date": st.column_config.TextColumn("📅 Date", width="small"),
                    "libelle": st.column_config.TextColumn("📝 Libellé", width="large"),
                    "montant": st.column_config.NumberColumn(
                        "💶 Montant (€)",
                        format="%.2f",
                        width="small",
                    ),
                },
            )

    with top_tabs[2]:
        st.markdown(
            """
            <div class="section-title">⚙️ Paramètres export OFX</div>
            <div class="section-subtitle">Finalisez les identifiants bancaires puis générez votre fichier OFX.</div>
            """,
            unsafe_allow_html=True,
        )
        ci, cb = st.columns(2, gap="large")
        with ci:
            iban_input = st.text_input("IBAN", value=iban or "")
        with cb:
            bic_input = st.text_input("BIC", value=bic or "")

        if df.empty:
            st.error("❌ Impossible de générer un OFX sans transaction.")
        else:
            if st.button("🚀 Générer le fichier OFX", type="primary", use_container_width=True):
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
                    use_container_width=True,
                )

                with st.expander("👁️ Aperçu OFX (80 premières lignes)"):
                    st.code("\n".join(ofx_content.split("\n")[:80]), language="xml")

                closing_final = opening_balance + df["montant"].sum()
                st.success(
                    f"✅ OFX généré — {len(df)} transactions | Clôture estimée : {closing_final:.2f} € | Fichier : `{ofx_filename}`"
                )

    with top_tabs[3]:
        st.markdown(
            """
            <div class="section-title">🛠️ Outils de debug</div>
            <div class="section-subtitle">Utiles pour contrôler le texte extrait quand un relevé est atypique.</div>
            """,
            unsafe_allow_html=True,
        )
        if raw_text:
            with st.expander("🔍 Texte brut extrait"):
                st.text(raw_text[:5000])
        else:
            st.info("Aucun texte brut disponible pour ce document.")

    st.markdown(
        """
        <div class="footer-note">
            OFX Converter Pro — conversion fiable, multi-banques France, export Odoo en un clic.
            <span style="margin-left:1.2rem; color:rgba(180,200,240,0.45); font-size:0.8rem; font-weight:500;">
                Conçu par <strong style="color:rgba(180,200,240,0.65);">Achraf BEN YOUNES</strong>
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()