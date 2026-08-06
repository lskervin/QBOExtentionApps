from __future__ import annotations

import base64
import io
import re
import zipfile
from difflib import SequenceMatcher
import os
import secrets
import time
from urllib.parse import quote, urlencode

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel


app = FastAPI(title="QBO Extension Apps Auth Server")


CLIENT_ID = os.environ["QBO_CLIENT_ID"]
CLIENT_SECRET = os.environ["QBO_CLIENT_SECRET"]
REDIRECT_URI = os.environ["QBO_REDIRECT_URI"]
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    REDIRECT_URI.rsplit("/qbo/callback", 1)[0],
).rstrip("/")

AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPE = "com.intuit.quickbooks.accounting"
QBO_API_BASE_URL = "https://quickbooks.api.intuit.com/v3/company"

# Temporary in-memory storage for testing.
# Replace this with a persistent database before production use.
oauth_sessions: dict[str, dict] = {}


class ConnectSessionResponse(BaseModel):
    session_id: str
    authorization_url: str


@app.get("/")
def home() -> dict[str, str]:
    return {
        "status": "online",
        "service": "QBO Extension Apps Auth Server",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/connect")
def connect() -> RedirectResponse:
    """
    Browser-only test route.
    Starts OAuth without creating a desktop polling session.
    """
    state = secrets.token_urlsafe(32)

    oauth_sessions[state] = {
        "session_id": None,
        "status": "waiting",
    }

    authorization_url = build_authorization_url(state)
    return RedirectResponse(url=authorization_url)


@app.post("/connect-session", response_model=ConnectSessionResponse)
def create_connect_session() -> ConnectSessionResponse:
    """
    Creates an OAuth login session for the desktop application.
    """
    session_id = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)

    oauth_sessions[state] = {
        "session_id": session_id,
        "status": "waiting",
    }

    return ConnectSessionResponse(
        session_id=session_id,
        authorization_url=build_authorization_url(state),
    )


@app.get("/connect-status/{session_id}")
def connect_status(session_id: str) -> dict:
    """
    Allows the desktop application to check whether OAuth completed.
    """
    for session in oauth_sessions.values():
        if session.get("session_id") == session_id:
            return {
                "status": session.get("status", "waiting"),
                "connected": session.get("status") == "connected",
                "realm_id": session.get("realm_id"),
                "message": session.get("message"),
            }

    raise HTTPException(
        status_code=404,
        detail="Connection session was not found or expired.",
    )


@app.get("/qbo/callback", response_class=HTMLResponse)
def qbo_callback(
    code: str | None = None,
    realmId: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """
    Receives Intuit's OAuth callback, exchanges the authorization code
    for tokens, and marks the desktop session as connected.
    """
    if not state or state not in oauth_sessions:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state.",
        )

    existing_session = oauth_sessions[state]

    if error:
        existing_session.update(
            {
                "status": "failed",
                "message": error_description or error,
            }
        )

        return HTMLResponse(
            content=build_result_page(
                heading="Connection unsuccessful",
                message=error_description or error,
                successful=False,
            ),
            status_code=400,
        )

    if not code or not realmId:
        existing_session.update(
            {
                "status": "failed",
                "message": "Missing authorization code or company ID.",
            }
        )

        raise HTTPException(
            status_code=400,
            detail="Missing authorization code or company ID.",
        )

    try:
        token_data = exchange_code_for_tokens(code)
    except HTTPException as exc:
        existing_session.update(
            {
                "status": "failed",
                "message": str(exc.detail),
            }
        )
        raise

    oauth_sessions[state] = {
        "session_id": existing_session.get("session_id"),
        "status": "connected",
        "realm_id": realmId,
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_in": token_data.get("expires_in"),
        "access_token_expires_at": (
            time.time() + int(token_data.get("expires_in", 3600)) - 60
        ),
        "refresh_token_expires_in": token_data.get(
            "x_refresh_token_expires_in"
        ),
    }

    return HTMLResponse(
        content=build_result_page(
            heading="QuickBooks connected",
            message=(
                "Your QuickBooks company was connected successfully. "
                "You can close this browser window and return to the desktop app."
            ),
            successful=True,
        )
    )


