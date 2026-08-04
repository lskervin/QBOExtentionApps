from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import keyring
import requests
from dotenv import load_dotenv
import os


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / "QBO.env")

CLIENT_ID = os.getenv("QBO_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv(
    "QBO_REDIRECT_URI",
    "http://localhost:8000/callback",
).strip()
ENVIRONMENT = os.getenv("QBO_ENVIRONMENT", "sandbox").strip().lower()

AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

QBO_SCOPE = "com.intuit.quickbooks.accounting"

KEYRING_SERVICE = "QBOExtensionApps"
ACCESS_TOKEN_KEY = "qbo_access_token"
REFRESH_TOKEN_KEY = "qbo_refresh_token"

APP_DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / "QBOExtensionApps"
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

CONNECTION_FILE = APP_DATA_DIR / "qbo_connection.json"


class QBOAuthError(RuntimeError):
    """Raised when QuickBooks authorization fails."""


@dataclass
class QBOConnection:
    realm_id: str
    access_token_expires_at: float
    refresh_token_expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "realm_id": self.realm_id,
            "access_token_expires_at": self.access_token_expires_at,
            "refresh_token_expires_at": self.refresh_token_expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QBOConnection":
        return cls(
            realm_id=str(data["realm_id"]),
            access_token_expires_at=float(
                data.get("access_token_expires_at", 0)
            ),
            refresh_token_expires_at=(
                float(data["refresh_token_expires_at"])
                if data.get("refresh_token_expires_at")
                else None
            ),
        )


class OAuthCallbackServer:
    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.result: dict[str, str] = {}
        self.error: str | None = None
        self.completed = threading.Event()

        parsed = urlparse(REDIRECT_URI)

        if parsed.scheme != "http":
            raise QBOAuthError(
                "The local callback in this example must use HTTP."
            )

        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise QBOAuthError(
                "The redirect URI must use localhost or 127.0.0.1."
            )

        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.callback_path = parsed.path or "/"

        parent = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request_url = urlparse(self.path)

                if request_url.path != parent.callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return

                query = parse_qs(request_url.query)

                returned_state = query.get("state", [""])[0]
                authorization_code = query.get("code", [""])[0]
                realm_id = query.get("realmId", [""])[0]
                oauth_error = query.get("error", [""])[0]
                oauth_error_description = query.get(
                    "error_description",
                    [""],
                )[0]

                if oauth_error:
                    parent.error = (
                        oauth_error_description or oauth_error
                    )
                    self._send_browser_response(
                        "QuickBooks connection was not completed.",
                        success=False,
                    )

                elif returned_state != parent.expected_state:
                    parent.error = (
                        "The returned OAuth state did not match."
                    )
                    self._send_browser_response(
                        "The authorization response could not be verified.",
                        success=False,
                    )

                elif not authorization_code or not realm_id:
                    parent.error = (
                        "The callback did not include a code and realmId."
                    )
                    self._send_browser_response(
                        "QuickBooks did not return the required information.",
                        success=False,
                    )

                else:
                    parent.result = {
                        "code": authorization_code,
                        "realm_id": realm_id,
                    }
                    self._send_browser_response(
                        "QuickBooks is connected. You can close this window.",
                        success=True,
                    )

                parent.completed.set()

            def _send_browser_response(
                self,
                message: str,
                success: bool,
            ) -> None:
                heading = (
                    "Connection successful"
                    if success
                    else "Connection unsuccessful"
                )

                html = f"""
                <!doctype html>
                <html lang="en">
                <head>
                    <meta charset="utf-8">
                    <title>{heading}</title>
                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            background: #f5f7fa;
                            margin: 0;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            min-height: 100vh;
                        }}

                        .card {{
                            background: white;
                            border-radius: 14px;
                            padding: 36px;
                            max-width: 520px;
                            box-shadow: 0 8px 30px rgba(0,0,0,.10);
                            text-align: center;
                        }}

                        h1 {{
                            margin-top: 0;
                        }}
                    </style>
                </head>
                <body>
                    <div class="card">
                        <h1>{heading}</h1>
                        <p>{message}</p>
                    </div>
                </body>
                </html>
                """

                body = html.encode("utf-8")

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )
                self.send_header(
                    "Content-Length",
                    str(len(body)),
                )
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = HTTPServer(
            (self.host, self.port),
            CallbackHandler,
        )

    def wait_for_callback(
        self,
        timeout_seconds: int = 300,
    ) -> dict[str, str]:
        server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        server_thread.start()

        try:
            received = self.completed.wait(timeout_seconds)

            if not received:
                raise QBOAuthError(
                    "Timed out waiting for QuickBooks authorization."
                )

            if self.error:
                raise QBOAuthError(self.error)

            return self.result
        finally:
            self.server.shutdown()
            self.server.server_close()


