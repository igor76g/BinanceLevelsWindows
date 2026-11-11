"""Tkinter UI: one window with two lists (1 = coins approaching a cascade, 2 = the coins closest to a cascade)
and a settings dialog.

Double-click a ticker in either list to open the coin in Vataga.terminal; click \"Copy\" to copy the ticker.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from . import __version__, notifier, popups, vataga
from .binance_api import BinanceApi
from .monitor import Monitor
from .settings import APP_NAME, LEVEL_INTERVALS, Settings, alert_times_path, alerts_log_path, load_settings, save_settings

MAX_LOG_LINES = 300
BOLD = ("Segoe UI", 10, "bold")

# (settings key, label, python type, minimum allowed value); label None = section header
SETTINGS_FIELDS = [
    ("Coin filters", None, None, None),
    ("min_volume_million", "Minimum 24h volume (million USDT)", float, 0.0),
    ("min_natr", "Minimum NATR of the last window (%, 0 = off)", float, 0.0),
    ("min_trades", "Minimum trades in the last window (0 = off)", int, 0),
    ("activity_window_minutes", "Window for NATR and trades: last N minutes", int, 1),
    ("Cascades of levels", None, None, None),
    ("level_interval", "Candle size for finding extremes", str, ""),
    ("level_lookback_candles", "History to search (number of candles)", int, 20),
    ("swing_strength", "Extreme must beat N candles on each side", int, 1),
    ("min_extremes", "Minimum extremes in a cascade", int, 2),
    ("level_tolerance_percent", "Extremes within this distance are one cascade (%)", float, 0.0),
    ("levels_refresh_minutes", "Recalculate cascades every (minutes)", int, 1),
    ("Approach and notifications", None, None, None),
    ("approach_candles", "Notify when distance <= N average 1-min candles", float, 0.0),
    ("avg_candle_minutes", "Average 1-min candle measured over (minutes)", int, 5),
    ("closest_count", "Coins in the 'closest' list", int, 1),
    ("cooldown_minutes", "Notification cooldown per coin and side (minutes)", int, 0),
    ("check_interval_seconds", "Check interval (seconds, at least 15)", int, 15),
    ("popup_auto_close_sec", "Close alert popups after (seconds, 0 = never)", int, 0),
]


def fmt_million(value: float) -> str:
    return f"{value / 1e6:,.1f}"


class SymbolTree(ttk.Frame):
    """Treeview with a vertical scrollbar; double-click on a row calls `on_open(symbol)`."""

    COPY_TEXT = "Copy"

    def __init__(self, master, columns, on_open, on_copy=None, height=12) -> None:
        super().__init__(master)
        self.on_open = on_open
        self.on_copy = on_copy
        columns = list(columns) + [("copy", "", 70)]  # last column: clickable "Copy" cell with the ticker
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height)
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key == "ticker" else ("center" if key == "copy" else "e"))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Button-1>", self._click)

    def _click(self, event) -> str | None:
        """A single click on the 'Copy' cell copies the ticker of that row."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        item = self.tree.identify_row(event.y)
        if item and self.tree.identify_column(event.x) == f"#{len(self.tree['columns'])}":
            if self.on_copy is not None:
                self.on_copy(str(self.tree.set(item, "ticker")))
            return "break"
        return None

    def _double_click(self, event) -> None:
        if self.tree.identify_column(event.x) == f"#{len(self.tree['columns'])}":
            return  # double-click on the Copy cell must not open Vataga
        item = self.tree.identify_row(event.y)
        if item:
            self.on_open(str(self.tree.set(item, "ticker")))

    def set_rows(self, rows: list[tuple]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", values=tuple(row) + (self.COPY_TEXT,))


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)

        settings = load_settings()
        self.vars: dict[str, tk.StringVar] = {}
        self.types: dict[str, tuple] = {}
        self.labels: dict[str, str] = {}

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        form = ttk.Frame(root)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        row = 0
        for key, label, py_type, minimum in SETTINGS_FIELDS:
            if label is None:
                ttk.Label(form, text=key, font=BOLD).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            else:
                var = tk.StringVar()
                value = getattr(settings, key)
                var.set(f"{value:g}" if isinstance(value, float) else str(value))
                self.vars[key] = var
                self.types[key] = (py_type, minimum)
                self.labels[key] = label
                ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 16))
                if key == "level_interval":
                    ttk.Combobox(form, textvariable=var, values=LEVEL_INTERVALS, width=10, state="readonly").grid(
                        row=row, column=1, sticky="e", pady=2)
                else:
                    ttk.Entry(form, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=2)
            row += 1

        self.sound_var = tk.BooleanVar(value=settings.sound_enabled)
        self.popup_var = tk.BooleanVar(value=settings.popup_enabled)
        self.toast_var = tk.BooleanVar(value=settings.toast_enabled)
        self.autostart_var = tk.BooleanVar(value=settings.autostart_monitoring)
        ttk.Label(root, text="Notifications (list 1 only)", font=BOLD).pack(anchor="w", pady=(12, 2))
        ttk.Checkbutton(root, text="Play a sound", variable=self.sound_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Popup window with buttons (always on top)", variable=self.popup_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Windows toast notification", variable=self.toast_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Start monitoring automatically when the app opens", variable=self.autostart_var).pack(anchor="w", pady=(6, 0))

        ttk.Label(root, text="Vataga.terminal", font=BOLD).pack(anchor="w", pady=(12, 2))
        vrow = ttk.Frame(root)
        vrow.pack(fill="x")
        self.vtype_var = tk.StringVar(value=settings.vataga_type)
        self.vport_var = tk.StringVar(value=str(settings.vataga_port))
        ttk.Label(vrow, text="Market").pack(side="left")
        ttk.Combobox(vrow, textvariable=self.vtype_var, values=["F", "S"], width=3, state="readonly").pack(side="left", padx=4)
        ttk.Label(vrow, text="(F = futures, S = spot)   Port").pack(side="left")
        ttk.Entry(vrow, textvariable=self.vport_var, width=7).pack(side="left", padx=4)
        ttk.Button(vrow, text="Test Vataga", command=self.test_vataga).pack(side="right")

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def read(self) -> Settings | None:
        values: dict = {}
        for key, var in self.vars.items():
            py_type, minimum = self.types[key]
            raw = var.get().strip().replace(",", ".")
            if py_type is str:
                values[key] = raw
                continue
            try:
                value = py_type(raw)
            except ValueError:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be a valid number.", parent=self)
                return None
            if value < minimum:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be at least {minimum:g}.", parent=self)
                return None
            values[key] = value
        try:
            port = int(self.vport_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Port must be a number from 1 to 65535.", parent=self)
            return None
        return Settings(
            sound_enabled=self.sound_var.get(),
            popup_enabled=self.popup_var.get(),
            toast_enabled=self.toast_var.get(),
            autostart_monitoring=self.autostart_var.get(),
            vataga_type=self.vtype_var.get() or "F",
            vataga_port=port,
            vataga_partner_id=load_settings().vataga_partner_id,
            **values,
        )

    def save(self) -> None:
        settings = self.read()
        if settings is None:
            return
        try:
            save_settings(settings)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save settings: {exc}", parent=self)
            return
        self.app.add_log("Settings saved")
        self.app.settings_saved()
        self.destroy()

    def test_vataga(self) -> None:
        settings = self.read()
        if settings is None:
            return
        ok, message = vataga.open_symbol("BTCUSDT", settings)
        if ok:
            messagebox.showinfo(APP_NAME, "Request sent. BTCUSDT should open in the window linked to 'Screener'.", parent=self)
        else:
            messagebox.showerror(APP_NAME, message, parent=self)


class App(tk.Tk):
    NEAR_COLUMNS = [
        ("ticker", "Ticker", 110),
        ("side", "Level", 80),
        ("level", "Level price", 100),
        ("price", "Price", 100),
        ("dist_pct", "Dist %", 65),
        ("dist_c", "Dist, candles", 90),
        ("touches", "Extremes", 70),
        ("vol24", "24h vol, M", 85),
        ("since", "Since", 60),
    ]
    CLOSEST_COLUMNS = [
        ("rank", "#", 30),
        ("ticker", "Ticker", 110),
        ("side", "Level", 80),
        ("level", "Level price", 100),
        ("price", "Price", 100),
        ("dist_pct", "Dist %", 65),
        ("dist_c", "Dist, candles", 90),
        ("touches", "Extremes", 70),
        ("vol24", "24h vol, M", 85),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("900x820+60+30")
        self.minsize(780, 640)

        self.api = BinanceApi()
        self.monitor: Monitor | None = None
        self.messages: queue.Queue = queue.Queue()
        self.last_settings = load_settings()

        self._build_widgets()
        notifier.alert_listener = lambda symbol, title, message, direction=0: self.messages.put(
            ("popup", (symbol, title, message, direction))
        )

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._drain_messages)
        if self.last_settings.autostart_monitoring:
            self.after(500, self.start_monitoring)

    # ------------------------------------------------------------------ UI

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Binance Futures: cascades of levels", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(root, textvariable=self.status_var, wraplength=780).pack(anchor="w", pady=(2, 8))

        buttons = ttk.Frame(root)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Start monitoring", command=self.start_monitoring)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_monitoring, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        self.check_button = ttk.Button(buttons, text="Check now", command=self.check_now, state="disabled")
        self.check_button.pack(side="left")
        ttk.Button(buttons, text="Settings", command=self.open_settings).pack(side="right")

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(10, 4))

        near_frame = ttk.LabelFrame(paned, text=" 1. Approaching a cascade ", padding=6)
        self.heading_var = tk.StringVar(value="Waiting for the first check...")
        ttk.Label(near_frame, textvariable=self.heading_var, font=BOLD, wraplength=840).pack(anchor="w")
        self.updated_var = tk.StringVar(value="")
        ttk.Label(near_frame, textvariable=self.updated_var).pack(anchor="w")
        self.near_list = SymbolTree(near_frame, self.NEAR_COLUMNS, self.open_symbol, self.copy_symbol, height=8)
        self.near_list.pack(fill="both", expand=True, pady=(4, 0))
        paned.add(near_frame, weight=1)

        closest_frame = ttk.LabelFrame(paned, text=" 2. Closest to a cascade ", padding=6)
        self.closest_heading_var = tk.StringVar(value="")
        ttk.Label(closest_frame, textvariable=self.closest_heading_var, font=BOLD, wraplength=840).pack(anchor="w")
        self.closest_list = SymbolTree(closest_frame, self.CLOSEST_COLUMNS, self.open_symbol, self.copy_symbol, height=5)
        self.closest_list.pack(fill="both", expand=True, pady=(4, 0))
        paned.add(closest_frame, weight=1)

        self.hint_var = tk.StringVar(value="Double-click a ticker to open it in Vataga.terminal; click \"Copy\" to copy the ticker")
        ttk.Label(root, textvariable=self.hint_var, wraplength=780).pack(anchor="w")

        tools = ttk.Frame(root)
        tools.pack(fill="x", pady=(8, 2))
        ttk.Label(tools, text="Log", font=BOLD).pack(side="left")
        ttk.Button(tools, text="Test notification", command=self.test_notification).pack(side="right")
        ttk.Button(tools, text="Alerts log", command=self.open_alerts_log).pack(side="right", padx=6)
        ttk.Button(tools, text="Close all popups", command=popups.close_all).pack(side="right")
        self.log_widget = scrolledtext.ScrolledText(root, height=6, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=False)

    # ------------------------------------------------------------- actions

    def open_settings(self) -> None:
        SettingsDialog(self)

    def settings_saved(self) -> None:
        self.last_settings = load_settings()
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()  # apply the new conditions right away

    def start_monitoring(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            return
        self.monitor = Monitor(
            api=self.api,
            load_settings=load_settings,
            on_results=lambda near, closest, since, now: self.messages.put(("results", (near, closest, since, now))),
            on_alert=lambda row, settings: notifier.show_level(
                row.symbol, row.side, row.level, row.price, row.distance_percent, row.distance_candles,
                row.touches, sound=settings.sound_enabled,
            ),
            log=lambda text: self.messages.put(("log", text)),
            set_status=lambda text: self.messages.put(("status", text)),
            warn=lambda title, text: notifier.show_warning(title, text, sound=load_settings().sound_enabled),
            state_path=alert_times_path(),
        )
        self.monitor.start()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.check_button.config(state="normal")
        self.status_var.set("Starting: the first check downloads candles of all coins and takes a little longer...")

    def stop_monitoring(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self.stop_button.config(state="disabled")
        self.check_button.config(state="disabled")

    def check_now(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()
            self.status_var.set("Checking now...")

    def copy_symbol(self, symbol: str) -> None:
        """Put the ticker (e.g. INITUSDT) on the clipboard."""
        try:
            self.clipboard_clear()
            self.clipboard_append(symbol)
            self.update_idletasks()
            self.hint_var.set(f"{symbol} copied to the clipboard")
        except tk.TclError as exc:
            self.hint_var.set(f"Could not copy {symbol}: {exc}")

    def open_symbol(self, symbol: str) -> None:
        """Open a coin in Vataga.terminal (called on double-click; the request runs in a thread)."""
        settings = load_settings()
        self.hint_var.set(f"Sending {symbol} to Vataga.terminal...")

        def work() -> None:
            ok, message = vataga.open_symbol(symbol, settings)
            self.messages.put(("hint", f"{symbol} opened in Vataga.terminal" if ok else message))

        threading.Thread(target=work, daemon=True).start()

    def test_notification(self) -> None:
        try:
            notifier.show_test(sound=load_settings().sound_enabled)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not show notification: {exc}")

    def open_alerts_log(self) -> None:
        path = alerts_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  (Windows only)
        except (OSError, AttributeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not open {path}: {exc}")

    # ---------------------------------------------------------------- misc

    def add_log(self, text: str) -> None:
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_widget.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    @staticmethod
    def _side_text(side: str) -> str:
        return "Resistance" if side == "resistance" else "Support"

    def _row_values(self, r) -> tuple:
        return (
            r.symbol, self._side_text(r.side), f"{r.level:.8g}", f"{r.price:.8g}",
            f"{r.distance_percent:.2f}", f"{r.distance_candles:.2f}", r.touches, fmt_million(r.volume_24h),
        )

    def _show_results(self, near, closest, since, now) -> None:
        s = load_settings()
        self.heading_var.set(
            f"Distance to a cascade <= {s.approach_candles:g} average 1-min candle(s) "
            f"(24h volume > {s.min_volume_million:g}M, NATR >= {s.min_natr:g}%, trades >= {s.min_trades})"
        )
        self.updated_var.set(f"Updated {time.strftime('%H:%M:%S', time.localtime(now))}; {len(near)} coin(s) match")
        self.near_list.set_rows(
            [self._row_values(r) + (time.strftime("%H:%M", time.localtime(since.get(r.key, now))),) for r in near]
        )
        self.closest_heading_var.set(
            f"The {s.closest_count} coins closest to a cascade of at least {s.min_extremes} extremes "
            f"({s.level_interval} candles, tolerance {s.level_tolerance_percent:g}%)"
        )
        self.closest_list.set_rows([(i + 1,) + self._row_values(r) for i, r in enumerate(closest)])

    def _drain_messages(self) -> None:
        """Move messages from the monitor thread into the UI. One bad message never stops the loop."""
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self.add_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "hint":
                    self.hint_var.set(payload)
                elif kind == "results":
                    self._show_results(*payload)
                elif kind == "popup":
                    popups.show_popup(self, *payload)
            except Exception as exc:
                try:
                    self.add_log(f"UI error ({kind}): {exc}")
                except Exception:
                    pass

        if self.monitor is not None and not self.monitor.is_alive():
            self.monitor = None
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            self.check_button.config(state="disabled")
        self.after(200, self._drain_messages)

    def _on_close(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self.destroy()


def main() -> None:
    # Toast button click: Windows starts the app with binancelevels://open?symbol=XXXUSDT
    if len(sys.argv) > 1 and vataga.handle_url(sys.argv[1]):
        return
    vataga.register_protocol()
    App().mainloop()
