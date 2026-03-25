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
    if "SOCIETE GENERALE" in t or "SOCIÉTÉ GÉNÉRALE" in t:
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
    # ── BNP Paribas : "Solde au 30 NOVEMBRE : + 3 351,28" ──
    r"[Ss]olde\s+au\s+\d+\s+[A-ZÉÈÊA-Za-zéèêàâùûîô]{3,}\s*[:\n]?\s*" + _AMT,
    # ── La Banque Postale ──
    r"(\d[\d\s\xa0]*,\d{2})\s*\n[Aa]ncien\s+solde",
    r"(\d[\d\s\xa0]*,\d{2})\s+[Aa]ncien\s+solde",
    r"[Aa]ncien\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Aa]ncien\s+solde\s*:?\s*" + _AMT,
    # ── Crédit Agricole : "Ancien solde créditeur au 31.12.2025 104,81" ──
    r"[Aa]ncien\s+solde\s+cr[ée]diteur\s+au\s+[\d\.\/\-]+\s*" + _AMT,
    # ── SG : "SOLDE PRÉCÉDENT AU 30/09/2025 4.908,86" ──
    r"SOLDE\s+PR[EÉ]C[EÉ]DENT\s+AU\s+\d{2}/\d{2}/\d{4}\s*" + _AMT,
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
    # ── Revolut Business (anglais) ──
    r"[Oo]pening\s+[Bb]alance\s*:?\s*€?\s*" + _AMT,
]

_CLOSING_PATTERNS = [
    # ── BNP Paribas : 2ème occurrence "Solde au 31 DÉCEMBRE" ──
    r"[Ss]olde\s+au\s+\d+\s+[A-ZÉÈÊA-Za-zéèêàâùûîô]{3,}\s*[:\n]?\s*" + _AMT,
    r"[Nn]ouveau\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Nn]ouveau\s+solde\s*:?\s*" + _AMT,
    r"[Ss]olde\s+final\s*:?\s*" + _AMT,
    r"[Ss]olde\s+[àa]\s+la\s+cl[ôo]ture\s*:?\s*" + _AMT,
    r"SOLDE\s+FINAL\s+" + _AMT,
    r"SOLDE\s+EN\s+FIN\s+DE\s+P[EÉ]RIODE\s+" + _AMT,
    r"[Cc]losing\s+[Bb]alance\s*:?\s*€?\s*" + _AMT,
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
    Revolut Business — format anglais.
    Table : Date (UTC) | Description | Money out | Money in | Balance
    Dates  : DD Mon YYYY  (ex : 28 Feb 2026)
    Signe  : déterminé par l'évolution du solde colonne Balance.
    """
    MONTHS = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }

    # Solde d'ouverture
    m = re.search(r"[Oo]pening\s+[Bb]alance\s+€\s*([\d\s,\.]+)", text)
    opening = clean_amount(m.group(1)) if m else None

    date_re = re.compile(
        r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\s*(.*)"
    )

    rows: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        dm = date_re.match(line)
        if not dm:
            continue
        day, mon_str, yr, rest = dm.groups()
        date_str = f"{day.zfill(2)}/{MONTHS.get(mon_str, '01')}/{yr}"

        # Tous les montants €XX.XX ou €X,XXX.XX sur la ligne
        euros = re.findall(r"€\s*([\d\s,\.]+)", rest)
        amounts = [clean_amount(e) for e in euros]
        amounts = [a for a in amounts if a is not None and a > 0]

        if len(amounts) < 2:
            continue

        balance = amounts[-1]
        transaction_amt = amounts[-2]  # avant le solde = montant de la transaction

        # Description : enlever les montants €
        desc = re.sub(r"€[\d\s,\.]+", "", rest)
        desc = re.sub(r"^\s*\w{2,4}\s+", "", desc.strip())  # enlever code type ex "MOS"
        desc = re.sub(r"\s{2,}", " ", desc).strip()[:120]

        rows.append({
            "date": date_str,
            "libelle": desc,
            "transaction_amt": transaction_amt,
            "balance": balance,
        })

    if not rows:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    # Revolut affiche du plus récent au plus ancien → renverser
    rows_chrono = list(reversed(rows))
    prev_bal = opening if opening is not None else 0.0
    transactions: list[dict] = []

    for row in rows_chrono:
        delta = row["balance"] - prev_bal
        montant = abs(row["transaction_amt"]) if delta >= 0 else -abs(row["transaction_amt"])
        prev_bal = row["balance"]
        transactions.append({
            "date": row["date"],
            "libelle": row["libelle"],
            "montant": round(montant, 2),
        })

    df = pd.DataFrame(transactions)
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
    Caisse d'Épargne — colonne MONTANT EN EUR avec signe explicite +/-.
    Ex : "-1 800,00"  |  "+128,00"
    Dates : DD/MM/YYYY
    """
    year = str(datetime.now().year)
    m = re.search(r"\b(20\d{2})\b", text)
    if m:
        year = m.group(1)

    # Stopper aux totaux
    for stop in [r"TOTAL DES OPERATIONS", r"Sous réserve"]:
        sm = re.search(stop, text, re.IGNORECASE)
        if sm:
            text = text[: sm.start()]

    transactions: list[dict] = []
    block: list[str] = []

    _SKIP = re.compile(
        r"DATE\s+D.OPERATION|DATE\s+DE\s+VALEUR|DETAIL|MONTANT|SYNTHESE|"
        r"COMPTE\s+COURANT|IBAN|BIC|Page\s+\d|SOLDE\s+CREDITEUR",
        re.IGNORECASE,
    )

    def process_ce_block(blines: list[str]) -> dict | None:
        full = " ".join(blines).strip()
        dm = re.match(r"^(\d{2}/\d{2}/\d{4})", full)
        if not dm:
            return None
        date_str = dm.group(1)

        # Montant final avec signe +/-
        m = re.search(r"([\+\-])\s*(\d[\d\s\xa0]*[,\.]\d{2})\s*$", full)
        if m:
            sign = m.group(1)
            amount = clean_amount(m.group(2))
            if amount is None:
                return None
            montant = abs(amount) if sign == "+" else -abs(amount)
        else:
            # Fallback : dernier montant sans signe
            raw = re.findall(r"[\+\-]?\s*\d[\d\s\xa0]*[,\.]\d{2}", full)
            if not raw:
                return None
            amount = clean_amount(raw[-1].replace("+", "").replace("-", ""))
            if amount is None:
                return None
            montant = amount

        # Description : enlever date opé + date valeur + montant
        desc = full[len(date_str):]
        desc = re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*", "", desc)  # enlever date valeur
        desc = re.sub(r"[\+\-]?\s*\d[\d\s\xa0]*[,\.]\d{2}\s*$", "", desc).strip()[:120]

        return {"date": date_str, "libelle": desc, "montant": round(montant, 2)}

    for line in (ln.strip() for ln in text.split("\n")):
        if not line or _SKIP.search(line):
            continue
        if re.match(r"^\d{2}/\d{2}/\d{4}\b", line):
            if block:
                tx = process_ce_block(block)
                if tx:
                    transactions.append(tx)
            block = [line]
        elif block:
            block.append(line)

    if block:
        tx = process_ce_block(block)
        if tx:
            transactions.append(tx)

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])
    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