class QBOAuthManager:
    def __init__(self) -> None:
        self._validate_configuration()

    @staticmethod
    def _validate_configuration() -> None:
        missing = []

        if not CLIENT_ID:
            missing.append("QBO_CLIENT_ID")

        if not CLIENT_SECRET:
            missing.append("QBO_CLIENT_SECRET")

        if not REDIRECT_URI:
            missing.append("QBO_REDIRECT_URI")

        if missing:
            raise QBOAuthError(
                "Missing QBO.env values: " + ", ".join(missing)
            )

    @staticmethod
    def _basic_auth_header() -> str:
        raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"

    @staticmethod
    def _save_tokens(
        access_token: str,
        refresh_token: str,
    ) -> None:
        keyring.set_password(
            KEYRING_SERVICE,
            ACCESS_TOKEN_KEY,
            access_token,
        )
        keyring.set_password(
            KEYRING_SERVICE,
            REFRESH_TOKEN_KEY,
            refresh_token,
        )

    @staticmethod
    def _get_access_token() -> str | None:
        return keyring.get_password(
            KEYRING_SERVICE,
            ACCESS_TOKEN_KEY,
        )

    @staticmethod
    def _get_refresh_token() -> str | None:
        return keyring.get_password(
            KEYRING_SERVICE,
            REFRESH_TOKEN_KEY,
        )

    @staticmethod
    def _delete_tokens() -> None:
        for key in (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY):
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass

    @staticmethod
    def _save_connection(connection: QBOConnection) -> None:
        CONNECTION_FILE.write_text(
            json.dumps(connection.to_dict(), indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def get_connection() -> QBOConnection | None:
        if not CONNECTION_FILE.exists():
            return None

        try:
            data = json.loads(
                CONNECTION_FILE.read_text(encoding="utf-8")
            )
            return QBOConnection.from_dict(data)
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

    def build_authorization_url(self, state: str) -> str:
        parameters = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": QBO_SCOPE,
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }

        return f"{AUTHORIZATION_URL}?{urlencode(parameters)}"

    def connect(self) -> QBOConnection:
        state = secrets.token_urlsafe(32)
        callback_server = OAuthCallbackServer(state)

        authorization_url = self.build_authorization_url(state)
        browser_opened = webbrowser.open(authorization_url)

        if not browser_opened:
            raise QBOAuthError(
                "The browser could not be opened automatically."
            )

        callback = callback_server.wait_for_callback()
        token_data = self._exchange_code_for_tokens(
            callback["code"]
        )

        current_time = time.time()

        connection = QBOConnection(
            realm_id=callback["realm_id"],
            access_token_expires_at=(
                current_time
                + int(token_data.get("expires_in", 3600))
                - 60
            ),
            refresh_token_expires_at=(
                current_time
                + int(token_data["x_refresh_token_expires_in"])
                if token_data.get("x_refresh_token_expires_in")
                else None
            ),
        )

        self._save_tokens(
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
        )
        self._save_connection(connection)

        return connection

    def _exchange_code_for_tokens(
        self,
        authorization_code: str,
    ) -> dict[str, Any]:
        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth_header(),
                "Accept": "application/json",
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
            },
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )

        return self._parse_token_response(response)

    def refresh_access_token(self) -> str:
        refresh_token = self._get_refresh_token()
        connection = self.get_connection()

        if not refresh_token or not connection:
            raise QBOAuthError(
                "QuickBooks is not connected."
            )

        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth_header(),
                "Accept": "application/json",
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )

        token_data = self._parse_token_response(response)

        latest_refresh_token = token_data.get(
            "refresh_token",
            refresh_token,
        )

        self._save_tokens(
            access_token=token_data["access_token"],
            refresh_token=latest_refresh_token,
        )

        connection.access_token_expires_at = (
            time.time()
            + int(token_data.get("expires_in", 3600))
            - 60
        )

        if token_data.get("x_refresh_token_expires_in"):
            connection.refresh_token_expires_at = (
                time.time()
                + int(token_data["x_refresh_token_expires_in"])
            )

        self._save_connection(connection)

        return token_data["access_token"]

    def get_valid_access_token(self) -> str:
        connection = self.get_connection()
        access_token = self._get_access_token()

        if not connection or not access_token:
            raise QBOAuthError(
                "QuickBooks is not connected."
            )

        if time.time() >= connection.access_token_expires_at:
            return self.refresh_access_token()

        return access_token

    def disconnect(self) -> None:
        refresh_token = self._get_refresh_token()

        if refresh_token:
            try:
                requests.post(
                    REVOKE_URL,
                    headers={
                        "Authorization": self._basic_auth_header(),
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json={"token": refresh_token},
                    timeout=30,
                )
            except requests.RequestException:
                pass

        self._delete_tokens()

        try:
            CONNECTION_FILE.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _parse_token_response(
        response: requests.Response,
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise QBOAuthError(
                "Intuit returned an unreadable response."
            ) from exc

        if not response.ok:
            description = (
                data.get("error_description")
                or data.get("error")
                or response.reason
            )
            raise QBOAuthError(
                f"QuickBooks authorization failed: {description}"
            )

        required = {
            "access_token",
            "refresh_token",
        }

        missing = required.difference(data)

        if missing:
            raise QBOAuthError(
                "Intuit's response did not contain: "
                + ", ".join(sorted(missing))
            )

        return data