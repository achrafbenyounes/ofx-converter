"""
OFX Converter Pro — Multi-banques France → Odoo
================================================
Compatible : La Banque Postale · BNP Paribas · Crédit Agricole · Société Générale
             CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire
             Qonto · Boursorama / BoursoBank · Revolut · Hello Bank · N26 · Fortuneo…

Stratégie d'extraction des transactions :
  ► Utilise les coordonnées x/y de pdfplumber (colonnes Débit/Crédit détectées
    automatiquement) pour éviter les faux montants issus des références numériques.
  ► Regroupe les montants fractionnés : "1" + "702,68" → 1 702,68 → 1702.68

Format OFX généré :
  ► OFX 1.x SGML, compatible toutes versions Odoo (14-17+)
  ► Montants au format X.XX (point décimal, SANS virgule, 2 décimales)
"""

import hashlib
import re
import pandas as pd
import pdfplumber
import streamlit as st
from datetime import datetime

st.set_page_config(page_title="OFX Converter Pro", layout="wide")


# =========================================================
# UTILS
# =========================================================

def clean_amount(text: str) -> float | None:
    """
    Convertit un montant français en float.
    Gère : espaces/NBSP comme séparateurs de milliers, virgule décimale, +/€/¤.
    Ex: "1 702,68" → 1702.68  |  "+ 46 728,51 ¤" → 46728.51
    """
    if not text:
        return None
    text = str(text).replace("\xa0", "").replace(" ", "")
    text = re.sub(r"[€$£¤▶►▸*]", "", text)
    text = text.replace("+", "").replace(",", ".")
    text = re.sub(r"[^\d.\-]", "", text)
    # "03.02.26" a plusieurs points → garder le dernier séparateur décimal
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
    """Vrai si la chaîne est de la forme NNN,NN (décimale d'un montant français)."""
    return bool(re.match(r"^\d{1,3},\d{2}$", s.strip()))


def is_leading_digit(s: str) -> bool:
    """Vrai si c'est 1-2 chiffres isolés (partie entière >999 séparée du reste)."""
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
    if "CAISSE D'EPARGNE" in t or "CAISSE EPARGNE" in t:
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
    return "GENERIC"


# =========================================================
# IBAN / BIC
# =========================================================

def extract_iban_bic(text: str) -> tuple[str | None, str | None]:
    iban, bic = None, None
    m = re.search(r"\bFR\d{2}[\s\dA-Z]{10,35}\b", text)
    if m:
        candidate = re.sub(r"\s+", "", m.group())
        if 14 <= len(candidate) <= 34:
            iban = candidate
    m = re.search(r"\b[A-Z]{4}FR[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", text)
    if m:
        bic = m.group()
    return iban, bic


# =========================================================
# SOLDES D'OUVERTURE / CLÔTURE — MULTI-BANQUES
# =========================================================

_AMT = r"([\+\-]?\s*\d[\d\s\xa0]*[,\.]\d{2})"

# Patterns couvrant toutes les banques françaises, par ordre de priorité
_OPENING_PATTERNS = [
    # La Banque Postale : montant sur la ligne précédant "Ancien solde"
    r"(\d[\d\s\xa0]*,\d{2})\s*\n[Aa]ncien\s+solde",
    r"(\d[\d\s\xa0]*,\d{2})\s+[Aa]ncien\s+solde",
    # Banque Postale / générique
    r"[Aa]ncien\s+solde\s+au\s+[\d/]+\s*" + _AMT,
    r"[Aa]ncien\s+solde\s*:?\s*" + _AMT,
    # Générique "solde"
    r"[Ss]olde\s+initial\s*:?\s*" + _AMT,
    r"[Ss]olde\s+pr[ée]c[ée]dent\s*:?\s*" + _AMT,
    r"[Ss]olde\s+ant[ée]rieur\s*:?\s*" + _AMT,
    r"[Ss]olde\s+d['']ouverture\s*:?\s*" + _AMT,
    r"[Rr]eport\s+[àa]\s+nouveau\s*:?\s*" + _AMT,
    r"[Rr]eport\s+du\s+mois\s+pr[ée]c[ée]dent\s*:?\s*" + _AMT,
    r"[Ss]olde\s+(?:cr[ée]diteur|d[ée]biteur)\s+au\s+\d{2}[\./\-]\d{2}[\./\-]\d{4}\s*" + _AMT,
    r"[Ss]olde\s+au\s+\d{2}[\./\-]\d{2}[\./\-]\d{4}\s*:?\s*" + _AMT,
    r"[Ss]olde\s+comptable\s+au\s+\d{2}[\./\-]\d{2}[\./\-]\d{4}\s*:?\s*" + _AMT,
    # Crédit Agricole / LCL
    r"SOLDE\s+ANTERIEUR\s+" + _AMT,
    r"SOLDE\s+PRECEDENT\s+" + _AMT,
    # BNP / Société Générale
    r"[Vv]otre\s+solde\s+au\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"[Bb]alance?\s+(?:pr[ée]c[ée]dente|initiale|d['']ouverture)\s*:?\s*" + _AMT,
    # Caisse d'Épargne / Banque Populaire
    r"[Ss]olde\s+[àa]\s+la\s+date\s+du\s+\d{2}/\d{2}/\d{4}\s*:?\s*" + _AMT,
    r"SOLDE\s+EN\s+DEBUT\s+DE\s+PERIODE\s*:?\s*" + _AMT,
    # Boursorama / N26 (en anglais)
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
    # Certaines banques affichent le solde en fin de relevé précédé d'un label
    r"[Ss]olde\s+au\s+\d{2}/\d{2}/\d{4}\s*[\+\-\s]*" + _AMT,
]


