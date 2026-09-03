"""
NFC Attendance Reader - Desktop App version

Same logic as nfc_attendance_reader.py (raw UID -> webhook -> printed name),
but wrapped in a simple Tkinter window instead of a bare console, so it can
be packaged into a .exe and launched from a desktop icon.

Layout:
- Big status line (Waiting for card / Reader not found / etc.)
- Big "last scanned name" display
- Scrolling log of all taps and events below

The NFC polling loop runs on a background thread so the GUI never freezes
while waiting on the reader or the network.
"""

import csv
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime

import requests

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    # Lets the app still run with the drawn placeholder if Pillow isn't
    # installed, instead of crashing on startup.
    PIL_AVAILABLE = False

try:
    from smartcard.System import readers
    from smartcard.util import toHexString
    SMARTCARD_AVAILABLE = True
except ImportError:
    # Lets the window still open with a clear error message instead of
    # crashing before the UI even appears, e.g. if pyscard isn't installed.
    SMARTCARD_AVAILABLE = False

# ---------- CONFIG ----------
SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwy7zCJUsQPZOX4PU_PsZdwe9g-e8WvzULxX6jGL02XK_ftUWiiw5kT84pn6VCdlIQU/exec"
GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]
POLL_INTERVAL_SECONDS = 0.5
DUPLICATE_TAP_COOLDOWN_SECONDS = 5

# 👉 CHANGE THIS if you want a different folder name. Photos should be
# named after the card's UID, e.g. photos/047EA223C22A81.jpg
PHOTOS_DIR = "Camera Roll"
PHOTO_EXTENSIONS = [".jpg", ".jpeg", ".png", ".bmp", ".gif"]
AVATAR_SIZE = (280, 280)  # matches the canvas size below
FAILED_POSTS_LOG = "failed_posts.jsonl"

# 👉 Folder where a CSV backup of "Registro de Clientes" is saved every
# time the app starts, so there's always a local copy even if the Google
# Sheet becomes unreachable or gets accidentally edited/deleted.
BACKUP_DIR = "backups info clientes"

# Thread-safe queue: background thread pushes events, GUI thread drains them.
event_queue = queue.Queue()


def find_photo_for_uid(uid):
    """Looks in PHOTOS_DIR for a file named after this UID (any of
    PHOTO_EXTENSIONS, case-insensitive). Returns the file path if found,
    or None if there's no matching photo yet."""
    if not os.path.isdir(PHOTOS_DIR):
        return None
    for filename in os.listdir(PHOTOS_DIR):
        name, ext = os.path.splitext(filename)
        if name.upper() == uid.upper() and ext.lower() in PHOTO_EXTENSIONS:
            return os.path.join(PHOTOS_DIR, filename)
    return None


