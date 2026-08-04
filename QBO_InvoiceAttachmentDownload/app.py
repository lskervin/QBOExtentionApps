from __future__ import annotations
import json
import os
import queue
import threading
import time
import webbrowser
import requests
import tkinter as tk
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional
import customtkinter as ctk


# ============================================================
# APP SETTINGS
# ============================================================

APP_NAME = "QBO Extension Apps"
APP_VERSION = "1.0.0"
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


@dataclass
class AppSettings:
    qbo_company_name: str = ""
    qbo_connected: bool = False
    divvy_export_path: str = ""
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
            text="Choose a task to get started.",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=15),
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, padx=8, pady=(0, 20), sticky="ew")

        self.connection_card = StatCard(
            self,
            "QuickBooks",
            "Not connected",
            "Connect before uploading",
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
            title="Invoice Attachments",
            description="Download and organize invoice attachments.",
            command=lambda: app.show_page("invoice"),
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
            self.connection_card.update_value("Not connected", "Connect before uploading")
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

        if not self.app.settings.qbo_connected:
            return

        confirmed = messagebox.askyesno(
            "Disconnect QuickBooks",
            "Remove this QuickBooks connection from the desktop app?",
        )
        if not confirmed:
            return

        # This currently clears only the desktop app's saved connection state.
        # Add a protected Render disconnect endpoint later to revoke and delete
        # the stored QBO refresh token on the server.
        self.app.settings.qbo_connected = False
        self.app.settings.qbo_company_name = ""
        self.app.save_settings()
        self.refresh()

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


# ============================================================
# DIVVY WIZARD
# ============================================================

class DivvyWizardPage(Page):
    STEP_TITLES = [
        "QuickBooks",
        "Divvy export",
        "Receipt folder",
        "Review",
        "Upload",
    ]

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.current_step = 0
        self.worker_queue: queue.Queue = queue.Queue()
        self.processing = False

        self.export_var = tk.StringVar(value=app.settings.divvy_export_path)
        self.receipt_var = tk.StringVar(value=app.settings.receipt_folder)
        self.output_var = tk.StringVar(value=app.settings.output_folder)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            self,
            text="Upload Divvy receipts",
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 3), sticky="ew")

        self.step_label = ctk.CTkLabel(
            self,
            text="",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.step_label.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")

        self.step_progress = ctk.CTkProgressBar(self)
        self.step_progress.grid(row=2, column=0, padx=8, pady=(0, 16), sticky="ew")

        self.content = ctk.CTkFrame(self, corner_radius=14)
        self.content.grid(row=3, column=0, padx=8, pady=8, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.grid(row=4, column=0, padx=8, pady=(14, 5), sticky="ew")
        self.footer.grid_columnconfigure(1, weight=1)

        self.back_button = ctk.CTkButton(
            self.footer,
            text="Back",
            width=105,
            fg_color="transparent",
            border_width=1,
            text_color=("gray15", "gray90"),
            command=self.go_back,
        )
        self.back_button.grid(row=0, column=0)

        self.next_button = ctk.CTkButton(
            self.footer,
            text="Next",
            width=120,
            command=self.go_next,
        )
        self.next_button.grid(row=0, column=2)

        self.render_step()

    def clear_content(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()

    def render_step(self) -> None:
        self.clear_content()

        self.step_label.configure(
            text=f"Step {self.current_step + 1} of {len(self.STEP_TITLES)} · "
                 f"{self.STEP_TITLES[self.current_step]}"
        )
        self.step_progress.set((self.current_step + 1) / len(self.STEP_TITLES))
        self.back_button.configure(
            state="disabled" if self.current_step == 0 or self.processing else "normal"
        )

        renderers = [
            self.render_connection_step,
            self.render_export_step,
            self.render_receipt_step,
            self.render_review_step,
            self.render_upload_step,
        ]
        renderers[self.current_step]()

    def render_connection_step(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, padx=30, pady=30, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        connected = self.app.settings.qbo_connected
        company = self.app.settings.qbo_company_name

        ctk.CTkLabel(
            frame,
            text="Connect to QuickBooks",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        status_text = (
            f"Connected to {company}" if connected else "QuickBooks is not connected."
        )
        ctk.CTkLabel(
            frame,
            text=status_text,
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, pady=(8, 18), sticky="ew")

        ctk.CTkButton(
            frame,
            text="Manage QuickBooks Connection",
            width=230,
            command=lambda: self.app.show_page("connection"),
        ).grid(row=2, column=0, sticky="w")

        self.next_button.configure(
            text="Next",
            state="normal" if connected else "disabled",
        )

    def render_export_step(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, padx=30, pady=30, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame,
            text="Select the Divvy export",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            frame,
            text="Choose the CSV or Excel export containing the transactions.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, pady=(7, 22), sticky="ew")

        selector = PathSelector(
            frame,
            "Divvy export file",
            self.export_var,
            self.select_export,
            "Choose file",
        )
        selector.grid(row=2, column=0, sticky="ew")

        self.next_button.configure(text="Next", state="normal")

    def render_receipt_step(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, padx=30, pady=30, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame,
            text="Select the receipt folder",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            frame,
            text="Choose the folder containing the receipt PDFs and images.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, pady=(7, 22), sticky="ew")

        receipt_selector = PathSelector(
            frame,
            "Receipt folder",
            self.receipt_var,
            self.select_receipt_folder,
            "Choose folder",
        )
        receipt_selector.grid(row=2, column=0, pady=(0, 18), sticky="ew")

        output_selector = PathSelector(
            frame,
            "Report/output folder",
            self.output_var,
            self.select_output_folder,
            "Choose folder",
        )
        output_selector.grid(row=3, column=0, sticky="ew")

        self.next_button.configure(text="Next", state="normal")

    def render_review_step(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, padx=30, pady=30, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame,
            text="Review your selections",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        company = self.app.settings.qbo_company_name or "Not selected"
        details = [
            ("QuickBooks company", company),
            ("Divvy export", self.export_var.get() or "Not selected"),
            ("Receipt folder", self.receipt_var.get() or "Not selected"),
            ("Output folder", self.output_var.get() or "Not selected"),
        ]

        for row, (label, value) in enumerate(details, start=1):
            item = ctk.CTkFrame(frame, corner_radius=10)
            item.grid(row=row, column=0, pady=7, sticky="ew")
            item.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                item,
                text=label,
                font=ctk.CTkFont(weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=16, pady=(12, 2), sticky="ew")

            ctk.CTkLabel(
                item,
                text=value,
                wraplength=760,
                justify="left",
                text_color=("gray35", "gray70"),
                anchor="w",
            ).grid(row=1, column=0, padx=16, pady=(0, 12), sticky="ew")

        self.next_button.configure(text="Start upload", state="normal")

    def render_upload_step(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, padx=30, pady=30, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        self.upload_title = ctk.CTkLabel(
            frame,
            text="Preparing upload...",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        )
        self.upload_title.grid(row=0, column=0, sticky="ew")

        self.upload_status = ctk.CTkLabel(
            frame,
            text="Starting",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self.upload_status.grid(row=1, column=0, pady=(7, 18), sticky="ew")

        self.upload_progress = ctk.CTkProgressBar(frame)
        self.upload_progress.grid(row=2, column=0, sticky="ew")
        self.upload_progress.set(0)

        self.upload_count = ctk.CTkLabel(
            frame,
            text="0 / 0",
            anchor="w",
        )
        self.upload_count.grid(row=3, column=0, pady=(8, 20), sticky="ew")

        self.log_box = ctk.CTkTextbox(frame, height=220)
        self.log_box.grid(row=4, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

        self.processing = True
        self.back_button.configure(state="disabled")
        self.next_button.configure(text="Close", state="disabled")

        self.start_demo_upload()

    def append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start_demo_upload(self) -> None:
        """
        Replace demo_worker() with your real Divvy/QBO processing function.

        Recommended real worker signature:
            run_divvy_upload(
                export_path: str,
                receipt_folder: str,
                output_folder: str,
                progress_callback: Callable[[int, int, str], None],
                log_callback: Callable[[str], None],
            )
        """

        def demo_worker() -> None:
            total = 75

            for index in range(1, total + 1):
                time.sleep(0.035)
                self.worker_queue.put(
                    ("progress", index, total, f"Processing receipt {index}.pdf")
                )

                if index % 12 == 0:
                    self.worker_queue.put(
                        ("log", f"Uploaded receipt {index}.pdf")
                    )

            self.worker_queue.put(
                ("done", total, total, "Upload completed successfully.")
            )

        threading.Thread(target=demo_worker, daemon=True).start()
        self.after(100, self.poll_worker_queue)

    def poll_worker_queue(self) -> None:
        try:
            while True:
                event = self.worker_queue.get_nowait()
                event_type = event[0]

                if event_type == "progress":
                    _, current, total, status = event
                    self.upload_progress.set(current / total)
                    self.upload_count.configure(text=f"{current} / {total}")
                    self.upload_status.configure(text=status)

                elif event_type == "log":
                    self.append_log(event[1])

                elif event_type == "done":
                    _, current, total, status = event
                    self.processing = False
                    self.upload_progress.set(1)
                    self.upload_count.configure(text=f"{current} / {total}")
                    self.upload_title.configure(text="Upload complete")
                    self.upload_status.configure(text=status)
                    self.append_log(status)
                    self.next_button.configure(text="Finish", state="normal")

                    self.app.last_job = "Divvy upload"
                    self.app.last_job_detail = f"{total} receipts processed"

                    return

        except queue.Empty:
            pass

        if self.processing:
            self.after(100, self.poll_worker_queue)

    def select_export(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Divvy export",
            filetypes=[
                ("Spreadsheet files", "*.csv *.xlsx *.xls"),
                ("CSV files", "*.csv"),
                ("Excel files", "*.xlsx *.xls"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.export_var.set(path)

    def select_receipt_folder(self) -> None:
        path = filedialog.askdirectory(title="Select receipt folder")
        if path:
            self.receipt_var.set(path)

    def select_output_folder(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_var.set(path)

    def validate_current_step(self) -> bool:
        if self.current_step == 0 and not self.app.settings.qbo_connected:
            messagebox.showwarning(
                "QuickBooks connection required",
                "Connect to QuickBooks before continuing.",
            )
            return False

        if self.current_step == 1:
            path = Path(self.export_var.get().strip())
            if not path.is_file():
                messagebox.showwarning(
                    "Select an export",
                    "Choose a valid Divvy export file.",
                )
                return False

        if self.current_step == 2:
            receipt_path = Path(self.receipt_var.get().strip())
            output_path = Path(self.output_var.get().strip())

            if not receipt_path.is_dir():
                messagebox.showwarning(
                    "Select a receipt folder",
                    "Choose a valid receipt folder.",
                )
                return False

            if not output_path.is_dir():
                messagebox.showwarning(
                    "Select an output folder",
                    "Choose a valid output folder.",
                )
                return False

        return True

    def save_paths(self) -> None:
        self.app.settings.divvy_export_path = self.export_var.get().strip()
        self.app.settings.receipt_folder = self.receipt_var.get().strip()
        self.app.settings.output_folder = self.output_var.get().strip()
        self.app.save_settings()

    def go_next(self) -> None:
        if self.processing:
            return

        if self.current_step == len(self.STEP_TITLES) - 1:
            self.current_step = 0
            self.render_step()
            self.app.show_page("home")
            return

        if not self.validate_current_step():
            return

        self.save_paths()
        self.current_step += 1
        self.render_step()

    def go_back(self) -> None:
        if self.current_step > 0 and not self.processing:
            self.current_step -= 1
            self.render_step()

    def on_show(self) -> None:
        if self.current_step == 0:
            self.render_step()


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
            self, text="Invoice Attachments",
            font=ctk.CTkFont(size=28, weight="bold"), anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, padx=8, pady=(0, 12), sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            toolbar,
            text='Invoices where PrintStatus is "NeedToPrint" and EmailStatus is "NotSet".',
            text_color=("gray35", "gray70"), anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        self.refresh_button = ctk.CTkButton(
            toolbar, text="Refresh invoices", width=135, command=self.refresh_invoices,
        )
        self.refresh_button.grid(row=0, column=1, padx=(12, 0))

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
        self.status_label.configure(text="Invoice list loaded.")
        self.count_label.configure(text=f"{len(invoices)} invoice{'' if len(invoices) == 1 else 's'}")
        self.populate_table(invoices)

    def _load_failed(self, message: str) -> None:
        self.loading = False
        self.refresh_button.configure(text="Refresh invoices", state="normal")
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
# PLACEHOLDER WORKFLOW PAGES
# ============================================================

class WorkflowPlaceholderPage(Page):
    def __init__(
        self,
        master,
        app: "QBOExtensionApp",
        title: str,
        description: str,
    ):
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(10, 5), sticky="ew")

        ctk.CTkLabel(
            self,
            text=description,
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=8, pady=(0, 20), sticky="ew")

        card = SectionCard(
            self,
            "Workflow shell ready",
            "Connect your existing Python processing function to this page.",
        )
        card.grid(row=2, column=0, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(
            card,
            text=(
                "This screen is intentionally left as a clean placeholder. "
                "Use the Divvy wizard as the pattern for file selection, "
                "review, progress updates, logging, and completion results."
            ),
            wraplength=760,
            justify="left",
            anchor="w",
        ).grid(row=2, column=0, padx=22, pady=(4, 18), sticky="ew")


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
            "4. Keep the application open until processing is complete.\n\n"
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
            text="QBO\nExtension Apps",
            font=ctk.CTkFont(size=22, weight="bold"),
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, padx=24, pady=(26, 28), sticky="ew")

        self.nav_buttons = {}
        nav_items = [
            ("home", "Home"),
            ("invoice", "Invoice Attachments"),
            ("settings", "Settings"),
            ("help", "Help"),
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
            "settings": SettingsPage(self.main_container, self),
            "help": HelpPage(self.main_container, self),
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
        active_nav_key = (
            "settings"
            if page_key == "connection"
            else page_key
        )

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