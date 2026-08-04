import os
import re
import json
import time
import zipfile
import tempfile
import pathlib
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from collections import defaultdict
from difflib import SequenceMatcher

import tkinter as tk
from tkinter import simpledialog, filedialog, messagebox

# =========================
# OPTIONAL OCR DEPENDENCIES
# =========================
# pip install pytesseract pillow pdfplumber pymupdf rapidfuzz sentence-transformers
# Windows: Install Tesseract OCR (UB-Mannheim build recommended)
OCR_ENABLED = True

try:
    import pytesseract
    from PIL import Image
except Exception:
    OCR_ENABLED = False

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import fitz  # PyMuPDF - used to OCR scanned/image PDFs
except Exception:
    fitz = None

try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None

try:
    import spacy
except Exception:
    spacy = None

_SPACY_NLP = None

try:
    from sentence_transformers import SentenceTransformer, util as st_util
except Exception:
    SentenceTransformer = None
    st_util = None

_ST_MODEL = None
_ST_MODEL_FAILED = False


# =========================
# TESSERACT PATH (Windows)
# =========================
DEFAULT_TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD = (os.environ.get("TESSERACT_CMD", "").strip() or DEFAULT_TESSERACT_EXE)

if OCR_ENABLED:
    if os.path.isfile(TESSERACT_CMD):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    else:
        OCR_ENABLED = False
        print(f"[WARN] Tesseract not found at: {TESSERACT_CMD}")
        print("[WARN] OCR disabled. Install Tesseract or set env var TESSERACT_CMD to the full path of tesseract.exe")


# =========================
# ENV
# =========================
ENV_PATH = pathlib.Path(__file__).with_name("QBO.env")
load_dotenv(dotenv_path=ENV_PATH, override=False)

missing = [
    k for k in ["QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REALM_ID", "QBO_REFRESH_TOKEN"]
    if not os.environ.get(k)
]
if missing:
    raise RuntimeError(f"Missing env vars: {missing}. Check QBO.env location: {ENV_PATH}")

CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "").strip()
REALM_ID = os.environ.get("QBO_REALM_ID", "").strip()
INITIAL_REFRESH_TOKEN = os.environ.get("QBO_REFRESH_TOKEN", "").strip()

ENV = os.environ.get("QBO_ENV", "production").strip().lower()
TOKEN_STORE_PATH = os.path.join(pathlib.Path.home(), ".qbo_token_store.json")

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
MINORVERSION = os.environ.get("QBO_MINORVERSION", "75").strip()
API_BASE = "https://sandbox-quickbooks.api.intuit.com" if ENV == "sandbox" else "https://quickbooks.api.intuit.com"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_match(success: bool, message: str):
    """
    Console match logging:
    - Green check for matched attachments
    - Red X for unmatched attachments
    """
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    symbol = "✔" if success else "✘"
    color = GREEN if success else RED

    print(f"{color}{symbol} {message}{RESET}", flush=True)


# =========================
# TOKEN STORE HELPERS
# =========================
def load_token_store() -> dict:
    if os.path.exists(TOKEN_STORE_PATH):
        with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_token_store(store: dict) -> None:
    with open(TOKEN_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def get_refresh_token() -> str:
    store = load_token_store()
    rt = (store.get("refresh_token") or "").strip()
    if rt:
        return rt
    if INITIAL_REFRESH_TOKEN:
        return INITIAL_REFRESH_TOKEN
    raise RuntimeError("No refresh token found. Set QBO_REFRESH_TOKEN in QBO.env or token store.")


def set_refresh_token(new_refresh_token: str) -> None:
    store = load_token_store()
    store["refresh_token"] = new_refresh_token.strip()
    store["refresh_token_saved_at"] = int(time.time())
    save_token_store(store)


# =========================
# AUTH
# =========================
def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Missing CLIENT_ID/CLIENT_SECRET. Set env vars QBO_CLIENT_ID and QBO_CLIENT_SECRET.")

    refresh_token = get_refresh_token().strip()

    # Guard: refresh token should NOT look like a JWT (eyJ...)
    if refresh_token.startswith("eyJ"):
        raise RuntimeError(
            "Your refresh token looks like an access token (starts with 'eyJ'). "
            "You must store Intuit's refresh_token, not access_token."
        )

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    log("Refreshing QBO access token...")
    r = requests.post(
        TOKEN_URL,
        data=urlencode(data),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=60,
    )
    log("Token refresh HTTP response received")
    if r.status_code != 200:
        raise RuntimeError(f"Failed to refresh token (HTTP {r.status_code}): {r.text}")

    payload = r.json()
    access_token = payload.get("access_token")
    new_refresh = payload.get("refresh_token")

    if new_refresh:
        set_refresh_token(new_refresh)

    if not access_token:
        raise RuntimeError(f"No access_token in response: {payload}")

    return access_token


# =========================
# QBO API HELPERS
# =========================
def qbo_query(access_token: str, sql: str) -> dict:
    url = f"{API_BASE}/v3/company/{REALM_ID}/query"
    params = {"query": sql, "minorversion": MINORVERSION}
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    log(f"Running QBO query: {sql}")
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"QBO query failed (HTTP {r.status_code}): {r.text}")
    return r.json()


