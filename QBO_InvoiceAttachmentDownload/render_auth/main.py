from __future__ import annotations

import base64
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel


app = FastAPI(title="QBO Extension Apps Auth Server")


CLIENT_ID = os.environ["QBO_CLIENT_ID"]
CLIENT_SECRET = os.environ["QBO_CLIENT_SECRET"]
REDIRECT_URI = os.environ["QBO_REDIRECT_URI"]

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
        query = f"SELECT * FROM Invoice STARTPOSITION {start_position} MAXRESULTS {page_size}"
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