from __future__ import annotations
import json
import sqlite3
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
import os
import sys
import threading
import time
import webbrowser
import requests
import tkinter as tk
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional
from urllib.parse import quote
import customtkinter as ctk
import xlsxwriter

# Optional local attachment-analysis dependencies.
try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except Exception:
    pytesseract = None
    Image = None
    ImageEnhance = None
    ImageFilter = None
    ImageOps = None

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None



try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

try:
    from paddleocr import PaddleOCR
except Exception:
    PaddleOCR = None

_PADDLE_OCR = None
_PADDLE_OCR_FAILED = False

# Windows OCR is especially useful on Windows 10/11 for photographed
# receipts and handwritten-looking totals. It is optional so the app can
# still run if the WinRT packages are not installed.
try:
    from winrt.windows.media.ocr import OcrEngine as WindowsOcrEngine
    from winrt.windows.globalization import Language as WindowsLanguage
    from winrt.windows.storage.streams import DataWriter as WindowsDataWriter
    from winrt.windows.graphics.imaging import (
        SoftwareBitmap as WindowsSoftwareBitmap,
        BitmapPixelFormat as WindowsBitmapPixelFormat,
    )
except Exception:
    WindowsOcrEngine = None
    WindowsLanguage = None
    WindowsDataWriter = None
    WindowsSoftwareBitmap = None
    WindowsBitmapPixelFormat = None


# ============================================================
# APP SETTINGS
# ============================================================

APP_NAME = "QBO Invoice Analyzer"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "Safe Hands Accounting"
WINDOW_SIZE = "1180x760"

RENDER_AUTH_BASE_URL = (
    "https://qbo-extension-auth.onrender.com"
)

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")


def get_app_data_dir() -> Path:
    """Store user settings outside the Git repository."""
    base = os.getenv("APPDATA") or str(Path.home())
    path = Path(base) / "QBOExtensionApps"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_FILE = get_app_data_dir() / "settings.json"

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(
    {
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/json",
    }
)

CACHE_ROOT = get_app_data_dir() / "cache"
ATTACHMENT_CACHE_DIR = CACHE_ROOT / "attachments"
ATTACHMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DB = CACHE_ROOT / "attachment_cache.sqlite3"

OCR_PDF_SUFFIX = "_OCR"