def log_failed_post(payload, error):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "payload": payload,
        "error": str(error),
    }
    try:
        with open(FAILED_POSTS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as file_err:
        event_queue.put(("log", f"Could not write failed posts log: {file_err}"))


def backup_roster_to_csv():
    """Fetches the full 'Registro de Clientes' roster from the Apps
    Script webhook (GET ?action=backup) and saves it as a timestamped
    CSV file in BACKUP_DIR. Runs once at app startup on a background
    thread so it never blocks the GUI from opening."""
    try:
        resp = requests.get(SCRIPT_URL, params={"action": "backup"}, timeout=15)
        resp.raise_for_status()
        rows = resp.json()  # list of lists: header row + one row per client

        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(BACKUP_DIR, "registro_de_clientes.csv")

        with open(backup_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        event_queue.put(("log", f"Backup del registro de clientes actualizado: {backup_path}"))
    except Exception as backup_err:
        event_queue.put(("log", f"No se pudo hacer el backup del registro de clientes: {backup_err}"))


def send_uid(uid):
    payload = {"uid": uid}
    try:
        resp = requests.post(SCRIPT_URL, json=payload, timeout=5)
        resp.raise_for_status()
        result = resp.json()
        # result is expected to be shaped like:
        # {"uid": ..., "name": ..., "membershipId": ...,
        #  "matriculaActiva": "Yes"/"No", "mensualidadActiva": "Yes"/"No",
        #  "proximoPago": "X dias"}
        event_queue.put(("tap", uid, result))
    except Exception as post_err:
        event_queue.put(("log", f"Error enviando el registro deL cliente con UID: {uid}. **ERROR 404 Client/Script**"))
        log_failed_post(payload, post_err)


def polling_loop(stop_event):
    """Runs on a background thread. Mirrors the console script's main loop,
    but reports status/events through the queue instead of print()."""

    if not SMARTCARD_AVAILABLE:
        event_queue.put(("status", "ERROR: pyscard not installed"))
        event_queue.put(("log", "Install it with: pip install pyscard"))
        return

    available_readers = readers()
    if not available_readers:
        event_queue.put(("status", "No se encontro el lector NFC"))
        event_queue.put(("log", "Conecte el lector y reinicie el app..."))
        return

    reader = available_readers[0]
    event_queue.put(("status", f"Ready - using {reader}"))
    event_queue.put(("log", f"Registro de entrada de los clientes:"))

    last_uid = None
    last_uid_time = 0

    while not stop_event.is_set():
        try:
            conn = reader.createConnection()
            conn.connect()
            data, sw1, sw2 = conn.transmit(GET_UID)

            if sw1 == 0x90:
                uid = toHexString(data).replace(" ", "")
                now = time.time()

                if uid != last_uid or (now - last_uid_time) > DUPLICATE_TAP_COOLDOWN_SECONDS:
                    event_queue.put(("status", "Tarjeta NFC detectada - enviando informacion para ser verificada..."))
                    send_uid(uid)
                    last_uid = uid
                    last_uid_time = now

            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception:
            last_uid = None
            if event_queue.empty():
                event_queue.put(("status", "Esperando en leer las tarjetas NFC de los clientes..."))
            time.sleep(POLL_INTERVAL_SECONDS)


class NfcAttendanceApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NFC Attendance Reader")
        self.root.configure(bg="#1e1e2e")

        # Center the window on whatever screen it opens on, and clamp its
        # size so it never exceeds the actual screen dimensions - fixes
        # the window opening partially off-screen on a different monitor
        # or a lower-resolution display than the one it was built on.
        self._center_window(root, desired_width=1080, desired_height=650)
        self.root.minsize(1300, 500)

        status_font = tkfont.Font(size=13)
        label_font = tkfont.Font(size=15)
        value_font = tkfont.Font(size=18, weight="bold")
        name_value_font = tkfont.Font(size=22, weight="bold")
        log_font = tkfont.Font(family="Consolas", size=9)

        # --- Status line (spans full width, centered) ---
        self.status_var = tk.StringVar(value="Starting...")
        tk.Label(
            root, textvariable=self.status_var, font=status_font,
            fg="#a6adc8", bg="#1e1e2e"
        ).pack(pady=(14, 10))

        # --- Two-column body: log on the left, name + avatar on the right ---
        body = tk.Frame(root, bg="#1e1e2e")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        body.columnconfigure(0, weight=2)  # log
        body.columnconfigure(1, weight=3)  # info panel now gets more room
        body.rowconfigure(0, weight=1)

        # --- Left: scrolling log ---
        log_frame = tk.Frame(body, bg="#1e1e2e")
        log_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")

        self.log_box = tk.Text(
            log_frame, font=log_font, bg="#11111b", fg="#cdd6f4",
            wrap="word", yscrollcommand=scrollbar.set, state="disabled",
            borderwidth=0, highlightthickness=0
        )
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log_box.yview)

        # --- Right: avatar photo on top, labeled info fields below ---
        right_panel = tk.Frame(body, bg="#1e1e2e")
        right_panel.grid(row=0, column=1, sticky="nsew")

        # Avatar box: shows a photo matching the tapped UID's filename in
        # PHOTOS_DIR, or a gray silhouette placeholder before any tap /
        # if no matching photo exists yet.
        self.avatar_canvas = tk.Canvas(
            right_panel, width=280, height=280, bg="white",
            highlightthickness=0
        )
        self.avatar_canvas.pack(pady=(16, 14))
        self.draw_placeholder_silhouette()

        # --- Info rows: Name / ID / Matricula Activa / Mensualidad Activa ---
        info_frame = tk.Frame(right_panel, bg="#1e1e2e")
        info_frame.pack(anchor="w", padx=8)
        info_frame.columnconfigure(1, weight=1)

        self.name_var = tk.StringVar(value="\u2014")
        self.id_var = tk.StringVar(value="\u2014")
        self.matricula_var = tk.StringVar(value="\u2014")
        self.mensualidad_var = tk.StringVar(value="\u2014")
        self.proximo_pago_var = tk.StringVar(value="\u2014")

        self._add_info_row(info_frame, 0, "Nombre:", self.name_var, label_font, name_value_font, wraplength=340)
        self._add_info_row(info_frame, 1, "ID:", self.id_var, label_font, value_font)
        self.matricula_label = self._add_info_row(info_frame, 2, "Matricula Activa:", self.matricula_var, label_font, value_font)
        self.mensualidad_label = self._add_info_row(info_frame, 3, "Mensualidad Activa:", self.mensualidad_var, label_font, value_font)
        self._add_info_row(info_frame, 4, "Proximo pago:", self.proximo_pago_var, label_font, value_font)

        # --- Background thread setup ---
        # Backup runs once at startup, on its own thread, so a slow/failed
        # network call never delays the GUI opening or blocks NFC polling.
        self.backup_thread = threading.Thread(target=backup_roster_to_csv, daemon=True)
        self.backup_thread.start()

        self.stop_event = threading.Event()
        self.worker_thread = threading.Thread(
            target=polling_loop, args=(self.stop_event,), daemon=True
        )
        self.worker_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self.drain_queue)

    @staticmethod
    def _center_window(root, desired_width, desired_height):
        """Sizes the window to desired_width x desired_height (or smaller,
        if the screen itself is smaller), then positions it centered on
        the screen instead of leaving placement to the OS default, which
        can land partially off-screen on a different monitor/resolution."""
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        # Leave a small margin so the window doesn't butt right up against
        # screen edges/taskbars on smaller displays.
        margin = 40
        width = min(desired_width, screen_width - margin)
        height = min(desired_height, screen_height - margin)

        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)

        root.geometry(f"{width}x{height}+{x}+{y}")

    def _add_info_row(self, parent, row, label_text, value_var, label_font, value_font, wraplength=None):
        """Builds one 'Label: value' row used in the info panel under the
        avatar. Returns the value Label widget so its color can be
        updated dynamically (e.g. green/red for Yes/No status).

        wraplength (pixels): if set, long values wrap onto additional
        lines instead of overflowing past the window edge - used for the
        Name row, where names can vary a lot in length."""
        tk.Label(
            parent, text=label_text, font=label_font,
            fg="#a6adc8", bg="#1e1e2e", anchor="w"
        ).grid(row=row, column=0, sticky="nw", pady=6)
        value_label = tk.Label(
            parent, textvariable=value_var, font=value_font,
            fg="#89dceb", bg="#1e1e2e", anchor="w", justify="left",
            wraplength=wraplength
        )
        value_label.grid(row=row, column=1, sticky="w", padx=(12, 0), pady=6)
        return value_label

    def show_avatar_for_uid(self, uid):
        """Looks up a photo matching this UID in PHOTOS_DIR and displays
        it, resized to fit the avatar canvas. Falls back to the drawn
        placeholder silhouette if no matching photo exists, the file
        can't be opened, or Pillow isn't installed."""
        c = self.avatar_canvas
        c.delete("all")

        if not PIL_AVAILABLE:
            self.append_log("Pillow not installed - showing placeholder. Run: pip install pillow")
            self.draw_placeholder_silhouette()
            return

        photo_path = find_photo_for_uid(uid)
        if photo_path is None:
            self.append_log(f"No se encontro foto del cliente en '{PHOTOS_DIR}/' - [FAVOR TOMARLE LA FOTO]")
            self.draw_placeholder_silhouette()
            return

        try:
            img = Image.open(photo_path)
            img = img.resize(AVATAR_SIZE, Image.LANCZOS)
            # Keep a reference on self - Tkinter drops PhotoImage objects
            # that aren't referenced anywhere, which would blank the canvas.
            self.avatar_image = ImageTk.PhotoImage(img)
            c.create_image(0, 0, anchor="nw", image=self.avatar_image)
        except Exception as img_err:
            self.append_log(f"Could not load '{photo_path}': {img_err}")
            self.draw_placeholder_silhouette()

    def draw_placeholder_silhouette(self):
        """Simple generic person silhouette, used as a fallback when no
        real photo is available. Scaled to fit a 280x280 canvas."""
        c = self.avatar_canvas
        c.create_oval(100, 55, 180, 135, fill="#b8bcc4", outline="")
        c.create_oval(45, 155, 235, 325, fill="#c7cbd1", outline="")

    @staticmethod
    def _status_color(status_text):
        """Green for an active/'Yes'/'Si' status, red otherwise."""
        normalized = str(status_text).strip().lower()
        if normalized in ("yes", "si", "sí", "true", "active", "activo"):
            return "#a6e3a1"  # green
        return "#f38ba8"  # red

    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def drain_queue(self):
        """Runs on the GUI thread; pulls events from the background thread
        and updates the widgets. Tkinter widgets must only be touched from
        the main thread, hence this poll-and-drain pattern."""
        try:
            while True:
                event = event_queue.get_nowait()
                kind = event[0]

                if kind == "status":
                    self.status_var.set(event[1])
                elif kind == "log":
                    self.append_log(event[1])
                elif kind == "tap":
                    uid, result = event[1], event[2]
                    name = result.get("name", "UNKNOWN")
                    membership_id = result.get("membershipId", "\u2014")
                    matricula = result.get("matriculaActiva", "\u2014")
                    mensualidad = result.get("mensualidadActiva", "\u2014")
                    proximo_pago = result.get("proximoPago", "\u2014")

                    self.name_var.set(name)
                    self.id_var.set(membership_id)
                    self.matricula_var.set(matricula)
                    self.mensualidad_var.set(mensualidad)
                    self.proximo_pago_var.set(proximo_pago)

                    # Color-code active/inactive status: green for Yes/Si, red otherwise
                    self.matricula_label.configure(fg=self._status_color(matricula))
                    self.mensualidad_label.configure(fg=self._status_color(mensualidad))

                    self.status_var.set("Esperando en leer las tarjetas NFC de los clientes...")
                    self.append_log(f"UID: {uid} | Nombre: {name}")
                    self.show_avatar_for_uid(uid)
        except queue.Empty:
            pass

        self.root.after(150, self.drain_queue)

    def on_close(self):
        self.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = NfcAttendanceApp(root)
    root.mainloop()