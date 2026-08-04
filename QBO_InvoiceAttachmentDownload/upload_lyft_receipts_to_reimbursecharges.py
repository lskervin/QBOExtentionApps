import os
import re
import json
import time
import mimetypes
import pathlib
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode
from datetime import date

import requests
from dotenv import load_dotenv

# ============================================================
# CONFIG
# ============================================================
ENV_PATH = pathlib.Path(__file__).with_name("QBO.env")
load_dotenv(dotenv_path=ENV_PATH, override=False)

CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "").strip()
REALM_ID = os.environ.get("QBO_REALM_ID", "").strip()
INITIAL_REFRESH_TOKEN = os.environ.get("QBO_REFRESH_TOKEN", "").strip()
ENV = os.environ.get("QBO_ENV", "production").strip().lower()
MINORVERSION = os.environ.get("QBO_MINORVERSION", "75").strip()

# Default receipt root:
# G:\Shared drives\Accounting\2026\1. January\MSLyftReceipts
LYFT_RECEIPT_BASE_DIR = pathlib.Path(
    os.environ.get("LYFT_RECEIPT_BASE_DIR", r"G:\Shared drives\Accounting")
)

QUERY_YEAR = int(os.environ.get("LYFT_QUERY_YEAR", str(date.today().year)))
QUERY_START_DATE = os.environ.get("LYFT_QUERY_START_DATE", f"{QUERY_YEAR}-01-01")
QUERY_END_DATE = os.environ.get("LYFT_QUERY_END_DATE", date.today().isoformat())

LYFT_DESCRIPTION_TEXT = "LYFT.COM/CHARGES"
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

TOKEN_STORE_PATH = os.path.join(pathlib.Path.home(), ".qbo_token_store.json")
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = "https://sandbox-quickbooks.api.intuit.com" if ENV == "sandbox" else "https://quickbooks.api.intuit.com"

MONTH_FOLDER_NAMES = {
    1: "1. January",
    2: "2. February",
    3: "3. March",
    4: "4. April",
    5: "5. May",
    6: "6. June",
    7: "7. July",
    8: "8. August",
    9: "9. September",
    10: "10. October",
    11: "11. November",
    12: "12. December",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"✔ {msg}", flush=True)


def bad(msg: str) -> None:
    print(f"✘ {msg}", flush=True)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ============================================================
# AUTH - same QBO refresh-token pattern
# ============================================================
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
    stored_token = (store.get("refresh_token") or "").strip()
    if stored_token:
        return stored_token
    if INITIAL_REFRESH_TOKEN:
        return INITIAL_REFRESH_TOKEN
    raise RuntimeError("No refresh token found. Set QBO_REFRESH_TOKEN in QBO.env or token store.")


def set_refresh_token(new_refresh_token: str) -> None:
    store = load_token_store()
    store["refresh_token"] = new_refresh_token.strip()
    store["refresh_token_saved_at"] = int(time.time())
    save_token_store(store)