def get_invoice_by_id(access_token: str, invoice_id: str) -> dict:
    inv_id = str(invoice_id).strip().replace("'", "\\'")
    sql = f"select * from Invoice where Id = '{inv_id}'"
    data = qbo_query(access_token, sql)
    invoices = (data.get("QueryResponse", {}) or {}).get("Invoice", []) or []
    if not invoices:
        raise RuntimeError(f"No invoice found with Id = {invoice_id}")
    return invoices[0]


def find_invoice_id_by_doc_number(access_token: str, doc_number: str) -> str:
    doc_escaped = doc_number.replace("'", "\\'")
    sql = f"select Id, DocNumber from Invoice where DocNumber = '{doc_escaped}'"
    data = qbo_query(access_token, sql)
    invoices = (data.get("QueryResponse", {}) or {}).get("Invoice", []) or []
    if not invoices:
        raise RuntimeError(f"No invoice found with DocNumber = {doc_number}")
    return invoices[0]["Id"]


def list_attachables_for_invoice(access_token: str, invoice_id: str) -> list[dict]:
    sql = (
        "select Id, FileName, ContentType from Attachable "
        "where AttachableRef.EntityRef.Type = 'Invoice' "
        f"and AttachableRef.EntityRef.value = '{invoice_id}'"
    )
    data = qbo_query(access_token, sql)
    return (data.get("QueryResponse", {}) or {}).get("Attachable", []) or []


def get_temp_download_url(access_token: str, attachable_id: str) -> str:
    url = f"{API_BASE}/v3/company/{REALM_ID}/download/{attachable_id}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "text/plain"}
    log(f"Requesting temp download URL for attachable {attachable_id}")
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Download link request failed for {attachable_id} (HTTP {r.status_code}): {r.text}")

    temp_url = r.text.strip()
    if not temp_url.startswith("http"):
        raise RuntimeError(f"Expected temp URL for {attachable_id}, got: {r.text}")
    return temp_url


# =========================
# FILE NAME + ZIP HELPERS
# =========================
def sanitize_filename(name: str) -> str:
    name = (name or "").strip() or "attachment"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = name.rstrip(". ").strip()
    return name or "attachment"


def unique_path(dir_path: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dir_path, filename)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dir_path, f"{base} ({i}){ext}")
        i += 1
    return candidate


def download_file(url: str, out_path: str) -> None:
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)


def is_image_file(path_or_name: str) -> bool:
    return (os.path.splitext(path_or_name)[1] or "").lower() in {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"
    }


def convert_image_to_pdf(image_path: str) -> str:
    """
    Converts an image attachment to a PDF and returns the new PDF path.
    Used at the final ZIP step so OCR can still read the original image first.
    """
    if "Image" not in globals():
        raise RuntimeError("Pillow/PIL is not available, so image-to-PDF conversion cannot run.")

    pdf_path = os.path.splitext(image_path)[0] + ".pdf"

    img = Image.open(image_path)

    # Multi-page TIFF support where possible
    frames = []
    try:
        n_frames = getattr(img, "n_frames", 1)
        for frame_index in range(n_frames):
            img.seek(frame_index)
            frame = img.copy()
            if frame.mode in ("RGBA", "P", "LA"):
                frame = frame.convert("RGB")
            elif frame.mode != "RGB":
                frame = frame.convert("RGB")
            frames.append(frame)
    except Exception:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        frames = [img]

    if len(frames) > 1:
        frames[0].save(pdf_path, "PDF", resolution=100.0, save_all=True, append_images=frames[1:])
    else:
        frames[0].save(pdf_path, "PDF", resolution=100.0)

    return pdf_path


# =========================
# MATCHING HELPERS
# =========================
def money_str(x) -> str:
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "0.00"


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def parse_amount_from_filename(filename: str):
    base = os.path.basename(filename).replace(",", "")

    m = re.search(r"(\d+\.\d{2})", base)
    if m:
        return float(m.group(1))

    m = re.search(r"(?<!\d)\.(\d{2})(?!\d)", base)
    if m:
        return float("0." + m.group(1))

    return None