# =========================================================
# PARSER TEXTE UNIVERSEL — Qonto + fallback OCR
# =========================================================

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
        r"\b(DATE|VALEUR|NATURE|OPERATION|DEBIT|CREDIT|LIBELLE|TOTAL"
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

    for page_words in pages_words:
        if not page_words:
            continue

        # Année depuis le texte de la page
        page_text = " ".join(w["text"] for w in page_words)
        ym = re.search(r"(20\d{2})", page_text)
        if ym:
            year = ym.group(1)

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
    "BNP_PARIBAS",
    "CIC",
    "CREDIT_MUTUEL",
    "SOCIETE_GENERALE",
    "CREDIT_AGRICOLE",
    "LCL",
    "GENERIC",
}

# Banques avec parser texte dédié (montants signés ou format spécifique)
_TEXT_BANKS = {
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
         - Parser colonne (DÉBIT|CRÉDIT)  : La Banque Postale, BNP, CIC, CM, SG, CA, LCL
         - Parser texte dédié             : Revolut, Shine, Banque Populaire, Caisse Épargne, Qonto
         - Parser texte universel         : fallback générique
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
    opening = extract_opening_balance(raw_text)
    closing = extract_closing_balance(raw_text)

    # Année de référence pour les formats DD/MM sans année
    year_match = re.search(r"(20\d{2})", raw_text)
    year_ref = year_match.group(1) if year_match else str(datetime.now().year)

    df = pd.DataFrame(columns=["date", "libelle", "montant"])

    # ── Dispatch parser ──

    if bank in _TEXT_BANKS:
        # Parsers texte dédiés (signés ou format spécifique)
        parser_fn = _TEXT_BANKS[bank]
        # Passer year si la fonction l'accepte (Shine, Qonto n'en ont pas besoin)
        try:
            df = parser_fn(raw_text, year_ref)
        except TypeError:
            df = parser_fn(raw_text)

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