def extract_opening_balance(text: str) -> float | None:
    for pattern in _OPENING_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val
    # Fallback intelligent : dernier montant avant la 1ère date de transaction
    first_tx = re.search(r"\d{2}/\d{2}", text)
    if first_tx:
        before = text[: first_tx.start()]
        amounts = re.findall(r"\d[\d\s\xa0]*,\d{2}", before)
        if amounts:
            val = clean_amount(amounts[-1])
            if val is not None:
                return val
    return None


def extract_closing_balance(text: str) -> float | None:
    for pattern in _CLOSING_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val
    return None


# =========================================================
# EXTRACTION TRANSACTIONS PAR COLONNES (ROBUSTE)
# =========================================================

def _detect_columns(words: list[dict]) -> tuple[float, float, float]:
    """
    Détecte automatiquement les positions x des colonnes Débit et Crédit.
    Retourne (desc_end_x, debit_col_x, credit_col_x).
    """
    debit_x, credit_x = None, None
    for w in words:
        txt = w["text"].upper()
        if re.search(r"D[EÉ]BIT", txt) and w["x0"] > 350:
            debit_x = w["x0"]
        if re.search(r"CR[EÉ]DIT", txt) and w["x0"] > 430:
            credit_x = w["x0"]
    # Valeurs par défaut calibrées sur La Banque Postale (fonctionne pour la plupart)
    if debit_x is None:
        debit_x = 432.0
    if credit_x is None:
        credit_x = 500.0
    return debit_x - 5, debit_x, credit_x


def _group_by_y(words: list[dict], y_tol: float = 2.0) -> dict[float, list[dict]]:
    """Groupe les mots par position verticale (tolérance de 2pt)."""
    lines: dict[float, list[dict]] = {}
    for w in words:
        y = round(w["top"] / y_tol) * y_tol
        lines.setdefault(y, []).append(w)
    return lines


