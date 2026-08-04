from __future__ import annotations
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse
import base64
import os
import secrets
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


app = FastAPI(title="QBO Extension Apps Auth Server")


CLIENT_ID = os.environ["QBO_CLIENT_ID"]
CLIENT_SECRET = os.environ["QBO_CLIENT_SECRET"]
REDIRECT_URI = os.environ["QBO_REDIRECT_URI"]

AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPE = "com.intuit.quickbooks.accounting"

# Temporary storage for the first test.
# This will be replaced with a database before real production use.
oauth_sessions: dict[str, dict] = {}


@app.get("/")
def home() -> dict[str, str]:
    return {
        "status": "online",
        "service": "QBO Extension Apps Auth Server",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/connect-session", response_model=ConnectSessionResponse)
def create_connect_session() -> ConnectSessionResponse:
    """
    Creates a login session for the desktop application.
    """

    session_id = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)

    oauth_sessions[state] = {
        "session_id": session_id,
        "status": "waiting",
    }

    authorization_parameters = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": QBO_SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }

    authorization_url = (
        f"{AUTHORIZATION_URL}?"
        f"{urlencode(authorization_parameters)}"
    )

    return ConnectSessionResponse(
        session_id=session_id,
        authorization_url=authorization_url,
    )


@app.get("/qbo/callback", response_class=HTMLResponse)
def qbo_callback(
    request: Request,
    code: str | None = None,
    realmId: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """
    Intuit sends the browser to this route after authorization.
    """

    if error:
        return HTMLResponse(
            content=build_result_page(
                heading="Connection unsuccessful",
                message=error_description or error,
                successful=False,
            ),
            status_code=400,
        )

    if not state or state not in oauth_sessions:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state.",
        )

    if not code or not realmId:
        raise HTTPException(
            status_code=400,
            detail="Missing authorization code or company ID.",
        )

@app.get("/connect-status/{session_id}")
def connect_status(session_id: str) -> dict:
    for session in oauth_sessions.values():
        if session.get("session_id") == session_id:
            return {
                "status": session.get("status", "waiting"),
                "connected": session.get("status") == "connected",
                "realm_id": session.get("realm_id"),
            }

    raise HTTPException(
        status_code=404,
        detail="Connection session was not found or expired.",
    )

    token_data = exchange_code_for_tokens(code)

    existing_session = oauth_sessions[state]

    oauth_sessions[state] = {
        "session_id": existing_session.get("session_id"),
        "status": "connected",
        "realm_id": realmId,
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_in": token_data.get("expires_in"),
        "refresh_token_expires_in": token_data.get(
            "x_refresh_token_expires_in"
        ),
    }

    return HTMLResponse(
        content=build_result_page(
            heading="QuickBooks connected",
            message=(
                "Your QuickBooks company was connected successfully. "
                "You can close this browser window."
            ),
            successful=True,
        )
    )

class ConnectSessionResponse(BaseModel):
    session_id: str
    authorization_url: str

def exchange_code_for_tokens(code: str) -> dict:
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("ascii")

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

    return data


def build_result_page(
    heading: str,
    message: str,
    successful: bool,
) -> str:
    symbol = "✓" if successful else "×"

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
                    background: #e8f5e9;
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