@app.get("/invoices/pending")
def get_pending_invoices() -> dict:
    state, session = get_connected_session()
    access_token = get_valid_access_token(state, session)
    realm_id = session["realm_id"]
    invoices = []
    start_position = 1
    page_size = 1000

    while True:
        query = (
            "SELECT * FROM Invoice "
            "WHERE Balance > '0' "
            f"STARTPOSITION {start_position} "
            f"MAXRESULTS {page_size}"
        )
        response = requests.get(
            f"{QBO_API_BASE_URL}/{realm_id}/query",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"query": query, "minorversion": "75"},
            timeout=60,
        )
        if response.status_code == 401:
            access_token = refresh_session_access_token(state, session)
            response = requests.get(
                f"{QBO_API_BASE_URL}/{realm_id}/query",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                params={"query": query, "minorversion": "75"},
                timeout=60,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="QuickBooks returned an unreadable invoice response.") from exc
        if not response.ok:
            raise HTTPException(status_code=502, detail=f"QuickBooks invoice query failed: {data.get('Fault') or response.reason}")
        page = data.get("QueryResponse", {}).get("Invoice", [])
        invoices.extend(page)
        if len(page) < page_size:
            break
        start_position += page_size

    filtered = [
        invoice for invoice in invoices
        if invoice.get("PrintStatus") == "NeedToPrint"
        and invoice.get("EmailStatus") == "NotSet"
        and float(invoice.get("Balance") or 0) > 0
    ]
    return {"count": len(filtered), "invoices": filtered}



@app.get("/invoices/{invoice_id}/detail")
def get_invoice_detail(invoice_id: str) -> dict:
    state, session = get_connected_session()
    access_token = get_valid_access_token(state, session)
    realm_id = session["realm_id"]

    invoice = qbo_get_invoice(access_token, realm_id, invoice_id)
    attachables = qbo_get_invoice_attachables(access_token, realm_id, invoice_id)

    lines = extract_invoice_lines(invoice)
    mapped_lines, unmatched = match_attachments_to_lines(lines, attachables)

    attachment_count = sum(
        len(line.get("attachments", []))
        for line in mapped_lines
    ) + len(unmatched)

    return {
        "invoice": invoice,
        "lines": mapped_lines,
        "unmatched_attachments": unmatched,
        "attachment_count": attachment_count,
    }



@app.get("/attachments/{attachable_id}/download")
def download_attachment(attachable_id: str) -> StreamingResponse:
    state, session = get_connected_session()
    access_token = get_valid_access_token(state, session)
    realm_id = session["realm_id"]

    attachable = qbo_read_attachable(
        access_token,
        realm_id,
        attachable_id,
    )
    temp_url = qbo_get_attachment_download_url(
        access_token,
        realm_id,
        attachable_id,
    )

    try:
        file_response = requests.get(
            temp_url,
            stream=True,
            timeout=180,
        )
        file_response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not download attachment {attachable_id}: {exc}",
        ) from exc

    filename = sanitize_filename(
        attachable.get("FileName")
        or f"attachment_{attachable_id}"
    )
    content_type = (
        attachable.get("ContentType")
        or file_response.headers.get("Content-Type")
        or "application/octet-stream"
    )

    return StreamingResponse(
        file_response.iter_content(chunk_size=1024 * 256),
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )


@app.get("/attachments/{attachable_id}/open")
def open_attachment(attachable_id: str) -> RedirectResponse:
    state, session = get_connected_session()
    access_token = get_valid_access_token(state, session)
    realm_id = session["realm_id"]

    temp_url = qbo_get_attachment_download_url(
        access_token,
        realm_id,
        attachable_id,
    )
    return RedirectResponse(url=temp_url, status_code=302)