def _extract_amount_from_line(
    line_words: list[dict], col_start: float, col_end: float
) -> float | None:
    """
    Extrait le montant dans la zone [col_start, col_end] d'une ligne.
    Gère les montants fractionnés : "1" + "702,68" → 1702.68
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
            # Le mot précédent dans la zone est-il la partie entière (1x xxx) ?
            if i > 0 and is_leading_digit(zone[i - 1]["text"]):
                prefix = int(zone[i - 1]["text"])
                return round(prefix * 1000 + base, 2)
            return round(base, 2)
    return None


def parse_transactions_by_column(pdf_file) -> pd.DataFrame:
    """
    Parse les transactions en utilisant les coordonnées x/y de pdfplumber.
    Identifie débit/crédit par la colonne du montant → fiabilité maximale.
    """
    year = str(datetime.now().year)
    transactions: list[dict] = []

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3)
            if not words:
                continue

            desc_end_x, debit_x, credit_x = _detect_columns(words)

            # Extraire l'année depuis le texte de la page
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
                libelle = " ".join(current_desc)[:120]
                libelle = re.sub(r"\s{2,}", " ", libelle).strip()
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
                # Ligne de transaction : commence par DD/MM en colonne de date (x < 80)
                date_candidates = [
                    w for w in line_words
                    if re.match(r"^\d{2}/\d{2}$", w["text"]) and w["x0"] < 80
                ]
                if date_candidates:
                    flush()
                    current_date = date_candidates[0]["text"]
                    current_debit = _extract_amount_from_line(
                        line_words, debit_x - 10, credit_x - 5
                    )
                    current_credit = _extract_amount_from_line(
                        line_words, credit_x - 5, 620
                    )
                    current_desc = [
                        w["text"]
                        for w in line_words
                        if 85 <= w["x0"] < desc_end_x
                    ]
                elif current_date is not None:
                    # Ligne de continuation : enrichit le libellé
                    extra = [
                        w["text"]
                        for w in line_words
                        if 85 <= w["x0"] < desc_end_x
                    ]
                    current_desc.extend(extra)

            flush()  # Dernière transaction de la page

    if not transactions:
        return pd.DataFrame(columns=["date", "libelle", "montant"])

    df = pd.DataFrame(transactions)
    df["montant"] = df["montant"].round(2)
    return df


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
    Génère un fichier OFX 1.x SGML.
    Règles Odoo (toutes versions) :
      - Montants : X.XX  (point décimal, SANS virgule, exactement 2 décimales)
      - TRNTYPE  : CREDIT ou DEBIT
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

        # ✅ Montant Odoo : TOUJOURS point décimal, JAMAIS virgule, 2 décimales
        amount_str = f"{row['montant']:.2f}"

        # Nettoyer les caractères XML spéciaux du libellé
        libelle_clean = re.sub(r"[<>&\"'\\]", " ", str(row["libelle"]))
        libelle_clean = re.sub(r"\s{2,}", " ", libelle_clean).strip()[:60]

        trntype = "CREDIT" if row["montant"] >= 0 else "DEBIT"

        lines += [
            "<STMTTRN>",
            f"<TRNTYPE>{trntype}",
            f"<DTPOSTED>{dt}",
            f"<TRNAMT>{amount_str}",
            f"<FITID>{fitid}",
            f"<n>{libelle_clean}",
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
        "CIC · Crédit Mutuel · LCL · Caisse d'Épargne · Banque Populaire · Qonto · Boursorama…"
    )

    uploaded = st.file_uploader("📁 Déposer le relevé bancaire (PDF)", type=["pdf"])
    if not uploaded:
        st.info("Importez un relevé bancaire PDF pour commencer.")
        return

    # ── Extraction du texte brut (soldes, IBAN, BIC) ──
    with pdfplumber.open(uploaded) as pdf:
        raw_pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
    raw_text = clean_text("\n".join(raw_pages))

    bank = detect_bank(raw_text)
    iban, bic = extract_iban_bic(raw_text)
    opening = extract_opening_balance(raw_text)
    closing = extract_closing_balance(raw_text)

    # ── Extraction transactions par colonnes ──
    uploaded.seek(0)
    df = parse_transactions_by_column(uploaded)

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
            help="Extrait automatiquement. Modifiable si nécessaire.",
        )
    with col_c:
        if closing is not None:
            st.metric("Solde de clôture extrait (€)", f"{closing:.2f}")
        else:
            st.warning("Solde de clôture non détecté dans le PDF")

    # ══════════ SECTION 3 : TRANSACTIONS ══════════
    if df.empty:
        st.error("❌ Aucune transaction détectée. Vérifiez le format du PDF.")
        return

    total_credit = df[df["montant"] > 0]["montant"].sum()
    total_debit = df[df["montant"] < 0]["montant"].sum()
    solde_calcule = opening_balance + total_credit + total_debit

    st.subheader(f"📊 Transactions — {len(df)} opérations")

    m1, m2, m3, m4 = st.columns(4)
    m1.success(f"✅ **Total Crédit**\n\n**{total_credit:.2f} €**")
    m2.error(f"🔻 **Total Débit**\n\n**{abs(total_debit):.2f} €**")
    m3.info(f"📌 **Solde calculé**\n\n**{solde_calcule:.2f} €**")
    if closing is not None:
        delta = abs(solde_calcule - closing)
        icon = "✅" if delta < 0.05 else "⚠️"
        msg = f"{icon} **Écart vs relevé**\n\n**{delta:.2f} €**"
        (m4.success if delta < 0.05 else m4.warning)(msg)

    # Affichage tableau — montants formatés à 2 décimales (sans virgule)
    df_display = df.copy()
    df_display["montant"] = df_display["montant"].apply(lambda x: f"{x:.2f}")
    st.dataframe(df_display, use_container_width=True, height=420)

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

        st.download_button(
            label="⬇️ Télécharger le fichier OFX",
            data=ofx_content,
            file_name=uploaded.name.replace(".pdf", ".ofx"),
            mime="text/plain",
        )

        with st.expander("👁️ Aperçu OFX (80 premières lignes)"):
            st.code("\n".join(ofx_content.split("\n")[:80]), language="xml")

        closing_final = opening_balance + df["montant"].sum()
        st.success(
            f"✅ OFX généré — **{len(df)} transactions** | "
            f"Clôture : **{closing_final:.2f} €**"
        )


if __name__ == "__main__":
    main()