from __future__ import annotations
import json
import os
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
            text='Invoices where PrintStatus is "NeedToPrint" and EmailStatus is "NotSet".',
            text_color=("gray35", "gray70"), anchor="w",
        ).grid(row=0, column=0, sticky="ew")

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

    def __init__(self, master, app: "QBOExtensionApp"):
        super().__init__(master, app)
        self.invoice_id = ""
        self.doc_number = ""
        self.loading = False
        self.line_records: dict[str, dict] = {}

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
        style.configure("InvoiceLine.Treeview.Heading", font=("Segoe UI", 10, "bold"))

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

        self.unmatched_frame = ctk.CTkFrame(self, corner_radius=12)
        self.unmatched_frame.grid(
            row=4,
            column=0,
            padx=8,
            pady=(0, 8),
            sticky="ew",
        )
        self.unmatched_frame.grid_columnconfigure(0, weight=1)

        self.unmatched_title = ctk.CTkLabel(
            self.unmatched_frame,
            text="Unmatched invoice attachments",
            font=ctk.CTkFont(weight="bold"),
            anchor="w",
        )
        self.unmatched_title.grid(
            row=0,
            column=0,
            padx=14,
            pady=(10, 4),
            sticky="ew",
        )

        self.unmatched_links = ctk.CTkFrame(
            self.unmatched_frame,
            fg_color="transparent",
        )
        self.unmatched_links.grid(
            row=1,
            column=0,
            padx=14,
            pady=(0, 10),
            sticky="ew",
        )

        self.unmatched_frame.grid_remove()

    def load_invoice(self, invoice_id: str, doc_number: str) -> None:
        self.invoice_id = str(invoice_id)
        self.doc_number = str(doc_number)
        self.title_label.configure(text=f"Invoice {self.doc_number}")
        self.summary_label.configure(text="Loading invoice lines and attachments...")
        self.status_label.configure(text="Loading...")
        self.count_label.configure(text="0 lines")
        self.open_attachment_button.configure(state="disabled")
        self.export_zip_button.configure(state="disabled")
        self.line_records.clear()
        self.unmatched_frame.grid_remove()

        for widget in self.unmatched_links.winfo_children():
            widget.destroy()

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        self.loading = True
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self) -> None:
        try:
            response = requests.get(
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
            self.after(0, lambda data=payload: self._load_succeeded(data))
        except requests.RequestException as exc:
            self.after(
                0,
                lambda error=exc: self._load_failed(
                    "Could not load the invoice details.\n\n" + str(error)
                ),
            )
        except Exception as exc:
            self.after(0, lambda error=exc: self._load_failed(str(error)))

    def _load_succeeded(self, payload: dict) -> None:
        self.loading = False
        invoice = payload.get("invoice", {})
        lines = payload.get("lines", [])
        unmatched = payload.get("unmatched_attachments", [])

        customer_ref = invoice.get("CustomerRef") or {}
        customer = customer_ref.get("name") or customer_ref.get("value") or ""

        self.summary_label.configure(
            text=(
                f"{customer}  •  Date: {invoice.get('TxnDate', '')}  •  "
                f"Total: {self.format_money(invoice.get('TotalAmt'))}  •  "
                f"Balance: {self.format_money(invoice.get('Balance'))}"
            )
        )

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        self.line_records.clear()
        attached_count = 0

        for index, line in enumerate(lines):
            attachments = line.get("attachments", [])
            first_attachment = attachments[0] if attachments else None

            if first_attachment:
                attached_count += len(attachments)
                attachment_name = first_attachment.get("file_name", "Open attachment")
                status = (
                    "Attachment available"
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
                    line.get("line_number", index + 1),
                    line.get("description", ""),
                    self.format_money(line.get("amount")),
                    attachment_name,
                    status,
                ),
            )
            self.line_records[item_id] = line

        self.count_label.configure(
            text=f"{len(lines)} lines • {attached_count} attachments"
        )

        status_text = "Invoice details loaded."
        if unmatched:
            status_text += (
                f" {len(unmatched)} invoice-level attachment"
                f"{'' if len(unmatched) == 1 else 's'} could not be matched to a line."
            )
        self.status_label.configure(text=status_text)
        self.export_zip_button.configure(
            state="normal" if payload.get("attachment_count", 0) else "disabled"
        )

        if unmatched:
            self.unmatched_frame.grid()

            for attachment in unmatched:
                link = ctk.CTkButton(
                    self.unmatched_links,
                    text=attachment.get("file_name", "Open attachment"),
                    anchor="w",
                    fg_color="transparent",
                    hover_color=("gray85", "gray25"),
                    text_color=("blue3", "sky blue"),
                    command=lambda item=attachment: self._open_attachment(item),
                )
                link.pack(fill="x", pady=2)
        else:
            self.unmatched_frame.grid_remove()

        self.update_attachment_button()

    def _load_failed(self, message: str) -> None:
        self.loading = False
        self.status_label.configure(text="Could not load invoice details.")
        messagebox.showerror("Invoice detail loading unsuccessful", message)

    def update_attachment_button(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            self.open_attachment_button.configure(state="disabled")
            return
        line = self.line_records.get(selection[0], {})
        self.open_attachment_button.configure(
            state="normal" if line.get("attachments") else "disabled"
        )

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
                "The attachment could not be opened in your browser.",
            )

    def export_all_attachments_zip(self) -> None:
        if not self.invoice_id:
            return

        output_path = filedialog.asksaveasfilename(
            title="Save invoice attachments ZIP",
            defaultextension=".zip",
            initialfile=f"Invoice_{self.doc_number}_Attachments.zip",
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")],
        )
        if not output_path:
            return

        self.export_zip_button.configure(text="Downloading...", state="disabled")
        self.status_label.configure(text="Downloading and building the ZIP archive...")
        threading.Thread(
            target=self._export_zip_worker,
            args=(output_path,),
            daemon=True,
        ).start()

    def _export_zip_worker(self, output_path: str) -> None:
        try:
            response = requests.get(
                f"{RENDER_AUTH_BASE_URL}/invoices/{quote(self.invoice_id, safe='')}/attachments.zip",
                stream=True,
                timeout=300,
            )
            if response.status_code == 401:
                raise RuntimeError(
                    "The QuickBooks server session has expired. "
                    "Open Settings → QuickBooks and reconnect."
                )
            response.raise_for_status()

            with open(output_path, "wb") as output_file:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        output_file.write(chunk)

            self.after(0, lambda path=output_path: self._export_succeeded(path))
        except Exception as exc:
            self.after(0, lambda error=exc: self._export_failed(str(error)))

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

    @staticmethod
    def format_money(value) -> str:
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return ""


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
            ("invoice", "Invoices"),
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
            "invoice_detail": InvoiceLineDetailsPage(
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