@app.get("/invoices/{invoice_id}/attachments.zip")
def export_invoice_attachments_zip(invoice_id: str) -> StreamingResponse:
    state, session = get_connected_session()
    access_token = get_valid_access_token(state, session)
    realm_id = session["realm_id"]

    invoice = qbo_get_invoice(access_token, realm_id, invoice_id)
    attachables = qbo_get_invoice_attachables(access_token, realm_id, invoice_id)

    if not attachables:
        raise HTTPException(status_code=404, detail="This invoice has no attachments.")

    lines = extract_invoice_lines(invoice)
    mapped_lines, _ = match_attachments_to_lines(lines, attachables)

    attachment_to_line = {}
    for line in mapped_lines:
        for attachment in line.get("attachments", []):
            attachment_to_line[str(attachment["id"])] = line

    memory_file = io.BytesIO()
    used_names = set()

    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as archive:
        for attachment in attachables:
            attachment_id = str(attachment.get("Id", ""))
            original_name = sanitize_filename(
                attachment.get("FileName") or f"attachment_{attachment_id}"
            )

            temp_url = qbo_get_attachment_download_url(
                access_token,
                realm_id,
                attachment_id,
            )
            file_response = requests.get(temp_url, timeout=120)
            file_response.raise_for_status()

            matched_line = attachment_to_line.get(attachment_id)
            extension = (
                "." + original_name.rsplit(".", 1)[1]
                if "." in original_name
                else ""
            )

            if matched_line:
                zip_name = sanitize_filename(
                    f"{matched_line['line_number']} - "
                    f"${float(matched_line['amount']):.2f} - "
                    f"{matched_line['description']}{extension}"
                )
            else:
                zip_name = sanitize_filename(f"REVIEW - {original_name}")

            zip_name = unique_archive_name(zip_name, used_names)
            archive.writestr(zip_name, file_response.content)

        missing_lines = [
            line for line in mapped_lines
            if not line.get("attachments")
        ]

        if missing_lines:
            report = [
                f"Invoice {invoice.get('DocNumber', invoice_id)} - Missing Attachments",
                "=" * 65,
                "",
            ]
            for line in missing_lines:
                report.append(
                    f"Line {line['line_number']} | "
                    f"${float(line['amount']):.2f} | "
                    f"{line['description']}"
                )

            archive.writestr(
                sanitize_filename(
                    f"{invoice.get('DocNumber', invoice_id)} - Missing Attachments.txt"
                ),
                "\n".join(report),
            )

    memory_file.seek(0)
    doc_number = sanitize_filename(str(invoice.get("DocNumber") or invoice_id))

    return StreamingResponse(
        memory_file,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="Invoice_{doc_number}_Attachments.zip"'
            )
        },
    )


def qbo_get_invoice(access_token: str, realm_id: str, invoice_id: str) -> dict:
    escaped_id = str(invoice_id).replace("'", "\\'")
    data = qbo_query_request(
        access_token,
        realm_id,
        f"SELECT * FROM Invoice WHERE Id = '{escaped_id}'",
    )
    invoices = data.get("QueryResponse", {}).get("Invoice", [])
    if not invoices:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} was not found.")
    return invoices[0]


def qbo_get_invoice_attachables(
    access_token: str,
    realm_id: str,
    invoice_id: str,
) -> list[dict]:
    """
    Lists every attachment linked to the invoice.

    QBO can return sparse Attachable records from a query. When that
    happens, read each Attachable by ID so FileName, ContentType, and
    AttachableRef are available to the desktop application.
    """
    escaped_id = str(invoice_id).replace("'", "\\'")
    query = (
        "SELECT * FROM Attachable "
        "WHERE AttachableRef.EntityRef.Type = 'Invoice' "
        f"AND AttachableRef.EntityRef.value = '{escaped_id}'"
    )

    data = qbo_query_request(access_token, realm_id, query)
    queried = data.get("QueryResponse", {}).get("Attachable", []) or []

    hydrated: list[dict] = []

    for attachable in queried:
        attachable_id = str(attachable.get("Id") or "").strip()
        if not attachable_id:
            continue

        # Sparse query results may contain only Id/sparse.
        if attachable.get("FileName") and attachable.get("ContentType"):
            hydrated.append(attachable)
            continue

        hydrated.append(
            qbo_read_attachable(
                access_token,
                realm_id,
                attachable_id,
            )
        )

    return hydrated


def qbo_read_attachable(
    access_token: str,
    realm_id: str,
    attachable_id: str,
) -> dict:
    response = requests.get(
        (
            f"{QBO_API_BASE_URL}/{realm_id}/attachable/"
            f"{quote(str(attachable_id), safe='')}"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        params={"minorversion": "75"},
        timeout=60,
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"QuickBooks returned an unreadable response for "
                f"attachment {attachable_id}."
            ),
        ) from exc

    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=(
                f"QuickBooks could not read attachment "
                f"{attachable_id}: {data.get('Fault') or response.reason}"
            ),
        )

    attachable = data.get("Attachable")
    if not attachable:
        raise HTTPException(
            status_code=502,
            detail=f"Attachment {attachable_id} was not returned by QuickBooks.",
        )

    return attachable