def get_runtime_base_dir() -> Path:
    """
    Returns the application directory in both normal Python and packaged
    PyInstaller builds.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def configure_tesseract() -> None:
    """
    Configure Tesseract from either a bundled copy or the standard
    Windows installation location.
    """
    if pytesseract is None:
        return

    candidates = [
        get_runtime_base_dir() / "tesseract" / "tesseract.exe",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]

    for candidate in candidates:
        if candidate.exists():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return


configure_tesseract()


def get_paddle_ocr():
    """
    Lazily initialize PaddleOCR only when a receipt actually needs OCR.

    PaddleOCR 3.x is attempted first. A compatibility fallback is included
    for older PaddleOCR releases.
    """
    global _PADDLE_OCR, _PADDLE_OCR_FAILED

    if PaddleOCR is None or _PADDLE_OCR_FAILED:
        return None

    if _PADDLE_OCR is not None:
        return _PADDLE_OCR

    try:
        # PaddleOCR 3.x
        _PADDLE_OCR = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            engine="paddle",
        )
        return _PADDLE_OCR
    except TypeError:
        # PaddleOCR 2.x compatibility
        try:
            _PADDLE_OCR = PaddleOCR(
                use_angle_cls=True,
                lang="en",
                show_log=False,
            )
            return _PADDLE_OCR
        except Exception:
            _PADDLE_OCR_FAILED = True
            return None
    except Exception:
        _PADDLE_OCR_FAILED = True
        return None





async def _await_winrt_operation(operation):
    return await operation


def windows_ocr_pil(image) -> str:
    """
    Use the Windows.Media.Ocr engine against a PIL image.

    This deliberately complements PaddleOCR/Tesseract because Windows'
    built-in OCR can recognize text patterns those engines miss on phone
    photos. The user's Windows OCR language pack must be installed.
    """
    if (
        image is None
        or WindowsOcrEngine is None
        or WindowsLanguage is None
        or WindowsDataWriter is None
        or WindowsSoftwareBitmap is None
        or WindowsBitmapPixelFormat is None
    ):
        return ""

    try:
        if image.mode != "RGBA":
            image = image.convert("RGBA")

        # Prefer English US, then fall back to the user's installed OCR
        # languages if that language is unavailable.
        language = WindowsLanguage("en-US")

        if WindowsOcrEngine.is_language_supported(language):
            engine = WindowsOcrEngine.try_create_from_language(language)
        else:
            engine = WindowsOcrEngine.try_create_from_user_profile_languages()

        if engine is None:
            return ""

        # Windows OCR has a maximum image dimension. Downscale only when
        # necessary, preserving the aspect ratio.
        try:
            maximum = int(WindowsOcrEngine.max_image_dimension)
        except Exception:
            maximum = 0

        if maximum and max(image.size) > maximum:
            ratio = maximum / max(image.size)
            image = image.resize(
                (
                    max(1, int(image.width * ratio)),
                    max(1, int(image.height * ratio)),
                ),
                Image.Resampling.LANCZOS,
            )

        writer = WindowsDataWriter()
        writer.write_bytes(image.tobytes())

        bitmap = WindowsSoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(),
            WindowsBitmapPixelFormat.RGBA8,
            image.width,
            image.height,
        )

        result = asyncio.run(
            _await_winrt_operation(
                engine.recognize_async(bitmap)
            )
        )

        # OcrResult exposes Text directly.
        recognized = getattr(result, "text", None)
        if recognized:
            return str(recognized)

        # Defensive fallback if a WinRT projection exposes only lines.
        lines = getattr(result, "lines", None)
        if lines:
            output = []
            for line in lines:
                line_text = getattr(line, "text", None)
                if line_text:
                    output.append(str(line_text))
            return "\n".join(output)

        return ""

    except Exception:
        return ""

@dataclass
class AppSettings:
    qbo_company_name: str = ""
    qbo_connected: bool = False
    receipt_folder: str = ""
    output_folder: str = ""
    archive_folder: str = ""


class SettingsStore:
    @staticmethod
    def load() -> AppSettings:
        if not CONFIG_FILE.exists():
            return AppSettings()

        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return AppSettings(**data)
        except (OSError, json.JSONDecodeError, TypeError):
            return AppSettings()

    @staticmethod
    def save(settings: AppSettings) -> None:
        CONFIG_FILE.write_text(
            json.dumps(asdict(settings), indent=2),
            encoding="utf-8",
        )


# ============================================================
# REUSABLE UI COMPONENTS
# ============================================================

class Page(ctk.CTkFrame):
    """Base class for application pages."""

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, fg_color="transparent")
        self.app = app

    def on_show(self) -> None:
        """Called whenever the page becomes visible."""


class SectionCard(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str = ""):
        super().__init__(master, corner_radius=14)

        self.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w",
        )
        title_label.grid(row=0, column=0, padx=22, pady=(18, 2), sticky="ew")

        if subtitle:
            subtitle_label = ctk.CTkLabel(
                self,
                text=subtitle,
                text_color=("gray35", "gray70"),
                anchor="w",
            )
            subtitle_label.grid(
                row=1,
                column=0,
                padx=22,
                pady=(0, 14),
                sticky="ew",
            )


class StatCard(ctk.CTkFrame):
    def __init__(self, master, title: str, value: str, detail: str = ""):
        super().__init__(master, corner_radius=14)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=title,
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(16, 3), sticky="ew")

        self.value_label = ctk.CTkLabel(
            self,
            text=value,
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        )
        self.value_label.grid(row=1, column=0, padx=18, sticky="ew")

        self.detail_label = ctk.CTkLabel(
            self,
            text=detail,
            text_color=("gray40", "gray65"),
            anchor="w",
        )
        self.detail_label.grid(row=2, column=0, padx=18, pady=(3, 16), sticky="ew")

    def update_value(self, value: str, detail: Optional[str] = None) -> None:
        self.value_label.configure(text=value)
        if detail is not None:
            self.detail_label.configure(text=detail)


class PathSelector(ctk.CTkFrame):
    def __init__(
        self,
        master,
        label: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None],
        button_text: str = "Browse",
    ):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=label,
            font=ctk.CTkFont(weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, pady=(0, 6), sticky="ew")

        self.entry = ctk.CTkEntry(self, textvariable=variable, height=38)
        self.entry.grid(row=1, column=0, padx=(0, 10), sticky="ew")

        ctk.CTkButton(
            self,
            text=button_text,
            width=105,
            height=38,
            command=browse_command,
        ).grid(row=1, column=1)


# ============================================================
# HOME PAGE
# ============================================================

class HomePage(Page):
    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(
            self,
            text="Good morning",
            font=ctk.CTkFont(size=30, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, padx=8, pady=(10, 2), sticky="ew")

        ctk.CTkLabel(
            self,
            text="Choose a QuickBooks tool to get started.",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=15),
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, padx=8, pady=(0, 20), sticky="ew")

        self.connection_card = StatCard(
            self,
            "QuickBooks",
            "Not connected",
            "Connect before using tools",
        )
        self.connection_card.grid(row=2, column=0, padx=8, pady=8, sticky="nsew")

        self.company_card = StatCard(self, "Company", "—", "No company selected")
        self.company_card.grid(row=2, column=1, padx=8, pady=8, sticky="nsew")

        self.last_job_card = StatCard(self, "Recent activity", "No jobs", "Nothing run yet")
        self.last_job_card.grid(row=2, column=2, padx=8, pady=8, sticky="nsew")

        action_card = SectionCard(
            self,
            "Start a new job",
            "Select the workflow you want to run.",
        )
        action_card.grid(
            row=3,
            column=0,
            columnspan=3,
            padx=8,
            pady=(18, 8),
            sticky="nsew",
        )
        action_card.grid_columnconfigure((0, 1, 2), weight=1)
        self._make_action_button(
            action_card,
            column=0,
            title="Invoices",
            description="Review, export, and manage QuickBooks invoices.",
            command=lambda: app.show_page("invoice"),
        )
        self._make_action_button(
            action_card,
            column=1,
            title="Reports",
            description="Analyze all pending invoices and review attachment coverage.",
            command=lambda: app.show_page("reports"),
        )

    @staticmethod
    def _make_action_button(master, column, title, description, command):
        card = ctk.CTkFrame(master, corner_radius=12)
        card.grid(row=2, column=column, padx=10, pady=(8, 20), sticky="nsew")
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(size=17, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(18, 5), sticky="ew")

        ctk.CTkLabel(
            card,
            text=description,
            wraplength=250,
            justify="left",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=18, pady=(0, 16), sticky="ew")

        ctk.CTkButton(
            card,
            text="Open",
            height=38,
            command=command,
        ).grid(row=2, column=0, padx=18, pady=(0, 18), sticky="ew")

    def on_show(self) -> None:
        settings = self.app.settings

        if settings.qbo_connected:
            self.connection_card.update_value("Connected", "Authorization available")
            self.company_card.update_value(
                settings.qbo_company_name or "Connected company",
                "Ready to use",
            )
        else:
            self.connection_card.update_value("Not connected", "Connect before using tools")
            self.company_card.update_value("—", "No company selected")

        if self.app.last_job:
            self.last_job_card.update_value(
                self.app.last_job,
                self.app.last_job_detail,
            )


# ============================================================
# QUICKBOOKS CONNECTION PAGE
# ============================================================

class ConnectionPage(Page):
    POLL_INTERVAL_SECONDS = 2
    CONNECTION_TIMEOUT_SECONDS = 300

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.connection_in_progress = False

        ctk.CTkLabel(
            self,
            text="QuickBooks connection",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")

        ctk.CTkLabel(
            self,
            text="Authorize this app to connect to a QuickBooks Online company.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=8, pady=(0, 20), sticky="ew")

        card = SectionCard(
            self,
            "Connection status",
            "Authentication is completed securely through the hosted Render service.",
        )
        card.grid(row=2, column=0, padx=8, pady=8, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            card,
            text="Not connected",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        )
        self.status_label.grid(row=2, column=0, padx=22, pady=(4, 4), sticky="ew")

        self.company_label = ctk.CTkLabel(
            card,
            text="",
            text_color=("gray35", "gray70"),
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.company_label.grid(row=3, column=0, padx=22, pady=(0, 14), sticky="ew")

        button_row = ctk.CTkFrame(card, fg_color="transparent")
        button_row.grid(row=4, column=0, padx=22, pady=(0, 22), sticky="w")

        self.connect_button = ctk.CTkButton(
            button_row,
            text="Connect to QuickBooks",
            height=40,
            command=self.connect_qbo,
        )
        self.connect_button.grid(row=0, column=0, padx=(0, 10))

        self.disconnect_button = ctk.CTkButton(
            button_row,
            text="Disconnect",
            height=40,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.disconnect_qbo,
        )
        self.disconnect_button.grid(row=0, column=1)

        note = ctk.CTkTextbox(self, height=185, corner_radius=12)
        note.grid(row=3, column=0, padx=8, pady=(18, 8), sticky="ew")
        note.insert(
            "1.0",
            "How connection works\n\n"
            "1. The app requests a temporary login session from Render.\n"
            "2. Your browser opens the Intuit authorization page.\n"
            "3. After approval, Intuit returns to the Render callback.\n"
            "4. This app checks Render until the connection is complete.\n\n"
            "Your Intuit client secret is not stored in this desktop application.",
        )
        note.configure(state="disabled")

    def connect_qbo(self) -> None:
        if self.connection_in_progress:
            return

        self.connection_in_progress = True
        self.connect_button.configure(
            text="Opening QuickBooks...",
            state="disabled",
        )
        self.disconnect_button.configure(state="disabled")
        self.status_label.configure(text="Starting connection")
        self.company_label.configure(
            text="Contacting the authentication service..."
        )

        threading.Thread(
            target=self._run_qbo_connection,
            daemon=True,
        ).start()

    def _run_qbo_connection(self) -> None:
        try:
            response = requests.post(
                f"{RENDER_AUTH_BASE_URL}/connect-session",
                timeout=60,
            )
            response.raise_for_status()

            try:
                connection_data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "The authentication server returned an unreadable response."
                ) from exc

            session_id = connection_data.get("session_id")
            authorization_url = connection_data.get("authorization_url")

            if not session_id or not authorization_url:
                raise RuntimeError(
                    "The authentication server did not return a login session."
                )

            browser_opened = webbrowser.open(authorization_url)
            if not browser_opened:
                raise RuntimeError(
                    "The QuickBooks login page could not be opened automatically."
                )

            self.after(0, self._show_waiting_for_browser)
            self._wait_for_qbo_connection(session_id)

        except requests.RequestException as exc:
            self.after(
                0,
                lambda error=exc: self._connection_failed(
                    "Could not contact the authentication server.\n\n"
                    f"{error}"
                ),
            )
        except Exception as exc:
            self.after(
                0,
                lambda error=exc: self._connection_failed(str(error)),
            )

    def _show_waiting_for_browser(self) -> None:
        self.status_label.configure(text="Waiting for QuickBooks authorization")
        self.company_label.configure(
            text=(
                "Complete the login and company selection in your browser. "
                "This window will update automatically."
            )
        )
        self.connect_button.configure(text="Waiting for approval...")

    def _wait_for_qbo_connection(self, session_id: str) -> None:
        deadline = time.time() + self.CONNECTION_TIMEOUT_SECONDS
        last_network_error: str | None = None

        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{RENDER_AUTH_BASE_URL}/connect-status/{session_id}",
                    timeout=30,
                )

                if response.status_code == 404:
                    time.sleep(self.POLL_INTERVAL_SECONDS)
                    continue

                response.raise_for_status()

                try:
                    status_data = response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        "The authentication server returned an unreadable status response."
                    ) from exc

                if status_data.get("connected"):
                    realm_id = status_data.get("realm_id")
                    company_name = status_data.get("company_name")

                    if not realm_id:
                        raise RuntimeError(
                            "QuickBooks connected, but no company ID was returned."
                        )

                    self.after(
                        0,
                        lambda value=realm_id, name=company_name:
                        self._connection_succeeded(value, name),
                    )
                    return

                status = str(status_data.get("status", "waiting")).lower()
                if status in {"failed", "error", "denied"}:
                    raise RuntimeError(
                        status_data.get("message")
                        or "QuickBooks authorization was not completed."
                    )

                last_network_error = None

            except requests.RequestException as exc:
                last_network_error = str(exc)

            time.sleep(self.POLL_INTERVAL_SECONDS)

        timeout_message = "The QuickBooks login timed out. Please try again."
        if last_network_error:
            timeout_message += f"\n\nLast server error: {last_network_error}"

        self.after(
            0,
            lambda message=timeout_message: self._connection_failed(message),
        )

    def _connection_succeeded(
        self,
        realm_id: str,
        company_name: str | None = None,
    ) -> None:
        self.connection_in_progress = False
        self.app.settings.qbo_connected = True
        self.app.settings.qbo_company_name = (
            company_name or f"QuickBooks Company {realm_id}"
        )
        self.app.save_settings()
        self.refresh()

        messagebox.showinfo(
            "QuickBooks connected",
            "Your QuickBooks company was connected successfully.",
        )

    def _connection_failed(self, message: str) -> None:
        self.connection_in_progress = False
        self.refresh()
        messagebox.showerror(
            "QuickBooks connection unsuccessful",
            message,
        )

    def disconnect_qbo(self) -> None:
        if self.connection_in_progress:
            return

        confirmed = messagebox.askyesno(
            "Disconnect QuickBooks",
            (
                "Disconnect this QuickBooks company?\n\n"
                "This revokes the server-side QuickBooks authorization "
                "and removes the saved connection."
            ),
        )

        if not confirmed:
            return

        self.connection_in_progress = True
        self.status_label.configure(
            text="Disconnecting..."
        )
        self.connect_button.configure(
            state="disabled"
        )
        self.disconnect_button.configure(
            state="disabled"
        )

        threading.Thread(
            target=self._disconnect_worker,
            daemon=True,
        ).start()

    def _disconnect_worker(self) -> None:
        try:
            response = requests.post(
                f"{RENDER_AUTH_BASE_URL}/disconnect",
                timeout=60,
            )
            response.raise_for_status()

            self.after(
                0,
                self._disconnect_succeeded,
            )

        except requests.RequestException as exc:
            self.after(
                0,
                lambda error=exc:
                self._disconnect_failed(str(error)),
            )

    def _disconnect_succeeded(self) -> None:
        self.connection_in_progress = False
        self.app.settings.qbo_connected = False
        self.app.settings.qbo_company_name = ""
        self.app.save_settings()
        self.refresh()

        messagebox.showinfo(
            "QuickBooks disconnected",
            "The QuickBooks connection was removed successfully.",
        )

    def _disconnect_failed(self, message: str) -> None:
        self.connection_in_progress = False
        self.refresh()

        messagebox.showerror(
            "Disconnect unsuccessful",
            (
                "The QuickBooks connection could not be "
                f"removed.\n\n{message}"
            ),
        )

    def refresh(self) -> None:
        settings = self.app.settings

        if self.connection_in_progress:
            self.connect_button.configure(state="disabled")
            self.disconnect_button.configure(state="disabled")
            return

        if settings.qbo_connected:
            self.status_label.configure(text="Connected")
            self.company_label.configure(
                text=f"Company: {settings.qbo_company_name or 'QuickBooks Online'}"
            )
            self.connect_button.configure(text="Reconnect", state="normal")
            self.disconnect_button.configure(state="normal")
        else:
            self.status_label.configure(text="Not connected")
            self.company_label.configure(
                text="Connect to begin using QBO workflows."
            )
            self.connect_button.configure(
                text="Connect to QuickBooks",
                state="normal",
            )
            self.disconnect_button.configure(state="disabled")

    def on_show(self) -> None:
        self.refresh()

        threading.Thread(
            target=self._sync_server_connection_state,
            daemon=True,
        ).start()

    def _sync_server_connection_state(self) -> None:
        try:
            response = requests.get(
                f"{RENDER_AUTH_BASE_URL}/connection-status",
                timeout=45,
            )
            response.raise_for_status()
            data = response.json()

            self.after(
                0,
                lambda payload=data:
                self._apply_server_connection_state(
                    payload
                ),
            )

        except Exception:
            # Do not falsely mark the app disconnected simply because the
            # Render service is temporarily unreachable.
            return

    def _apply_server_connection_state(
        self,
        data: dict,
    ) -> None:
        connected = bool(
            data.get("connected")
        )
        realm_id = data.get("realm_id")

        self.app.settings.qbo_connected = connected

        if connected:
            if (
                not self.app.settings.qbo_company_name
                or self.app.settings.qbo_company_name
                .startswith("QuickBooks Company ")
            ):
                self.app.settings.qbo_company_name = (
                    f"QuickBooks Company {realm_id}"
                    if realm_id
                    else "QuickBooks Online"
                )
        else:
            self.app.settings.qbo_company_name = ""

        self.app.save_settings()
        self.refresh()



# ============================================================
# INVOICE ATTACHMENTS PAGE
# ============================================================

class InvoiceAttachmentsPage(Page):
    COLUMNS = (
        "doc_number", "date", "customer", "total", "balance",
        "print_status", "email_status", "invoice_id",
    )

    HEADINGS = {
        "doc_number": "Invoice #",
        "date": "Date",
        "customer": "Customer",
        "total": "Total",
        "balance": "Balance",
        "print_status": "Print Status",
        "email_status": "Email Status",
        "invoice_id": "QBO ID",
    }

    WIDTHS = {
        "doc_number": 95, "date": 95, "customer": 260,
        "total": 105, "balance": 105, "print_status": 115,
        "email_status": 105, "invoice_id": 85,
    }

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.loading = False
        self.invoices: list[dict] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            self, text="Invoices",
            font=ctk.CTkFont(size=28, weight="bold"), anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, padx=8, pady=(0, 12), sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            toolbar,
            text='Pending invoices with Balance > 0, PrintStatus "NeedToPrint", and EmailStatus "NotSet".',
            text_color=("gray35", "gray70"), anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_search_filter())

        self.search_entry = ctk.CTkEntry(
            toolbar,
            textvariable=self.search_var,
            width=250,
            placeholder_text="Search invoice, customer, date...",
        )
        self.search_entry.grid(
            row=1,
            column=0,
            pady=(10, 0),
            sticky="w",
        )

        action_buttons = ctk.CTkFrame(
            toolbar,
            fg_color="transparent",
        )
        action_buttons.grid(
            row=0,
            column=1,
            padx=(12, 0),
            sticky="ne",
        )

        self.refresh_button = ctk.CTkButton(
            action_buttons,
            text="Refresh invoices",
            width=145,
            command=self.refresh_invoices,
        )
        self.refresh_button.grid(
            row=0,
            column=0,
            pady=(0, 8),
            sticky="ew",
        )

        self.export_button = ctk.CTkButton(
            action_buttons,
            text="Export to Excel",
            width=145,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.export_table_to_xlsx,
            state="disabled",
        )
        self.export_button.grid(
            row=1,
            column=0,
            sticky="ew",
        )

        status_frame = ctk.CTkFrame(self, corner_radius=12)
        status_frame.grid(row=2, column=0, padx=8, pady=(0, 12), sticky="ew")
        status_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(status_frame, text="Status:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(16, 8), pady=12, sticky="w"
        )
        self.status_label = ctk.CTkLabel(status_frame, text="Ready", anchor="w")
        self.status_label.grid(row=0, column=1, pady=12, sticky="ew")
        self.count_label = ctk.CTkLabel(status_frame, text="0 invoices", text_color=("gray35", "gray70"))
        self.count_label.grid(row=0, column=2, padx=16, pady=12, sticky="e")

        table_card = ctk.CTkFrame(self, corner_radius=14)
        table_card.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Invoice.Treeview", rowheight=32, font=("Segoe UI", 10))
        style.configure("Invoice.Treeview.Heading", font=("Segoe UI", 10, "bold"))

        self.tree = ttk.Treeview(
            table_card, columns=self.COLUMNS, show="headings",
            style="Invoice.Treeview", selectmode="browse",
        )
        for column in self.COLUMNS:
            self.tree.heading(column, text=self.HEADINGS[column], command=lambda col=column: self.sort_by_column(col, False))
            self.tree.column(
                column, width=self.WIDTHS[column], minwidth=65,
                anchor="e" if column in {"total", "balance"} else "w",
                stretch=column == "customer",
            )

        vscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=(12, 0))
        vscroll.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=(12, 0))
        hscroll.grid(row=1, column=0, sticky="ew", padx=(12, 0), pady=(0, 12))
        self.tree.bind("<Double-1>", self.show_selected_invoice)
        self.tree.bind("<ButtonRelease-1>", self.open_invoice_from_number_click)

    def on_show(self) -> None:
        if not self.invoices and not self.loading:
            self.refresh_invoices()

    def refresh_invoices(self) -> None:
        if self.loading:
            return
        if not self.app.settings.qbo_connected:
            self.status_label.configure(text="Connect QuickBooks in Settings before loading invoices.")
            messagebox.showwarning(
                "QuickBooks connection required",
                "Open Settings → QuickBooks and connect a company first.",
            )
            return
        self.loading = True
        self.refresh_button.configure(text="Loading...", state="disabled")
        self.export_button.configure(state="disabled")
        self.status_label.configure(text="Loading invoices from QuickBooks...")
        threading.Thread(target=self._load_invoices_worker, daemon=True).start()

    def _load_invoices_worker(self) -> None:
        try:
            response = requests.get(f"{RENDER_AUTH_BASE_URL}/invoices/pending", timeout=120)
            response.raise_for_status()
            payload = response.json()
            raw_invoices = payload if isinstance(payload, list) else payload.get("invoices", [])
            filtered = [
                invoice for invoice in raw_invoices
                if invoice.get("PrintStatus") == "NeedToPrint"
                and invoice.get("EmailStatus") == "NotSet"
                and float(invoice.get("Balance") or 0) > 0
            ]
            self.after(0, lambda invoices=filtered: self._load_succeeded(invoices))
        except requests.RequestException as exc:
            self.after(0, lambda error=exc: self._load_failed(
                "Could not load invoices from the authentication server.\n\n" + str(error)
            ))
        except Exception as exc:
            self.after(0, lambda error=exc: self._load_failed(str(error)))

    def _load_succeeded(self, invoices: list[dict]) -> None:
        self.loading = False
        self.invoices = invoices
        self.refresh_button.configure(text="Refresh invoices", state="normal")
        self.export_button.configure(
            state="normal" if invoices else "disabled"
        )
        self.status_label.configure(text="Invoice list loaded.")
        self.count_label.configure(text=f"{len(invoices)} invoice{'' if len(invoices) == 1 else 's'}")
        self.populate_table(invoices)

    def _load_failed(self, message: str) -> None:
        self.loading = False
        self.refresh_button.configure(text="Refresh invoices", state="normal")
        self.export_button.configure(
            state="normal" if self.tree.get_children() else "disabled"
        )
        self.status_label.configure(text="Could not load invoices.")
        messagebox.showerror("Invoice loading unsuccessful", message)

    def populate_table(self, invoices: list[dict]) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for invoice in invoices:
            customer_ref = invoice.get("CustomerRef") or {}
            customer_name = customer_ref.get("name") or customer_ref.get("value") or ""
            self.tree.insert("", "end", values=(
                invoice.get("DocNumber", ""), invoice.get("TxnDate", ""), customer_name,
                self.format_money(invoice.get("TotalAmt")),
                self.format_money(invoice.get("Balance")),
                invoice.get("PrintStatus", ""), invoice.get("EmailStatus", ""),
                invoice.get("Id", ""),
            ))

    def apply_search_filter(self) -> None:
        query = self.search_var.get().strip().lower()

        if not query:
            self.populate_table(self.invoices)
            return

        filtered = []
        for invoice in self.invoices:
            customer_ref = invoice.get("CustomerRef") or {}
            searchable = " ".join(
                [
                    str(invoice.get("DocNumber", "")),
                    str(invoice.get("TxnDate", "")),
                    str(customer_ref.get("name") or customer_ref.get("value") or ""),
                    str(invoice.get("TotalAmt", "")),
                    str(invoice.get("Balance", "")),
                ]
            ).lower()

            if query in searchable:
                filtered.append(invoice)

        self.populate_table(filtered)
        self.count_label.configure(
            text=f"{len(filtered)} of {len(self.invoices)} invoices"
        )

    @staticmethod
    def format_money(value) -> str:
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return ""

    def sort_by_column(self, column: str, descending: bool) -> None:
        rows = [(self.tree.set(item_id, column), item_id) for item_id in self.tree.get_children("")]
        if column in {"total", "balance"}:
            def key(item):
                try:
                    return float(item[0].replace("$", "").replace(",", ""))
                except ValueError:
                    return 0.0
        else:
            def key(item):
                return item[0].lower()
        rows.sort(key=key, reverse=descending)
        for index, (_, item_id) in enumerate(rows):
            self.tree.move(item_id, "", index)
        self.tree.heading(column, command=lambda: self.sort_by_column(column, not descending))


    def open_invoice_from_number_click(self, event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)

        if region != "cell" or column != "#1" or not item_id:
            return

        values = self.tree.item(item_id, "values")
        if not values or len(values) < 8:
            return

        detail_page = self.app.pages["invoice_detail"]
        detail_page.load_invoice(str(values[7]), str(values[0]))
        self.app.show_page("invoice_detail")

    def export_table_to_xlsx(self) -> None:
        """
        Exports the rows currently displayed in the table, preserving
        the user's current sort order.
        """
        item_ids = self.tree.get_children("")

        if not item_ids:
            messagebox.showinfo(
                "Nothing to export",
                "There are no invoices in the table to export.",
            )
            return

        default_name = (
            f"Pending_Invoices_"
            f"{time.strftime('%Y-%m-%d')}.xlsx"
        )

        output_path = filedialog.asksaveasfilename(
            title="Export invoice table",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[
                ("Excel workbook", "*.xlsx"),
                ("All files", "*.*"),
            ],
        )

        if not output_path:
            return

        try:
            workbook = xlsxwriter.Workbook(output_path)
            worksheet = workbook.add_worksheet("Pending Invoices")

            title_format = workbook.add_format(
                {
                    "bold": True,
                    "font_size": 16,
                    "align": "left",
                    "valign": "vcenter",
                }
            )
            subtitle_format = workbook.add_format(
                {
                    "font_color": "#666666",
                    "italic": True,
                }
            )
            header_format = workbook.add_format(
                {
                    "bold": True,
                    "bg_color": "#D9EAF7",
                    "border": 1,
                    "align": "center",
                    "valign": "vcenter",
                }
            )
            text_format = workbook.add_format(
                {
                    "border": 1,
                    "valign": "top",
                }
            )
            date_format = workbook.add_format(
                {
                    "border": 1,
                    "num_format": "mm/dd/yyyy",
                    "valign": "top",
                }
            )
            money_format = workbook.add_format(
                {
                    "border": 1,
                    "num_format": "$#,##0.00",
                    "valign": "top",
                }
            )
            total_label_format = workbook.add_format(
                {
                    "bold": True,
                    "top": 1,
                    "align": "right",
                }
            )
            total_money_format = workbook.add_format(
                {
                    "bold": True,
                    "top": 1,
                    "num_format": "$#,##0.00",
                }
            )

            worksheet.merge_range(
                "A1:H1",
                "Pending QuickBooks Invoices",
                title_format,
            )
            worksheet.merge_range(
                "A2:H2",
                (
                    'PrintStatus = "NeedToPrint", '
                    'EmailStatus = "NotSet", and Balance > 0'
                ),
                subtitle_format,
            )

            headers = [
                self.HEADINGS[column]
                for column in self.COLUMNS
            ]

            header_row = 3
            for column_index, header in enumerate(headers):
                worksheet.write(
                    header_row,
                    column_index,
                    header,
                    header_format,
                )

            total_amount = 0.0
            total_balance = 0.0

            for row_offset, item_id in enumerate(item_ids, start=1):
                values = self.tree.item(item_id, "values")
                excel_row = header_row + row_offset

                for column_index, value in enumerate(values):
                    if column_index in {3, 4}:
                        numeric_value = self._money_to_float(value)
                        worksheet.write_number(
                            excel_row,
                            column_index,
                            numeric_value,
                            money_format,
                        )

                        if column_index == 3:
                            total_amount += numeric_value
                        else:
                            total_balance += numeric_value

                    elif column_index == 1:
                        try:
                            parsed_date = time.strptime(
                                str(value),
                                "%Y-%m-%d",
                            )
                            excel_date = (
                                parsed_date.tm_year,
                                parsed_date.tm_mon,
                                parsed_date.tm_mday,
                            )
                            from datetime import datetime
                            worksheet.write_datetime(
                                excel_row,
                                column_index,
                                datetime(*excel_date),
                                date_format,
                            )
                        except (TypeError, ValueError):
                            worksheet.write(
                                excel_row,
                                column_index,
                                value,
                                text_format,
                            )

                    else:
                        worksheet.write(
                            excel_row,
                            column_index,
                            value,
                            text_format,
                        )

            last_data_row = header_row + len(item_ids)
            totals_row = last_data_row + 1

            worksheet.write(
                totals_row,
                2,
                "Totals:",
                total_label_format,
            )
            worksheet.write_number(
                totals_row,
                3,
                total_amount,
                total_money_format,
            )
            worksheet.write_number(
                totals_row,
                4,
                total_balance,
                total_money_format,
            )

            worksheet.freeze_panes(header_row + 1, 0)
            worksheet.autofilter(
                header_row,
                0,
                last_data_row,
                len(headers) - 1,
            )

            column_widths = [
                14, 12, 34, 14, 14, 17, 15, 12
            ]
            for index, width in enumerate(column_widths):
                worksheet.set_column(index, index, width)

            worksheet.set_row(0, 24)
            worksheet.set_row(header_row, 22)

            workbook.close()

            self.status_label.configure(
                text=f"Exported {len(item_ids)} invoices to Excel."
            )

            messagebox.showinfo(
                "Export complete",
                (
                    f"{len(item_ids)} invoices were exported successfully.\n\n"
                    f"{output_path}"
                ),
            )

        except Exception as exc:
            try:
                workbook.close()
            except Exception:
                pass

            messagebox.showerror(
                "Export unsuccessful",
                f"The Excel file could not be created.\n\n{exc}",
            )

    @staticmethod
    def _money_to_float(value) -> float:
        try:
            return float(
                str(value)
                .replace("$", "")
                .replace(",", "")
                .strip()
                or 0
            )
        except (TypeError, ValueError):
            return 0.0

    def show_selected_invoice(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        messagebox.showinfo(
            "Selected invoice",
            f"Invoice: {values[0]}\nDate: {values[1]}\nCustomer: {values[2]}\n"
            f"Total: {values[3]}\nBalance: {values[4]}\nQBO ID: {values[7]}",
        )



# ============================================================
# INVOICE LINE DETAILS PAGE
# ============================================================

class InvoiceLineDetailsPage(Page):
    """
    Optimized invoice attachment analysis:

    1. Reuses one HTTP session.
    2. Downloads attachments concurrently.
    3. Stores attachments persistently in the local app cache.
    4. Reuses cached OCR and embedded PDF text.
    5. Matches by filename before doing OCR.
    6. OCRs only files that remain unmatched.
    7. Reuses cached files when creating the ZIP.
    """

    COLUMNS = ("line_number", "description", "amount", "attachment", "status")

    HEADINGS = {
        "line_number": "Line",
        "description": "Description",
        "amount": "Amount",
        "attachment": "Attachment",
        "status": "Status",
    }

    WIDTHS = {
        "line_number": 70,
        "description": 390,
        "amount": 110,
        "attachment": 260,
        "status": 150,
    }

    IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"
    }

    MAX_DOWNLOAD_WORKERS = min(4, os.cpu_count() or 2)
    FAST_PDF_SCALE = 1.5
    RETRY_PDF_SCALE = 2.0

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)

        self.invoice_id = ""
        self.doc_number = ""
        self.invoice: dict = {}
        self.loading = False
        self.line_records: dict[str, dict] = {}
        self.lines: list[dict] = []
        self.attachments: list[dict] = []
        self.unmatched_attachments: list[dict] = []
        self.selected_unmatched_attachment: dict | None = None
        self.unmatched_attachment_buttons: dict[str, ctk.CTkButton] = {}

        self._initialize_cache_database()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")
        top_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            top_bar,
            text="← Back to Invoices",
            width=150,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=lambda: self.app.show_page("invoice"),
        ).grid(row=0, column=0, padx=(0, 14), sticky="w")

        self.title_label = ctk.CTkLabel(
            top_bar,
            text="Invoice Details",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        )
        self.title_label.grid(row=0, column=1, sticky="ew")

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=1, column=0, padx=8, pady=(0, 12), sticky="ew")
        action_frame.grid_columnconfigure(0, weight=1)

        self.summary_label = ctk.CTkLabel(
            action_frame,
            text="Select an invoice.",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.summary_label.grid(row=0, column=0, sticky="ew")

        self.open_attachment_button = ctk.CTkButton(
            action_frame,
            text="Open Attachment",
            width=145,
            command=self.open_selected_attachment,
            state="disabled",
        )
        self.open_attachment_button.grid(row=0, column=1, padx=(12, 8))

        self.export_zip_button = ctk.CTkButton(
            action_frame,
            text="Export All as ZIP",
            width=145,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.export_all_attachments_zip,
            state="disabled",
        )
        self.export_zip_button.grid(row=0, column=2)

        status_frame = ctk.CTkFrame(self, corner_radius=12)
        status_frame.grid(row=2, column=0, padx=8, pady=(0, 12), sticky="ew")
        status_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            status_frame,
            text="Status:",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=(16, 8), pady=12, sticky="w")

        self.status_label = ctk.CTkLabel(status_frame, text="Ready", anchor="w")
        self.status_label.grid(row=0, column=1, pady=12, sticky="ew")

        self.count_label = ctk.CTkLabel(
            status_frame,
            text="0 lines",
            text_color=("gray35", "gray70"),
        )
        self.count_label.grid(row=0, column=2, padx=16, pady=12, sticky="e")

        table_card = ctk.CTkFrame(self, corner_radius=14)
        table_card.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("InvoiceLine.Treeview", rowheight=34, font=("Segoe UI", 10))
        style.configure(
            "InvoiceLine.Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
        )

        self.tree = ttk.Treeview(
            table_card,
            columns=self.COLUMNS,
            show="headings",
            style="InvoiceLine.Treeview",
            selectmode="browse",
        )

        for column in self.COLUMNS:
            self.tree.heading(column, text=self.HEADINGS[column])
            self.tree.column(
                column,
                width=self.WIDTHS[column],
                minwidth=65,
                anchor="e" if column == "amount" else "w",
                stretch=column == "description",
            )

        vscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=(12, 0))
        vscroll.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=(12, 0))
        hscroll.grid(row=1, column=0, sticky="ew", padx=(12, 0), pady=(0, 12))

        self.tree.bind("<<TreeviewSelect>>", self.update_attachment_button)
        self.tree.bind("<Double-1>", self.open_selected_attachment)

        self.unmatched_frame = ctk.CTkFrame(
            self,
            corner_radius=12,
        )
        self.unmatched_frame.grid(
            row=4,
            column=0,
            padx=8,
            pady=(0, 8),
            sticky="ew",
        )
        self.unmatched_frame.grid_columnconfigure(0, weight=1)

        unmatched_header = ctk.CTkFrame(
            self.unmatched_frame,
            fg_color="transparent",
        )
        unmatched_header.grid(
            row=0,
            column=0,
            padx=14,
            pady=(10, 4),
            sticky="ew",
        )
        unmatched_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            unmatched_header,
            text="Unmatched invoice attachments",
            font=ctk.CTkFont(weight="bold"),
            anchor="w",
        ).grid(
            row=0,
            column=0,
            sticky="ew",
        )

        self.assign_attachment_button = ctk.CTkButton(
            unmatched_header,
            text="Assign to Selected Line",
            width=175,
            command=self.assign_selected_attachment_to_line,
            state="disabled",
        )
        self.assign_attachment_button.grid(
            row=0,
            column=1,
            padx=(10, 0),
        )

        self.unmatched_instruction_label = ctk.CTkLabel(
            self.unmatched_frame,
            text=(
                "Select a missing invoice line above, then select a receipt "
                "below and click Assign to Selected Line."
            ),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.unmatched_instruction_label.grid(
            row=1,
            column=0,
            padx=14,
            pady=(0, 6),
            sticky="ew",
        )

        self.unmatched_links = ctk.CTkScrollableFrame(
            self.unmatched_frame,
            height=180,
            corner_radius=8,
        )
        self.unmatched_links.grid(
            row=2,
            column=0,
            padx=14,
            pady=(0, 12),
            sticky="ew",
        )

        self.unmatched_frame.grid_remove()

    @staticmethod
    def _initialize_cache_database() -> None:
        with sqlite3.connect(CACHE_DB) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attachment_text_cache (
                    attachment_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    embedded_text TEXT NOT NULL DEFAULT '',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_attachment_assignments (
                    invoice_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    line_key TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (invoice_id, attachment_id)
                )
                """
            )

            connection.commit()

    def load_invoice(self, invoice_id: str, doc_number: str) -> None:
        self.invoice_id = str(invoice_id)
        self.doc_number = str(doc_number)
        self.title_label.configure(text=f"Invoice {self.doc_number}")
        self.summary_label.configure(text="Loading invoice lines and attachments...")
        self.status_label.configure(text="Loading...")
        self.count_label.configure(text="0 lines")
        self.open_attachment_button.configure(state="disabled")
        self.export_zip_button.configure(state="disabled")
        self.unmatched_frame.grid_remove()

        self.line_records.clear()
        self.lines = []
        self.attachments = []
        self.unmatched_attachments = []
        self.selected_unmatched_attachment = None
        self.unmatched_attachment_buttons = {}

        if hasattr(self, "assign_attachment_button"):
            self.assign_attachment_button.configure(state="disabled")

        for widget in self.unmatched_links.winfo_children():
            widget.destroy()

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        self.loading = True
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self) -> None:
        try:
            response = HTTP_SESSION.get(
                f"{RENDER_AUTH_BASE_URL}/invoices/{quote(self.invoice_id, safe='')}/detail",
                timeout=120,
            )

            if response.status_code == 401:
                raise RuntimeError(
                    "The QuickBooks server session has expired. "
                    "Open Settings → QuickBooks and reconnect."
                )

            response.raise_for_status()
            payload = response.json()

            invoice = payload.get("invoice", {})
            lines = payload.get("lines", [])

            attachments = payload.get("attachments")
            if attachments is None:
                attachments = list(payload.get("unmatched_attachments", []))
                for line in lines:
                    attachments.extend(line.get("attachments", []))

            self.after(
                0,
                lambda inv=invoice, line_data=lines, files=attachments:
                self._start_local_analysis(inv, line_data, files),
            )

        except requests.RequestException as exc:
            self.after(
                0,
                lambda error=exc: self._load_failed(
                    "Could not load the invoice details.\n\n" + str(error)
                ),
            )
        except Exception as exc:
            self.after(0, lambda error=exc: self._load_failed(str(error)))

    def _start_local_analysis(
        self,
        invoice: dict,
        lines: list[dict],
        attachments: list[dict],
    ) -> None:
        self.invoice = invoice
        self.lines = [{**line, "attachments": []} for line in lines]
        self.attachments = [dict(item) for item in attachments]

        customer_ref = invoice.get("CustomerRef") or {}
        customer = customer_ref.get("name") or customer_ref.get("value") or ""

        self.summary_label.configure(
            text=(
                f"{customer}  •  Date: {invoice.get('TxnDate', '')}  •  "
                f"Total: {self.format_money(invoice.get('TotalAmt'))}  •  "
                f"Balance: {self.format_money(invoice.get('Balance'))}"
            )
        )

        # Show invoice lines immediately so the page feels responsive.
        self._populate_line_table()

        if not attachments:
            self.loading = False
            self.status_label.configure(text="This invoice has no attachments.")
            return

        self.status_label.configure(
            text=f"Preparing {len(attachments)} attachments..."
        )

        threading.Thread(
            target=self._analyze_attachments_worker,
            daemon=True,
        ).start()

    def _analyze_attachments_worker(self) -> None:
        try:
            prepared = self._prepare_attachments_concurrently(self.attachments)

            # Stage 1: filename-only matching. No text extraction or OCR.
            lines, filename_unmatched = self._match_pass(
                self.lines,
                prepared,
                use_text=False,
            )

            # Stage 2: embedded PDF text and cached text only.
            for index, attachment in enumerate(filename_unmatched, start=1):
                self._set_status_threadsafe(
                    f"Reading document text {index} of {len(filename_unmatched)}..."
                )
                attachment["text"] = self._get_embedded_or_cached_text(attachment)

            lines, text_unmatched = self._match_pass(
                lines,
                filename_unmatched,
                use_text=True,
            )

            # Stage 3: OCR only files that still could not be matched.
            for index, attachment in enumerate(text_unmatched, start=1):
                self._set_status_threadsafe(
                    f"Scan & OCR conversion {index} of {len(text_unmatched)}..."
                )
                attachment["text"] = self._get_or_create_ocr_text(attachment)

            lines, final_unmatched = self._match_pass(
                lines,
                text_unmatched,
                use_text=True,
            )

            self.after(
                0,
                lambda matched=lines, remaining=final_unmatched:
                self._analysis_succeeded(matched, remaining),
            )

        except Exception as exc:
            self.after(
                0,
                lambda error=exc: self._load_failed(
                    f"Attachment analysis failed.\n\n{error}"
                ),
            )

    def _prepare_attachments_concurrently(
        self,
        attachments: list[dict],
    ) -> list[dict]:
        prepared: list[dict] = []
        total = len(attachments)

        with ThreadPoolExecutor(
            max_workers=self.MAX_DOWNLOAD_WORKERS
        ) as executor:
            future_map = {
                executor.submit(self._ensure_cached_attachment, attachment):
                attachment
                for attachment in attachments
            }

            completed = 0
            for future in as_completed(future_map):
                completed += 1
                self._set_status_threadsafe(
                    f"Downloading attachments {completed} of {total}..."
                )

                attachment = dict(future_map[future])
                local_path = future.result()
                attachment["cached_path"] = str(local_path)
                attachment["file_size"] = local_path.stat().st_size
                attachment["text"] = ""
                prepared.append(attachment)

        # Preserve QBO's original attachment order.
        order = {
            str(item.get("id")): index
            for index, item in enumerate(attachments)
        }
        prepared.sort(key=lambda item: order.get(str(item.get("id")), 999999))
        return prepared

    def _ensure_cached_attachment(self, attachment: dict) -> Path:
        attachment_id = str(attachment.get("id") or "").strip()
        filename = self.sanitize_filename(
            attachment.get("file_name")
            or f"attachment_{attachment_id}"
        )

        invoice_cache = ATTACHMENT_CACHE_DIR / self.invoice_id
        invoice_cache.mkdir(parents=True, exist_ok=True)

        cache_name = self.sanitize_filename(
            f"{attachment_id}_{filename}"
        )
        cache_path = invoice_cache / cache_name

        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path

        download_url = attachment.get("download_url")
        if not download_url:
            download_url = (
                f"{RENDER_AUTH_BASE_URL}/attachments/"
                f"{quote(attachment_id, safe='')}/download"
            )

        temporary_path = cache_path.with_suffix(cache_path.suffix + ".part")

        response = HTTP_SESSION.get(
            download_url,
            stream=True,
            timeout=180,
        )
        response.raise_for_status()

        with open(temporary_path, "wb") as output_file:
            for chunk in response.iter_content(1024 * 256):
                if chunk:
                    output_file.write(chunk)

        temporary_path.replace(cache_path)
        return cache_path

    def _match_pass(
        self,
        lines: list[dict],
        attachments: list[dict],
        use_text: bool,
    ) -> tuple[list[dict], list[dict]]:
        remaining_indexes = [
            index
            for index, line in enumerate(lines)
            if not line.get("attachments")
        ]
        unmatched = []

        for attachment in attachments:
            filename = attachment.get("file_name", "")
            text = attachment.get("text", "") if use_text else ""
            likely_amounts = self.extract_likely_amounts(filename, text)
            candidates = []

            for line_index in remaining_indexes:
                line = lines[line_index]
                score = self.score_match(
                    filename,
                    text,
                    likely_amounts,
                    line,
                )
                candidates.append((score, line_index))

            candidates.sort(reverse=True)

            if candidates:
                best_score, best_index = candidates[0]
                second_score = candidates[1][0] if len(candidates) > 1 else 0
                gap = best_score - second_score

                if use_text:
                    accepted = (
                        best_score >= 105
                        or (best_score >= 85 and gap >= 15)
                        or (best_score >= 65 and gap >= 30)
                    )
                else:
                    # Filename-only stage should be conservative.
                    accepted = (
                        best_score >= 125
                        or (best_score >= 110 and gap >= 20)
                    )

                if accepted:
                    lines[best_index]["attachments"].append(attachment)
                    remaining_indexes.remove(best_index)
                    continue

            unmatched.append(attachment)

        return lines, unmatched

    def _get_embedded_or_cached_text(self, attachment: dict) -> str:
        cached = self._read_text_cache(attachment)
        if cached and cached.get("embedded_text"):
            return cached["embedded_text"]

        path = attachment.get("cached_path", "")
        embedded_text = self.extract_embedded_text(path)

        self._write_text_cache(
            attachment,
            embedded_text=embedded_text,
            ocr_text=(cached or {}).get("ocr_text", ""),
        )
        return embedded_text

    def _get_or_create_ocr_text(self, attachment: dict) -> str:
        cached = self._read_text_cache(attachment)
        if cached and cached.get("ocr_text"):
            return cached["ocr_text"]

        path = attachment.get("cached_path", "")
        embedded_text = (cached or {}).get("embedded_text", "")
        ocr_text = self.extract_ocr_text(path)

        self._write_text_cache(
            attachment,
            embedded_text=embedded_text,
            ocr_text=ocr_text,
        )
        return ocr_text or embedded_text

    @staticmethod
    def _read_text_cache(attachment: dict) -> dict | None:
        attachment_id = str(attachment.get("id") or "")
        file_name = str(attachment.get("file_name") or "")
        file_size = int(attachment.get("file_size") or 0)

        with sqlite3.connect(CACHE_DB) as connection:
            row = connection.execute(
                """
                SELECT file_name, file_size, embedded_text, ocr_text
                FROM attachment_text_cache
                WHERE attachment_id = ?
                """,
                (attachment_id,),
            ).fetchone()

        if not row:
            return None

        cached_name, cached_size, embedded_text, ocr_text = row
        if cached_name != file_name or int(cached_size) != file_size:
            return None

        return {
            "embedded_text": embedded_text or "",
            "ocr_text": ocr_text or "",
        }

    @staticmethod
    def _write_text_cache(
        attachment: dict,
        embedded_text: str,
        ocr_text: str,
    ) -> None:
        with sqlite3.connect(CACHE_DB) as connection:
            connection.execute(
                """
                INSERT INTO attachment_text_cache (
                    attachment_id,
                    file_name,
                    file_size,
                    embedded_text,
                    ocr_text,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    embedded_text = excluded.embedded_text,
                    ocr_text = excluded.ocr_text,
                    updated_at = excluded.updated_at
                """,
                (
                    str(attachment.get("id") or ""),
                    str(attachment.get("file_name") or ""),
                    int(attachment.get("file_size") or 0),
                    embedded_text or "",
                    ocr_text or "",
                    int(time.time()),
                ),
            )
            connection.commit()

    def score_match(
        self,
        filename: str,
        text: str,
        amounts: list[float],
        line: dict,
    ) -> float:
        score = 0.0
        line_amount = round(float(line.get("amount") or 0), 2)
        description = str(line.get("description") or "")
        line_number = str(line.get("line_number") or "")
        combined = f"{filename}\n{text[:8000]}"
        normalized_combined = self.normalize(combined)
        normalized_description = self.normalize(description)

        if line_number and re.match(
            rf"^\s*{re.escape(line_number)}(?:\s|[-_])",
            filename,
            re.IGNORECASE,
        ):
            score += 150

        if any(abs(amount - line_amount) <= 0.01 for amount in amounts):
            score += 110

        description_tokens = {
            token
            for token in normalized_description.split()
            if len(token) >= 4
        }
        hits = sum(
            1 for token in description_tokens
            if token in normalized_combined
        )
        score += min(45, hits * 10)

        score += (
            SequenceMatcher(
                None,
                self.normalize(filename),
                normalized_description,
            ).ratio()
            * 35
        )

        if text:
            score += (
                SequenceMatcher(
                    None,
                    self.normalize(text[:3000]),
                    normalized_description,
                ).ratio()
                * 25
            )

        return score

    @staticmethod
    def extract_likely_amounts(filename: str, text: str) -> list[float]:
        """
        Extract receipt totals aggressively enough to handle photographed
        restaurant receipts and handwritten tip/total lines.

        Key improvement:
        If OCR sees AMOUNT and TIP but misses the handwritten TOTAL, infer
        TOTAL = AMOUNT + TIP. Example: 239.35 + 60.00 = 299.35.
        """
        combined = f"{filename}\n{text or ''}".replace(",", "")
        values: list[float] = []

        def parse_money(raw: str | None) -> float | None:
            if raw is None:
                return None

            value = str(raw).strip()
            value = value.replace("$", "").replace(" ", "")
            value = value.replace("O", "0").replace("o", "0")
            value = value.replace("I", "1").replace("l", "1")
            value = value.replace(",", ".")

            # OCR sometimes returns 29935 instead of 299.35.
            if re.fullmatch(r"-?\d{3,6}", value):
                negative = value.startswith("-")
                digits = value.lstrip("-")
                value = (
                    ("-" if negative else "")
                    + digits[:-2]
                    + "."
                    + digits[-2:]
                )

            match = re.search(r"-?\d+(?:\.\d{1,2})?", value)
            if not match:
                return None

            try:
                return round(float(match.group(0)), 2)
            except ValueError:
                return None

        def add(value: float | None) -> None:
            if value is None or value == 0:
                return
            value = round(float(value), 2)
            if value not in values:
                values.append(value)

        money_token = (
            r"\$?\s*[-]?[0-9OoIl]{1,6}"
            r"(?:[\.,\s][0-9OoIl]{2})?"
        )

        def find_labeled(label_pattern: str) -> float | None:
            match = re.search(
                rf"(?is)\b(?:{label_pattern})\b\s*[:\-]?\s*({money_token})",
                combined,
            )
            return parse_money(match.group(1)) if match else None

        total = find_labeled(
            r"grand\s*total|total\s*paid|amount\s*paid|"
            r"total\s*charged|total"
        )
        amount = find_labeled(r"amount|subtotal|check\s*amount")
        tip = find_labeled(r"tip|gratuity")

        add(total)

        # This is the important restaurant-receipt fallback.
        if amount is not None and tip is not None:
            add(amount + tip)

        add(amount)
        add(tip)

        for label in (
            r"charged",
            r"payment",
            r"balance\s*due",
            r"amount\s*due",
        ):
            add(find_labeled(label))

        # Generic money candidates as a final fallback.
        for match in re.finditer(
            r"(?<!\d)(-?\d{1,6}(?:\.\d{2}))(?!\d)",
            combined,
        ):
            add(parse_money(match.group(1)))

        # Windows OCR and phone-gallery OCR commonly recognize handwritten
        # decimal totals as two groups, e.g. "299 35" instead of "299.35".
        # Treat a 1-6 digit group followed by exactly two digits as cents.
        for match in re.finditer(
            r"(?<!\d)(\d{1,6})[ \t]+(\d{2})(?!\d)",
            combined,
        ):
            try:
                whole = int(match.group(1))
                cents = int(match.group(2))
                if 0 <= cents <= 99:
                    add(float(f"{whole}.{cents:02d}"))
            except Exception:
                pass

        # OCR may also separate the amount with a newline:
        #     TOTAL:
        #     299 35
        # Keep this targeted to common receipt labels to avoid interpreting
        # dates or card digits as currency.
        labeled_spaced = re.finditer(
            r"(?is)\b(?:grand\s*total|total|amount\s*paid|"
            r"total\s*charged|amount|tip|gratuity)\b"
            r".{0,40}?\b(\d{1,6})[ \t]+(\d{2})\b",
            combined,
        )
        for match in labeled_spaced:
            try:
                add(
                    float(
                        f"{int(match.group(1))}."
                        f"{int(match.group(2)):02d}"
                    )
                )
            except Exception:
                pass

        return values

    def extract_embedded_text(self, path: str) -> str:
        file_path = Path(path)
        if file_path.suffix.lower() != ".pdf" or pdfplumber is None:
            return ""

        try:
            with pdfplumber.open(file_path) as pdf:
                # First page usually contains enough receipt information.
                first_page_text = pdf.pages[0].extract_text() or ""
                if len(first_page_text.strip()) >= 20:
                    return first_page_text

                return "\n".join(
                    page.extract_text() or ""
                    for page in pdf.pages
                )
        except Exception:
            return ""

    def extract_ocr_text(self, path: str) -> str:
        """
        Production receipt OCR pipeline.

        Images:
            OpenCV receipt detection/perspective correction
            -> PaddleOCR
            -> Tesseract fallback

        PDFs:
            Render PDF pages to images
            -> PaddleOCR
            -> searchable Tesseract OCR PDF fallback

        Normal embedded PDF text is still attempted earlier in the workflow,
        so this method is only reached for difficult files.
        """
        file_path = Path(path)
        extension = file_path.suffix.lower()

        if extension in self.IMAGE_EXTENSIONS:
            return self.extract_image_text(str(file_path))

        if extension != ".pdf":
            return ""

        # Try PaddleOCR against rendered PDF pages first.
        paddle_text = self.extract_pdf_text_with_paddle(file_path)
        if len(paddle_text.strip()) >= 20:
            return paddle_text

        # Fall back to the existing searchable-PDF/Tesseract path.
        if fitz is None or Image is None or pytesseract is None:
            return paddle_text

        try:
            searchable_pdf = self.ensure_searchable_ocr_pdf(file_path)
            if not searchable_pdf:
                return paddle_text

            tesseract_text = self.extract_text_from_searchable_pdf(
                searchable_pdf
            )

            if paddle_text and tesseract_text:
                return paddle_text + "\n" + tesseract_text

            return tesseract_text or paddle_text

        except Exception:
            return paddle_text

    @classmethod
    def extract_pdf_text_with_paddle(
        cls,
        pdf_path: str | Path,
    ) -> str:
        """
        Render PDF pages and OCR them with the same image-receipt pipeline.

        First page is attempted first because most receipts are one page.
        Remaining pages are only processed if needed.
        """
        if fitz is None or Image is None:
            return ""

        document = None

        try:
            document = fitz.open(pdf_path)

            if len(document) == 0:
                return ""

            texts = []

            first_image = cls._render_pdf_page_to_pil(document[0], 1.8)
            if first_image is not None:
                first_text = cls.extract_image_text_from_pil(first_image)
                if first_text:
                    texts.append(first_text)

                # Most receipts can be identified from page 1.
                if len(first_text.strip()) >= 30:
                    return first_text

            for page_index in range(1, len(document)):
                image = cls._render_pdf_page_to_pil(
                    document[page_index],
                    1.6,
                )
                if image is None:
                    continue

                page_text = cls.extract_image_text_from_pil(image)
                if page_text:
                    texts.append(page_text)

            return "\n".join(texts)

        except Exception:
            return ""

        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass

    @staticmethod
    def _render_pdf_page_to_pil(page, scale: float):
        if fitz is None or Image is None:
            return None

        try:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
            )

            return Image.frombytes(
                "RGB",
                [pixmap.width, pixmap.height],
                pixmap.samples,
            )

        except Exception:
            return None

    @classmethod
    def ensure_searchable_ocr_pdf(
        cls,
        pdf_path: str | Path,
    ) -> Path | None:
        """
        Converts a PDF into a searchable OCR PDF and caches the result next
        to the original cached attachment.

        Example:
            123_receipt.pdf
            123_receipt_OCR.pdf

        If the OCR copy already exists and is newer than the source PDF,
        it is reused immediately.
        """
        source_path = Path(pdf_path)

        if (
            source_path.suffix.lower() != ".pdf"
            or not source_path.exists()
            or fitz is None
            or Image is None
            or pytesseract is None
        ):
            return None

        # Do not repeatedly OCR a file that is already one of our OCR copies.
        if source_path.stem.endswith(OCR_PDF_SUFFIX):
            return source_path

        target_path = source_path.with_name(
            source_path.stem + OCR_PDF_SUFFIX + ".pdf"
        )

        try:
            if (
                target_path.exists()
                and target_path.stat().st_size > 0
                and target_path.stat().st_mtime >= source_path.stat().st_mtime
            ):
                return target_path
        except OSError:
            pass

        temporary_path = target_path.with_suffix(".pdf.part")

        source_document = None
        output_document = None

        try:
            source_document = fitz.open(source_path)

            if len(source_document) == 0:
                return None

            output_document = fitz.open()

            # Render every page and let Tesseract create a PDF page with a
            # searchable text layer. 1.8x is a useful accuracy/speed balance.
            for page in source_document:
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(1.8, 1.8),
                    alpha=False,
                )

                image = Image.frombytes(
                    "RGB",
                    [pixmap.width, pixmap.height],
                    pixmap.samples,
                )

                page_pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                    image,
                    extension="pdf",
                    config="--oem 3 --psm 6",
                )

                ocr_page = fitz.open(
                    "pdf",
                    page_pdf_bytes,
                )

                try:
                    output_document.insert_pdf(ocr_page)
                finally:
                    ocr_page.close()

            # PyMuPDF requires a normal PDF-looking temporary filename.
            temp_pdf_path = target_path.with_name(
                target_path.stem + "_building.pdf"
            )

            output_document.save(
                temp_pdf_path,
                garbage=4,
                deflate=True,
            )

            # Close before moving on Windows.
            output_document.close()
            output_document = None
            source_document.close()
            source_document = None

            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

            temp_pdf_path.replace(target_path)
            return target_path

        except Exception:
            return None

        finally:
            if output_document is not None:
                try:
                    output_document.close()
                except Exception:
                    pass

            if source_document is not None:
                try:
                    source_document.close()
                except Exception:
                    pass

            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def extract_text_from_searchable_pdf(
        pdf_path: str | Path,
    ) -> str:
        """
        Reads the OCR text layer from a searchable PDF.
        """
        if fitz is None:
            return ""

        document = None

        try:
            document = fitz.open(pdf_path)
            text_parts = []

            for page in document:
                text_parts.append(
                    page.get_text("text") or ""
                )

            return "\\n".join(text_parts)

        except Exception:
            return ""

        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass

    @classmethod
    def extract_image_text(cls, path: str) -> str:
        """
        OCR a native image attachment.

        OpenCV is used to isolate and flatten photographed receipts.
        PaddleOCR is the primary OCR engine.
        Tesseract remains a fallback and supplemental pass.
        """
        if Image is None:
            return ""

        try:
            image = Image.open(path)
            if image.mode != "RGB":
                image = image.convert("RGB")

            return cls.extract_image_text_from_pil(image)

        except Exception:
            return ""

    @classmethod
    def extract_image_text_from_pil(cls, image) -> str:
        if Image is None:
            return ""

        try:
            if image.mode != "RGB":
                image = image.convert("RGB")

            # Preserve detail but avoid huge phone-photo inference inputs.
            max_dimension = max(image.size)
            if max_dimension > 3400:
                ratio = 3400 / max_dimension
                image = image.resize(
                    (
                        max(1, int(image.width * ratio)),
                        max(1, int(image.height * ratio)),
                    ),
                    Image.Resampling.LANCZOS,
                )

            variants = cls.build_receipt_image_variants(image)

            outputs = []
            seen = set()

            # --------------------------------------------------------
            # 1. Windows OCR — first choice on Windows
            # --------------------------------------------------------
            # Microsoft Photos / Gallery text recognition can outperform
            # general-purpose OCR on difficult phone photos. We use the
            # Windows OCR API first when it is available.
            windows_variants = [
                item
                for item in variants
                if item[0] in {
                    "receipt_flattened",
                    "original",
                    "total_region",
                    "high_contrast",
                }
            ]

            for variant_name, variant in windows_variants:
                result = windows_ocr_pil(variant)
                normalized = result.strip()

                if normalized and normalized not in seen:
                    seen.add(normalized)
                    outputs.append(
                        f"\n--- Windows OCR {variant_name} ---\n"
                        f"{normalized}"
                    )

            # --------------------------------------------------------
            # 2. PaddleOCR — secondary OCR engine
            # --------------------------------------------------------
            paddle = get_paddle_ocr()

            if paddle is not None:
                for variant_name, variant in variants:
                    paddle_text = cls.run_paddle_ocr(
                        paddle,
                        variant,
                    )

                    normalized = paddle_text.strip()

                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        outputs.append(
                            f"\n--- PaddleOCR {variant_name} ---\n"
                            f"{normalized}"
                        )

                    # Good OCR from the perspective-corrected receipt is
                    # usually enough; still allow important focused regions
                    # through below.
                    if (
                        variant_name == "receipt_flattened"
                        and len(normalized) >= 120
                    ):
                        continue

            # --------------------------------------------------------
            # 3. Tesseract — fallback / supplemental OCR
            # --------------------------------------------------------
            if pytesseract is not None:
                tesseract_configs = [
                    "--oem 3 --psm 6",
                    "--oem 3 --psm 11",
                ]

                # Limit Tesseract to the most useful variants so this does
                # not become unnecessarily slow.
                tesseract_variants = [
                    item
                    for item in variants
                    if item[0] in {
                        "receipt_flattened",
                        "high_contrast",
                        "total_region",
                    }
                ]

                if not tesseract_variants:
                    tesseract_variants = variants[:2]

                for variant_name, variant in tesseract_variants:
                    for config in tesseract_configs:
                        try:
                            result = pytesseract.image_to_string(
                                variant,
                                config=config,
                            ) or ""
                        except Exception:
                            continue

                        normalized = result.strip()

                        if normalized and normalized not in seen:
                            seen.add(normalized)
                            outputs.append(
                                f"\n--- Tesseract {variant_name} "
                                f"{config} ---\n{normalized}"
                            )

            return "\n".join(outputs)

        except Exception:
            return ""

    @classmethod
    def build_receipt_image_variants(cls, image) -> list[tuple[str, object]]:
        """
        Create OCR-ready versions of a photographed receipt.

        OpenCV performs:
          - receipt contour detection
          - perspective correction
          - grayscale / CLAHE enhancement
          - adaptive thresholding

        PIL-only variants are retained when OpenCV is not installed.
        """
        variants = []

        # Always retain the source as a safety fallback.
        variants.append(("original", image))

        flattened = None

        if cv2 is not None and np is not None:
            try:
                rgb = np.array(image)
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

                flattened_bgr = cls.detect_and_flatten_receipt(bgr)

                if flattened_bgr is not None:
                    flattened_rgb = cv2.cvtColor(
                        flattened_bgr,
                        cv2.COLOR_BGR2RGB,
                    )

                    flattened = Image.fromarray(flattened_rgb)
                    variants.insert(
                        0,
                        ("receipt_flattened", flattened),
                    )

                    gray = cv2.cvtColor(
                        flattened_bgr,
                        cv2.COLOR_BGR2GRAY,
                    )

                    clahe = cv2.createCLAHE(
                        clipLimit=2.0,
                        tileGridSize=(8, 8),
                    )
                    enhanced = clahe.apply(gray)

                    enhanced = cv2.fastNlMeansDenoising(
                        enhanced,
                        None,
                        8,
                        7,
                        21,
                    )

                    variants.append(
                        (
                            "high_contrast",
                            Image.fromarray(enhanced),
                        )
                    )

                    threshold = cv2.adaptiveThreshold(
                        enhanced,
                        255,
                        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY,
                        31,
                        11,
                    )

                    variants.append(
                        (
                            "adaptive_threshold",
                            Image.fromarray(threshold),
                        )
                    )

            except Exception:
                flattened = None

        # PIL fallback enhancement if OpenCV could not isolate the receipt.
        base = flattened or image

        if ImageOps is not None and ImageEnhance is not None:
            try:
                gray = ImageOps.grayscale(base)
                gray = ImageOps.autocontrast(gray)
                gray = ImageEnhance.Contrast(gray).enhance(1.8)

                if ImageFilter is not None:
                    gray = gray.filter(ImageFilter.SHARPEN)

                variants.append(("pil_contrast", gray))
            except Exception:
                pass

        # Focused region for restaurant Amount / Tip / Total fields.
        try:
            width, height = base.size

            total_region = base.crop(
                (
                    int(width * 0.18),
                    int(height * 0.30),
                    int(width * 0.92),
                    int(height * 0.70),
                )
            )

            # Upscale the total area; isolated handwritten numbers are easier
            # to recognize when they occupy more pixels.
            total_region = total_region.resize(
                (
                    max(1, total_region.width * 2),
                    max(1, total_region.height * 2),
                ),
                Image.Resampling.LANCZOS,
            )

            if ImageOps is not None and ImageEnhance is not None:
                total_region = ImageOps.grayscale(total_region)
                total_region = ImageOps.autocontrast(total_region)
                total_region = ImageEnhance.Contrast(
                    total_region
                ).enhance(2.1)

            variants.append(("total_region", total_region))

        except Exception:
            pass

        # Remove exact duplicate dimensions/modes only when appropriate,
        # while keeping differently processed variants.
        return variants

    @staticmethod
    def detect_and_flatten_receipt(image_bgr):
        """
        Find the largest plausible four-corner receipt and perspective-warp
        it into a flat document image.

        If no reliable contour is found, return the original image.
        """
        if cv2 is None or np is None:
            return image_bgr

        try:
            original = image_bgr.copy()
            height, width = original.shape[:2]

            # Downscale only for contour detection.
            target_height = 900
            ratio = height / float(target_height)

            if height > target_height:
                detection = cv2.resize(
                    original,
                    (
                        int(width / ratio),
                        target_height,
                    ),
                )
            else:
                detection = original.copy()
                ratio = 1.0

            gray = cv2.cvtColor(
                detection,
                cv2.COLOR_BGR2GRAY,
            )
            gray = cv2.GaussianBlur(gray, (5, 5), 0)

            edges = cv2.Canny(gray, 50, 150)

            kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT,
                (5, 5),
            )
            edges = cv2.morphologyEx(
                edges,
                cv2.MORPH_CLOSE,
                kernel,
                iterations=2,
            )

            contours, _ = cv2.findContours(
                edges,
                cv2.RETR_LIST,
                cv2.CHAIN_APPROX_SIMPLE,
            )

            contours = sorted(
                contours,
                key=cv2.contourArea,
                reverse=True,
            )[:15]

            page_contour = None
            image_area = detection.shape[0] * detection.shape[1]

            for contour in contours:
                perimeter = cv2.arcLength(contour, True)
                approximation = cv2.approxPolyDP(
                    contour,
                    0.02 * perimeter,
                    True,
                )

                if len(approximation) != 4:
                    continue

                contour_area = cv2.contourArea(approximation)

                # Ignore tiny boxes that cannot plausibly be the receipt.
                if contour_area < image_area * 0.12:
                    continue

                page_contour = approximation.reshape(4, 2)
                break

            if page_contour is None:
                return original

            page_contour = page_contour.astype("float32") * ratio
            ordered = InvoiceLineDetailsPage.order_quad_points(
                page_contour
            )

            top_left, top_right, bottom_right, bottom_left = ordered

            width_a = np.linalg.norm(
                bottom_right - bottom_left
            )
            width_b = np.linalg.norm(
                top_right - top_left
            )
            max_width = int(max(width_a, width_b))

            height_a = np.linalg.norm(
                top_right - bottom_right
            )
            height_b = np.linalg.norm(
                top_left - bottom_left
            )
            max_height = int(max(height_a, height_b))

            if max_width < 100 or max_height < 150:
                return original

            destination = np.array(
                [
                    [0, 0],
                    [max_width - 1, 0],
                    [max_width - 1, max_height - 1],
                    [0, max_height - 1],
                ],
                dtype="float32",
            )

            transform = cv2.getPerspectiveTransform(
                ordered,
                destination,
            )

            warped = cv2.warpPerspective(
                original,
                transform,
                (max_width, max_height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )

            return warped

        except Exception:
            return image_bgr

    @staticmethod
    def order_quad_points(points):
        """
        Order points as:
        top-left, top-right, bottom-right, bottom-left.
        """
        rect = np.zeros((4, 2), dtype="float32")

        sums = points.sum(axis=1)
        rect[0] = points[np.argmin(sums)]
        rect[2] = points[np.argmax(sums)]

        differences = np.diff(points, axis=1).reshape(-1)
        rect[1] = points[np.argmin(differences)]
        rect[3] = points[np.argmax(differences)]

        return rect

    @staticmethod
    def run_paddle_ocr(paddle, image) -> str:
        """
        Run PaddleOCR and normalize both PaddleOCR 3.x and legacy 2.x output
        into a single newline-delimited string.
        """
        if paddle is None or image is None:
            return ""

        try:
            if image.mode != "RGB":
                image = image.convert("RGB")

            input_array = np.array(image) if np is not None else image

            # PaddleOCR 3.x
            if hasattr(paddle, "predict"):
                results = paddle.predict(input_array)
                texts = []

                for result in results:
                    payload = None

                    # PaddleX Result objects commonly expose .json.
                    try:
                        payload = getattr(result, "json", None)
                        if callable(payload):
                            payload = payload()
                    except Exception:
                        payload = None

                    if payload is None:
                        try:
                            payload = getattr(result, "res", None)
                        except Exception:
                            payload = None

                    if payload is None:
                        try:
                            payload = result.__dict__
                        except Exception:
                            payload = None

                    texts.extend(
                        InvoiceLineDetailsPage.extract_paddle_texts(
                            payload
                        )
                    )

                return "\n".join(
                    text
                    for text in texts
                    if str(text).strip()
                )

            # PaddleOCR 2.x
            if hasattr(paddle, "ocr"):
                results = paddle.ocr(input_array, cls=True)
                texts = []

                for page in results or []:
                    for item in page or []:
                        try:
                            text_value = item[1][0]
                        except Exception:
                            continue

                        if text_value:
                            texts.append(str(text_value))

                return "\n".join(texts)

        except Exception:
            return ""

        return ""

    @staticmethod
    def extract_paddle_texts(payload) -> list[str]:
        """
        Recursively locate PaddleOCR recognition text arrays/fields.
        """
        texts = []

        if payload is None:
            return texts

        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == "rec_texts" and isinstance(
                    value,
                    (list, tuple),
                ):
                    texts.extend(str(item) for item in value if item)
                    continue

                if key == "rec_text" and isinstance(value, str):
                    if value:
                        texts.append(value)
                    continue

                texts.extend(
                    InvoiceLineDetailsPage.extract_paddle_texts(
                        value
                    )
                )

        elif isinstance(payload, (list, tuple)):
            for item in payload:
                texts.extend(
                    InvoiceLineDetailsPage.extract_paddle_texts(
                        item
                    )
                )

        return texts

    def _analysis_succeeded(
        self,
        lines: list[dict],
        unmatched: list[dict],
    ) -> None:
        self.loading = False

        lines, unmatched = self._apply_saved_manual_assignments(
            lines,
            unmatched,
        )

        self.lines = lines
        self.unmatched_attachments = unmatched
        self.attachments = [
            attachment
            for line in lines
            for attachment in line.get("attachments", [])
        ] + unmatched

        self._populate_line_table()

        self._refresh_detail_counts()
        self.status_label.configure(
            text="Attachment analysis complete."
        )
        self._populate_unmatched_links()

    def _populate_line_table(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        self.line_records.clear()

        for index, line in enumerate(self.lines, start=1):
            attachments = line.get("attachments", [])
            first = attachments[0] if attachments else None

            if first:
                attachment_name = first.get("file_name", "Open attachment")
                status = (
                    "Matched"
                    if len(attachments) == 1
                    else f"{len(attachments)} attachments"
                )
            else:
                attachment_name = "Missing Attachment"
                status = "Missing attachment"

            item_id = self.tree.insert(
                "",
                "end",
                values=(
                    line.get("line_number", index),
                    line.get("description", ""),
                    self.format_money(line.get("amount")),
                    attachment_name,
                    status,
                ),
            )
            self.line_records[item_id] = line

        self.update_attachment_button()

    def _populate_unmatched_links(self) -> None:
        for widget in self.unmatched_links.winfo_children():
            widget.destroy()

        self.unmatched_attachment_buttons = {}
        self.selected_unmatched_attachment = None
        self.assign_attachment_button.configure(state="disabled")

        if not self.unmatched_attachments:
            self.unmatched_frame.grid_remove()
            return

        self.unmatched_frame.grid()

        for attachment in self.unmatched_attachments:
            attachment_id = str(
                attachment.get("id")
                or attachment.get("Id")
                or attachment.get("file_name")
                or id(attachment)
            )

            row = ctk.CTkFrame(
                self.unmatched_links,
                corner_radius=8,
            )
            row.pack(
                fill="x",
                padx=2,
                pady=3,
            )
            row.grid_columnconfigure(0, weight=1)

            select_button = ctk.CTkButton(
                row,
                text=attachment.get(
                    "file_name",
                    "Unnamed attachment",
                ),
                anchor="w",
                fg_color="transparent",
                hover_color=("gray82", "gray28"),
                text_color=("blue3", "sky blue"),
                command=lambda item=attachment:
                self.select_unmatched_attachment(item),
            )
            select_button.grid(
                row=0,
                column=0,
                padx=(4, 8),
                pady=4,
                sticky="ew",
            )

            open_button = ctk.CTkButton(
                row,
                text="Open",
                width=70,
                fg_color="transparent",
                border_width=1,
                text_color=("gray15", "gray90"),
                command=lambda item=attachment:
                self._open_attachment(item),
            )
            open_button.grid(
                row=0,
                column=1,
                padx=(0, 4),
                pady=4,
            )

            self.unmatched_attachment_buttons[
                attachment_id
            ] = select_button

    def select_unmatched_attachment(
        self,
        attachment: dict,
    ) -> None:
        self.selected_unmatched_attachment = attachment

        selected_id = str(
            attachment.get("id")
            or attachment.get("Id")
            or attachment.get("file_name")
            or id(attachment)
        )

        for attachment_id, button in (
            self.unmatched_attachment_buttons.items()
        ):
            if attachment_id == selected_id:
                button.configure(
                    fg_color=("gray75", "gray30"),
                    text_color=("gray10", "gray95"),
                )
            else:
                button.configure(
                    fg_color="transparent",
                    text_color=("blue3", "sky blue"),
                )

        self._update_assign_button_state()

    def _update_assign_button_state(self) -> None:
        selection = self.tree.selection()

        if (
            not selection
            or self.selected_unmatched_attachment is None
        ):
            self.assign_attachment_button.configure(
                state="disabled"
            )
            return

        line = self.line_records.get(selection[0], {})

        # Manual assignment is intended for a line that currently has
        # no supporting attachment.
        if line.get("attachments"):
            self.assign_attachment_button.configure(
                state="disabled"
            )
            return

        self.assign_attachment_button.configure(
            state="normal"
        )

    def assign_selected_attachment_to_line(self) -> None:
        selection = self.tree.selection()

        if not selection:
            messagebox.showinfo(
                "Select an invoice line",
                (
                    "Select the invoice line that should receive "
                    "the unmatched receipt."
                ),
            )
            return

        attachment = self.selected_unmatched_attachment

        if attachment is None:
            messagebox.showinfo(
                "Select a receipt",
                "Select an unmatched receipt first.",
            )
            return

        item_id = selection[0]
        line = self.line_records.get(item_id)

        if not line:
            return

        if line.get("attachments"):
            messagebox.showwarning(
                "Attachment already assigned",
                (
                    "The selected invoice line already has an attachment. "
                    "Select a line marked Missing Attachment."
                ),
            )
            return

        attachment_name = attachment.get(
            "file_name",
            "Unnamed attachment",
        )
        line_number = line.get("line_number", "")
        line_amount = self.format_money(
            line.get("amount")
        )

        confirmed = messagebox.askyesno(
            "Assign receipt",
            (
                f"Assign this receipt:\n\n"
                f"{attachment_name}\n\n"
                f"to invoice line {line_number} "
                f"({line_amount})?"
            ),
        )

        if not confirmed:
            return

        line.setdefault("attachments", []).append(
            attachment
        )

        self.unmatched_attachments = [
            item
            for item in self.unmatched_attachments
            if not self._same_attachment(
                item,
                attachment,
            )
        ]

        self._save_manual_assignment(
            attachment,
            line,
        )

        self.selected_unmatched_attachment = None

        self._populate_line_table()
        self._populate_unmatched_links()
        self._refresh_detail_counts()

        self.status_label.configure(
            text=(
                f"Receipt manually assigned to line "
                f"{line_number}."
            )
        )

    def _refresh_detail_counts(self) -> None:
        matched_count = sum(
            len(line.get("attachments", []))
            for line in self.lines
        )

        self.count_label.configure(
            text=(
                f"{len(self.lines)} lines • "
                f"{matched_count} matched • "
                f"{len(self.unmatched_attachments)} unmatched"
            )
        )

        self.export_zip_button.configure(
            state="normal"
            if self.attachments
            else "disabled"
        )

    def _save_manual_assignment(
        self,
        attachment: dict,
        line: dict,
    ) -> None:
        attachment_id = self._attachment_key(
            attachment
        )
        line_key = self._line_key(line)

        if not attachment_id or not line_key:
            return

        with sqlite3.connect(CACHE_DB) as connection:
            connection.execute(
                """
                INSERT INTO manual_attachment_assignments (
                    invoice_id,
                    attachment_id,
                    line_key,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(invoice_id, attachment_id)
                DO UPDATE SET
                    line_key = excluded.line_key,
                    updated_at = excluded.updated_at
                """,
                (
                    str(self.invoice_id),
                    attachment_id,
                    line_key,
                    int(time.time()),
                ),
            )
            connection.commit()

    def _apply_saved_manual_assignments(
        self,
        lines: list[dict],
        unmatched: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        if not lines or not unmatched:
            return lines, unmatched

        with sqlite3.connect(CACHE_DB) as connection:
            rows = connection.execute(
                """
                SELECT attachment_id, line_key
                FROM manual_attachment_assignments
                WHERE invoice_id = ?
                """,
                (str(self.invoice_id),),
            ).fetchall()

        if not rows:
            return lines, unmatched

        assignment_map = {
            str(attachment_id): str(line_key)
            for attachment_id, line_key in rows
        }

        line_map = {
            self._line_key(line): line
            for line in lines
        }

        remaining = []

        for attachment in unmatched:
            attachment_key = self._attachment_key(
                attachment
            )
            saved_line_key = assignment_map.get(
                attachment_key
            )

            target_line = line_map.get(saved_line_key)

            if (
                target_line is None
                or target_line.get("attachments")
            ):
                remaining.append(attachment)
                continue

            target_line.setdefault(
                "attachments",
                [],
            ).append(attachment)

        assigned_ids = {
            self._attachment_key(attachment)
            for line in lines
            for attachment in line.get(
                "attachments",
                [],
            )
        }

        remaining = [
            attachment
            for attachment in remaining
            if self._attachment_key(attachment)
            not in assigned_ids
        ]

        return lines, remaining

    @staticmethod
    def _attachment_key(
        attachment: dict,
    ) -> str:
        return str(
            attachment.get("id")
            or attachment.get("Id")
            or attachment.get("file_name")
            or ""
        ).strip()

    @staticmethod
    def _line_key(line: dict) -> str:
        line_id = str(
            line.get("line_id")
            or ""
        ).strip()

        if line_id:
            return f"id:{line_id}"

        return (
            f"line:{line.get('line_number', '')}|"
            f"amount:{float(line.get('amount') or 0):.2f}|"
            f"description:{line.get('description', '')}"
        )

    @staticmethod
    def _same_attachment(
        left: dict,
        right: dict,
    ) -> bool:
        left_key = (
            left.get("id")
            or left.get("Id")
            or left.get("file_name")
        )
        right_key = (
            right.get("id")
            or right.get("Id")
            or right.get("file_name")
        )
        return str(left_key) == str(right_key)

    def _load_failed(self, message: str) -> None:
        self.loading = False
        self.status_label.configure(text="Could not load invoice details.")
        messagebox.showerror("Invoice detail loading unsuccessful", message)

    def update_attachment_button(self, _event=None) -> None:
        selection = self.tree.selection()

        if not selection:
            self.open_attachment_button.configure(
                state="disabled"
            )
            self._update_assign_button_state()
            return

        line = self.line_records.get(
            selection[0],
            {},
        )

        self.open_attachment_button.configure(
            state="normal"
            if line.get("attachments")
            else "disabled"
        )

        self._update_assign_button_state()

    def open_selected_attachment(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return

        line = self.line_records.get(selection[0], {})
        attachments = line.get("attachments", [])

        if not attachments:
            messagebox.showinfo(
                "Missing attachment",
                "No attachment was matched to this invoice line.",
            )
            return

        if len(attachments) == 1:
            self._open_attachment(attachments[0])
            return

        menu = tk.Menu(self, tearoff=False)
        for attachment in attachments:
            menu.add_command(
                label=attachment.get("file_name", "Attachment"),
                command=lambda item=attachment: self._open_attachment(item),
            )

        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    @staticmethod
    def _open_attachment(attachment: dict) -> None:
        cached_path = attachment.get("cached_path")
        if cached_path and Path(cached_path).exists():
            try:
                os.startfile(cached_path)
                return
            except OSError:
                pass

        open_url = attachment.get("open_url")
        if not open_url:
            messagebox.showerror(
                "Attachment unavailable",
                "The attachment link is unavailable.",
            )
            return

        if not webbrowser.open(open_url):
            messagebox.showerror(
                "Could not open attachment",
                "The attachment could not be opened.",
            )

    def export_all_attachments_zip(self) -> None:
        if not self.attachments:
            return

        output_path = filedialog.asksaveasfilename(
            title="Save invoice attachments ZIP",
            defaultextension=".zip",
            initialfile=f"Invoice_{self.doc_number}_Attachments.zip",
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")],
        )

        if not output_path:
            return

        self.export_zip_button.configure(text="Building ZIP...", state="disabled")
        self.status_label.configure(
            text="Renaming and packaging cached attachments..."
        )

        threading.Thread(
            target=self._export_zip_worker,
            args=(output_path,),
            daemon=True,
        ).start()

    def _export_zip_worker(self, output_path: str) -> None:
        try:
            attachment_to_line = {}

            for line in self.lines:
                for attachment in line.get("attachments", []):
                    attachment_to_line[str(attachment.get("id"))] = line

            prepared_files = []

            for attachment in self.attachments:
                cached_path = Path(
                    attachment.get("cached_path")
                    or self._ensure_cached_attachment(attachment)
                )

                local_path = cached_path
                matched_line = attachment_to_line.get(
                    str(attachment.get("id"))
                )
                extension = local_path.suffix.lower()

                # If this PDF had to go through Scan & OCR during matching,
                # export the searchable OCR copy instead of the original.
                if extension == ".pdf":
                    ocr_candidate = local_path.with_name(
                        local_path.stem + OCR_PDF_SUFFIX + ".pdf"
                    )
                    if (
                        ocr_candidate.exists()
                        and ocr_candidate.stat().st_size > 0
                    ):
                        local_path = ocr_candidate
                        extension = ".pdf"

                if extension in self.IMAGE_EXTENSIONS:
                    converted_path = self.convert_image_to_pdf(str(local_path))
                    if converted_path:
                        local_path = Path(converted_path)
                        extension = ".pdf"

                if matched_line:
                    new_name = self.sanitize_filename(
                        (
                            f"{matched_line.get('line_number')} - "
                            f"${float(matched_line.get('amount') or 0):.2f} - "
                            f"{matched_line.get('description', '')}"
                            f"{extension}"
                        )
                    )
                else:
                    new_name = self.sanitize_filename(
                        f"REVIEW - {attachment.get('file_name', local_path.name)}"
                    )
                    if not Path(new_name).suffix:
                        new_name += extension

                prepared_files.append((local_path, new_name))

            missing_lines = [
                line for line in self.lines
                if not line.get("attachments")
            ]

            with zipfile.ZipFile(
                output_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                used_names = set()

                for local_path, desired_name in prepared_files:
                    archive_name = self.unique_archive_name(
                        desired_name,
                        used_names,
                    )
                    archive.write(local_path, archive_name)

                if missing_lines:
                    report = [
                        f"Invoice {self.doc_number} - Missing Attachments",
                        "=" * 65,
                        "",
                    ]

                    for line in missing_lines:
                        report.append(
                            (
                                f"Line {line.get('line_number')} | "
                                f"${float(line.get('amount') or 0):.2f} | "
                                f"{line.get('description', '')}"
                            )
                        )

                    archive.writestr(
                        self.sanitize_filename(
                            f"{self.doc_number} - Missing Attachments.txt"
                        ),
                        "\n".join(report),
                    )

            self.after(
                0,
                lambda path=output_path: self._export_succeeded(path),
            )

        except Exception as exc:
            self.after(0, lambda error=exc: self._export_failed(str(error)))

    @classmethod
    def convert_image_to_pdf(cls, image_path: str) -> str | None:
        if Image is None:
            return None

        try:
            image = Image.open(image_path)
            frames = []

            frame_count = getattr(image, "n_frames", 1)
            for frame_index in range(frame_count):
                image.seek(frame_index)
                frame = image.copy()

                if frame.mode != "RGB":
                    frame = frame.convert("RGB")

                frames.append(frame)

            source = Path(image_path)
            pdf_path = source.with_name(source.stem + "_converted.pdf")

            # Reuse an existing conversion.
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return str(pdf_path)

            if len(frames) > 1:
                frames[0].save(
                    pdf_path,
                    "PDF",
                    save_all=True,
                    append_images=frames[1:],
                    resolution=100.0,
                )
            else:
                frames[0].save(
                    pdf_path,
                    "PDF",
                    resolution=100.0,
                )

            return str(pdf_path)
        except Exception:
            return None

    def _export_succeeded(self, output_path: str) -> None:
        self.export_zip_button.configure(text="Export All as ZIP", state="normal")
        self.status_label.configure(text="Attachment ZIP exported successfully.")
        messagebox.showinfo(
            "Export complete",
            f"The attachments were saved to:\n\n{output_path}",
        )

    def _export_failed(self, message: str) -> None:
        self.export_zip_button.configure(text="Export All as ZIP", state="normal")
        self.status_label.configure(text="Could not export the attachment ZIP.")
        messagebox.showerror("ZIP export unsuccessful", message)

    def _set_status_threadsafe(self, text: str) -> None:
        self.after(
            0,
            lambda value=text: self.status_label.configure(text=value),
        )

    @staticmethod
    def normalize(value: str) -> str:
        return " ".join(
            re.sub(
                r"[^a-z0-9]+",
                " ",
                str(value or "").lower(),
            ).split()
        )

    @staticmethod
    def sanitize_filename(value: str) -> str:
        cleaned = re.sub(
            r'[<>:"/\\|?*\x00-\x1F]',
            "_",
            str(value or "").strip(),
        )
        return cleaned.rstrip(". ").strip() or "attachment"

    @staticmethod
    def unique_archive_name(filename: str, used_names: set[str]) -> str:
        if filename not in used_names:
            used_names.add(filename)
            return filename

        path = Path(filename)
        counter = 2

        while True:
            candidate = f"{path.stem} ({counter}){path.suffix}"
            if candidate not in used_names:
                used_names.add(candidate)
                return candidate
            counter += 1

    @staticmethod
    def format_money(value) -> str:
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return ""



# ============================================================
# REPORTS PAGE
# ============================================================

class ReportsPage(Page):
    """
    Phase 1 report:
        Analyze All Pending Invoices

    The report processes every invoice returned by /invoices/pending and
    summarizes attachment coverage at the invoice level.
    """

    COLUMNS = (
        "invoice",
        "customer",
        "total",
        "balance",
        "lines",
        "matched",
        "missing",
        "unmatched",
        "coverage",
        "status",
    )

    HEADINGS = {
        "invoice": "Invoice #",
        "customer": "Customer",
        "total": "Total",
        "balance": "Balance",
        "lines": "Lines",
        "matched": "Matched",
        "missing": "Missing",
        "unmatched": "Unmatched Files",
        "coverage": "Coverage",
        "status": "Status",
    }

    WIDTHS = {
        "invoice": 95,
        "customer": 240,
        "total": 105,
        "balance": 105,
        "lines": 70,
        "matched": 80,
        "missing": 80,
        "unmatched": 110,
        "coverage": 95,
        "status": 135,
    }

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)

        self.running = False
        self.results: list[dict] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self,
            text="Reports",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        ).grid(
            row=0,
            column=0,
            padx=8,
            pady=(10, 5),
            sticky="ew",
        )

        ctk.CTkLabel(
            self,
            text="Batch analysis and audit reports for QuickBooks invoices.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(
            row=1,
            column=0,
            padx=8,
            pady=(0, 16),
            sticky="ew",
        )

        action_card = SectionCard(
            self,
            "Analyze All Pending Invoices",
            (
                "Analyze every pending invoice, match receipt attachments to "
                "invoice lines, and identify missing supporting documents."
            ),
        )
        action_card.grid(
            row=2,
            column=0,
            padx=8,
            pady=(0, 12),
            sticky="ew",
        )
        action_card.grid_columnconfigure(0, weight=1)

        button_row = ctk.CTkFrame(action_card, fg_color="transparent")
        button_row.grid(
            row=2,
            column=0,
            padx=22,
            pady=(4, 10),
            sticky="ew",
        )
        button_row.grid_columnconfigure(0, weight=1)

        self.analyze_button = ctk.CTkButton(
            button_row,
            text="Analyze All Pending Invoices",
            width=210,
            height=40,
            command=self.analyze_all,
        )
        self.analyze_button.grid(row=0, column=0, sticky="w")

        self.export_button = ctk.CTkButton(
            button_row,
            text="Export Report to Excel",
            width=170,
            height=40,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.export_report,
            state="disabled",
        )
        self.export_button.grid(row=0, column=1, padx=(10, 0), sticky="e")

        self.progress = ctk.CTkProgressBar(action_card)
        self.progress.grid(
            row=3,
            column=0,
            padx=22,
            pady=(0, 8),
            sticky="ew",
        )
        self.progress.set(0)

        self.progress_label = ctk.CTkLabel(
            action_card,
            text="Ready",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.progress_label.grid(
            row=4,
            column=0,
            padx=22,
            pady=(0, 18),
            sticky="ew",
        )

        self.summary_frame = ctk.CTkFrame(self, corner_radius=12)
        self.summary_frame.grid(
            row=3,
            column=0,
            padx=8,
            pady=(0, 12),
            sticky="ew",
        )
        self.summary_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.invoice_count_card = StatCard(
            self.summary_frame,
            "Invoices",
            "0",
            "Not analyzed",
        )
        self.invoice_count_card.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")

        self.complete_card = StatCard(
            self.summary_frame,
            "Complete",
            "0",
            "No missing lines",
        )
        self.complete_card.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")

        self.missing_card = StatCard(
            self.summary_frame,
            "Missing receipts",
            "0",
            "Invoice lines",
        )
        self.missing_card.grid(row=0, column=2, padx=6, pady=6, sticky="nsew")

        self.coverage_card = StatCard(
            self.summary_frame,
            "Overall coverage",
            "—",
            "Matched lines",
        )
        self.coverage_card.grid(row=0, column=3, padx=6, pady=6, sticky="nsew")

        table_card = ctk.CTkFrame(self, corner_radius=14)
        table_card.grid(
            row=4,
            column=0,
            padx=8,
            pady=(0, 8),
            sticky="nsew",
        )
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Report.Treeview",
            rowheight=32,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Report.Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
        )

        self.tree = ttk.Treeview(
            table_card,
            columns=self.COLUMNS,
            show="headings",
            style="Report.Treeview",
            selectmode="browse",
        )

        for column in self.COLUMNS:
            self.tree.heading(column, text=self.HEADINGS[column])
            self.tree.column(
                column,
                width=self.WIDTHS[column],
                minwidth=60,
                anchor="e" if column in {"total", "balance"} else "w",
                stretch=column == "customer",
            )

        vscroll = ttk.Scrollbar(
            table_card,
            orient="vertical",
            command=self.tree.yview,
        )
        hscroll = ttk.Scrollbar(
            table_card,
            orient="horizontal",
            command=self.tree.xview,
        )

        self.tree.configure(
            yscrollcommand=vscroll.set,
            xscrollcommand=hscroll.set,
        )
        self.tree.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(12, 0),
            pady=(12, 0),
        )
        vscroll.grid(
            row=0,
            column=1,
            sticky="ns",
            padx=(0, 12),
            pady=(12, 0),
        )
        hscroll.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=(12, 0),
            pady=(0, 12),
        )

        self.tree.bind("<Double-1>", self.open_selected_invoice)

    def analyze_all(self) -> None:
        if self.running:
            return

        if not self.app.settings.qbo_connected:
            messagebox.showwarning(
                "QuickBooks connection required",
                "Open Settings → QuickBooks and connect a company first.",
            )
            return

        self.running = True
        self.results = []
        self.analyze_button.configure(
            text="Analyzing...",
            state="disabled",
        )
        self.export_button.configure(state="disabled")
        self.progress.set(0)
        self.progress_label.configure(text="Loading pending invoices...")

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        threading.Thread(
            target=self._batch_worker,
            daemon=True,
        ).start()

    def _batch_worker(self) -> None:
        try:
            response = HTTP_SESSION.get(
                f"{RENDER_AUTH_BASE_URL}/invoices/pending",
                timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            invoices = (
                payload
                if isinstance(payload, list)
                else payload.get("invoices", [])
            )

            invoices = [
                invoice
                for invoice in invoices
                if invoice.get("PrintStatus") == "NeedToPrint"
                and invoice.get("EmailStatus") == "NotSet"
                and float(invoice.get("Balance") or 0) > 0
            ]

            if not invoices:
                self.after(0, self._no_invoices)
                return

            results = []
            total = len(invoices)

            for index, invoice in enumerate(invoices, start=1):
                doc_number = str(invoice.get("DocNumber") or invoice.get("Id") or "")
                invoice_id = str(invoice.get("Id") or "")

                self.after(
                    0,
                    lambda current=index, count=total, doc=doc_number:
                    self._update_batch_progress(
                        current - 1,
                        count,
                        f"Analyzing invoice {doc} ({current} of {count})...",
                    ),
                )

                try:
                    result = self._analyze_invoice(invoice_id, invoice)
                except Exception as exc:
                    customer_ref = invoice.get("CustomerRef") or {}
                    result = {
                        "invoice_id": invoice_id,
                        "invoice": doc_number,
                        "customer": (
                            customer_ref.get("name")
                            or customer_ref.get("value")
                            or ""
                        ),
                        "total": float(invoice.get("TotalAmt") or 0),
                        "balance": float(invoice.get("Balance") or 0),
                        "lines": 0,
                        "matched": 0,
                        "missing": 0,
                        "unmatched": 0,
                        "coverage": 0.0,
                        "status": f"Error: {exc}",
                    }

                results.append(result)

                self.after(
                    0,
                    lambda current=index, count=total, row=result:
                    self._append_result(current, count, row),
                )

            self.after(
                0,
                lambda final_results=results:
                self._batch_complete(final_results),
            )

        except Exception as exc:
            self.after(
                0,
                lambda error=exc:
                self._batch_failed(str(error)),
            )

    def _analyze_invoice(
        self,
        invoice_id: str,
        invoice_summary: dict,
    ) -> dict:
        response = HTTP_SESSION.get(
            (
                f"{RENDER_AUTH_BASE_URL}/invoices/"
                f"{quote(invoice_id, safe='')}/detail"
            ),
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()

        invoice = payload.get("invoice", invoice_summary)
        raw_lines = payload.get("lines", [])

        # Support both versions of the Render detail endpoint.
        #
        # Newer backend:
        #     {"attachments": [...]}
        #
        # Older/current backend:
        #     attachments are split between each line's "attachments"
        #     list and "unmatched_attachments".
        #
        # The invoice detail screen already handles both formats. The
        # original Reports implementation did not, which caused the batch
        # report to analyze zero attachment files and report 0% coverage.
        attachments = payload.get("attachments")

        if attachments is None:
            attachments = list(
                payload.get("unmatched_attachments", []) or []
            )

            seen_attachment_ids: set[str] = {
                str(item.get("id") or item.get("Id") or "")
                for item in attachments
            }

            for line in raw_lines:
                for attachment in line.get("attachments", []) or []:
                    attachment_id = str(
                        attachment.get("id")
                        or attachment.get("Id")
                        or ""
                    )

                    if attachment_id and attachment_id in seen_attachment_ids:
                        continue

                    attachments.append(attachment)

                    if attachment_id:
                        seen_attachment_ids.add(attachment_id)

        attachments = attachments or []

        lines = [
            {
                **line,
                "attachments": [],
            }
            for line in raw_lines
        ]

        prepared = self._prepare_batch_attachments(
            invoice_id,
            attachments,
        )

        detail_page: InvoiceLineDetailsPage = self.app.pages["invoice_detail"]

        lines, unmatched = self._batch_match_pass(
            detail_page,
            lines,
            prepared,
            use_text=False,
        )

        for attachment in unmatched:
            attachment["text"] = self._batch_embedded_text(
                detail_page,
                attachment,
            )

        lines, unmatched = self._batch_match_pass(
            detail_page,
            lines,
            unmatched,
            use_text=True,
        )

        for attachment in unmatched:
            attachment["text"] = self._batch_ocr_text(
                detail_page,
                attachment,
            )

        lines, unmatched = self._batch_match_pass(
            detail_page,
            lines,
            unmatched,
            use_text=True,
        )

        matched_lines = sum(
            1
            for line in lines
            if line.get("attachments")
        )
        line_count = len(lines)
        missing_lines = max(0, line_count - matched_lines)
        coverage = (
            (matched_lines / line_count) * 100
            if line_count
            else 100.0
        )

        customer_ref = invoice.get("CustomerRef") or {}

        return {
            "invoice_id": invoice_id,
            "invoice": str(invoice.get("DocNumber") or invoice_id),
            "customer": (
                customer_ref.get("name")
                or customer_ref.get("value")
                or ""
            ),
            "total": float(invoice.get("TotalAmt") or 0),
            "balance": float(invoice.get("Balance") or 0),
            "lines": line_count,
            "matched": matched_lines,
            "missing": missing_lines,
            "unmatched": len(unmatched),
            "attachment_count": len(attachments),
            "coverage": coverage,
            "status": (
                "No QBO attachments"
                if not attachments and line_count > 0
                else "Complete"
                if missing_lines == 0
                else "Needs review"
            ),
        }

    def _prepare_batch_attachments(
        self,
        invoice_id: str,
        attachments: list[dict],
    ) -> list[dict]:
        if not attachments:
            return []

        prepared = []

        with ThreadPoolExecutor(
            max_workers=InvoiceLineDetailsPage.MAX_DOWNLOAD_WORKERS
        ) as executor:
            futures = {
                executor.submit(
                    self._ensure_batch_cached_attachment,
                    invoice_id,
                    attachment,
                ): attachment
                for attachment in attachments
            }

            for future in as_completed(futures):
                attachment = dict(futures[future])
                local_path = future.result()
                attachment["cached_path"] = str(local_path)
                attachment["file_size"] = local_path.stat().st_size
                attachment["text"] = ""
                prepared.append(attachment)

        order = {
            str(item.get("id")): index
            for index, item in enumerate(attachments)
        }
        prepared.sort(
            key=lambda item: order.get(
                str(item.get("id")),
                999999,
            )
        )
        return prepared

    @staticmethod
    def _ensure_batch_cached_attachment(
        invoice_id: str,
        attachment: dict,
    ) -> Path:
        attachment_id = str(attachment.get("id") or "").strip()
        filename = InvoiceLineDetailsPage.sanitize_filename(
            attachment.get("file_name")
            or f"attachment_{attachment_id}"
        )

        invoice_cache = ATTACHMENT_CACHE_DIR / str(invoice_id)
        invoice_cache.mkdir(parents=True, exist_ok=True)

        cache_name = InvoiceLineDetailsPage.sanitize_filename(
            f"{attachment_id}_{filename}"
        )
        cache_path = invoice_cache / cache_name

        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path

        download_url = attachment.get("download_url") or (
            f"{RENDER_AUTH_BASE_URL}/attachments/"
            f"{quote(attachment_id, safe='')}/download"
        )

        temp_path = cache_path.with_suffix(
            cache_path.suffix + ".part"
        )

        response = HTTP_SESSION.get(
            download_url,
            stream=True,
            timeout=180,
        )
        response.raise_for_status()

        with open(temp_path, "wb") as output_file:
            for chunk in response.iter_content(1024 * 256):
                if chunk:
                    output_file.write(chunk)

        temp_path.replace(cache_path)
        return cache_path

    @staticmethod
    def _batch_match_pass(
        detail_page: "InvoiceLineDetailsPage",
        lines: list[dict],
        attachments: list[dict],
        use_text: bool,
    ) -> tuple[list[dict], list[dict]]:
        remaining_indexes = [
            index
            for index, line in enumerate(lines)
            if not line.get("attachments")
        ]
        unmatched = []

        for attachment in attachments:
            filename = attachment.get("file_name", "")
            attachment_text = (
                attachment.get("text", "")
                if use_text
                else ""
            )
            amounts = detail_page.extract_likely_amounts(
                filename,
                attachment_text,
            )

            candidates = []

            for line_index in remaining_indexes:
                line = lines[line_index]
                score = detail_page.score_match(
                    filename,
                    attachment_text,
                    amounts,
                    line,
                )
                candidates.append((score, line_index))

            candidates.sort(reverse=True)

            if candidates:
                best_score, best_index = candidates[0]
                second_score = (
                    candidates[1][0]
                    if len(candidates) > 1
                    else 0
                )
                gap = best_score - second_score

                if use_text:
                    accepted = (
                        best_score >= 105
                        or (best_score >= 85 and gap >= 15)
                        or (best_score >= 65 and gap >= 30)
                    )
                else:
                    accepted = (
                        best_score >= 125
                        or (best_score >= 110 and gap >= 20)
                    )

                if accepted:
                    lines[best_index]["attachments"].append(
                        attachment
                    )
                    remaining_indexes.remove(best_index)
                    continue

            unmatched.append(attachment)

        return lines, unmatched

    @staticmethod
    def _batch_embedded_text(
        detail_page: "InvoiceLineDetailsPage",
        attachment: dict,
    ) -> str:
        cached = detail_page._read_text_cache(attachment)

        if cached and cached.get("embedded_text"):
            return cached["embedded_text"]

        embedded = detail_page.extract_embedded_text(
            attachment.get("cached_path", "")
        )

        detail_page._write_text_cache(
            attachment,
            embedded_text=embedded,
            ocr_text=(cached or {}).get("ocr_text", ""),
        )
        return embedded

    @staticmethod
    def _batch_ocr_text(
        detail_page: "InvoiceLineDetailsPage",
        attachment: dict,
    ) -> str:
        cached = detail_page._read_text_cache(attachment)

        if cached and cached.get("ocr_text"):
            return cached["ocr_text"]

        embedded = (cached or {}).get("embedded_text", "")
        ocr_text = detail_page.extract_ocr_text(
            attachment.get("cached_path", "")
        )

        detail_page._write_text_cache(
            attachment,
            embedded_text=embedded,
            ocr_text=ocr_text,
        )
        return ocr_text or embedded

    def _update_batch_progress(
        self,
        completed: int,
        total: int,
        message: str,
    ) -> None:
        self.progress.set(
            completed / total
            if total
            else 0
        )
        self.progress_label.configure(text=message)

    def _append_result(
        self,
        completed: int,
        total: int,
        result: dict,
    ) -> None:
        self.tree.insert(
            "",
            "end",
            values=(
                result["invoice"],
                result["customer"],
                f"${result['total']:,.2f}",
                f"${result['balance']:,.2f}",
                result["lines"],
                result["matched"],
                result["missing"],
                result["unmatched"],
                f"{result['coverage']:.0f}%",
                result.get("status", ""),
            ),
        )

        self.progress.set(completed / total)

    def _batch_complete(self, results: list[dict]) -> None:
        self.running = False
        self.results = results

        invoice_count = len(results)
        complete_count = sum(
            1
            for item in results
            if item.get("missing", 0) == 0
            and not str(item.get("status", "")).startswith("Error")
        )
        total_lines = sum(item.get("lines", 0) for item in results)
        matched_lines = sum(item.get("matched", 0) for item in results)
        missing_lines = sum(item.get("missing", 0) for item in results)
        coverage = (
            (matched_lines / total_lines) * 100
            if total_lines
            else 100.0
        )

        self.progress.set(1)
        self.progress_label.configure(
            text=f"Analysis complete. {invoice_count} invoices processed."
        )
        self.analyze_button.configure(
            text="Analyze All Pending Invoices",
            state="normal",
        )
        self.export_button.configure(
            state="normal" if results else "disabled"
        )

        self.invoice_count_card.update_value(
            str(invoice_count),
            "Pending invoices analyzed",
        )
        self.complete_card.update_value(
            str(complete_count),
            "No missing lines",
        )
        self.missing_card.update_value(
            str(missing_lines),
            "Invoice lines",
        )
        self.coverage_card.update_value(
            f"{coverage:.1f}%",
            f"{matched_lines} of {total_lines} lines matched",
        )

        self.app.last_job = "Pending invoice analysis"
        self.app.last_job_detail = (
            f"{invoice_count} invoices • {coverage:.1f}% coverage"
        )

    def _batch_failed(self, message: str) -> None:
        self.running = False
        self.analyze_button.configure(
            text="Analyze All Pending Invoices",
            state="normal",
        )
        self.progress_label.configure(text="Analysis failed.")

        messagebox.showerror(
            "Report unsuccessful",
            message,
        )

    def _no_invoices(self) -> None:
        self.running = False
        self.analyze_button.configure(
            text="Analyze All Pending Invoices",
            state="normal",
        )
        self.progress.set(1)
        self.progress_label.configure(
            text="No pending invoices matched the report criteria."
        )

    def open_selected_invoice(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return

        values = self.tree.item(selection[0], "values")
        if not values:
            return

        doc_number = str(values[0])

        result = next(
            (
                item
                for item in self.results
                if str(item.get("invoice")) == doc_number
            ),
            None,
        )

        if not result:
            return

        detail_page = self.app.pages["invoice_detail"]
        detail_page.load_invoice(
            str(result["invoice_id"]),
            doc_number,
        )
        self.app.show_page("invoice_detail")

    def export_report(self) -> None:
        if not self.results:
            messagebox.showinfo(
                "Nothing to export",
                "Run the report before exporting.",
            )
            return

        output_path = filedialog.asksaveasfilename(
            title="Export pending invoice analysis",
            defaultextension=".xlsx",
            initialfile=(
                "Pending_Invoice_Analysis_"
                f"{time.strftime('%Y-%m-%d')}.xlsx"
            ),
            filetypes=[
                ("Excel workbook", "*.xlsx"),
                ("All files", "*.*"),
            ],
        )

        if not output_path:
            return

        try:
            workbook = xlsxwriter.Workbook(output_path)
            worksheet = workbook.add_worksheet("Invoice Analysis")

            title_format = workbook.add_format(
                {
                    "bold": True,
                    "font_size": 16,
                }
            )
            subtitle_format = workbook.add_format(
                {
                    "font_color": "#666666",
                    "italic": True,
                }
            )
            header_format = workbook.add_format(
                {
                    "bold": True,
                    "bg_color": "#D9EAF7",
                    "border": 1,
                    "align": "center",
                }
            )
            text_format = workbook.add_format(
                {
                    "border": 1,
                }
            )
            money_format = workbook.add_format(
                {
                    "border": 1,
                    "num_format": "$#,##0.00",
                }
            )
            percent_format = workbook.add_format(
                {
                    "border": 1,
                    "num_format": "0.0%",
                }
            )

            worksheet.merge_range(
                "A1:J1",
                "Analyze All Pending Invoices",
                title_format,
            )
            worksheet.merge_range(
                "A2:J2",
                (
                    f"{APP_NAME} {APP_VERSION} • "
                    f"Generated {time.strftime('%Y-%m-%d %H:%M')}"
                ),
                subtitle_format,
            )

            headers = [
                "Invoice #",
                "Customer",
                "Total",
                "Balance",
                "Invoice Lines",
                "Matched Lines",
                "Missing Lines",
                "Unmatched Files",
                "Coverage",
                "Status",
            ]

            header_row = 3
            for column, header in enumerate(headers):
                worksheet.write(
                    header_row,
                    column,
                    header,
                    header_format,
                )

            for row_offset, item in enumerate(
                self.results,
                start=1,
            ):
                row = header_row + row_offset

                worksheet.write(row, 0, item["invoice"], text_format)
                worksheet.write(row, 1, item["customer"], text_format)
                worksheet.write_number(row, 2, item["total"], money_format)
                worksheet.write_number(row, 3, item["balance"], money_format)
                worksheet.write_number(row, 4, item["lines"], text_format)
                worksheet.write_number(row, 5, item["matched"], text_format)
                worksheet.write_number(row, 6, item["missing"], text_format)
                worksheet.write_number(row, 7, item["unmatched"], text_format)
                worksheet.write_number(
                    row,
                    8,
                    item["coverage"] / 100,
                    percent_format,
                )
                worksheet.write(row, 9, item["status"], text_format)

            worksheet.freeze_panes(header_row + 1, 0)
            worksheet.autofilter(
                header_row,
                0,
                header_row + len(self.results),
                len(headers) - 1,
            )

            widths = [13, 34, 14, 14, 13, 13, 13, 15, 11, 15]
            for index, width in enumerate(widths):
                worksheet.set_column(index, index, width)

            workbook.close()

            messagebox.showinfo(
                "Report exported",
                f"The report was saved to:\n\n{output_path}",
            )

        except Exception as exc:
            messagebox.showerror(
                "Export unsuccessful",
                str(exc),
            )


# ============================================================
# SETTINGS PAGE
# ============================================================

class SettingsPage(Page):
    """
    Settings landing page with two large options:

    • Default Folders opens the folder settings form.
    • QuickBooks opens the existing QuickBooks connection page.
    """

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self.receipt_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.archive_var = tk.StringVar()

        self.title_label = ctk.CTkLabel(
            self,
            text="Settings",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        )
        self.title_label.grid(
            row=0,
            column=0,
            padx=8,
            pady=(10, 5),
            sticky="ew",
        )

        self.subtitle_label = ctk.CTkLabel(
            self,
            text="Choose the settings you want to manage.",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.subtitle_label.grid(
            row=1,
            column=0,
            padx=8,
            pady=(0, 20),
            sticky="ew",
        )

        self.content = ctk.CTkFrame(
            self,
            fg_color="transparent",
        )
        self.content.grid(
            row=2,
            column=0,
            padx=8,
            pady=8,
            sticky="nsew",
        )
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.show_settings_menu()

    def clear_content(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()

    def show_settings_menu(self) -> None:
        self.clear_content()

        self.title_label.configure(text="Settings")
        self.subtitle_label.configure(
            text="Choose the settings you want to manage."
        )

        menu = ctk.CTkFrame(
            self.content,
            fg_color="transparent",
        )
        menu.grid(
            row=0,
            column=0,
            padx=12,
            pady=18,
            sticky="nsew",
        )
        menu.grid_columnconfigure((0, 1), weight=1)

        self._create_settings_tile(
            master=menu,
            column=0,
            icon_text="📁",
            label="Default Folders",
            description="Choose the default receipt, output, and archive folders.",
            command=self.show_default_folders,
        )

        self._create_settings_tile(
            master=menu,
            column=1,
            icon_text="QB",
            label="QuickBooks",
            description="Connect, reconnect, or disconnect QuickBooks Online.",
            command=lambda: self.app.show_page("connection"),
        )

    def _create_settings_tile(
        self,
        master,
        column: int,
        icon_text: str,
        label: str,
        description: str,
        command: Callable[[], None],
    ) -> None:
        tile = ctk.CTkFrame(
            master,
            corner_radius=16,
            border_width=1,
            border_color=("gray75", "gray35"),
        )
        tile.grid(
            row=0,
            column=column,
            padx=12,
            pady=12,
            sticky="nsew",
        )
        tile.grid_columnconfigure(0, weight=1)

        icon_button = ctk.CTkButton(
            tile,
            text=icon_text,
            width=112,
            height=112,
            corner_radius=22,
            font=ctk.CTkFont(
                size=42 if icon_text != "QB" else 34,
                weight="bold",
            ),
            command=command,
        )
        icon_button.grid(
            row=0,
            column=0,
            padx=30,
            pady=(30, 14),
        )

        label_button = ctk.CTkButton(
            tile,
            text=label,
            fg_color="transparent",
            hover_color=("gray85", "gray25"),
            text_color=("gray10", "gray95"),
            font=ctk.CTkFont(size=18, weight="bold"),
            command=command,
        )
        label_button.grid(
            row=1,
            column=0,
            padx=20,
            pady=(0, 8),
        )

        ctk.CTkLabel(
            tile,
            text=description,
            wraplength=280,
            justify="center",
            text_color=("gray35", "gray70"),
        ).grid(
            row=2,
            column=0,
            padx=24,
            pady=(0, 30),
        )

    def show_default_folders(self) -> None:
        self.clear_content()

        self.title_label.configure(text="Default Folders")
        self.subtitle_label.configure(
            text="Set the folders the application should use automatically."
        )

        wrapper = ctk.CTkFrame(
            self.content,
            fg_color="transparent",
        )
        wrapper.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        wrapper.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            wrapper,
            text="← Back to Settings",
            width=150,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.show_settings_menu,
        ).grid(
            row=0,
            column=0,
            pady=(0, 14),
            sticky="w",
        )

        card = SectionCard(
            wrapper,
            "Default folders",
            "These folders will be preselected when you start a workflow.",
        )
        card.grid(
            row=1,
            column=0,
            sticky="ew",
        )
        card.grid_columnconfigure(0, weight=1)

        PathSelector(
            card,
            "Receipt folder",
            self.receipt_var,
            lambda: self.pick_folder(self.receipt_var),
        ).grid(
            row=2,
            column=0,
            padx=22,
            pady=(8, 16),
            sticky="ew",
        )

        PathSelector(
            card,
            "Output folder",
            self.output_var,
            lambda: self.pick_folder(self.output_var),
        ).grid(
            row=3,
            column=0,
            padx=22,
            pady=(0, 16),
            sticky="ew",
        )

        PathSelector(
            card,
            "Archive folder",
            self.archive_var,
            lambda: self.pick_folder(self.archive_var),
        ).grid(
            row=4,
            column=0,
            padx=22,
            pady=(0, 16),
            sticky="ew",
        )

        ctk.CTkButton(
            card,
            text="Save settings",
            height=40,
            command=self.save,
        ).grid(
            row=5,
            column=0,
            padx=22,
            pady=(0, 22),
            sticky="e",
        )

    @staticmethod
    def pick_folder(variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Choose folder")
        if path:
            variable.set(path)

    def save(self) -> None:
        self.app.settings.receipt_folder = self.receipt_var.get().strip()
        self.app.settings.output_folder = self.output_var.get().strip()
        self.app.settings.archive_folder = self.archive_var.get().strip()
        self.app.save_settings()

        messagebox.showinfo(
            "Settings saved",
            "Your default folders have been saved.",
        )

    def on_show(self) -> None:
        settings = self.app.settings
        self.receipt_var.set(settings.receipt_folder)
        self.output_var.set(settings.output_folder)
        self.archive_var.set(settings.archive_folder)
        self.show_settings_menu()



# ============================================================
# ABOUT PAGE
# ============================================================

class AboutPage(Page):
    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=APP_NAME,
            font=ctk.CTkFont(size=30, weight="bold"),
            anchor="w",
        ).grid(
            row=0,
            column=0,
            padx=8,
            pady=(18, 4),
            sticky="ew",
        )

        ctk.CTkLabel(
            self,
            text=f"Version {APP_VERSION}",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(
            row=1,
            column=0,
            padx=8,
            pady=(0, 20),
            sticky="ew",
        )

        card = SectionCard(
            self,
            "Invoice attachment analysis for QuickBooks Online",
            (
                "Securely connect to QuickBooks, review invoice lines and "
                "supporting attachments, identify missing receipts, and export "
                "analysis results to Excel or receipt packages to ZIP. PDFs that "
                "cannot be read normally are processed locally with OpenCV receipt cleanup, "
                "Windows OCR, PaddleOCR, and Tesseract fallback."
            ),
        )
        card.grid(
            row=2,
            column=0,
            padx=8,
            pady=8,
            sticky="ew",
        )

        ctk.CTkLabel(
            card,
            text=(
                f"Publisher: {APP_PUBLISHER}\n\n"
                "QuickBooks authentication is handled by the hosted OAuth "
                "service. Local receipt analysis and OCR remain on this computer.\n\n"
                f"© {time.strftime('%Y')} {APP_PUBLISHER}"
            ),
            justify="left",
            anchor="w",
            wraplength=760,
        ).grid(
            row=2,
            column=0,
            padx=22,
            pady=(8, 22),
            sticky="ew",
        )


# ============================================================
# HELP PAGE
# ============================================================

class HelpPage(Page):
    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="Help",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")

        ctk.CTkLabel(
            self,
            text="Basic instructions and troubleshooting information.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=8, pady=(0, 20), sticky="ew")

        help_box = ctk.CTkTextbox(self, corner_radius=12)
        help_box.grid(row=2, column=0, padx=8, pady=8, sticky="nsew")
        help_box.insert(
            "1.0",
            "Getting started\n"
            "1. Open QuickBooks Connection and authorize your company.\n"
            "2. Select the workflow you want to run.\n"
            "3. Follow each step and review the selected files.\n"
            "4. Keep the application open while a QuickBooks task is running.\n\n"
            "Troubleshooting\n"
            "• Confirm that the selected folders still exist.\n"
            "• Confirm that the export file is not open in Excel.\n"
            "• Reconnect QuickBooks if authorization has expired.\n"
            "• Use detailed logs only when troubleshooting.\n\n"
            f"Settings file\n{CONFIG_FILE}\n\n"
            f"Version\n{APP_VERSION}",
        )
        help_box.configure(state="disabled")


# ============================================================
# MAIN APPLICATION
# ============================================================

class QBOExtensionApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(APP_NAME)
        self.geometry(WINDOW_SIZE)
        self.minsize(980, 650)

        icon_path = Path(__file__).resolve().with_name("app_icon.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        self.settings = SettingsStore.load()
        self.last_job = ""
        self.last_job_detail = ""

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=245, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(9, weight=1)

        ctk.CTkLabel(
            self.sidebar,
            text="QBO Invoice\nAnalyzer",
            font=ctk.CTkFont(size=22, weight="bold"),
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, padx=24, pady=(26, 28), sticky="ew")

        self.nav_buttons = {}
        nav_items = [
            ("home", "Home"),
            ("invoice", "Invoices"),
            ("reports", "Reports"),
            ("settings", "Settings"),
            ("help", "Help"),
            ("about", "About"),
        ]

        for row, (page_key, text) in enumerate(nav_items, start=1):
            button = ctk.CTkButton(
                self.sidebar,
                text=text,
                anchor="w",
                height=42,
                corner_radius=8,
                fg_color="transparent",
                text_color=("gray15", "gray90"),
                hover_color=("gray80", "gray25"),
                command=lambda key=page_key: self.show_page(key),
            )
            button.grid(row=row, column=0, padx=14, pady=4, sticky="ew")
            self.nav_buttons[page_key] = button

        self.appearance_menu = ctk.CTkOptionMenu(
            self.sidebar,
            values=["System", "Light", "Dark"],
            command=self.change_appearance,
        )
        self.appearance_menu.set("System")
        self.appearance_menu.grid(row=10, column=0, padx=18, pady=(10, 22), sticky="ew")

        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.grid(row=0, column=1, padx=24, pady=18, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

        self.pages = {
            "home": HomePage(self.main_container, self),
            "connection": ConnectionPage(self.main_container, self),
            "invoice": InvoiceAttachmentsPage(
                self.main_container,
                self,
            ),
            "invoice_detail": InvoiceLineDetailsPage(
                self.main_container,
                self,
            ),
            "reports": ReportsPage(self.main_container, self),
            "settings": SettingsPage(self.main_container, self),
            "help": HelpPage(self.main_container, self),
            "about": AboutPage(self.main_container, self),
        }

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self.current_page = ""
        self.show_page("home")

    def save_settings(self) -> None:
        try:
            SettingsStore.save(self.settings)
        except OSError as exc:
            messagebox.showerror(
                "Could not save settings",
                f"The settings file could not be saved.\n\n{exc}",
            )

    def show_page(self, page_key: str) -> None:
        page = self.pages[page_key]
        page.tkraise()
        page.on_show()
        self.current_page = page_key

        # The QuickBooks connection page is accessed from Settings,
        # so keep Settings highlighted while that page is open.
        if page_key == "connection":
            active_nav_key = "settings"
        elif page_key == "invoice_detail":
            active_nav_key = "invoice"
        else:
            active_nav_key = page_key

        for key, button in self.nav_buttons.items():
            if key == active_nav_key:
                button.configure(
                    fg_color=("gray75", "gray30"),
                    text_color=("gray10", "gray95"),
                )
            else:
                button.configure(
                    fg_color="transparent",
                    text_color=("gray15", "gray90"),
                )

    @staticmethod
    def change_appearance(choice: str) -> None:
        ctk.set_appearance_mode(choice.lower())


if __name__ == "__main__":
    app = QBOExtensionApp()
    app.mainloop()