def vendor_hint_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    tokens = norm(stem).split()
    tokens = [t for t in tokens if not re.fullmatch(r"\d+(\.\d+)?", t)]
    return tokens[0] if tokens else ""


def get_invoice_lines_for_naming(invoice: dict) -> list[dict]:
    lines = invoice.get("Line", []) or []
    out = []
    for ln in lines:
        detail_type = (ln.get("DetailType") or "").strip()
        if detail_type in {"SubTotalLineDetail", "DiscountLineDetail"}:
            continue

        amt = ln.get("Amount", None)
        if amt is None:
            continue

        desc = (ln.get("Description") or "").strip()

        if not desc:
            desc = f"Line_{ln.get('Id') or 'Unknown'}"

        out.append({
            "line_num": ln.get("LineNum") or ln.get("Id") or "0",   # <-- ADD THIS
            "desc": desc,
            "amount": float(amt)
        })

    return out


# =========================
# OCR HELPERS
# =========================
CURRENCY_RE = re.compile(
    r"(?ix)(?P<neg>-|\(|refund|credit)?\s*(?:\bUSD\b\s*)?(?:\$\s*)?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?)\)?"
)


def extract_text_from_image(path: str) -> str:
    if not OCR_ENABLED:
        return ""
    try:
        img = Image.open(path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Attempt auto-rotate
        try:
            osd = pytesseract.image_to_osd(img)
            rot_match = re.search(r"Rotate:\s+(\d+)", osd)
            if rot_match:
                rot = int(rot_match.group(1))
                if rot in (90, 180, 270):
                    img = img.rotate(360 - rot, expand=True)
        except Exception:
            pass

        gray = img.convert("L")
        w, h = gray.size
        if max(w, h) < 1600:
            gray = gray.resize((w * 2, h * 2), Image.Resampling.LANCZOS)

        gray = gray.point(lambda x: 0 if x < 160 else 255, "1")

        config1 = r"--oem 3 --psm 6"
        config2 = r"--oem 3 --psm 4"

        t1 = pytesseract.image_to_string(gray, config=config1) or ""
        if len(t1.strip()) >= 20:
            return t1
        return pytesseract.image_to_string(gray, config=config2) or ""
    except Exception:
        return ""


def extract_text_from_pdf(path: str) -> str:
    """
    Extracts text from PDFs.
    1) First tries embedded/selectable text via pdfplumber.
    2) If blank, renders each PDF page as an image with PyMuPDF and OCRs it.
    This is the main improvement for scanned/image-only receipt PDFs.
    """
    text = ""

    # 1) Try normal embedded PDF text first
    if pdfplumber is not None:
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        except Exception:
            text = ""

    if text.strip():
        return text

    # 2) OCR scanned/image PDFs
    if not OCR_ENABLED or fitz is None:
        return ""

    try:
        doc = fitz.open(path)
        ocr_text = ""

        for page in doc:
            # 2x scale improves OCR accuracy without being too slow
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            gray = img.convert("L")
            w, h = gray.size
            if max(w, h) < 1600:
                gray = gray.resize((w * 2, h * 2), Image.Resampling.LANCZOS)

            # Basic thresholding for cleaner receipt OCR
            gray = gray.point(lambda x: 0 if x < 170 else 255, "1")

            # Try psm 6 first; fall back to psm 4 if text is too short
            t1 = pytesseract.image_to_string(gray, config="--oem 3 --psm 6") or ""
            if len(t1.strip()) < 20:
                t1 = pytesseract.image_to_string(gray, config="--oem 3 --psm 4") or ""

            ocr_text += t1 + "\n"

        return ocr_text

    except Exception:
        return ""

def extract_amount_candidates_from_text(text: str) -> list[float]:
    """
    Pull all money-looking amounts from OCR/text, including negatives,
    refunds, credits, and parenthesis negatives.
    """
    if not text:
        return []

    candidates = []

    for m in CURRENCY_RE.finditer(text):
        num = (m.group("num") or "").strip()
        neg = (m.group("neg") or "").lower().strip()

        try:
            amt = float(num.replace(",", ""))
        except Exception:
            continue

        if "refund" in neg or "credit" in neg or neg in {"-", "("}:
            amt = -amt

        if amt == 0:
            continue

        candidates.append(round(amt, 2))

    # Preserve order while removing duplicates
    return list(dict.fromkeys(candidates))


def extract_likely_total_amounts(text: str) -> list[float]:
    """
    Prioritizes likely receipt totals over every random amount on the receipt.
    Helpful for Lyft/Uber/hotel/email receipts where many amounts appear.
    """
    if not text:
        return []

    text_clean = text.replace(",", "")
    amounts = []

    patterns = [
        r"(?i)(?:grand total|amount paid|total paid|total charged|total|charged|payment|your ride)\D{0,60}\$?\s*(-?\d+\.\d{2})",
        r"(?i)(?:american express|amex|visa|mastercard|card ending|paid with).*?\$?\s*(-?\d+\.\d{2})",
        r"(?i)(?:refund|credit)\D{0,60}\$?\s*(\d+\.\d{2})",
    ]

    for pat in patterns:
        for m in re.finditer(pat, text_clean, re.DOTALL):
            try:
                amt = round(float(m.group(1)), 2)
                if re.search(r"(?i)refund|credit", m.group(0)) and amt > 0:
                    amt = -amt
                amounts.append(amt)
            except Exception:
                pass

    return list(dict.fromkeys(amounts))

# ✅ FIXED: this now REMOVES the chosen line from remaining_lines (so it can’t be reused)
def pick_line_by_amount_candidates(amount_cands: list[float], remaining_lines: list[dict]) -> dict | None:
    if not amount_cands or not remaining_lines:
        return None

    # Fast lookup from remaining_lines, but we still need to pop from remaining_lines itself.
    # Prefer the largest OCR amount that matches a line.
    for amt in amount_cands:
        target = round(amt, 2)
        # scan remaining_lines so we can pop the exact item
        matches = [(i, ln) for i, ln in enumerate(remaining_lines) if round(float(ln["amount"]), 2) == target]
        if not matches:
            continue
        if len(matches) == 1:
            i, _ = matches[0]
            return remaining_lines.pop(i)

        # tie-break among same-amount remaining lines: longer description wins
        best_i = max(matches, key=lambda t: len((t[1].get("desc") or "")))[0]
        return remaining_lines.pop(best_i)

    return None


def extract_amounts_from_text(text: str) -> set[float]:
    text = text.replace(",", "")
    matches = re.findall(r"(?<!\d)(\d{1,6}\.\d{2})(?!\d)", text)
    amts = set()
    for m in matches:
        try:
            amts.add(round(float(m), 2))
        except Exception:
            pass
    return amts


def best_line_for_ocr(text: str, remaining_lines: list[dict]) -> dict | None:
    if not remaining_lines:
        return None

    amts = extract_amounts_from_text(text)
    if not amts and not text.strip():
        return None

    best_i, best_score = None, -1.0
    for i, ln in enumerate(remaining_lines):
        desc = ln["desc"]
        amt = round(float(ln["amount"]), 2)

        score = 0.0
        if amt in amts:
            score += 3.0

        desc_tokens = [t for t in norm(desc).split() if len(t) >= 4]
        if desc_tokens:
            hits = sum(1 for t in desc_tokens if t in norm(text))
            score += min(1.5, hits * 0.25)

        score += sim(text[:4000], desc) * 0.75

        if score > best_score:
            best_score = score
            best_i = i

    if best_i is None or best_score < 1.25:
        return None

    return remaining_lines.pop(best_i)


# =========================
# ENHANCED CONFIDENCE MATCHING HELPERS
# =========================
# These helpers make the script "guess" more intelligently instead of only using exact OCR.
# Optional install for better scoring:
#   pip install rapidfuzz spacy sentence-transformers
#   python -m spacy download en_core_web_sm

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "your", "you", "receipt", "invoice",
    "payment", "paid", "total", "amount", "charge", "charged", "card", "ending", "trip", "ride",
    "services", "service", "expense", "expenses", "reimbursement", "reimbursable", "fee", "line",
    "new", "york", "ny", "usa", "inc", "llc", "ltd", "co", "company", "corp", "corporation"
}