def qbo_query_request(access_token: str, realm_id: str, query: str) -> dict:
    response = requests.get(
        f"{QBO_API_BASE_URL}/{realm_id}/query",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        params={"query": query, "minorversion": "75"},
        timeout=60,
    )
    data = response.json()
    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"QuickBooks query failed: {data.get('Fault') or response.reason}",
        )
    return data


def qbo_get_attachment_download_url(
    access_token: str,
    realm_id: str,
    attachable_id: str,
) -> str:
    response = requests.get(
        f"{QBO_API_BASE_URL}/{realm_id}/download/{quote(str(attachable_id), safe='')}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/plain",
        },
        timeout=60,
    )
    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"Could not create a download link for attachment {attachable_id}.",
        )

    temp_url = response.text.strip()
    if not temp_url.startswith("http"):
        raise HTTPException(
            status_code=502,
            detail="QuickBooks returned an invalid attachment URL.",
        )
    return temp_url


def extract_invoice_lines(invoice: dict) -> list[dict]:
    output = []

    for index, line in enumerate(invoice.get("Line", []) or [], start=1):
        detail_type = str(line.get("DetailType") or "")
        if detail_type in {"SubTotalLineDetail", "DiscountLineDetail"}:
            continue

        amount = line.get("Amount")
        if amount is None:
            continue

        description = str(line.get("Description") or "").strip()
        if not description:
            description = f"Invoice line {line.get('Id') or index}"

        output.append(
            {
                "line_number": line.get("LineNum") or line.get("Id") or index,
                "line_id": str(line.get("Id") or ""),
                "description": description,
                "amount": float(amount),
                "detail_type": detail_type,
                "attachments": [],
            }
        )

    return output


def match_attachments_to_lines(lines: list[dict], attachables: list[dict]):
    """
    Matches invoice-level attachments to invoice lines.

    Strong signals:
      1. Filename starts with the line number.
      2. Filename contains the exact line amount.
      3. Filename resembles the line description.

    Attachments that cannot be matched confidently remain visible in the
    desktop app under "Unmatched invoice attachments".
    """
    unmatched = []

    for attachable in attachables:
        attachment = attachment_for_response(attachable)
        filename = attachment["file_name"]
        normalized_filename = normalize_text(filename)
        filename_amount = amount_from_filename(filename)
        candidates = []

        for line_index, line in enumerate(lines):
            score = 0.0
            line_number = str(line.get("line_number") or "").strip()
            line_amount = round(float(line.get("amount") or 0), 2)

            # Many exported files are named:
            # "Line - $Amount - Description.pdf"
            if line_number and re.match(
                rf"^\s*{re.escape(line_number)}(?:\s|[-_])",
                filename,
                flags=re.IGNORECASE,
            ):
                score += 150.0

            if (
                filename_amount is not None
                and abs(round(filename_amount, 2) - line_amount) <= 0.01
            ):
                score += 110.0

            description_score = (
                SequenceMatcher(
                    None,
                    normalized_filename,
                    normalize_text(line.get("description", "")),
                ).ratio()
                * 40.0
            )
            score += description_score

            candidates.append((score, line_index))

        candidates.sort(reverse=True)

        if candidates and candidates[0][0] >= 80:
            _, best_index = candidates[0]
            lines[best_index]["attachments"].append(attachment)
        else:
            unmatched.append(attachment)

    return lines, unmatched


def attachment_for_response(attachable: dict) -> dict:
    attachment_id = str(attachable.get("Id") or "")
    return {
        "id": attachment_id,
        "file_name": attachable.get("FileName") or f"attachment_{attachment_id}",
        "content_type": attachable.get("ContentType") or "",
        "open_url": (
            f"{PUBLIC_BASE_URL}/attachments/"
            f"{quote(attachment_id, safe='')}/open"
        ),
        "download_url": (
            f"{PUBLIC_BASE_URL}/attachments/"
            f"{quote(attachment_id, safe='')}/download"
        ),
    }