def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET or not REALM_ID:
        raise RuntimeError("Missing QBO_CLIENT_ID, QBO_CLIENT_SECRET, or QBO_REALM_ID in QBO.env")

    refresh_token = get_refresh_token().strip()
    if refresh_token.startswith("eyJ"):
        raise RuntimeError("Your refresh token looks like an access token. Store Intuit's refresh_token instead.")

    log("Refreshing QBO access token...")
    response = requests.post(
        TOKEN_URL,
        data=urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Failed to refresh token (HTTP {response.status_code}): {response.text}")

    payload = response.json()
    if payload.get("refresh_token"):
        set_refresh_token(payload["refresh_token"])
    if not payload.get("access_token"):
        raise RuntimeError(f"No access_token returned: {payload}")
    return payload["access_token"]


# ============================================================
# QBO API
# ============================================================
def qbo_query(access_token: str, sql: str) -> dict:
    url = f"{API_BASE}/v3/company/{REALM_ID}/query"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {"minorversion": MINORVERSION, "query": sql}

    log(f"Running QBO query: {sql}")
    response = requests.get(url, headers=headers, params=params, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"QBO query failed (HTTP {response.status_code}): {response.text}")
    return response.json()


def get_purchases(access_token: str, start_date: str, end_date: str) -> list[dict]:
    """
    Uses ONLY Purchase records. No ReimburseCharge query.

    Equivalent QBO query:
      SELECT * FROM Purchase
      WHERE TxnDate >= '2026-01-01'
      AND TxnDate <= '2026-03-31'
      STARTPOSITION 1 MAXRESULTS 1000
    """
    all_purchases: list[dict] = []
    start_position = 1
    max_results = 1000

    while True:
        sql = (
            "SELECT * FROM Purchase "
            f"WHERE TxnDate >= '{start_date}' "
            f"AND TxnDate <= '{end_date}' "
            f"STARTPOSITION {start_position} MAXRESULTS {max_results}"
        )
        data = qbo_query(access_token, sql)
        batch = data.get("QueryResponse", {}).get("Purchase", []) or []
        all_purchases.extend(batch)

        if len(batch) < max_results:
            break
        start_position += max_results

    return all_purchases


def upload_receipt_to_purchase(access_token: str, purchase_id: str, file_path: pathlib.Path) -> dict:
    url = f"{API_BASE}/v3/company/{REALM_ID}/upload?minorversion={MINORVERSION}"
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    metadata = {
        "AttachableRef": [
            {
                "EntityRef": {
                    "type": "Purchase",
                    "value": str(purchase_id),
                },
                "IncludeOnSend": True,
            }
        ],
        "FileName": file_path.name,
        "ContentType": content_type,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    with open(file_path, "rb") as f:
        files = {
            "file_metadata_01": ("metadata.json", json.dumps(metadata), "application/json"),
            "file_content_01": (file_path.name, f, content_type),
        }
        log(f"Uploading {file_path.name} to Purchase Id {purchase_id}...")
        response = requests.post(url, headers=headers, files=files, timeout=120)

    log(f"Upload HTTP {response.status_code} for Purchase {purchase_id}")
    try:
        payload = response.json()
        print(json.dumps(payload, indent=2), flush=True)
    except Exception:
        payload = {"raw_response": response.text}
        print(response.text, flush=True)

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed for {file_path.name} -> Purchase {purchase_id}: {response.text}")

    attachable_response = payload.get("AttachableResponse", []) or []
    attachable_id = None
    if attachable_response:
        attachable_id = (attachable_response[0].get("Attachable") or {}).get("Id")

    if not attachable_id:
        raise RuntimeError(f"Upload response did not include Attachable.Id for {file_path.name}: {payload}")

    return payload


# ============================================================
# MATCHING HELPERS
# ============================================================
def line0_description(purchase: dict) -> str:
    lines = purchase.get("Line", []) or []
    if not lines:
        return ""
    return lines[0].get("Description") or ""


def is_lyft_purchase(purchase: dict) -> bool:
    return LYFT_DESCRIPTION_TEXT in line0_description(purchase).upper()


def parse_receipt_filename(file_path: pathlib.Path) -> tuple[str, Decimal]:
    """
    Expected filename:
      2026-05-01_FW Your ride with Cheikh ahmed on May 1_28.75.pdf

    Logic requested:
      - date = all characters from the far left until the first underscore
      - amount = all characters after the final underscore, before extension
    """
    stem = file_path.stem
    if "_" not in stem:
        raise ValueError("filename does not contain underscores")

    txn_date = stem.split("_", 1)[0].strip()
    amount_text = stem.rsplit("_", 1)[1].strip().replace("$", "").replace(",", "")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", txn_date):
        raise ValueError("left side of filename is not YYYY-MM-DD")
    if not re.fullmatch(r"\d+(?:\.\d{2})", amount_text):
        raise ValueError("right side of filename is not a valid amount")

    return txn_date, money(amount_text)


def month_receipt_folder(txn_date: str) -> pathlib.Path:
    year_text, month_text, _ = txn_date.split("-")
    month_folder = MONTH_FOLDER_NAMES[int(month_text)]
    return LYFT_RECEIPT_BASE_DIR / year_text / month_folder / "MSLyftReceipts"


def find_matching_receipt_file(txn_date: str, amount: Decimal) -> tuple[pathlib.Path | None, str | None]:
    folder = month_receipt_folder(txn_date)
    if not folder.exists() or not folder.is_dir():
        return None, f"folder not found: {folder}"

    matches: list[pathlib.Path] = []
    for file_path in folder.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        try:
            file_txn_date, file_amount = parse_receipt_filename(file_path)
        except Exception:
            continue
        if file_txn_date == txn_date and file_amount == amount:
            matches.append(file_path)

    if not matches:
        return None, f"no file in {folder} for {txn_date} / ${amount}"
    if len(matches) > 1:
        return None, "multiple matching files: " + ", ".join(p.name for p in matches)

    return matches[0], None


def build_purchase_match_index(purchases: list[dict]) -> dict[tuple[str, Decimal], list[dict]]:
    index: dict[tuple[str, Decimal], list[dict]] = {}
    for purchase in purchases:
        key = (purchase.get("TxnDate"), money(purchase.get("TotalAmt", 0)))
        index.setdefault(key, []).append(purchase)
    return index


def get_attachable_id(upload_payload: dict) -> str | None:
    attachable_response = upload_payload.get("AttachableResponse", []) or []
    if not attachable_response:
        return None
    return (attachable_response[0].get("Attachable") or {}).get("Id")


# ============================================================
# MAIN
# ============================================================
def attach_lyft_receipts_to_purchases() -> None:
    access_token = get_access_token()

    purchases = get_purchases(access_token, QUERY_START_DATE, QUERY_END_DATE)
    lyft_purchases = [p for p in purchases if is_lyft_purchase(p)]
    purchase_index = build_purchase_match_index(lyft_purchases)

    log(f"Found {len(purchases)} Purchase records from {QUERY_START_DATE} through {QUERY_END_DATE}.")
    log(f"Filtered to {len(lyft_purchases)} Purchase records where Line[0].Description contains '{LYFT_DESCRIPTION_TEXT}'.")
    log(f"Receipt base folder: {LYFT_RECEIPT_BASE_DIR}")

    uploaded = 0
    skipped = 0
    used_files: set[pathlib.Path] = set()

    for purchase in lyft_purchases:
        purchase_id = str(purchase.get("Id", "")).strip()
        txn_date = purchase.get("TxnDate")
        amount = money(purchase.get("TotalAmt", 0))
        desc = line0_description(purchase)

        duplicate_purchases = purchase_index.get((txn_date, amount), [])
        if len(duplicate_purchases) > 1:
            ids = ", ".join(str(x.get("Id")) for x in duplicate_purchases)
            bad(f"AMBIGUOUS QBO PURCHASES | TxnDate={txn_date} | Amount=${amount} | Purchase IDs={ids} | {desc}")
            skipped += 1
            continue

        receipt_file, reason = find_matching_receipt_file(txn_date, amount)
        if not receipt_file:
            bad(f"NO FILE | Purchase {purchase_id} | {txn_date} | ${amount} | {reason} | {desc}")
            skipped += 1
            continue

        if receipt_file in used_files:
            bad(f"FILE ALREADY USED | Purchase {purchase_id} | {receipt_file.name} | {txn_date} | ${amount}")
            skipped += 1
            continue

        try:
            payload = upload_receipt_to_purchase(access_token, purchase_id, receipt_file)
            attachable_id = get_attachable_id(payload)
            used_files.add(receipt_file)
            uploaded += 1
            ok(f"UPLOADED | {receipt_file.name} -> Purchase {purchase_id} | Attachable {attachable_id} | {txn_date} | ${amount} | {desc}")
        except Exception as exc:
            skipped += 1
            bad(f"UPLOAD ERROR | {receipt_file.name} -> Purchase {purchase_id} | {txn_date} | ${amount} | {exc}")

    log(f"Done. Uploaded {uploaded}. Skipped {skipped}.")


if __name__ == "__main__":
    attach_lyft_receipts_to_purchases()