VENDOR_ALIASES = {
    "uber": ["uber", "uber trip", "uber technologies", "uber bv", "ubereats", "uber eats"],
    "lyft": ["lyft", "lyft ride", "lyft.com", "lyft bikes"],
    "delta": ["delta", "delta air", "delta airlines"],
    "american": ["american airlines", "aa.com", "americanair"],
    "united": ["united", "united airlines", "ual"],
    "jetblue": ["jetblue", "jet blue"],
    "southwest": ["southwest", "southwest airlines"],
    "amtrak": ["amtrak"],
    "marriott": ["marriott", "courtyard", "residence inn", "springhill", "fairfield", "ac hotel", "westin", "sheraton", "moxy"],
    "hilton": ["hilton", "hampton", "doubletree", "garden inn", "homewood", "embassy suites"],
    "hyatt": ["hyatt", "hyatt place", "hyatt house"],
    "ihg": ["holiday inn", "crowne plaza", "intercontinental", "kimpton", "hotel indigo"],
    "airbnb": ["airbnb", "air bnb"],
    "expedia": ["expedia", "hotels.com", "travelocity"],
    "doordash": ["doordash", "door dash"],
    "grubhub": ["grubhub", "grub hub"],
    "starbucks": ["starbucks"],
}

DATE_RE = re.compile(r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{4}[/-]\d{1,2}[/-]\d{1,2})(?!\d)")