def amount_from_filename(filename: str):
    cleaned = str(filename or "").replace(",", "")
    match = re.search(r"(?<!\\d)(\\d+\\.\\d{2})(?!\\d)", cleaned)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def normalize_text(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split()
    )


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(
        r'[<>:"/\\\\|?*\\x00-\\x1F]',
        "_",
        str(value or "").strip(),
    )
    return cleaned.rstrip(". ").strip() or "attachment"


def unique_archive_name(filename: str, used_names: set[str]) -> str:
    if filename not in used_names:
        used_names.add(filename)
        return filename

    if "." in filename:
        stem, extension = filename.rsplit(".", 1)
        extension = "." + extension
    else:
        stem = filename
        extension = ""

    counter = 2
    while True:
        candidate = f"{stem} ({counter}){extension}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1



def get_connected_session() -> tuple[str, dict]:
    connected = [
        (state, session) for state, session in oauth_sessions.items()
        if session.get("status") == "connected"
        and session.get("realm_id")
        and session.get("refresh_token")
    ]
    if not connected:
        raise HTTPException(status_code=401, detail="No QuickBooks company is connected.")
    return connected[-1]


def get_valid_access_token(state: str, session: dict) -> str:
    if session.get("access_token") and time.time() < float(session.get("access_token_expires_at", 0)):
        return session["access_token"]
    return refresh_session_access_token(state, session)


def refresh_session_access_token(state: str, session: dict) -> str:
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="The QuickBooks refresh token is unavailable.")
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("ascii")
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {encoded_credentials}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    try:
        token_data = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Intuit returned an unreadable refresh response.") from exc
    if not response.ok:
        description = token_data.get("error_description") or token_data.get("error") or response.reason
        raise HTTPException(status_code=401, detail=f"QuickBooks authorization refresh failed: {description}")
    session["access_token"] = token_data["access_token"]
    session["refresh_token"] = token_data.get("refresh_token", refresh_token)
    session["expires_in"] = token_data.get("expires_in", 3600)
    session["access_token_expires_at"] = time.time() + int(token_data.get("expires_in", 3600)) - 60
    oauth_sessions[state] = session
    return session["access_token"]


def build_authorization_url(state: str) -> str:
    authorization_parameters = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": QBO_SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }

    return (
        f"{AUTHORIZATION_URL}?"
        f"{urlencode(authorization_parameters)}"
    )


def exchange_code_for_tokens(code: str) -> dict:
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("ascii")

    try:
        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not contact Intuit's token service: {exc}",
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Intuit returned an unreadable response.",
        ) from exc

    if not response.ok:
        description = (
            data.get("error_description")
            or data.get("error")
            or response.reason
        )

        raise HTTPException(
            status_code=502,
            detail=f"Token exchange failed: {description}",
        )

    if "access_token" not in data or "refresh_token" not in data:
        raise HTTPException(
            status_code=502,
            detail="Intuit's token response was missing required tokens.",
        )

    return data


def build_result_page(
    heading: str,
    message: str,
    successful: bool,
) -> str:
    symbol = "✓" if successful else "×"
    symbol_background = "#e8f5e9" if successful else "#fdecec"

    return f"""
    <!doctype html>
    <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta
                name="viewport"
                content="width=device-width, initial-scale=1"
            >
            <title>{heading}</title>

            <style>
                body {{
                    margin: 0;
                    min-height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    background: #f4f6f8;
                    font-family: Arial, sans-serif;
                }}

                .card {{
                    width: min(500px, calc(100% - 40px));
                    padding: 38px;
                    box-sizing: border-box;
                    border-radius: 16px;
                    background: white;
                    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
                    text-align: center;
                }}

                .symbol {{
                    width: 64px;
                    height: 64px;
                    margin: 0 auto 20px;
                    border-radius: 50%;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    background: {symbol_background};
                    font-size: 38px;
                    font-weight: bold;
                }}

                h1 {{
                    margin-bottom: 12px;
                }}

                p {{
                    color: #555;
                    line-height: 1.5;
                }}
            </style>
        </head>

        <body>
            <main class="card">
                <div class="symbol">{symbol}</div>
                <h1>{heading}</h1>
                <p>{message}</p>
            </main>
        </body>
    </html>
    """