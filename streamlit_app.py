"""
OFX Converter for ODOO — app.py
======================================
Auteur  : Achraf BEN YOUNES
- Sidebar configuration supprimée (intégrée dans la page principale)
- Nouveau design clair, moderne et attractif
- Banques étendues : France, Belgique, Suisse, Tunisie
- pdf2image + GCV pour PDF vectoriels
"""

import hashlib
import io
import re
import traceback
from datetime import datetime
from io import BytesIO, StringIO

import pandas as pd
import pdfplumber
import streamlit as st
from PIL import Image
import base64

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

st.set_page_config(
    page_title="OFX Converter — Odoo",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Sora:wght@300;400;500;600;700;800&display=swap');

:root {
    --bg:        #f0f4ff;
    --bg2:       #e8eeff;
    --surface:   #ffffff;
    --card:      #ffffff;
    --card2:     #f7f9ff;
    --border:    #dde3f5;
    --border2:   #c5d0f0;
    --accent:    #3b5bdb;
    --accent2:   #0ca678;
    --accent3:   #f76707;
    --success:   #099268;
    --warning:   #e67700;
    --danger:    #c92a2a;
    --text:      #1a1f36;
    --text2:     #3d4466;
    --muted:     #8891b2;
    --muted2:    #5c6490;
    --glow:      rgba(59,91,219,0.12);
}

*, *::before, *::after { box-sizing: border-box; }

html, body, [data-testid="stAppViewContainer"] {
    background: var(--bg) !important;
    font-family: 'Sora', sans-serif !important;
    color: var(--text) !important;
}

/* Hide sidebar toggle */
[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stSidebar"] { display: none !important; }
section[data-testid="stSidebarContent"] { display: none !important; }

/* Main content full width */
.main .block-container {
    max-width: 1280px !important;
    padding: 2rem 2.5rem !important;
}

/* ── Hero Header ── */
.ofx-hero {
    background: linear-gradient(135deg, #3b5bdb 0%, #1971c2 35%, #0ca678 100%);
    border-radius: 24px;
    padding: 40px 48px;
    margin-bottom: 32px;
    position: relative;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(59,91,219,0.25);
}
.ofx-hero::before {
    content: '';
    position: absolute; top: -50%; right: -10%; width: 500px; height: 500px;
    background: radial-gradient(circle, rgba(255,255,255,0.08) 0%, transparent 70%);
    border-radius: 50%;
}
.ofx-hero::after {
    content: '';
    position: absolute; bottom: -30%; left: 20%; width: 300px; height: 300px;
    background: radial-gradient(circle, rgba(12,166,120,0.15) 0%, transparent 70%);
    border-radius: 50%;
}
.hero-dots {
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background-image: radial-gradient(rgba(255,255,255,0.12) 1.5px, transparent 1.5px);
    background-size: 28px 28px;
    pointer-events: none;
}
.hero-content { position: relative; z-index: 1; }
.hero-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.25);
    border-radius: 999px; padding: 4px 12px;
    font-family: 'DM Mono', monospace; font-size: 0.68rem; color: rgba(255,255,255,0.9);
    letter-spacing: 0.08em; margin-bottom: 16px;
}
.hero-title {
    font-family: 'Sora', sans-serif; font-size: 2.6rem; font-weight: 800;
    color: #fff; line-height: 1.1; margin: 0 0 10px 0;
}
.hero-title span { opacity: 0.7; font-weight: 300; }
.hero-sub {
    font-family: 'DM Mono', monospace; font-size: 0.75rem;
    color: rgba(255,255,255,0.7); letter-spacing: 0.05em; margin-bottom: 24px;
}
.hero-stats {
    display: flex; gap: 32px; flex-wrap: wrap;
}
.hero-stat {
    display: flex; flex-direction: column; gap: 2px;
}
.hero-stat-val {
    font-family: 'Sora', sans-serif; font-size: 1.5rem; font-weight: 700; color: #fff;
}
.hero-stat-lbl {
    font-family: 'DM Mono', monospace; font-size: 0.62rem;
    color: rgba(255,255,255,0.6); letter-spacing: 0.1em; text-transform: uppercase;
}

/* ── Cards ── */
.config-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
    margin-bottom: 24px;
}
.config-grid-2 { grid-template-columns: repeat(2, 1fr); }
.config-grid-4 { grid-template-columns: repeat(4, 1fr); }

.card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: 0 2px 12px rgba(59,91,219,0.06);
}
.card-accent { border-top: 3px solid var(--accent); }
.card-teal   { border-top: 3px solid var(--accent2); }
.card-orange { border-top: 3px solid var(--accent3); }

.card-title {
    font-family: 'Sora', sans-serif; font-size: 0.70rem; font-weight: 700;
    letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted);
    margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
}
.card-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }

/* ── Step cards ── */
.step-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 24px 28px; margin-bottom: 20px;
    box-shadow: 0 2px 16px rgba(59,91,219,0.07);
    transition: box-shadow 0.2s;
}
.step-card:hover { box-shadow: 0 6px 28px rgba(59,91,219,0.12); }
.step-header { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }
.step-num {
    width: 36px; height: 36px; border-radius: 10px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--accent), #1971c2);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Sora', sans-serif; font-weight: 800; font-size: 0.85rem; color: #fff;
    box-shadow: 0 4px 12px rgba(59,91,219,0.3);
}
.step-title {
    font-family: 'Sora', sans-serif; font-weight: 700; font-size: 1.05rem; color: var(--text);
}
.step-desc {
    font-family: 'Sora', sans-serif; font-size: 0.78rem; color: var(--muted); margin-top: 2px;
}

/* ── Pills ── */
.pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 12px; border-radius: 999px;
    font-family: 'DM Mono', monospace; font-size: 0.68rem; font-weight: 400;
}
.pill-ok     { background: rgba(9,146,104,.10);  color: #099268; border: 1px solid rgba(9,146,104,.25); }
.pill-warn   { background: rgba(230,119,0,.10);  color: #e67700; border: 1px solid rgba(230,119,0,.25); }
.pill-info   { background: rgba(59,91,219,.10);  color: var(--accent); border: 1px solid rgba(59,91,219,.25); }
.pill-danger { background: rgba(201,42,42,.10);  color: #c92a2a; border: 1px solid rgba(201,42,42,.25); }
.pill-teal   { background: rgba(12,166,120,.10); color: var(--accent2); border: 1px solid rgba(12,166,120,.25); }

/* ── Metrics ── */
.metrics-grid {
    display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 22px;
}
.metric-box {
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px 18px;
}
.metric-label {
    font-family: 'DM Mono', monospace; font-size: 0.60rem; color: var(--muted);
    letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 6px;
}
.metric-value { font-family: 'Sora', sans-serif; font-size: 1.5rem; font-weight: 700; }
.metric-value.blue   { color: var(--accent); }
.metric-value.green  { color: var(--success); }
.metric-value.red    { color: var(--danger); }
.metric-value.teal   { color: var(--accent2); }

/* ── OFX Preview ── */
.ofx-preview {
    background: #1a1f36; border: 1px solid #2d3561;
    border-left: 3px solid var(--accent2); border-radius: 12px;
    padding: 16px 20px;
    font-family: 'DM Mono', monospace; font-size: 0.70rem;
    color: #8bb8ff; line-height: 1.9;
    max-height: 300px; overflow-y: auto; overflow-x: auto;
}
.ofx-tag   { color: #60a5fa; }
.ofx-value { color: #a5f3fc; }

/* ── Buttons ── */
.stButton > button {
    font-family: 'Sora', sans-serif !important; font-weight: 600 !important;
    border-radius: 10px !important; border: 1.5px solid var(--border2) !important;
    background: var(--card) !important; color: var(--text) !important;
    transition: all 0.18s !important;
}
.stButton > button:hover {
    background: rgba(59,91,219,0.07) !important;
    border-color: var(--accent) !important; color: var(--accent) !important;
}
.stDownloadButton > button {
    font-family: 'Sora', sans-serif !important; font-weight: 700 !important;
    background: linear-gradient(135deg, #3b5bdb, #0ca678) !important;
    border: none !important; color: #fff !important;
    border-radius: 10px !important; padding: 12px 24px !important;
    width: 100% !important; box-shadow: 0 4px 16px rgba(59,91,219,0.25) !important;
}

/* ── Inputs ── */
.stSelectbox > div > div,
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stTextArea textarea {
    background: var(--card2) !important; border-color: var(--border2) !important;
    color: var(--text) !important; font-family: 'DM Mono', monospace !important;
    font-size: 0.82rem !important; border-radius: 10px !important;
}
.stSelectbox > div > div:focus-within,
.stTextInput > div > div > input:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px rgba(59,91,219,0.12) !important;
}
[data-baseweb="select"] { background: var(--card2) !important; }

/* ── Labels ── */
.stSelectbox label, .stTextInput label, .stNumberInput label, .stTextArea label,
.stCheckbox label { 
    font-family: 'Sora', sans-serif !important; font-size: 0.78rem !important;
    font-weight: 600 !important; color: var(--text2) !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] {
    background: linear-gradient(135deg, rgba(59,91,219,0.04), rgba(12,166,120,0.04)) !important;
    border: 2px dashed var(--border2) !important;
    border-radius: 16px !important;
    transition: border-color 0.2s !important;
}
[data-testid="stFileUploader"]:hover { border-color: var(--accent) !important; }
[data-testid="stFileUploader"] * { color: var(--text2) !important; }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: var(--bg2) !important; border-radius: 10px 10px 0 0 !important;
    gap: 2px !important; padding: 4px !important;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'Sora', sans-serif !important; font-weight: 600 !important;
    font-size: 0.80rem !important; color: var(--muted2) !important;
    background: transparent !important; border-radius: 7px !important;
}
.stTabs [aria-selected="true"] {
    color: var(--accent) !important; background: var(--card) !important;
    box-shadow: 0 2px 8px rgba(59,91,219,0.12) !important;
}
.stTabs [data-baseweb="tab-panel"] {
    background: var(--card) !important;
    border: 1px solid var(--border) !important;
    border-top: none !important;
    border-radius: 0 0 12px 12px !important;
    padding: 16px !important;
}

/* ── Alerts ── */
.stAlert {
    border-radius: 12px !important; font-family: 'Sora', sans-serif !important;
    font-size: 0.82rem !important;
}

/* ── Expander ── */
.streamlit-expanderHeader {
    font-family: 'DM Mono', monospace !important; font-size: 0.78rem !important;
    color: var(--muted2) !important; background: var(--card2) !important;
    border-radius: 10px !important;
}
details { background: var(--card) !important; border: 1px solid var(--border) !important; border-radius: 12px !important; }

/* ── Checkbox ── */
.stCheckbox > label { font-family: 'Sora', sans-serif !important; font-size: 0.80rem !important; color: var(--text2) !important; }

/* ── Progress bar ── */
.stProgress > div > div { background: linear-gradient(90deg, var(--accent), var(--accent2)) !important; border-radius: 999px !important; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg2); border-radius: 3px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--accent); }

/* ── Divider ── */
hr { border-color: var(--border) !important; margin: 24px 0 !important; }

/* ── No-data ── */
.no-data-panel {
    background: linear-gradient(135deg, rgba(59,91,219,0.04), rgba(12,166,120,0.04));
    border: 2px dashed var(--border2); border-radius: 20px;
    padding: 56px 32px; text-align: center;
}
.no-data-icon { font-size: 3.5rem; margin-bottom: 16px; }
.no-data-title { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 1rem; color: var(--text2); margin-bottom: 8px; }
.no-data-sub { font-family: 'DM Mono', monospace; font-size: 0.68rem; color: var(--muted); line-height: 2.0; }

/* ── Validation ── */
.validation-row {
    display: flex; align-items: center; gap: 10px; padding: 7px 0;
    border-bottom: 1px solid var(--border);
    font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--text2);
}
.validation-row:last-child { border-bottom: none; }
.v-icon { font-size: 0.85rem; flex-shrink: 0; }
.v-ok { color: var(--success); } .v-warn { color: var(--warning); } .v-err { color: var(--danger); }

/* ── Footer ── */
.ofx-footer {
    text-align: center; padding: 20px 0 8px;
    font-family: 'DM Mono', monospace; font-size: 0.65rem; color: var(--muted);
}

/* ── Section divider ── */
.section-label {
    font-family: 'Sora', sans-serif; font-size: 0.68rem; font-weight: 700;
    letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted);
    display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
}
.section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }

/* ── Key indicator ── */
.key-ok {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(9,146,104,.08); border: 1px solid rgba(9,146,104,.2);
    border-radius: 8px; padding: 8px 12px;
    font-family: 'DM Mono', monospace; font-size: 0.70rem; color: var(--success);
}

/* ── Country tabs ── */
.country-strip {
    display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px;
}
.ctry-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 8px; padding: 5px 12px;
    font-family: 'Sora', sans-serif; font-size: 0.72rem; font-weight: 600; color: var(--muted2);
}
.ctry-chip span { font-size: 1rem; }

/* Streamlit dataframe */
[data-testid="stDataFrame"] { border-radius: 12px !important; overflow: hidden !important; }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════════
MAX_FILE_MB      = 25
MAX_ROWS_WARNING = 5_000
NONE_OPT         = "— Sélectionner —"

DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M",    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y",          "%Y-%m-%d",
    "%m/%d/%Y",          "%d-%m-%Y",
    "%d.%m.%Y",          "%d/%m/%y",
    "%m/%d/%y",          "%Y%m%d",
    "%d %b %Y",          "%d %B %Y",
    "%B %d, %Y",         "%b %d, %Y",
    "%d-%b-%Y",          "%Y/%m/%d",
    "%d %m %Y",          "%m-%d-%Y",
]