def token_similarity(a: str, b: str) -> float:
    """0-100 similarity. Uses RapidFuzz when available, otherwise SequenceMatcher."""
    a2, b2 = norm(a), norm(b)
    if not a2 or not b2:
        return 0.0
    if fuzz is not None:
        return float(max(
            fuzz.token_set_ratio(a2, b2),
            fuzz.partial_ratio(a2, b2),
            fuzz.WRatio(a2, b2),
        ))
    return sim(a2, b2) * 100.0


def get_sentence_transformer_model():
    """
    Optional semantic similarity model.
    This lets the matcher understand that different words can mean the same thing,
    e.g. "cab ride to airport" vs "Uber/Lyft transportation".

    Install:
        pip install sentence-transformers

    Notes:
    - First run may download the model unless it is already cached.
    - Set ST_MODEL_NAME in QBO.env to use a different model.
    - Set DISABLE_SENTENCE_TRANSFORMERS=1 in QBO.env to turn this off.
    """
    global _ST_MODEL, _ST_MODEL_FAILED

    if os.environ.get("DISABLE_SENTENCE_TRANSFORMERS", "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        return None

    if SentenceTransformer is None:
        return None

    if _ST_MODEL is not None:
        return _ST_MODEL

    if _ST_MODEL_FAILED:
        return None

    model_name = os.environ.get("ST_MODEL_NAME", "all-MiniLM-L6-v2").strip() or "all-MiniLM-L6-v2"

    try:
        log(f"Loading sentence-transformers model: {model_name}")
        _ST_MODEL = SentenceTransformer(model_name)
        return _ST_MODEL
    except Exception as e:
        _ST_MODEL_FAILED = True
        log(f"[WARN] sentence-transformers disabled: {e}")
        return None


def compact_text_for_embedding(filename: str, text: str) -> str:
    """
    Shortens noisy OCR text before embedding so the model focuses on useful receipt clues.
    """
    raw = f"{filename}\n{text or ''}"
    lines = []

    # Prioritize lines likely to contain merchant, charge, route, hotel, date, or total info.
    keep_words = re.compile(
        r"(?i)(total|amount|paid|charged|payment|receipt|invoice|trip|ride|hotel|inn|air|airline|flight|"
        r"uber|lyft|taxi|delta|american|united|jetblue|southwest|amtrak|marriott|hilton|hyatt|kimpton|"
        r"check.?in|check.?out|arrival|departure|fare|room|reservation|booking|restaurant|cafe)"
    )

    for line in raw.splitlines():
        clean = " ".join(line.split())
        if not clean:
            continue
        if keep_words.search(clean) or re.search(r"\$?\s*-?\d+\.\d{2}", clean):
            lines.append(clean)
        if len(" ".join(lines)) > 1800:
            break

    if not lines:
        return raw[:1800]

    return "\n".join(lines)[:2200]


def semantic_similarity(filename: str, text: str, desc: str) -> float:
    """
    Returns 0-100 semantic similarity using sentence-transformers.
    Safe no-op if the library/model is unavailable.
    """
    model = get_sentence_transformer_model()
    if model is None or not desc or not (filename or text):
        return 0.0

    try:
        receipt_blob = compact_text_for_embedding(filename, text)
        if not receipt_blob.strip():
            return 0.0

        embeddings = model.encode(
            [receipt_blob, desc],
            convert_to_tensor=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        if st_util is not None:
            val = float(st_util.cos_sim(embeddings[0], embeddings[1]).item())
        else:
            # With normalized embeddings, dot product equals cosine similarity.
            val = float((embeddings[0] * embeddings[1]).sum().item())

        # Map cosine range roughly [-1, 1] to [0, 100], clamped.
        return max(0.0, min(100.0, ((val + 1.0) / 2.0) * 100.0))
    except Exception as e:
        log(f"[WARN] sentence-transformers similarity failed: {e}")
        return 0.0


def get_spacy_nlp():
    global _SPACY_NLP
    if spacy is None:
        return None
    if _SPACY_NLP is not None:
        return _SPACY_NLP
    try:
        _SPACY_NLP = spacy.load("en_core_web_sm")
    except Exception:
        _SPACY_NLP = False
    return _SPACY_NLP if _SPACY_NLP is not False else None


def extract_entities_spacy(text: str) -> dict:
    """Optional spaCy extraction for organizations/dates/money. Safe no-op if spaCy is not installed."""
    out = {"orgs": [], "dates": [], "money": []}
    nlp = get_spacy_nlp()
    if not nlp or not text.strip():
        return out
    try:
        doc = nlp(text[:5000])
        for ent in doc.ents:
            label = ent.label_
            val = ent.text.strip()
            if label == "ORG":
                out["orgs"].append(val)
            elif label == "DATE":
                out["dates"].append(val)
            elif label == "MONEY":
                out["money"].append(val)
    except Exception:
        pass
    return out


def important_tokens(s: str) -> set[str]:
    toks = set()
    for t in norm(s).split():
        if len(t) < 4:
            continue
        if t in STOPWORDS:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", t):
            continue
        toks.add(t)
    return toks


def canonical_vendor(text: str) -> str:
    n = norm(text)
    for vendor, aliases in VENDOR_ALIASES.items():
        for alias in aliases:
            if norm(alias) in n:
                return vendor
    return ""


def extract_dates_from_text(text: str) -> list[str]:
    if not text:
        return []
    vals = []
    for m in DATE_RE.finditer(text):
        vals.append(m.group(0))
    return list(dict.fromkeys(vals))


def amount_near(a: float, b: float, tolerance: float = 0.01) -> bool:
    try:
        return abs(round(float(a), 2) - round(float(b), 2)) <= tolerance
    except Exception:
        return False


def score_line_against_attachment(filename: str, text: str, line: dict, likely_amounts: list[float], all_amounts: list[float]) -> tuple[float, list[str]]:
    """
    Weighted scoring model. Exact amount is strongest. Vendor/text clues help disambiguate duplicates.
    Returns score and human-readable reasons.
    """
    score = 0.0
    reasons = []
    desc = line.get("desc", "") or ""
    line_amt = round(float(line.get("amount", 0)), 2)
    full_text = f"{filename}\n{text[:8000]}"
    full_norm = norm(full_text)
    desc_norm = norm(desc)

    filename_amt = parse_amount_from_filename(filename)
    if filename_amt is not None and amount_near(filename_amt, line_amt):
        score += 115
        reasons.append("filename amount exact")

    if any(amount_near(x, line_amt) for x in likely_amounts):
        score += 125
        reasons.append("likely total exact")
    elif any(amount_near(x, line_amt) for x in all_amounts):
        score += 95
        reasons.append("OCR amount exact")
    elif any(amount_near(abs(x), abs(line_amt)) for x in all_amounts):
        score += 70
        reasons.append("OCR absolute amount match")

    # Vendor alias matching.
    vend_desc = canonical_vendor(desc)
    vend_file = canonical_vendor(filename)
    vend_text = canonical_vendor(text)
    if vend_desc and (vend_desc == vend_file or vend_desc == vend_text):
        score += 45
        reasons.append(f"vendor alias match: {vend_desc}")
    elif vend_file and vend_file in desc_norm:
        score += 25
        reasons.append("filename vendor in line description")

    # Token overlap between invoice description and extracted text/filename.
    desc_tokens = important_tokens(desc)
    if desc_tokens:
        hits = [t for t in desc_tokens if t in full_norm]
        if hits:
            add = min(45, len(hits) * 10)
            score += add
            reasons.append("keyword hits: " + ", ".join(hits[:6]))

    # Fuzzy similarity.
    fn_sim = token_similarity(filename, desc)
    if fn_sim >= 80:
        score += 35
        reasons.append(f"strong filename similarity {fn_sim:.0f}")
    elif fn_sim >= 60:
        score += 18
        reasons.append(f"filename similarity {fn_sim:.0f}")

    text_sim = token_similarity(text[:3000], desc)
    if text_sim >= 85:
        score += 35
        reasons.append(f"strong OCR similarity {text_sim:.0f}")
    elif text_sim >= 65:
        score += 18
        reasons.append(f"OCR similarity {text_sim:.0f}")

    # spaCy org support, if installed.
    entities = extract_entities_spacy(text)
    if entities.get("orgs"):
        org_blob = " ".join(entities["orgs"][:10])
        org_sim = token_similarity(org_blob, desc)
        if org_sim >= 70:
            score += 20
            reasons.append(f"spaCy org similarity {org_sim:.0f}")

    # sentence-transformers semantic support, if installed.
    # This is intentionally a supporting signal, not stronger than an exact amount match.
    sem_sim = semantic_similarity(filename, text, desc)
    if sem_sim >= 82:
        score += 38
        reasons.append(f"semantic similarity {sem_sim:.0f}")
    elif sem_sim >= 74:
        score += 24
        reasons.append(f"semantic similarity {sem_sim:.0f}")
    elif sem_sim >= 68:
        score += 12
        reasons.append(f"semantic similarity {sem_sim:.0f}")

    return score, reasons


def pick_best_line_by_confidence(filename: str, text: str, remaining_lines: list[dict]) -> tuple[dict | None, float, str]:
    if not remaining_lines:
        return None, 0.0, "no remaining lines"

    likely_amounts = extract_likely_total_amounts(text)
    all_amounts = extract_amount_candidates_from_text(text)

    # Put likely totals first but keep all exact amount candidates available.
    merged_all = list(dict.fromkeys(likely_amounts + all_amounts))

    scored = []
    for i, ln in enumerate(remaining_lines):
        score, reasons = score_line_against_attachment(filename, text, ln, likely_amounts, merged_all)
        scored.append((score, i, ln, reasons))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_i, best_ln, best_reasons = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    gap = best_score - second_score

    # Confidence rules:
    # - >=110: normally an exact amount + some clue, or likely total exact.
    # - >=95 with a 20-point gap: amount match is likely enough.
    # - >=70 with a 30-point gap: allow a reasoned guess when there is no exact amount but good vendor/text match.
    accept = False
    if best_score >= 110:
        accept = True
    elif best_score >= 95 and gap >= 20:
        accept = True
    elif best_score >= 70 and gap >= 30:
        accept = True

    reason = "; ".join(best_reasons[:5]) or "best weighted score"
    reason += f" | score {best_score:.1f}, gap {gap:.1f}"

    if not accept:
        return None, best_score, reason

    return remaining_lines.pop(best_i), best_score, reason


def extract_text_for_attachment(path: str) -> str:
    ext = (os.path.splitext(path)[1] or "").lower()
    if ext in [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"]:
        return extract_text_from_image(path)
    if ext == ".pdf":
        return extract_text_from_pdf(path)
    if ext in [".txt", ".csv", ".html", ".htm"]:
        try:
            return pathlib.Path(path).read_text(errors="ignore")
        except Exception:
            return ""
    return ""


# =========================
# MAIN WORK
# =========================
def download_invoice_attachments_to_zip(doc_number: str, save_dir: str) -> str:
    access_token = get_access_token()

    invoice_id = find_invoice_id_by_doc_number(access_token, doc_number)
    invoice = get_invoice_by_id(access_token, invoice_id)
    attachables = list_attachables_for_invoice(access_token, invoice_id)

    if not attachables:
        raise RuntimeError(f"Invoice {doc_number} (Id {invoice_id}) has no attachments.")

    lines = get_invoice_lines_for_naming(invoice)

    lines_by_amount = defaultdict(list)
    for ln in lines:
        lines_by_amount[round(float(ln["amount"]), 2)].append(ln)

    def pick_line_for_filename(filename: str) -> dict | None:
        amt = parse_amount_from_filename(filename)
        vend = vendor_hint_from_filename(filename)
        if amt is None:
            return None

        bucket = lines_by_amount.get(round(amt, 2), [])
        if not bucket:
            return None

        if len(bucket) == 1:
            return bucket.pop(0)

        best_i, best_score = None, -1.0
        for i, ln in enumerate(bucket):
            score = 0.0
            if vend and vend in norm(ln["desc"]):
                score += 1.0
            score += sim(filename, ln["desc"])
            if score > best_score:
                best_score = score
                best_i = i

        return bucket.pop(best_i)

    zip_name = sanitize_filename(f"{doc_number}.zip")
    zip_path = os.path.join(save_dir, zip_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = tmp
        downloaded = []
        remaining_lines = []

        for a in attachables:
            att_id = a.get("Id")
            original_name = sanitize_filename(a.get("FileName") or f"attachable_{att_id}")
            _, ext = os.path.splitext(original_name)
            ext = ext or ""

            temp_url = get_temp_download_url(access_token, att_id)
            temp_save_name = sanitize_filename(f"tmp_{att_id}{ext}")
            temp_path = unique_path(tmpdir, temp_save_name)
            download_file(temp_url, temp_path)

            ln = pick_line_for_filename(original_name)

            downloaded.append({
                "att_id": att_id,
                "path": temp_path,
                "original_name": original_name,
                "ext": ext,
                "matched_line": ln,
            })

        # rebuild remaining lines after filename-based pops
        for _, bucket in lines_by_amount.items():
            remaining_lines.extend(bucket)

        if OCR_ENABLED:
            log("OCR is enabled. Attempting OCR mapping for unmatched attachments...")
        else:
            log("OCR is not enabled (Tesseract missing). Unmatched items will stay REVIEW-*.")

        for item in downloaded:
            # Already matched by filename amount
            if item["matched_line"] is not None:
                ln = item["matched_line"]
                log_match(
                    True,
                    f"{item['original_name']} -> "
                    f"Filename matched Line {ln['line_num']} | "
                    f"${money_str(ln['amount'])} | "
                    f"{ln['desc']}"
                )
                continue

            if not OCR_ENABLED:
                log_match(
                    False,
                    f"{item['original_name']} -> OCR disabled / NO MATCH FOUND"
                )
                continue

            p = item["path"]
            text = extract_text_for_attachment(p)

            if not text.strip():
                log_match(
                    False,
                    f"{item['original_name']} -> No OCR text extracted / NO MATCH FOUND"
                )
                continue

            ln, conf_score, conf_reason = pick_best_line_by_confidence(
                item["original_name"],
                text,
                remaining_lines,
            )

            if ln:
                item["matched_line"] = ln
                log_match(
                    True,
                    f"{item['original_name']} -> "
                    f"Confidence matched Line {ln['line_num']} | "
                    f"${money_str(ln['amount'])} | "
                    f"{ln['desc']} | {conf_reason}"
                )
            else:
                log_match(
                    False,
                    f"{item['original_name']} -> LOW CONFIDENCE / NO MATCH FOUND | {conf_reason}"
                )

        # =========================
        # BUILD MISSING ATTACHMENTS LIST
        # =========================
        matched_keys = set()

        for item in downloaded:
            ln = item.get("matched_line")
            if ln:
                key = (
                    str(ln.get("line_num")),
                    round(float(ln.get("amount", 0)), 2),
                    ln.get("desc", "").strip()
                )
                matched_keys.add(key)

        missing_lines = []

        for ln in lines:
            key = (
                str(ln.get("line_num")),
                round(float(ln.get("amount", 0)), 2),
                ln.get("desc", "").strip()
            )

            if key not in matched_keys:
                missing_lines.append(ln)

        # Create TXT report if missing lines exist
        if missing_lines:
            txt_name = sanitize_filename(f"{doc_number} - Missing Attachments.txt")
            txt_path = os.path.join(tmpdir, txt_name)

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"Invoice {doc_number} - Missing Attachments\n")
                f.write("=" * 60 + "\n\n")

                for ln in missing_lines:
                    f.write(
                        f"Line {ln['line_num']} | "
                        f"${money_str(ln['amount'])} | "
                        f"{ln['desc']}\n"
                    )

            downloaded.append({
                "att_id": "MISSING_REPORT",
                "path": txt_path,
                "original_name": txt_name,
                "ext": ".txt",
                "matched_line": None
            })
            
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for item in downloaded:
                ln = item["matched_line"]
                original_name = item["original_name"]
                ext = item["ext"]

                # =========================
                # CONVERT IMAGE ATTACHMENTS TO PDF BEFORE ZIPPING
                # =========================
                # This happens AFTER matching/OCR so image OCR still works normally.
                if is_image_file(item["path"]):
                    try:
                        pdf_path = convert_image_to_pdf(item["path"])
                        try:
                            os.remove(item["path"])
                        except Exception:
                            pass

                        item["path"] = pdf_path
                        ext = ".pdf"
                        item["ext"] = ".pdf"
                        original_name = os.path.splitext(original_name)[0] + ".pdf"
                        item["original_name"] = original_name

                        log(f"Converted image attachment to PDF: {original_name}")
                    except Exception as e:
                        log_match(False, f"{original_name} -> Image-to-PDF conversion failed: {e}")

                if ln:
                    new_name = sanitize_filename(
                        f"{ln['line_num']} - ${money_str(ln['amount'])} - {ln['desc']}{ext}"
                    )
                else:
                    new_name = sanitize_filename(f"REVIEW - {original_name}")

                final_path = unique_path(tmpdir, new_name)
                os.replace(item["path"], final_path)
                z.write(final_path, os.path.basename(final_path))

    return zip_path


# =========================
# UI (Tkinter popups)
# =========================
def main():
    root = tk.Tk()
    root.withdraw()

    doc_number = simpledialog.askstring("QBO Invoice", "Enter the QBO Invoice Number (DocNumber):")
    if not doc_number:
        return

    save_dir = filedialog.askdirectory(title="Choose a folder to save the ZIP")
    if not save_dir:
        return

    try:
        zip_path = download_invoice_attachments_to_zip(doc_number.strip(), save_dir)
        messagebox.showinfo("Success", f"Saved attachments ZIP:\n{zip_path}")
    except Exception as e:
        messagebox.showerror("Error", str(e))


if __name__ == "__main__":
    main()