# ── Profils bancaires — France, Belgique, Suisse, Tunisie ──────────────────
BANK_PROFILES = {
    # ── FRANCE ────────────────────────────────────────────────────────────────
    "🇫🇷 Qonto":               {"bank_id": "QNTOFRP1XXX", "account_id": "FR76169580000178951302618940", "currency": "EUR"},
    "🇫🇷 BNP Paribas":         {"bank_id": "BNPAFRPP",    "account_id": "FR76300900010000000000000",    "currency": "EUR"},
    "🇫🇷 Société Générale":    {"bank_id": "SOGEFRPP",    "account_id": "FR76300300000000000000000",    "currency": "EUR"},
    "🇫🇷 Crédit Agricole":     {"bank_id": "AGRIFRPP",    "account_id": "FR76183500000000000000000",    "currency": "EUR"},
    "🇫🇷 LCL":                 {"bank_id": "CRLYFRPP",    "account_id": "FR76305000000000000000000",    "currency": "EUR"},
    "🇫🇷 CIC":                 {"bank_id": "CMCIFRPP",    "account_id": "FR76100700000000000000000",    "currency": "EUR"},
    "🇫🇷 Crédit Mutuel":       {"bank_id": "CMBRFR2B",    "account_id": "FR76100700000000000000000",    "currency": "EUR"},
    "🇫🇷 La Banque Postale":   {"bank_id": "PSSTFRPPPAR", "account_id": "FR76200410100000000000000",    "currency": "EUR"},
    "🇫🇷 HSBC France":         {"bank_id": "CCFRFRPP",    "account_id": "FR76300560000000000000000",    "currency": "EUR"},
    "🇫🇷 Boursorama":          {"bank_id": "BOUSFRPP",    "account_id": "FR76400000000000000000000",    "currency": "EUR"},
    "🇫🇷 Hello Bank":          {"bank_id": "BNPAFRPPIFB", "account_id": "FR76300900010000000000000",    "currency": "EUR"},
    "🇫🇷 Fortuneo":            {"bank_id": "FTNOFRP1",    "account_id": "FR76130070000000000000000",    "currency": "EUR"},
    "🇫🇷 Orange Bank":         {"bank_id": "BNPAFRPP",    "account_id": "FR76000000000000000000000",    "currency": "EUR"},
    "🇫🇷 Caisse d'Épargne":    {"bank_id": "CEPAFRPP",    "account_id": "FR76159000000000000000000",    "currency": "EUR"},
    "🇫🇷 Banque Populaire":    {"bank_id": "CCBPFRPPNAN", "account_id": "FR76104000000000000000000",    "currency": "EUR"},
    "🇫🇷 Crédit du Nord":      {"bank_id": "NORDFRPP",    "account_id": "FR76300920000000000000000",    "currency": "EUR"},
    "🇫🇷 AXA Banque":          {"bank_id": "AXABFRPP",    "account_id": "FR76190200000000000000000",    "currency": "EUR"},
    "🇫🇷 Revolut (FR)":        {"bank_id": "REVOLT21",    "account_id": "FR76000000000000000000000",    "currency": "EUR"},
    "🇫🇷 N26 (FR)":            {"bank_id": "NTSBDEB1",    "account_id": "DE00000000000000000000",       "currency": "EUR"},
    # ── BELGIQUE ───────────────────────────────────────────────────────────────
    "🇧🇪 BNP Paribas Fortis":  {"bank_id": "GEBABEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 ING Belgique":        {"bank_id": "BBRUBEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 KBC":                 {"bank_id": "KREDBEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 Belfius":             {"bank_id": "GKCCBEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 Argenta":             {"bank_id": "ARSPBE22",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 Fintro":              {"bank_id": "GEBABEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 Nagelmackers":        {"bank_id": "NICABEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    "🇧🇪 Crelan":              {"bank_id": "NICABEBB",    "account_id": "BE00000000000000",             "currency": "EUR"},
    # ── SUISSE ────────────────────────────────────────────────────────────────
    "🇨🇭 UBS":                 {"bank_id": "UBSWCHZH",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 Credit Suisse":       {"bank_id": "CRESCHZZ",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 Raiffeisen":          {"bank_id": "RAIFCH22",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 PostFinance":         {"bank_id": "POFICHBE",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 ZKB":                 {"bank_id": "ZKBKCHZZ",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 Cantonal Vaud (BCVS)":{"bank_id": "BCVLCH2L",   "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 Neon":                {"bank_id": "RAIFCH22",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    "🇨🇭 Yuh":                 {"bank_id": "POFICHBE",    "account_id": "CH0000000000000000000",        "currency": "CHF"},
    # ── TUNISIE ────────────────────────────────────────────────────────────────
    "🇹🇳 STB":                 {"bank_id": "STBKTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 BNA":                 {"bank_id": "BNATTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 Attijari Bank TN":    {"bank_id": "BSTUTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 BIAT":                {"bank_id": "BIATTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 UIB":                 {"bank_id": "UIBATNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 Amen Bank":           {"bank_id": "CFCTTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 BH Bank":             {"bank_id": "BHBKTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 BT (Banque de Tunis)":{"bank_id": "BTUNTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 Zitouna Bank":        {"bank_id": "ZITUTNTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    "🇹🇳 QNB Tunisie":         {"bank_id": "QNBATSTT",    "account_id": "TN59000000000000000000",       "currency": "TND"},
    # ── PERSONNALISÉ ────────────────────────────────────────────────────────────
    "⚙️ Personnalisé":         {"bank_id": "000000000",   "account_id": "FR76000000000000000000000000", "currency": "EUR"},
}

CURRENCIES_BY_COUNTRY = {
    "🇫🇷": "EUR", "🇧🇪": "EUR", "🇨🇭": "CHF", "🇹🇳": "TND"
}

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _is_na(val) -> bool:
    if val is None: return True
    try: return bool(pd.isna(val))
    except (TypeError, ValueError): return False

def amount_to_float(val) -> float:
    if _is_na(val): return 0.0
    if isinstance(val, bool): return 0.0
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    if not s or s.lower() in ("nan","none","-","n/a","—",""): return 0.0
    negative = s.startswith("(") and s.endswith(")")
    if negative: s = s[1:-1].strip()
    s = re.sub(r"[^\d.,\-+]", "", s)
    if not s or s in ("-","+",".",","): return 0.0
    if s.startswith("-"): negative = not negative; s = s[1:]
    elif s.startswith("+"): s = s[1:]
    if not s: return 0.0
    dot_c = s.count("."); comma_c = s.count(",")
    if dot_c >= 1 and comma_c >= 1:
        if s.rfind(",") > s.rfind("."): s = s.replace(".","").replace(",",".")
        else: s = s.replace(",","")
    elif dot_c == 1 and comma_c == 0:
        parts = s.split(".")
        if len(parts)==2 and len(parts[1])==3 and parts[0].lstrip("0"): s = s.replace(".","")
    elif comma_c == 1 and dot_c == 0:
        parts = s.split(",")
        if len(parts)==2 and len(parts[1])==3 and parts[0].lstrip("0"): s = s.replace(",","")
        else: s = s.replace(",",".")
    elif dot_c > 1: s = s.replace(".","")
    elif comma_c > 1: s = s.replace(",","")
    try:
        result = float(s)
        return -result if negative else result
    except ValueError: return 0.0

def format_ofx_date(dt) -> str:
    if _is_na(dt): return datetime.now().strftime("%Y%m%d%H%M%S")
    if hasattr(dt, "strftime"): return dt.strftime("%Y%m%d%H%M%S")
    if isinstance(dt, str):
        s = " ".join(dt.split()).strip()
        if not s: return datetime.now().strftime("%Y%m%d%H%M%S")
        for fmt in DATE_FORMATS:
            try: return datetime.strptime(s, fmt).strftime("%Y%m%d%H%M%S")
            except ValueError: continue
        try: return pd.to_datetime(s, dayfirst=True, errors="coerce").strftime("%Y%m%d%H%M%S")
        except Exception:
            m = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", s)
            if m:
                try: return pd.to_datetime(m.group(1), dayfirst=True).strftime("%Y%m%d%H%M%S")
                except: pass
            return datetime.now().strftime("%Y%m%d%H%M%S")
    try: return pd.to_datetime(dt).strftime("%Y%m%d%H%M%S")
    except Exception: return datetime.now().strftime("%Y%m%d%H%M%S")

def xml_escape(text: str) -> str:
    return (str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            .replace('"',"&quot;").replace("'","&apos;"))

def _read_bytes(file) -> bytes:
    if hasattr(file, "seek"): file.seek(0)
    if hasattr(file, "read"): return file.read()
    with open(file, "rb") as fh: return fh.read()

# ═══════════════════════════════════════════════════════════════════════════════
# OFX GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def build_ofx(transactions, bank_id, account_id, account_type, currency, language="ENG", opening_balance=0.0):
    now_str = datetime.now().strftime("%Y%m%d%H%M%S")
    parsed_dates = []
    for d in transactions["date"]:
        try: parsed_dates.append(pd.to_datetime(d, dayfirst=True))
        except Exception: pass
    if parsed_dates:
        dt_start = min(parsed_dates).strftime("%Y%m%d")
        dt_end   = max(parsed_dates).strftime("%Y%m%d%H%M%S")
    else: dt_start = dt_end = now_str
    total_mvt = sum(amount_to_float(a) for a in transactions["amount"])
    closing_balance = opening_balance + total_mvt
    header = ("OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:151\r\nSECURITY:NONE\r\n"
              "ENCODING:UTF-8\r\nCHARSET:UTF-8\r\nCOMPRESSION:NONE\r\n"
              "OLDFILEUID:NONE\r\nNEWFILEUID:NONE\r\n\r\n")
    lines = ["<OFX>","<SIGNONMSGSRSV1>","<SONRS>","<STATUS>","<CODE>0</CODE>",
             "<SEVERITY>INFO</SEVERITY>","</STATUS>",f"<DTSERVER>{now_str}</DTSERVER>",
             f"<LANGUAGE>{language}</LANGUAGE>","</SONRS>","</SIGNONMSGSRSV1>",
             "<BANKMSGSRSV1>","<STMTTRNRS>","<TRNUID>1001</TRNUID>","<STATUS>",
             "<CODE>0</CODE>","<SEVERITY>INFO</SEVERITY>","</STATUS>","<STMTRS>",
             f"<CURDEF>{currency}</CURDEF>","<BANKACCTFROM>",
             f"<BANKID>{xml_escape(bank_id)}</BANKID>",
             f"<ACCTID>{xml_escape(account_id)}</ACCTID>",
             f"<ACCTTYPE>{account_type}</ACCTTYPE>","</BANKACCTFROM>","<BANKTRANLIST>",
             f"<DTSTART>{dt_start}</DTSTART>",f"<DTEND>{dt_end[:8]}</DTEND>"]
    seen_fitids = set()
    for seq, (_, row) in enumerate(transactions.iterrows()):
        amt = amount_to_float(row["amount"])
        ttype = "CREDIT" if amt >= 0 else "DEBIT"
        date_fmt = format_ofx_date(row["date"])
        name = xml_escape(str(row.get("name","TRANSACTION")).strip()[:32])
        memo = xml_escape(str(row.get("memo","")).strip()[:255])
        if not memo: memo = name
        raw_id = f"{date_fmt}{name}{amt:.2f}{seq}"
        fitid = hashlib.sha1(raw_id.encode()).hexdigest()[:16].upper()
        if fitid in seen_fitids: fitid = f"{fitid[:12]}{str(seq).zfill(4)}"
        seen_fitids.add(fitid)
        lines += ["<STMTTRN>",f"<TRNTYPE>{ttype}</TRNTYPE>",f"<DTPOSTED>{date_fmt}</DTPOSTED>",
                  f"<TRNAMT>{amt:.2f}</TRNAMT>",f"<FITID>{fitid}</FITID>",
                  f"<NAME>{name}</NAME>",f"<MEMO>{memo}</MEMO>","</STMTTRN>"]
    lines += ["</BANKTRANLIST>","<LEDGERBAL>",f"<BALAMT>{closing_balance:.2f}</BALAMT>",
              f"<DTASOF>{dt_end}</DTASOF>","</LEDGERBAL>","<AVAILBAL>",
              f"<BALAMT>{closing_balance:.2f}</BALAMT>",f"<DTASOF>{dt_end}</DTASOF>",
              "</AVAILBAL>","</STMTRS>","</STMTTRNRS>","</BANKMSGSRSV1>","</OFX>"]
    return header + "\r\n".join(lines)

# ═══════════════════════════════════════════════════════════════════════════════
# GCV
# ═══════════════════════════════════════════════════════════════════════════════

def _get_gcv_api_key() -> str:
    try:
        key = st.secrets.get("GCV_API_KEY", "")
        if key: return key
    except Exception: pass
    return st.session_state.get("gcv_api_key", "")

def _rasterize_pdf_bytes_to_images(raw_bytes: bytes, dpi: int = 200) -> list:
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        raise ImportError("pdf2image manquant. Ajoutez 'pdf2image>=2.7.0' dans requirements.txt et 'poppler-utils' dans packages.txt.")
    return convert_from_bytes(raw_bytes, dpi=dpi, fmt="JPEG")

def _image_to_base64_jpeg(img, quality: int = 85) -> str:
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def _ocr_page_google_vision(img, api_key: str) -> str:
    import json, urllib.request, urllib.error
    if not api_key: return ""
    img_b64 = _image_to_base64_jpeg(img, quality=85)
    payload = json.dumps({
        "requests": [{
            "image": {"content": img_b64},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
            "imageContext": {"languageHints": ["fr","en","de"],
                             "textDetectionParams": {"enableTextDetectionConfidenceScore": True}}
        }]
    }).encode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        responses = result.get("responses", [])
        if not responses: return ""
        return responses[0].get("fullTextAnnotation", {}).get("text", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try: err_msg = json.loads(body).get("error", {}).get("message", body[:200])
        except: err_msg = body[:200]
        st.warning(f"⚠️ Google Vision API : {err_msg}")
        return ""
    except Exception as e:
        st.warning(f"⚠️ Google Vision API : {e}")
        return ""

def _infer_year_from_text(text: str) -> str:
    m = re.search(r'\d{2}/\d{2}/(\d{4})', text)
    return m.group(1) if m else str(datetime.now().year)

def _complete_short_date(date_str: str, year: str) -> str:
    if re.match(r'\d{1,2}/\d{2}$', date_str.strip()):
        return f"{date_str.strip()}/{year}"
    return date_str.strip()

def parse_qonto_gcv_text(text: str) -> pd.DataFrame:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    year = _infer_year_from_text(text)
    header_idx = None
    for i, line in enumerate(lines):
        ll = line.lower()
        if ("date" in ll or "valeur" in ll) and ("transaction" in ll or "débit" in ll or "debit" in ll or "crédit" in ll):
            header_idx = i; break
    if header_idx is None: return pd.DataFrame()
    date_pat = re.compile(r'^(\d{1,2}/\d{2}(?:/\d{2,4})?)\s+(.*)')
    amount_pat = re.compile(r'([+\-]\s*[\d\s.,]+\s*(?:EUR|CHF|TND|USD|GBP))', re.IGNORECASE)
    skip_kw = {"toutes les cartes","apple pay","google pay","marque de","agréé","du 0",
               "iban:","bic:","solde au","solde de","entrées","sorties","relevé","relevés",
               "sas ","rue ","paris","france","belgium","suisse","tunisie","bruxelles"}
    transactions = []
    current = None
    for line in lines[header_idx + 1:]:
        ll = line.lower()
        if any(kw in ll for kw in skip_kw):
            if current: transactions.append(current); current = None
            continue
        m_date = date_pat.match(line)
        if m_date:
            if current: transactions.append(current)
            date_raw = _complete_short_date(m_date.group(1), year)
            rest = m_date.group(2).strip()
            m_amt = amount_pat.search(rest)
            amount_s = m_amt.group(1).strip() if m_amt else ""
            label = rest[:m_amt.start()].strip() if m_amt else rest
            current = {"date": date_raw, "label": label, "amount_str": amount_s, "memo_parts": []}
        elif current:
            m_amt = amount_pat.search(line)
            if m_amt and not current["amount_str"]: current["amount_str"] = m_amt.group(1).strip()
            elif line and not m_amt: current["memo_parts"].append(line)
    if current: transactions.append(current)
    if not transactions: return pd.DataFrame()
    rows = []
    for t in transactions:
        memo = " | ".join(t["memo_parts"]) if t["memo_parts"] else t["label"]
        rows.append({"date": t["date"], "libellé": t["label"] or (t["memo_parts"][0] if t["memo_parts"] else ""),
                     "montant": t["amount_str"], "memo": memo})
    return pd.DataFrame(rows)

def _is_qonto_format(text: str) -> bool:
    text_lower = text.lower()
    return ("qonto" in text_lower or "qntofrp" in text_lower or
            ("date de valeur" in text_lower and "transactions" in text_lower))

def _parse_ocr_text_to_df(all_text: str) -> pd.DataFrame:
    if not all_text.strip(): return pd.DataFrame()
    if _is_qonto_format(all_text):
        df = parse_qonto_gcv_text(all_text)
        if not df.empty: return df
    lines_data = []
    for line in all_text.split("\n"):
        line = line.strip()
        if not line or len(line) < 4: continue
        parts = re.split(r"  +|\t", line)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            if re.match(r"^\d{1,2}[/-]\d{1,2}", line):
                parts = line.split(" ")
                parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 2: lines_data.append(parts)
    if not lines_data: return pd.DataFrame()
    max_cols = max(len(r) for r in lines_data)
    rows_padded = [r + [""] * (max_cols - len(r)) for r in lines_data]
    date_kw = {"date","jour","day","opération","operation","valeur","libellé","libelle","montant","amount","débit","crédit","datum","betrag"}
    header_idx = 0; best_score = 0
    for i, row in enumerate(rows_padded[:15]):
        row_lower = " ".join(row).lower()
        score = sum(1 for kw in date_kw if kw in row_lower)
        if score > best_score: best_score = score; header_idx = i
    headers = rows_padded[header_idx] if rows_padded else [f"Col{i}" for i in range(max_cols)]
    data = rows_padded[header_idx + 1:] if header_idx < len(rows_padded) - 1 else rows_padded
    seen = {}; unique_h = []
    for h in headers:
        key = h.strip() if h.strip() else "Col"
        cnt = seen.get(key, 0)
        unique_h.append(key if cnt == 0 else f"{key}_{cnt}")
        seen[key] = cnt + 1
    return pd.DataFrame(data, columns=unique_h)

# ═══════════════════════════════════════════════════════════════════════════════
# PDF PARSER v3.2
# ═══════════════════════════════════════════════════════════════════════════════

def parse_pdf(file, gcv_api_key: str = "") -> tuple:
    raw = _read_bytes(file)
    rows = []
    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables({"vertical_strategy":"lines","horizontal_strategy":"lines","intersection_x_tolerance":15}) or []
                if not tables: tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        clean = [str(c).strip() if c is not None else "" for c in row]
                        if any(c for c in clean): rows.append(clean)
    except Exception: pass
    if rows and len(rows) > 1:
        df = _rows_to_df(rows)
        if not df.empty: return df, "pdfplumber-tables"

    for flavor in ["lattice","stream"]:
        try:
            import camelot, tempfile, os
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(raw); tmp_path = tmp.name
            try:
                tables = camelot.read_pdf(tmp_path, flavor=flavor, pages="all")
                if tables and len(tables) > 0:
                    dfs = [t.df for t in tables if not t.df.empty and len(t.df) > 1]
                    if dfs:
                        combined = pd.concat(dfs, ignore_index=True)
                        combined.columns = [str(c).strip() for c in combined.columns]
                        return combined, f"camelot-{flavor}"
            finally:
                try: os.unlink(tmp_path)
                except: pass
        except Exception: pass

    try:
        import tabula, tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(raw); tmp_path = tmp.name
        try:
            dfs = tabula.read_pdf(tmp_path, pages="all", multiple_tables=True, silent=True, pandas_options={"dtype": str})
            dfs = [d for d in dfs if not d.empty]
            if dfs:
                combined = pd.concat(dfs, ignore_index=True)
                combined.columns = [str(c).strip() for c in combined.columns]
                return combined, "tabula"
        finally:
            try: os.unlink(tmp_path)
            except: pass
    except Exception: pass

    text_lines = []; _has_curves = False
    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.split("\n"):
                    line = line.strip()
                    if line: text_lines.append(line)
                if len(page.curves) > 50: _has_curves = True
    except Exception: pass

    all_text = "\n".join(text_lines)
    if len(all_text.strip()) > 100:
        df = _parse_ocr_text_to_df(all_text)
        if not df.empty: return df, "pdfplumber-text"

    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            recovered_lines = []
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1.5, y_tolerance=3, horizontal_ltr=True, use_text_flow=True)
                if words:
                    line_map = {}
                    for w in words:
                        y = round(w['top'], 0)
                        line_map.setdefault(y, []).append(w['text'])
                    for y in sorted(line_map.keys()):
                        recovered_lines.append("  ".join(line_map[y]))
                else:
                    t = page.extract_text(x_tolerance=1, y_tolerance=1)
                    if t: recovered_lines.append(t)
            full_recovered = "\n".join(recovered_lines)
            if len(full_recovered.strip()) > 20:
                df = _parse_ocr_text_to_df(full_recovered)
                if not df.empty: return df, "vector-geometry-engine"
    except Exception: pass

    # GCV via pdf2image
    key = gcv_api_key or _get_gcv_api_key()
    if key:
        try:
            page_images = _rasterize_pdf_bytes_to_images(raw, dpi=200)
        except ImportError as e:
            st.warning(f"⚠️ {e}"); page_images = []
        except Exception as e:
            st.warning(f"⚠️ Rasterisation PDF : {e}"); page_images = []

        if page_images:
            ocr_texts = []
            for img in page_images:
                text = _ocr_page_google_vision(img, key)
                if text.strip(): ocr_texts.append(text)
            if ocr_texts:
                all_ocr = "\n".join(ocr_texts)
                df = _parse_ocr_text_to_df(all_ocr)
                if not df.empty:
                    label = "google-vision-ocr (Qonto)" if _is_qonto_format(all_ocr) else "google-vision-ocr"
                    return df, label

    if HAS_TESSERACT:
        try:
            page_images = _rasterize_pdf_bytes_to_images(raw, dpi=300)
            ocr_texts = []
            for img in page_images:
                text = pytesseract.image_to_string(img, lang="fra+eng")
                if text.strip(): ocr_texts.append(text)
            if ocr_texts:
                all_ocr = "\n".join(ocr_texts)
                df = _parse_ocr_text_to_df(all_ocr)
                if not df.empty: return df, "tesseract-ocr"
        except Exception: pass

    return pd.DataFrame(), "failed"

def _rows_to_df(rows: list) -> pd.DataFrame:
    if not rows: return pd.DataFrame()
    max_cols = max(len(r) for r in rows)
    headers = rows[0]; data = rows[1:] if len(rows) > 1 else rows
    headers = [headers[i] if i < len(headers) else f"Col{i}" for i in range(max_cols)]
    seen = {}; unique_h = []
    for h in headers:
        key = h.strip() if h.strip() else "Col"
        cnt = seen.get(key, 0)
        unique_h.append(key if cnt == 0 else f"{key}_{cnt}")
        seen[key] = cnt + 1
    return pd.DataFrame([r + [""]*(max_cols-len(r)) for r in data], columns=unique_h)

# ═══════════════════════════════════════════════════════════════════════════════
# CSV / EXCEL
# ═══════════════════════════════════════════════════════════════════════════════

def parse_csv(file) -> pd.DataFrame:
    raw = _read_bytes(file)
    content = None
    for enc in ("utf-8-sig","utf-8","latin-1","cp1252","iso-8859-15"):
        try: content = raw.decode(enc); break
        except UnicodeDecodeError: continue
    if content is None: content = raw.decode("utf-8", errors="replace")
    lines = content.splitlines()
    if not lines: return pd.DataFrame()
    sep = _detect_separator(lines[:20])
    header_idx = _find_header_row(lines, sep)
    relevant = "\n".join(lines[header_idx:])
    kwargs = dict(sep=sep, dtype=str, quotechar='"', on_bad_lines="skip", encoding_errors="replace")
    try: df = pd.read_csv(StringIO(relevant), **kwargs)
    except TypeError: df = pd.read_csv(StringIO(relevant), sep=sep, dtype=str, quotechar='"', error_bad_lines=False)
    return _clean_df(df)

def _detect_separator(sample_lines: list) -> str:
    sep_counts = {";":0,",":0,"\t":0,"|":0}
    for line in sample_lines:
        if not line.strip(): continue
        for sep in sep_counts: sep_counts[sep] = max(sep_counts[sep], line.count(sep))
    best_sep = max(sep_counts, key=sep_counts.get)
    return best_sep if sep_counts[best_sep] >= 2 else ","

def _find_header_row(lines: list, sep: str) -> int:
    date_kw = {"date","jour","day","dt","valeur","opération","operation","mouvement","libellé","libelle","montant","amount","datum","betrag"}
    for i, line in enumerate(lines[:25]):
        if not line.strip(): continue
        cols = [c.strip().strip('"').lower() for c in line.split(sep)]
        if len([c for c in cols if c]) < 2: continue
        if sum(1 for c in cols if any(kw in c for kw in date_kw)) >= 1: return i
    return 0

def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all")
    df.columns = [str(c).strip().strip('"') for c in df.columns]
    df = df[df.apply(lambda r: any(str(v).strip() not in ("","nan") for v in r), axis=1)]
    if df.columns.size > 0:
        first_col = df.columns[0]
        df = df[df[first_col].apply(lambda v: str(v).strip() not in ("","nan"))]
    return df.reset_index(drop=True)

def get_excel_sheets(raw_bytes: bytes) -> list:
    try: return pd.ExcelFile(BytesIO(raw_bytes)).sheet_names
    except Exception: return []

def parse_excel(raw_bytes: bytes, sheet_name=None) -> pd.DataFrame:
    for engine in ["openpyxl","xlrd"]:
        try:
            df = pd.read_excel(BytesIO(raw_bytes), sheet_name=sheet_name, dtype=str, engine=engine, header=None)
            if isinstance(df, dict): df = df[list(df.keys())[0]]
            df = _find_excel_header(df)
            df.columns = [str(c).strip() for c in df.columns]
            return _clean_df(df)
        except Exception: continue
    return pd.DataFrame()

def _find_excel_header(df: pd.DataFrame) -> pd.DataFrame:
    date_kw = {"date","jour","day","montant","amount","libellé","libelle","opération","operation","valeur","débit","crédit","credit","debit","datum","betrag"}
    for i, row in df.iterrows():
        row_str = " ".join(str(v).lower() for v in row if not _is_na(v))
        if sum(1 for kw in date_kw if kw in row_str) >= 1:
            new_df = df.iloc[i+1:].copy()
            new_df.columns = [str(df.iloc[i,j]).strip() for j in range(len(df.columns))]
            return new_df.reset_index(drop=True)
        if i > 20: break
    new_df = df.iloc[1:].copy()
    new_df.columns = [str(df.iloc[0,j]).strip() for j in range(len(df.columns))]
    return new_df.reset_index(drop=True)

def auto_map_columns(df: pd.DataFrame) -> dict:
    mapping = {k: None for k in ("date","amount","debit","credit","name","memo")}
    kw_map = {
        "date":   ["date de valeur","date de l'opération","date opération","date operation","date valeur",
                   "dateop","date d'opération","date comptable","date","jour","day","dt","datum","buchungsdatum","valutadatum"],
        "debit":  ["débit","debit","retrait","withdrawal","sortie","montant débit","montant debit","dépense","depense","ausgabe","lastschrift","belastung"],
        "credit": ["crédit","credit","versement","deposit","entrée","entree","montant crédit","montant credit","recette","gutschrift","eingang"],
        "amount": ["montant (eur)","montant (chf)","montant (tnd)","montant (usd)","montant_brut","montant","amount","somme","sum","trnamt",
                   "value","total","net","prix","betrag","saldo"],
        "name":   ["libellé opération","libellé","libelle","label","description","transactions","intitulé","intitule",
                   "référence","reference","nom","name","communication","motif opération","wording","buchungstext","verwendungszweck"],
        "memo":   ["note","catégorie","categorie","memo","motif","objet","détail","detail","commentaire","complément","complement","information","mitteilung"],
    }
    cols_lower = {c.lower().strip(): c for c in df.columns}
    used = set()
    for field, keywords in kw_map.items():
        for kw in keywords:
            for col_l, col_o in cols_lower.items():
                if kw in col_l and col_o not in used:
                    mapping[field] = col_o; used.add(col_o); break
            if mapping[field]: break
    return mapping

def validate_ofx(ofx_str: str) -> list:
    checks = []
    checks.append(("ok" if "OFXHEADER:100" in ofx_str else "err", "En-tête OFXHEADER:100"))
    checks.append(("ok" if "ENCODING:UTF-8" in ofx_str else "err", "Encodage UTF-8"))
    checks.append(("ok" if "<OFX>" in ofx_str else "err", "Balise <OFX> présente"))
    checks.append(("ok" if "<STMTRS>" in ofx_str else "err", "Balise <STMTRS> présente"))
    checks.append(("ok" if "<STMTTRN>" in ofx_str else "err", "Transactions <STMTTRN>"))
    checks.append(("ok" if "<FITID>" in ofx_str else "err", "Identifiants FITID"))
    checks.append(("ok" if "<LEDGERBAL>" in ofx_str else "err", "Solde LEDGERBAL"))
    checks.append(("ok" if "<AVAILBAL>" in ofx_str else "err", "Solde AVAILBAL"))
    checks.append(("ok" if "<MEMO>" in ofx_str else "warn", "Champ MEMO (Odoo 12/13)"))
    fitids = re.findall(r"<FITID>([^<]+)</FITID>", ofx_str)
    if fitids:
        unique_fitids = set(fitids)
        if len(fitids) == len(unique_fitids): checks.append(("ok", f"FITID uniques ({len(fitids)})"))
        else: checks.append(("err", f"FITID dupliqués ({len(fitids)-len(unique_fitids)} doublons)"))
    dates = re.findall(r"<DTPOSTED>(\d+)</DTPOSTED>", ofx_str)
    bad_dates = [d for d in dates if len(d) != 14]
    if dates:
        checks.append(("ok" if not bad_dates else "warn",
                       f"Format dates YYYYMMDDHHMMSS ({len(dates)-len(bad_dates)}/{len(dates)} OK)"))
    return checks

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════════════════
defaults = {
    "ofx_content": None, "ofx_filename": None, "df_parsed": None,
    "parse_method": None, "downloaded": False, "file_deleted": False,
    "raw_pdf_bytes": None, "raw_pdf_name": None, "last_gcv_key": "",
}
for k, v in defaults.items():
    if k not in st.session_state: st.session_state[k] = v

# ═══════════════════════════════════════════════════════════════════════════════
# HERO HEADER
# ═══════════════════════════════════════════════════════════════════════════════
_gcv_ready = bool(_get_gcv_api_key())

st.markdown(f"""
<div class="ofx-hero">
    <div class="hero-dots"></div>
    <div class="hero-content">
        <div class="hero-badge">✦ v4.0 · OFX SGML v1.5.1 · Odoo 12–17</div>
        <div class="hero-title">💳 OFX Converter <span>for</span> Odoo</div>
        <div class="hero-sub">// PDF · CSV · XLSX → OFX · France · Belgique · Suisse · Tunisie</div>
        <div class="hero-stats">
            <div class="hero-stat"><div class="hero-stat-val">45+</div><div class="hero-stat-lbl">Banques</div></div>
            <div class="hero-stat"><div class="hero-stat-val">4</div><div class="hero-stat-lbl">Pays</div></div>
            <div class="hero-stat"><div class="hero-stat-val">{"✓ GCV" if _gcv_ready else "—"}</div><div class="hero-stat-lbl">OCR Vision</div></div>
            <div class="hero-stat"><div class="hero-stat-val">100%</div><div class="hero-stat-lbl">Odoo compat.</div></div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION INLINE (remplace la sidebar)
# ═══════════════════════════════════════════════════════════════════════════════

with st.expander("⚙️ Configuration — Banque · Compte · OCR", expanded=True):
    st.markdown('<div class="section-label">Profil bancaire</div>', unsafe_allow_html=True)

    # Country filter
    st.markdown("""
    <div class="country-strip">
        <div class="ctry-chip"><span>🇫🇷</span> France</div>
        <div class="ctry-chip"><span>🇧🇪</span> Belgique</div>
        <div class="ctry-chip"><span>🇨🇭</span> Suisse</div>
        <div class="ctry-chip"><span>🇹🇳</span> Tunisie</div>
    </div>
    """, unsafe_allow_html=True)

    cfg_col1, cfg_col2, cfg_col3 = st.columns([2, 2, 1])
    with cfg_col1:
        selected_profile = st.selectbox("🏛️ Banque / Établissement", list(BANK_PROFILES.keys()), key="profile_select")
        profile_data = BANK_PROFILES[selected_profile]
    with cfg_col2:
        bank_id = st.text_input("BIC / Bank ID", value=profile_data["bank_id"])
    with cfg_col3:
        # Auto-detect currency from profile
        default_currency = profile_data.get("currency", "EUR")
        all_currencies = ["EUR","CHF","TND","USD","GBP","MAD","DZD","CAD","SGD","JPY"]
        currency = st.selectbox("Devise", all_currencies, index=all_currencies.index(default_currency))

    account_id = st.text_input("IBAN / Account ID", value=profile_data["account_id"])

    cfg_col4, cfg_col5, cfg_col6, cfg_col7 = st.columns(4)
    with cfg_col4:
        account_type = st.selectbox("Type de compte", ["CHECKING","SAVINGS","CREDITLINE"])
    with cfg_col5:
        opening_bal = st.number_input("Solde d'ouverture", value=0.0, step=0.01, format="%.2f",
                                       help="Solde AVANT la 1ère transaction")
    with cfg_col6:
        language = st.selectbox("Langue OFX", ["ENG","FRA"], index=0)
    with cfg_col7:
        ofx_filename_input = st.text_input("Nom du fichier", value="transactions_odoo")

    st.markdown('<div class="section-label" style="margin-top:16px;">OCR — PDF vectoriels (Qonto, Revolut…)</div>', unsafe_allow_html=True)

    _secret_key = ""
    try: _secret_key = st.secrets.get("GCV_API_KEY", "")
    except Exception: pass

    gcv_col1, gcv_col2 = st.columns([2, 3])
    with gcv_col1:
        if _secret_key:
            st.markdown('<div class="key-ok">✓ Clé GCV chargée depuis st.secrets</div>', unsafe_allow_html=True)
            gcv_api_key = _secret_key
        else:
            gcv_api_key = st.text_input("Clé API Google Cloud Vision", value=st.session_state.get("gcv_api_key",""),
                                         type="password", placeholder="AIzaSy…",
                                         help="Nécessaire pour les PDF vectoriels (Qonto, Revolut, N26…)")
            st.session_state["gcv_api_key"] = gcv_api_key
    with gcv_col2:
        st.markdown(f"""
        <div style="background:rgba(59,91,219,0.05);border:1px solid var(--border);border-radius:10px;
                    padding:10px 14px;font-family:'DM Mono',monospace;font-size:0.68rem;color:var(--muted2);line-height:2.0;">
            <b style="color:var(--text2);">PDF vectoriels supportés :</b> Qonto · Revolut · N26 · Orange Bank<br>
            Sans clé → PDF texte traités normalement · CSV Qonto recommandé<br>
            <a href="https://console.cloud.google.com" target="_blank" style="color:var(--accent);">console.cloud.google.com</a>
            → Activer <i>Cloud Vision API</i> → Identifiants → Créer clé API
        </div>
        """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# LAYOUT PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════
col_main, col_right = st.columns([3, 2], gap="large")

with col_main:

    # ── Étape 1 : Upload ─────────────────────────────────────────────────────
    st.markdown("""
    <div class="step-card">
        <div class="step-header">
            <div class="step-num">1</div>
            <div>
                <div class="step-title">Importer votre relevé bancaire</div>
                <div class="step-desc">PDF · CSV · XLSX · XLS — jusqu'à 25 MB</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    uploaded_file = st.file_uploader("Glissez-déposez ou cliquez",
                                      type=["pdf","csv","xlsx","xls"], label_visibility="collapsed")

    if uploaded_file:
        size_mb = uploaded_file.size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            st.error(f"❌ Fichier trop volumineux ({size_mb:.1f} MB). Maximum : {MAX_FILE_MB} MB.")
            st.stop()

        ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
        size_kb = uploaded_file.size / 1024
        ext_icons = {"pdf":"📄","csv":"📊","xlsx":"📗","xls":"📗"}

        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:10px;margin:10px 0 16px;flex-wrap:wrap;">
            <span class="pill pill-ok">✓ Fichier chargé</span>
            <span class="pill pill-info">{ext_icons.get(ext,'📁')} {ext.upper()}</span>
            <span style="font-size:0.72rem;color:var(--text2);font-family:'Sora',sans-serif;">{uploaded_file.name}</span>
            <span style="font-size:0.70rem;color:var(--muted);font-family:'DM Mono',monospace;">{size_kb:.1f} KB</span>
        </div>
        """, unsafe_allow_html=True)

        selected_sheet = None
        if ext in ("xlsx","xls"):
            raw_for_sheets = _read_bytes(uploaded_file)
            sheets = get_excel_sheets(raw_for_sheets)
            if len(sheets) > 1:
                selected_sheet = st.selectbox(f"📋 Feuille Excel ({len(sheets)} feuilles) :", sheets)
            elif sheets: selected_sheet = sheets[0]

        # Auto-retry si clé GCV vient d'être saisie
        _gcv_key_changed = (
            gcv_api_key and
            gcv_api_key != st.session_state.get("last_gcv_key", "") and
            st.session_state.get("parse_method") == "failed" and
            st.session_state.get("raw_pdf_bytes") is not None
        )
        if _gcv_key_changed:
            st.info("🔄 Clé GCV détectée — relance automatique…")
            _df_r, _meth_r = parse_pdf(BytesIO(st.session_state["raw_pdf_bytes"]), gcv_api_key=gcv_api_key)
            st.session_state.df_parsed = _df_r; st.session_state.parse_method = _meth_r
            st.session_state["last_gcv_key"] = gcv_api_key
            if not _df_r.empty:
                st.success(f"✅ OCR réussi ({_meth_r})")
                st.rerun()

        progress_bar = st.progress(0, text="Initialisation…")
        with st.spinner(""):
            try:
                progress_bar.progress(15, text="Lecture du fichier…")
                if ext == "pdf":
                    _raw_pdf = _read_bytes(uploaded_file)
                    st.session_state["raw_pdf_bytes"] = _raw_pdf
                    st.session_state["raw_pdf_name"] = uploaded_file.name
                    progress_bar.progress(30, text="Analyse PDF…")
                    df_raw, method = parse_pdf(BytesIO(_raw_pdf), gcv_api_key=gcv_api_key)
                    st.session_state["last_gcv_key"] = gcv_api_key
                    if "google-vision-ocr" in (method or ""):
                        progress_bar.progress(80, text="Google Cloud Vision OCR terminé…")
                    elif method == "failed":
                        progress_bar.progress(80, text="⚠ Vérifiez votre clé GCV dans la configuration")
                    else:
                        progress_bar.progress(80, text=f"Méthode : {method}…")
                elif ext == "csv":
                    progress_bar.progress(50, text="Parsing CSV…")
                    df_raw = parse_csv(uploaded_file); method = "csv-parser"
                    progress_bar.progress(80, text="Nettoyage…")
                else:
                    progress_bar.progress(40, text="Lecture Excel…")
                    raw_xl = _read_bytes(uploaded_file)
                    df_raw = parse_excel(raw_xl, sheet_name=selected_sheet); method = "excel-parser"
                    progress_bar.progress(80, text="Nettoyage…")
                st.session_state.df_parsed = df_raw; st.session_state.parse_method = method
                progress_bar.progress(100, text="✓ Terminé")
            except Exception as e:
                progress_bar.progress(100, text="❌ Erreur")
                st.error(f"❌ Erreur lors de la lecture : {e}")
                with st.expander("🔍 Détail"): st.code(traceback.format_exc())
                st.session_state.df_parsed = pd.DataFrame()

        # Résultats
        if st.session_state.df_parsed is not None and not st.session_state.df_parsed.empty:
            df_raw = st.session_state.df_parsed
            n_rows = len(df_raw); n_cols = len(df_raw.columns)
            method = st.session_state.parse_method or "—"
            method_colors = {
                "pdfplumber-tables":"pill-ok","camelot-lattice":"pill-ok","camelot-stream":"pill-teal",
                "tabula":"pill-teal","pdfplumber-text":"pill-warn","vector-geometry-engine":"pill-teal",
                "google-vision-ocr":"pill-teal","google-vision-ocr (Qonto)":"pill-teal",
                "csv-parser":"pill-ok","excel-parser":"pill-ok","failed":"pill-danger","manual-entry":"pill-info",
            }
            m_cls = method_colors.get(method, "pill-info")
            st.markdown(f"""
            <div style="display:flex;align-items:center;gap:10px;margin:8px 0 16px;flex-wrap:wrap;">
                <span class="pill {m_cls}">⚡ {method}</span>
                <span class="pill pill-info">{n_rows:,} lignes</span>
                <span class="pill pill-info">{n_cols} colonnes</span>
            </div>
            """, unsafe_allow_html=True)
            if n_rows > MAX_ROWS_WARNING:
                st.warning(f"⚠️ {n_rows:,} lignes — import Odoo peut être lent au-delà de 5 000 transactions.")
            if "google-vision-ocr" in method:
                st.info("🔍 PDF traité via Google Cloud Vision (pdf2image + GCV). Vérifiez le mapping ci-dessous.")
            with st.expander("👁️ Aperçu données brutes (15 lignes)", expanded=False):
                st.dataframe(df_raw.head(15), use_container_width=True, height=260)

        elif st.session_state.df_parsed is not None and st.session_state.df_parsed.empty:
            method_failed = st.session_state.get("parse_method") == "failed"
            is_pdf = uploaded_file is not None and uploaded_file.name.upper().endswith(".PDF")
            if method_failed and is_pdf:
                st.markdown("""
                <div style="background:rgba(201,42,42,0.06);border:1px solid rgba(201,42,42,0.2);border-radius:14px;padding:20px;margin-bottom:16px;">
                    <b style="color:#c92a2a;">❌ Extraction impossible — PDF Vectoriel</b><br>
                    <span style="font-size:0.85em;color:var(--text2);">Ce PDF dessine les lettres en vecteurs (Qonto, Revolut, N26…).
                    Renseignez votre clé Google Cloud Vision dans la <b>Configuration</b> ci-dessus, puis cliquez sur Relancer l'OCR.</span>
                </div>
                """, unsafe_allow_html=True)
                tab_gcv, tab_csv, tab_manual = st.tabs(["🔑 Google Vision","📊 Export CSV","✏️ Saisie manuelle"])
                with tab_gcv:
                    _cur_key = gcv_api_key or st.session_state.get("gcv_api_key","")
                    if _cur_key and st.session_state.get("raw_pdf_bytes"):
                        st.markdown('<div class="key-ok" style="margin-bottom:12px;">✓ Clé GCV disponible — prêt pour l\'OCR</div>', unsafe_allow_html=True)
                        if st.button("🔄 Relancer l'OCR avec Google Cloud Vision", use_container_width=True, key="retry_gcv_btn"):
                            with st.spinner("🔍 pdf2image + Google Cloud Vision en cours…"):
                                _df_r, _meth_r = parse_pdf(BytesIO(st.session_state["raw_pdf_bytes"]), gcv_api_key=_cur_key)
                            st.session_state.df_parsed = _df_r; st.session_state.parse_method = _meth_r
                            st.session_state["last_gcv_key"] = _cur_key
                            if not _df_r.empty:
                                st.success(f"✅ OCR réussi — {len(_df_r)} lignes extraites")
                                st.rerun()
                            else:
                                st.error("❌ OCR sans résultat. Vérifiez la clé GCV.")
                    else:
                        st.info("Renseignez la clé Google Cloud Vision dans la **Configuration** en haut de page.")
                with tab_csv:
                    st.markdown("""
                    <div style="font-family:'DM Mono',monospace;font-size:0.72rem;color:var(--text2);line-height:2.0;">
                        <b>Qonto :</b> app.qonto.com → Compte → Relevés → Exporter CSV<br>
                        <b>Revolut :</b> App Revolut → Compte → Relevé → Export CSV<br>
                        <b>N26 :</b> app.n26.com → Transactions → Exporter CSV<br>
                        <span style="color:var(--success);">✓ Format CSV pris en charge nativement</span>
                    </div>
                    """, unsafe_allow_html=True)
                with tab_manual:
                    manual_text = st.text_area("Transactions (DD/MM/YYYY ; Libellé ; Montant)",
                        placeholder="01/06/2025 ; Qonto - Abonnement ; -70.80\n17/06/2025 ; URSSAF ; -339.00",
                        height=180, key="manual_txn_input")
                    if st.button("📥 Charger", use_container_width=True):
                        rows = []
                        for line in manual_text.strip().split("\n"):
                            line = line.strip()
                            if not line: continue
                            parts = re.split(r"[;,\t]", line, maxsplit=2)
                            parts = [p.strip() for p in parts]
                            if len(parts) >= 3: rows.append({"date":parts[0],"libellé":parts[1],"montant":parts[2],"memo":parts[1]})
                            elif len(parts) == 2: rows.append({"date":parts[0],"libellé":parts[1],"montant":"0","memo":parts[1]})
                        if rows:
                            st.session_state.df_parsed = pd.DataFrame(rows); st.session_state.parse_method = "manual-entry"
                            st.success(f"✅ {len(rows)} transaction(s) chargée(s).")
                            st.rerun()
                        else: st.warning("⚠️ Format : Date ; Libellé ; Montant")
            else:
                st.error("❌ Aucune donnée extraite. Vérifiez le format du fichier.")

    # ── Étape 2 : Mapping ────────────────────────────────────────────────────
    if st.session_state.df_parsed is not None and not st.session_state.df_parsed.empty:
        df = st.session_state.df_parsed
        mapping = auto_map_columns(df)
        cols = list(df.columns)
        none_list = [NONE_OPT]

        st.markdown("""
        <div class="step-card" style="margin-top:20px;">
            <div class="step-header">
                <div class="step-num">2</div>
                <div>
                    <div class="step-title">Mapper les colonnes</div>
                    <div class="step-desc">Association automatique détectée — vérifiez et ajustez si nécessaire</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        auto_mapped = [f for f, v in mapping.items() if v is not None]
        if auto_mapped:
            st.markdown(f"""
            <div style="margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <span class="pill pill-teal">🎯 Auto-mapping</span>
                <span style="font-size:0.72rem;color:var(--text2);font-family:'Sora',sans-serif;">
                    {len(auto_mapped)} colonne(s) détectée(s) : {', '.join(auto_mapped)}
                </span>
            </div>
            """, unsafe_allow_html=True)

        def _sel(label, key, options, default):
            idx = options.index(default) if default in options else 0
            return st.selectbox(label, options, index=idx, key=key)

        c1, c2 = st.columns(2)
        with c1:
            col_date = _sel("📅 DATE *", "col_date", none_list+cols, mapping["date"] if mapping["date"] else NONE_OPT)
            col_name = _sel("🏷️ LIBELLÉ", "col_name", none_list+cols, mapping["name"] if mapping["name"] else NONE_OPT)
        with c2:
            col_amount = _sel("💶 MONTANT *", "col_amount", none_list+cols, mapping["amount"] if mapping["amount"] else NONE_OPT)
            col_memo = _sel("📝 MEMO (optionnel)", "col_memo", none_list+cols, mapping["memo"] if mapping["memo"] else NONE_OPT)

        use_split = st.checkbox("🔀 Colonnes DÉBIT / CRÉDIT séparées", value=False)
        col_debit = NONE_OPT; col_credit = NONE_OPT
        if use_split:
            cs1, cs2 = st.columns(2)
            with cs1: col_debit = _sel("➖ DÉBIT","col_debit",none_list+cols, mapping["debit"] if mapping["debit"] else NONE_OPT)
            with cs2: col_credit = _sel("➕ CRÉDIT","col_credit",none_list+cols, mapping["credit"] if mapping["credit"] else NONE_OPT)
            if col_debit != NONE_OPT and col_credit != NONE_OPT and col_debit == col_credit:
                st.warning("⚠️ DÉBIT et CRÉDIT doivent être des colonnes différentes.")

        if col_date != NONE_OPT:
            with st.expander("🔬 Prévisualisation parsing (5 premières lignes)", expanded=False):
                preview_cols = {"date": col_date}
                if not use_split and col_amount != NONE_OPT: preview_cols["montant"] = col_amount
                if use_split:
                    if col_debit != NONE_OPT: preview_cols["débit"] = col_debit
                    if col_credit != NONE_OPT: preview_cols["crédit"] = col_credit
                if col_name != NONE_OPT: preview_cols["libellé"] = col_name
                if col_memo != NONE_OPT: preview_cols["memo"] = col_memo
                preview_df = pd.DataFrame()
                for label, col in preview_cols.items():
                    if col in df.columns: preview_df[label] = df[col].head(5)
                if not preview_df.empty:
                    if "montant" in preview_df.columns:
                        preview_df["montant_parsé"] = preview_df["montant"].apply(amount_to_float)
                    if "date" in preview_df.columns:
                        preview_df["date_OFX"] = preview_df["date"].apply(format_ofx_date)
                    st.dataframe(preview_df, use_container_width=True)

        # ── Étape 3 : Génération ──────────────────────────────────────────────
        st.markdown("""
        <div class="step-card" style="margin-top:20px;">
            <div class="step-header">
                <div class="step-num">3</div>
                <div>
                    <div class="step-title">Générer le fichier OFX</div>
                    <div class="step-desc">Conversion vers le format OFX SGML v1.5.1 compatible Odoo</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        split_ok = (use_split and col_debit != NONE_OPT and col_credit != NONE_OPT and col_debit != col_credit)
        amount_ok = (not use_split and col_amount != NONE_OPT) or split_ok
        ready = col_date != NONE_OPT and amount_ok

        if not ready:
            st.markdown('<span class="pill pill-warn">⚠️ Mappez au minimum la DATE et le MONTANT</span>', unsafe_allow_html=True)

        if st.button("⚡ Générer le fichier OFX", disabled=not ready, type="primary", use_container_width=True):
            with st.spinner("🔧 Génération OFX en cours…"):
                try:
                    txn_df = pd.DataFrame()
                    txn_df["date"] = df[col_date].apply(lambda x: " ".join(str(x).split()).strip() if not _is_na(x) else "")
                    if split_ok:
                        txn_df["amount"] = (
                            df[col_debit].apply(lambda x: -abs(amount_to_float(x)) if amount_to_float(x) != 0 else 0)
                            + df[col_credit].apply(lambda x: abs(amount_to_float(x))))
                    else:
                        txn_df["amount"] = df[col_amount].apply(amount_to_float)
                    txn_df["name"] = df[col_name].fillna("TRANSACTION").astype(str) if col_name != NONE_OPT else "TRANSACTION"
                    txn_df["memo"] = df[col_memo].fillna("").astype(str) if col_memo != NONE_OPT else ""
                    mask_date = txn_df["date"].notna() & (txn_df["date"].astype(str).str.strip() != "")
                    mask_amount = txn_df["amount"] != 0.0
                    txn_df = txn_df[mask_date & mask_amount].reset_index(drop=True)
                    if txn_df.empty:
                        st.error("❌ Aucune transaction valide (dates vides ou montants nuls).")
                    else:
                        ofx_str = build_ofx(txn_df, bank_id=bank_id, account_id=account_id,
                                            account_type=account_type, currency=currency,
                                            language=language, opening_balance=opening_bal)
                        fname = f"{ofx_filename_input.strip() or 'transactions_odoo'}.ofx"
                        st.session_state.ofx_content = ofx_str; st.session_state.ofx_filename = fname
                        st.session_state.downloaded = False; st.session_state.file_deleted = False
                        st.success(f"✅ {len(txn_df):,} transactions converties avec succès !")
                except Exception as e:
                    st.error(f"❌ Erreur : {e}")
                    with st.expander("🔍 Détail"): st.code(traceback.format_exc())

# ═══════════════════════════════════════════════════════════════════════════════
# COLONNE DROITE
# ═══════════════════════════════════════════════════════════════════════════════
with col_right:
    if st.session_state.ofx_content and not st.session_state.file_deleted:
        ofx = st.session_state.ofx_content
        txn_count = ofx.count("<STMTTRN>")
        debit_count = ofx.count("<TRNTYPE>DEBIT</TRNTYPE>")
        credit_count = ofx.count("<TRNTYPE>CREDIT</TRNTYPE>")
        bal_match = re.search(r"<BALAMT>([\-\d.]+)</BALAMT>", ofx)
        bal_str = bal_match.group(1) if bal_match else "—"

        st.markdown(f"""
        <div class="metrics-grid">
            <div class="metric-box"><div class="metric-label">Transactions</div><div class="metric-value blue">{txn_count:,}</div></div>
            <div class="metric-box"><div class="metric-label">Débits</div><div class="metric-value red">{debit_count:,}</div></div>
            <div class="metric-box"><div class="metric-label">Crédits</div><div class="metric-value green">{credit_count:,}</div></div>
            <div class="metric-box"><div class="metric-label">Solde clôture</div><div class="metric-value teal">{bal_str}</div></div>
        </div>
        """, unsafe_allow_html=True)

        tab_prev, tab_valid = st.tabs(["📄 Aperçu OFX","✅ Validation"])
        with tab_prev:
            lines_ofx = ofx.split("\n")
            preview_html = ""
            for line in lines_ofx[:80]:
                esc = line.replace("&","&amp;").replace("<","§L§").replace(">","§G§")
                esc = esc.replace("§L§","<span class='ofx-tag'>&lt;").replace("§G§","&gt;</span>")
                preview_html += f"<div>{esc}</div>"
            if len(lines_ofx) > 80:
                preview_html += f"<div style='color:#5c6490;margin-top:8px;'>… +{len(lines_ofx)-80} lignes</div>"
            st.markdown(f'<div class="ofx-preview">{preview_html}</div>', unsafe_allow_html=True)

        with tab_valid:
            checks = validate_ofx(ofx)
            icons = {"ok":"✅","warn":"⚠️","err":"❌"}
            cls = {"ok":"v-ok","warn":"v-warn","err":"v-err"}
            rows_html = "".join(
                f'<div class="validation-row"><span class="v-icon {cls[l]}">{icons[l]}</span><span>{m}</span></div>'
                for l, m in checks)
            all_ok = all(l=="ok" for l,_ in checks); has_err = any(l=="err" for l,_ in checks)
            summary_cls = "pill-ok" if all_ok else ("pill-danger" if has_err else "pill-warn")
            summary_txt = "OFX valide" if all_ok else ("Erreurs détectées" if has_err else "Avertissements")
            st.markdown(f"""
            <div style="margin-bottom:12px;"><span class="pill {summary_cls}">{summary_txt}</span></div>
            <div style="background:var(--card2);border:1px solid var(--border);border-radius:12px;padding:14px 16px;">{rows_html}</div>
            """, unsafe_allow_html=True)

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        st.download_button(
            label=f"⬇️  Télécharger {st.session_state.ofx_filename}",
            data=st.session_state.ofx_content.encode("utf-8"),
            file_name=st.session_state.ofx_filename,
            mime="application/x-ofx",
            use_container_width=True,
            on_click=lambda: setattr(st.session_state, 'downloaded', True),
        )
        ofx_size = len(st.session_state.ofx_content.encode("utf-8"))
        st.markdown(f"""
        <div style="text-align:center;margin-top:8px;font-family:'DM Mono',monospace;font-size:0.65rem;color:var(--muted);">
            {ofx_size/1024:.1f} KB · UTF-8 · OFX SGML v1.5.1
        </div>
        """, unsafe_allow_html=True)

        if st.session_state.downloaded:
            st.markdown("""
            <div style="background:rgba(230,119,0,0.08);border:1px solid rgba(230,119,0,0.2);
                        border-radius:12px;padding:14px 16px;margin-top:16px;">
                <div style="font-family:'Sora',sans-serif;font-weight:700;font-size:0.80rem;color:#e67700;margin-bottom:4px;">⚠️ Fichier téléchargé</div>
                <div style="font-family:'DM Mono',monospace;font-size:0.68rem;color:var(--muted);">
                    Supprimez les données de session pour effacer le fichier de la mémoire.
                </div>
            </div>
            """, unsafe_allow_html=True)
            if st.button("🗑️ Effacer la session", use_container_width=True):
                for k in ("ofx_content","ofx_filename","df_parsed","parse_method","raw_pdf_bytes","raw_pdf_name","last_gcv_key"):
                    st.session_state[k] = None
                st.session_state.downloaded = False; st.session_state.file_deleted = True
                st.rerun()

    elif st.session_state.file_deleted:
        st.markdown("""
        <div style="background:rgba(9,146,104,0.06);border:1px solid rgba(9,146,104,0.2);
                    border-radius:16px;padding:40px;text-align:center;margin-top:20px;">
            <div style="font-size:3rem;margin-bottom:12px;">🗑️</div>
            <div style="font-family:'Sora',sans-serif;font-weight:700;color:var(--success);font-size:1rem;margin-bottom:6px;">Session effacée</div>
            <div style="font-family:'DM Mono',monospace;font-size:0.68rem;color:var(--muted);">Le fichier OFX a été supprimé de la mémoire.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="no-data-panel" style="margin-top:10px;">
            <div class="no-data-icon">📂</div>
            <div class="no-data-title">En attente de génération</div>
            <div class="no-data-sub">
                Importez un relevé PDF, CSV ou Excel<br>
                mappez les colonnes, puis cliquez sur<br><br>
                <span style="color:var(--accent);font-weight:600;">⚡ Générer le fichier OFX</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

st.markdown("<br><hr>", unsafe_allow_html=True)
st.markdown("""
<div class="ofx-footer">
    OFX Converter for Odoo v4.0 &nbsp;·&nbsp;
    <span style="color:var(--accent);">Développé par Achraf BEN YOUNES</span>
    &nbsp;·&nbsp; 🇫🇷 France · 🇧🇪 Belgique · 🇨🇭 Suisse · 🇹🇳 Tunisie
    &nbsp;·&nbsp; pdf2image · Google Vision OCR
</div>
""", unsafe_allow_html=True)