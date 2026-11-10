"""
Treadmill Logger
----------------
Reads serial data from up to 6 treadmill devices and writes CSV logs.
Supports combined or per-device logs, ID read/set, and live UI status.
"""

import csv
import glob
import re
from collections import deque
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
except Exception:  # pragma: no cover
    matplotlib = None
    Figure = None
    FigureCanvasTkAgg = None

# Optional dependency: pyserial. If missing, the UI will still load,
# but serial features will show an error until installed.
try:
    import serial
    from serial.tools import list_ports
except Exception as exc:  # pragma: no cover
    serial = None
    list_ports = None

try:
    from openpyxl import Workbook
except Exception:  # pragma: no cover
    Workbook = None


# ===== Configuration =====
DEFAULT_BAUD = 192000
MAX_DEVICES = 6
RECONNECT_DELAY_SEC = 2.0
READ_TIMEOUT_SEC = 0.5
ROW_LIMIT = 524_288
# If no data arrives within this window, show RX as idle.
RX_TIMEOUT_SEC = 1.0
SPEED_STOP_TIMEOUT_SEC = 1.0


@dataclass
class DeviceConfig:
    # One device row from the GUI.
    enabled: bool
    port: str
    device_id: str
    device_label: str


@dataclass
class LogRecord:
    # One parsed serial line with timestamps added.
    timestamp_iso: str
    elapsed_s: float
    device_id: str
    device_label: str
    port: str
    distance_mm: Optional[float]
    speed_mms: Optional[float]
    device_micros: Optional[int]


# Background thread that reads one treadmill's serial stream.
# Background thread that reads one treadmill's serial stream.
class SerialWorker(threading.Thread):
    def __init__(self, cfg: DeviceConfig, record_queue_ref, status_cb, ui_queue: queue.Queue, graph_queue: queue.Queue, start_mono_ref):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.record_queue_ref = record_queue_ref
        self.status_cb = status_cb
        self.ui_queue = ui_queue
        self.graph_queue = graph_queue
        self.start_mono_ref = start_mono_ref
        self._stop_event = threading.Event()

    def stop(self):
        # Tell the thread to exit its loop.
        self._stop_event.set()

    def stop_and_wait(self, timeout: float = 1.0):
        # Request stop and wait briefly for thread to exit.
        self._stop_event.set()
        self.join(timeout=timeout)

    def run(self):
        # Keep trying to connect and read until stopped.
        while not self._stop_event.is_set():
            if not self.cfg.enabled or not self.cfg.port:
                # Disabled or no port selected.
                self.status_cb(self.cfg.port, "disabled")
                time.sleep(0.5)
                continue

            if serial is None:
                # pyserial isn't installed.
                self.status_cb(self.cfg.port, "pyserial missing")
                time.sleep(1.0)
                continue

            try:
                self.status_cb(self.cfg.port, "connecting")
                # Open the serial port and read line-by-line.
                with serial.Serial(
                    self.cfg.port,
                    DEFAULT_BAUD,
                    timeout=READ_TIMEOUT_SEC,
                    write_timeout=READ_TIMEOUT_SEC,
                ) as ser:
                    self.status_cb(self.cfg.port, "connected")
                    while not self._stop_event.is_set():
                        # Read one line at a time from the device.
                        line = ser.readline()
                        if not line:
                            continue
                        try:
                            text = line.decode("utf-8", errors="ignore").strip()
                        except Exception:
                            continue
                        if not text:
                            continue
                        rec = self._parse_line(text)
                        if rec:
                            # Send to file writer (if enabled) and UI updater.
                            q = self.record_queue_ref.get("queue")
                            if q is not None:
                                q.put(rec)
                            # UI only needs the most recent state; keep these queues bounded so
                            # a fast serial stream cannot starve Tk's event loop.
                            self._put_latest(self.graph_queue, rec)
                            self._put_latest(self.ui_queue, rec)
            except Exception:
                # If the port drops, wait and then retry.
                self.status_cb(self.cfg.port, "disconnected")
                time.sleep(RECONNECT_DELAY_SEC)

    @staticmethod
    def _put_latest(target: queue.Queue, item):
        try:
            target.put_nowait(item)
        except queue.Full:
            try:
                target.get_nowait()
            except queue.Empty:
                pass
            try:
                target.put_nowait(item)
            except queue.Full:
                pass

    def _parse_line(self, text: str) -> Optional[LogRecord]:
        # Accept:
        #   distance,speed
        # or
        #   micros,distance,speed
        parts = [p.strip() for p in text.split(",") if p.strip() != ""]
        device_micros = None
        distance = None
        speed = None

        if len(parts) == 2:
            try:
                distance = float(parts[0])
                speed = float(parts[1])
            except Exception:
                return None
        elif len(parts) >= 3:
            try:
                device_micros = int(float(parts[0]))
                distance = float(parts[1])
                speed = float(parts[2])
            except Exception:
                return None
        else:
            return None

        # Timestamp the record at receipt time.
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        # Elapsed seconds since Start was pressed.
        elapsed_s = 0.0
        t0 = self.start_mono_ref.get("t0")
        if t0 is not None:
            elapsed_s = time.monotonic() - t0
        return LogRecord(
            timestamp_iso=timestamp_iso,
            elapsed_s=elapsed_s,
            device_id=self.cfg.device_id or "",
            device_label=self.cfg.device_label,
            port=self.cfg.port,
            distance_mm=distance,
            speed_mms=speed,
            device_micros=device_micros,
        )


class SyncWorker(threading.Thread):
    """Read TTL state/edge messages from a separate Arduino Uno serial port."""
    def __init__(self, port: str, event_queue: queue.Queue):
        super().__init__(daemon=True)
        self.port = port
        self.event_queue = event_queue
        self._stop_event = threading.Event()

    def stop_and_wait(self, timeout: float = 1.0):
        self._stop_event.set()
        self.join(timeout=timeout)

    def run(self):
        if serial is None:
            self.event_queue.put({"kind": "status", "text": "pyserial missing"})
            return
        while not self._stop_event.is_set():
            try:
                self.event_queue.put({"kind": "status", "text": f"Connecting {self.port}"})
                with serial.Serial(self.port, 115200, timeout=0.2, write_timeout=0.5) as ser:
                    # Opening an Uno's USB serial port resets it; query its current level after boot.
                    if self._stop_event.wait(1.0):
                        break
                    ser.reset_input_buffer()
                    ser.write(b"SYNC?\n")
                    ser.flush()
                    self.event_queue.put({"kind": "status", "text": f"Listening on {self.port}"})
                    while not self._stop_event.is_set():
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("ascii", errors="ignore").strip().upper()
                        parts = [part.strip() for part in line.split(",")]
                        if len(parts) < 3 or parts[0] != "SYNC":
                            continue
                        if parts[1] == "STATE" and parts[2] in ("HIGH", "LOW", "1", "0"):
                            active = parts[2] in ("HIGH", "1")
                        elif parts[1] == "EDGE" and parts[2] in ("RISING", "FALLING", "HIGH", "LOW"):
                            active = parts[2] in ("RISING", "HIGH")
                        else:
                            continue
                        try:
                            source_micros = int(parts[3]) if len(parts) > 3 else None
                        except ValueError:
                            source_micros = None
                        self.event_queue.put({
                            "kind": "state", "active": active,
                            "source_micros": source_micros,
                            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
                            "received_mono": time.monotonic(),
                        })
            except Exception as exc:
                self.event_queue.put({"kind": "status", "text": f"Sync disconnected: {exc}"})
                self._stop_event.wait(RECONNECT_DELAY_SEC)


# Background thread that writes incoming records to CSV files.
class LogWriter(threading.Thread):
    def __init__(self, record_queue: queue.Queue, base_path: str, combined: bool, combined_labels: Optional[List[str]] = None):
        super().__init__(daemon=True)
        # Queue of LogRecord entries to write.
        self.record_queue = record_queue
        # Base output path without extension/suffix.
        self.base_path = base_path
        # Combined = one wide CSV, otherwise one CSV per device.
        self.combined = combined
        self._stop_event = threading.Event()
        self._files = {}
        self._writers = {}
        self._combined_header = None
        self._row_count = {}
        self._file_index = {}
        if combined_labels:
            # Build the combined header up-front so the first row has all columns.
            cols = ["timestamp_iso", "elapsed_s"]
            for label in combined_labels:
                cols.extend(
                    [f"{label}_position_mm", f"{label}_speed_mm_s", f"{label}_micros"]
                )
            self._combined_header = cols

    def stop(self):
        # Signal the writer to stop once the queue is drained.
        self._stop_event.set()

    def run(self):
        # Drain the queue and write all records to disk.
        while not self._stop_event.is_set() or not self.record_queue.empty():
            try:
                rec = self.record_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # Write each record to the appropriate file.
            self._write_record(rec)

        # Close any open files.
        for f in self._files.values():
            try:
                f.flush()
                f.close()
            except Exception:
                pass

    def _path_for(self, key: str, index: int) -> str:
        # Map device key + rotation index to a file path.
        if self.combined:
            if index <= 1:
                return self.base_path + ".csv"
            return f"{self.base_path}_{index}.csv"
        if index <= 1:
            return f"{self.base_path}_{key}.csv"
        return f"{self.base_path}_{key}_{index}.csv"

    def _open_writer(self, key: str, header: List[str]):
        # Open a CSV writer for this key (combined or per-device).
        if key in self._writers:
            return self._writers[key]
        os.makedirs(os.path.dirname(self.base_path), exist_ok=True)
        index = self._file_index.get(key, 1)
        path = self._path_for(key, index)
        # Append if file exists; create if not.
        f = open(path, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            # New file: write header and start row count at 1.
            writer.writerow(header)
            self._row_count[key] = 1
        else:
            try:
                # Count rows when appending to an existing file so rotation stays correct.
                with open(path, "r", newline="") as count_f:
                    self._row_count[key] = sum(1 for _ in count_f)
            except Exception:
                self._row_count[key] = 0
        self._files[key] = f
        self._writers[key] = writer
        return writer

    def _rotate_if_needed(self, key: str, header: List[str]):
        # If the current file hit the row limit, close it and open a new one.
        if self._row_count.get(key, 0) < ROW_LIMIT:
            return
        f = self._files.get(key)
        if f:
            try:
                f.flush()
                f.close()
            except Exception:
                pass
        self._files.pop(key, None)
        self._writers.pop(key, None)
        self._file_index[key] = self._file_index.get(key, 1) + 1
        self._row_count[key] = 0
        self._open_writer(key, header)

    def _write_record(self, rec: LogRecord):
        # Write one record to the correct file layout.
        if self.combined:
            # Combined mode: one wide row with per-device columns.
            label = rec.device_label
            col_pos = f"{label}_position_mm"
            col_spd = f"{label}_speed_mm_s"
            col_mic = f"{label}_micros"
            if self._combined_header is None:
                self._combined_header = ["timestamp_iso", "elapsed_s", col_pos, col_spd, col_mic]
            else:
                for col in (col_pos, col_spd, col_mic):
                    if col not in self._combined_header:
                        self._combined_header.append(col)

            # Ensure file is open and below the row limit.
            writer = self._open_writer("combined", self._combined_header)
            self._rotate_if_needed("combined", self._combined_header)
            writer = self._open_writer("combined", self._combined_header)
            row = [""] * len(self._combined_header)
            row[0] = rec.timestamp_iso
            row[1] = f"{rec.elapsed_s:.6f}"
            # Place values into the device-specific columns.
            row[self._combined_header.index(col_pos)] = rec.distance_mm
            row[self._combined_header.index(col_spd)] = rec.speed_mms
            row[self._combined_header.index(col_mic)] = (
                rec.device_micros if rec.device_micros is not None else ""
            )
            writer.writerow(row)
            self._row_count["combined"] = self._row_count.get("combined", 0) + 1
        else:
            # Per-device mode: each device gets its own file.
            key = rec.device_id if rec.device_id else rec.port.replace("/", "_")
            header = ["timestamp_iso", "elapsed_s", "distance_mm", "speed_mm_s", "device_micros"]
            writer = self._open_writer(key, header)
            self._rotate_if_needed(key, header)
            writer = self._open_writer(key, header)
            writer.writerow(
                [
                    rec.timestamp_iso,
                    f"{rec.elapsed_s:.6f}",
                    rec.distance_mm,
                    rec.speed_mms,
                    rec.device_micros if rec.device_micros is not None else "",
                ]
            )
            self._row_count[key] = self._row_count.get(key, 0) + 1


class WorkbookExportWorker(threading.Thread):
    """Export a completed recording and its plotted samples to separate Excel sheets."""
    def __init__(self, log_writer, base_path, combined, device_specs,
                 session_start_iso, session_end_iso, graph_data, sync_path,
                 output_path, status_queue):
        super().__init__(daemon=False)
        self.log_writer = log_writer
        self.base_path = base_path
        self.combined = combined
        self.device_specs = device_specs
        self.session_start_iso = session_start_iso
        self.session_end_iso = session_end_iso
        self.graph_data = graph_data
        self.sync_path = sync_path
        self.output_path = output_path
        self.status_queue = status_queue

    def _paths_for(self, key=None):
        base_name = os.path.basename(self.base_path)
        paths = glob.glob(self.base_path + "*.csv")
        if self.combined:
            pattern = re.compile(r"^" + re.escape(base_name) + r"(?:_\d+)?\.csv$", re.IGNORECASE)
            patterns = [pattern]
        else:
            # A reader worker can retain an older device ID after the UI ID is
            # changed. The writer then names the CSV by COM port, so include
            # both the configured ID and port fallback when exporting a device.
            keys = key if isinstance(key, (list, tuple, set)) else [key]
            patterns = [
                re.compile(
                    r"^" + re.escape(base_name + "_" + str(candidate))
                    + r"(?:_\d+)?\.csv$",
                    re.IGNORECASE,
                )
                for candidate in keys if candidate
            ]
        return sorted({
            path for path in paths
            if any(pattern.match(os.path.basename(path)) for pattern in patterns)
        })

    def _write_raw_sheet(self, ws, label, paths, combined):
        wrote_header = False
        for path in paths:
            try:
                with open(path, "r", newline="", encoding="utf-8-sig") as source:
                    reader = csv.reader(source)
                    header = next(reader, None)
                    if not header:
                        continue
                    if not wrote_header:
                        if combined:
                            ws.append(["source_file"] + header)
                        else:
                            ws.append(["device_label", "source_file"] + header)
                        wrote_header = True
                    for row in reader:
                        if not row:
                            continue
                        stamp = row[0] if row else ""
                        if stamp < self.session_start_iso or stamp > self.session_end_iso:
                            continue
                        if combined:
                            ws.append([os.path.basename(path)] + row)
                        else:
                            ws.append([label, os.path.basename(path)] + row)
            except (OSError, csv.Error) as exc:
                self.status_queue.put({"kind": "export", "text": f"Workbook CSV read error: {exc}"})
        if not wrote_header:
            if combined:
                ws.append(["source_file", "timestamp_iso", "elapsed_s"])
            else:
                ws.append(["device_label", "source_file", "timestamp_iso", "elapsed_s", "distance_mm", "speed_mm_s", "device_micros"])

    def run(self):
        if Workbook is None:
            self.status_queue.put({"kind": "export", "text": "Excel export unavailable: install openpyxl"})
            return
        try:
            # Wait until the CSV writer has drained and closed its output files.
            self.log_writer.join()
            workbook = Workbook(write_only=True)
            info = workbook.create_sheet("SessionInfo")
            info.append(["session_start_iso", self.session_start_iso])
            info.append(["session_end_iso", self.session_end_iso])
            if self.combined:
                raw = workbook.create_sheet("TreadmillData")
                self._write_raw_sheet(raw, "", self._paths_for(), True)
            else:
                wrote_any = False
                for label, keys in self.device_specs:
                    paths = self._paths_for(keys)
                    if not paths:
                        continue
                    safe_label = re.sub(r"[\\/*?:\[\]]", "_", label)[:22] or "Device"
                    raw = workbook.create_sheet(("Raw_" + safe_label)[:31])
                    self._write_raw_sheet(raw, label, paths, False)
                    wrote_any = True
                if not wrote_any:
                    raw = workbook.create_sheet("TreadmillData")
                    raw.append(["device_label", "source_file", "timestamp_iso", "elapsed_s", "distance_mm", "speed_mm_s", "device_micros"])

            graph = workbook.create_sheet("GraphData")
            graph.append(["time_s", "device", "speed_mm_s", "direction"])
            for _port, samples in self.graph_data:
                for t, speed, direction, label in samples:
                    graph.append([round(t, 6), label, speed, direction])

            sync = workbook.create_sheet("SyncEvents")
            sync_written = False
            if os.path.exists(self.sync_path):
                with open(self.sync_path, "r", newline="", encoding="utf-8-sig") as source:
                    reader = csv.reader(source)
                    header = next(reader, None)
                    if header:
                        sync.append(header)
                        sync_written = True
                    for row in reader:
                        if row and row[0] == self.session_start_iso:
                            sync.append(row)
            if not sync_written:
                sync.append(["session_start_iso", "edge_timestamp_iso", "elapsed_s", "ttl_state", "source_micros"])

            workbook.save(self.output_path)
            self.status_queue.put({"kind": "export", "text": f"Excel saved: {self.output_path}"})
        except Exception as exc:
            self.status_queue.put({"kind": "export", "text": f"Excel export failed: {type(exc).__name__}: {exc}"})


# Main GUI application.
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Treadmill Logger (PID {os.getpid()})")
        self.geometry("1120x565")

        # Queues for cross-thread communication.
        self.record_queue = queue.Queue()
        self.record_queue_ref = {"queue": None}
        # Serial records are losslessly retained by record_queue while recording.
        # These two queues feed live UI state and deliberately keep only a short backlog.
        self.ui_queue = queue.Queue(maxsize=MAX_DEVICES * 4)
        self.graph_queue = queue.Queue(maxsize=MAX_DEVICES * 4)
        self.monitor_workers = {}
        self.writer: Optional[LogWriter] = None
        # State flags.
        self.logging = False
        self.start_mono = None
        self.start_mono_ref = {"t0": None}
        self.last_rx = {}
        self.monitor_paused = False
        self.recording_blink = False
        self.graph_t0 = time.monotonic()
        self.graph_data = {}
        self.graph_sample_total = 0
        self.graph_latest = {}
        self.graph_inputs = {}
        self.graph_sample_last = {}
        self.connected_since = {}
        self.graph_window = None
        self.graph_canvas = None
        self.graph_lines = {}
        self.graph_max_hz = tk.IntVar(value=10)
        self.graph_last_draw = 0.0
        self.sync_enabled_var = tk.BooleanVar(value=False)
        self.sync_port_var = tk.StringVar(value="")
        self.sync_status_var = tk.StringVar(value="TTL sync disabled")
        self.sync_event_queue = queue.Queue()
        self.export_status_queue = queue.Queue()
        self.sync_worker = None
        self.sync_worker_port = None
        self.sync_active_state = None
        self.sync_start_pending = False
        self.pending_sync_start_event = None
        self.current_base_path = None
        self.session_start_iso = None

        # Build UI and populate port list.
        self._build_ui()
        self._refresh_ports()
        self._ensure_monitor_workers()

    def _build_ui(self):
        # Top bar with file settings and control buttons.
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        ttk.Label(top, text="Base File Name").grid(row=0, column=0, sticky="w")
        self.base_name_var = tk.StringVar(value="treadmill_log")
        ttk.Entry(top, textvariable=self.base_name_var, width=28).grid(
            row=0, column=1, sticky="w", padx=(8, 16)
        )

        ttk.Label(top, text="Directory").grid(row=0, column=2, sticky="w")
        self.dir_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(top, textvariable=self.dir_var, width=40).grid(
            row=0, column=3, sticky="w", padx=(8, 8)
        )
        ttk.Button(top, text="Browse", command=self._browse_dir).grid(
            row=0, column=4, sticky="w"
        )

        ttk.Label(top, text="Log Mode").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.combined_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(top, text="Combined", variable=self.combined_var, value=True).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Radiobutton(
            top, text="Per Device", variable=self.combined_var, value=False
        ).grid(row=1, column=1, sticky="e", pady=(8, 0))

        ttk.Button(top, text="Rescan Ports", command=self._refresh_ports).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        ttk.Button(top, text="Auto-Assign ESP32-S2", command=self._auto_assign_esp32s2).grid(
            row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        graph_controls = ttk.Frame(top)
        graph_controls.grid(row=2, column=0, columnspan=8, sticky="w", pady=(8, 0))
        self.graph_button = ttk.Button(graph_controls, text="Live Graph", command=self._open_graph)
        self.graph_button.pack(side="left")
        ttk.Label(graph_controls, text="Sampling frequency (Hz)").pack(side="left", padx=(10, 4))
        ttk.Spinbox(graph_controls, from_=1, to=30, textvariable=self.graph_max_hz, width=4).pack(
            side="left"
        )

        sync_controls = ttk.Frame(top)
        sync_controls.grid(row=3, column=0, columnspan=8, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            sync_controls, text="TTL start/stop", variable=self.sync_enabled_var,
            command=self._ensure_sync_worker,
        ).pack(side="left")
        ttk.Label(sync_controls, text="Sync port").pack(side="left", padx=(10, 4))
        self.sync_port_combo = ttk.Combobox(
            sync_controls, textvariable=self.sync_port_var, values=[], width=10, state="readonly"
        )
        self.sync_port_combo.pack(side="left")
        self.sync_port_combo.bind("<<ComboboxSelected>>", lambda _event: self._ensure_sync_worker())
        ttk.Label(sync_controls, textvariable=self.sync_status_var).pack(side="left", padx=(10, 0))

        self.start_button = ttk.Button(top, text="Start", command=self.start_logging)
        self.start_button.grid(row=1, column=4, sticky="w", pady=(8, 0))
        self.stop_button = ttk.Button(top, text="Stop", command=self.stop_logging, state="disabled")
        self.stop_button.grid(row=1, column=5, sticky="w", pady=(8, 0))

        # Recording indicator (blinks red while logging).
        ttk.Label(top, text="Recording").grid(row=1, column=6, sticky="w", padx=(12, 4), pady=(8, 0))
        self.recording_dot = ttk.Label(top, text="●", font=("Segoe UI", 12, "bold"), foreground="gray40")
        self.recording_dot.grid(row=1, column=7, sticky="w", pady=(8, 0))

        sep = ttk.Separator(self, orient="horizontal")
        sep.pack(fill="x", padx=12, pady=6)

        # Table of devices and their live status.
        self.rows_frame = ttk.Frame(self)
        self.rows_frame.pack(fill="both", expand=True, padx=12, pady=6)

        headers = ["Enable", "Port", "Device ID", "Position (mm)", "Status", "Receiving", ""]
        for col, title in enumerate(headers):
            ttk.Label(self.rows_frame, text=title).grid(row=0, column=col, sticky="w", padx=6)

        self.device_rows = []
        for i in range(MAX_DEVICES):
            enabled = tk.BooleanVar(value=False)
            port = tk.StringVar(value="")
            device_id = tk.StringVar(value="")
            position = tk.StringVar(value="")
            rx = tk.StringVar(value="●")
            status = tk.StringVar(value="idle")

            chk = ttk.Checkbutton(self.rows_frame, variable=enabled)
            chk.grid(row=i + 1, column=0, sticky="w", padx=6, pady=4)

            port_combo = ttk.Combobox(
                self.rows_frame, textvariable=port, values=[], width=16
            )
            port_combo.grid(row=i + 1, column=1, sticky="w", padx=6, pady=4)

            ttk.Entry(self.rows_frame, textvariable=device_id, width=18).grid(
                row=i + 1, column=2, sticky="w", padx=6, pady=4
            )

            ttk.Label(self.rows_frame, textvariable=position).grid(
                row=i + 1, column=3, sticky="w", padx=6, pady=4
            )

            ttk.Label(self.rows_frame, textvariable=status).grid(
                row=i + 1, column=4, sticky="w", padx=6, pady=4
            )

            rx_label = ttk.Label(self.rows_frame, textvariable=rx, font=("Segoe UI", 12, "bold"))
            rx_label.grid(row=i + 1, column=5, sticky="w", padx=6, pady=4)

            id_btn = ttk.Button(
                self.rows_frame, text="Read/Set ID", command=lambda idx=i: self._id_dialog(idx)
            )
            id_btn.grid(row=i + 1, column=6, sticky="w", padx=6, pady=4)

            self.device_rows.append(
                {
                    "enabled": enabled,
                    "port": port,
                    "device_id": device_id,
                    "position": position,
                    "rx": rx,
                    "status": status,
                    "port_combo": port_combo,
                    "rx_label": rx_label,
                    "id_btn": id_btn,
                }
            )

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=12, pady=6)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(footer, textvariable=self.status_var).pack(anchor="w")

        if serial is None:
            self.status_var.set("pyserial not installed; run 'pip install pyserial'")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_ui_queue)
        self.after(500, self._update_recording_indicator)

    def _browse_dir(self):
        # Let the user pick a folder for log files.
        path = filedialog.askdirectory()
        if path:
            self.dir_var.set(path)

    def _refresh_ports(self):
        # Query the system for available serial ports.
        ports = []
        if list_ports is not None:
            ports = [p.device for p in list_ports.comports()]
        for row in self.device_rows:
            row["port_combo"].configure(values=ports)
        self.sync_port_combo.configure(values=ports)
        if not ports:
            self.status_var.set("No serial ports found")
        else:
            self.status_var.set(f"Found {len(ports)} ports")
        self._ensure_monitor_workers()

    def _auto_assign_esp32s2(self):
        # Auto-fill rows with ports matching the ESP32-S2 VID/PID.
        if list_ports is None:
            self.status_var.set("pyserial not installed")
            return
        ports = list_ports.comports()
        matches = []
        for p in ports:
            desc = (p.description or "").upper()
            manuf = (p.manufacturer or "").upper()
            hwid = (p.hwid or "").upper()
            # Primary match: known Adafruit VID/PID for this board.
            if "VID:PID=239A:80ED" in hwid:
                matches.append(p.device)
                continue
            # Fallback match: human-readable strings.
            if "ESP32-S2" in desc or "ESP32-S2" in manuf or "FEATHER" in desc:
                matches.append(p.device)

        if not matches:
            self.status_var.set("No ESP32-S2 devices found")
            return

        for i, row in enumerate(self.device_rows):
            if i < len(matches):
                row["port"].set(matches[i])
                row["enabled"].set(True)
            else:
                row["enabled"].set(False)
        self.status_var.set(f"Auto-assigned {min(len(matches), MAX_DEVICES)} device(s)")
        self._ensure_monitor_workers()

    def _set_row_status(self, port: str, status: str):
        # Update the connection status text for a given port.
        for row in self.device_rows:
            if row["port"].get() == port:
                row["status"].set(status)
                if status == "connected":
                    self.connected_since.setdefault(port, time.monotonic())
                else:
                    self.connected_since.pop(port, None)

    def _set_row_position(self, port: str, distance_mm: float):
        # Update the live position readout and RX status for a given port.
        for row in self.device_rows:
            if row["port"].get() == port:
                row["position"].set(f"{distance_mm:.1f}")
                row["rx"].set("RX")
                self.last_rx[port] = time.time()

    def _build_configs(self) -> List[DeviceConfig]:
        # Convert the GUI table rows into DeviceConfig objects.
        cfgs = []
        for idx, row in enumerate(self.device_rows):
            device_id = row["device_id"].get().strip()
            label = device_id if device_id else f"DEV{idx + 1}"
            cfgs.append(
                DeviceConfig(
                    enabled=row["enabled"].get(),
                    port=row["port"].get().strip(),
                    device_id=device_id,
                    device_label=label,
                )
            )
        return cfgs

    def start_logging(self, from_sync: bool = False):
        # Validate settings, then start writer and enable logging on readers.
        if self.writer is not None:
            return
        base_name = self.base_name_var.get().strip()
        directory = self.dir_var.get().strip()
        if not base_name:
            if from_sync:
                self.sync_status_var.set("TTL HIGH — enter a base file name")
            else:
                messagebox.showerror("Missing", "Base file name is required")
            return False
        if not directory:
            if from_sync:
                self.sync_status_var.set("TTL HIGH — choose a save directory")
            else:
                messagebox.showerror("Missing", "Directory is required")
            return False
        base_path = os.path.join(directory, base_name)

        cfgs = self._build_configs()
        if not any(c.enabled and c.port for c in cfgs):
            if from_sync:
                self.sync_status_var.set("TTL HIGH — enable a treadmill row and select its port")
            else:
                messagebox.showerror("Missing", "Enable at least one device with a port")
            return False
        # Warn if we will append to existing files.
        existing = self._existing_log_paths(base_path, cfgs, self.combined_var.get())
        if existing and not from_sync:
            msg = "Existing log files will be appended:\n\n" + "\n".join(existing)
            if not messagebox.askyesno("File Exists", msg + "\n\nProceed?"):
                return

        combined_labels = None
        if self.combined_var.get():
            combined_labels = [c.device_label for c in cfgs if c.enabled and c.port]
        self.current_base_path = base_path
        self.session_start_iso = datetime.now(timezone.utc).isoformat()
        # Each session gets its own queue so a quick TTL stop/start cannot mix writer queues.
        self.record_queue = queue.Queue()
        self.writer = LogWriter(self.record_queue, base_path, self.combined_var.get(), combined_labels)
        self.writer.start()

        # Start elapsed timer and enable file writing.
        self.start_mono = time.monotonic()
        self.start_mono_ref["t0"] = self.start_mono
        self._reset_graph_for_recording(self.start_mono)
        self.record_queue_ref["queue"] = self.record_queue
        # Ensure background readers are running.
        self._ensure_monitor_workers()

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Logging")
        self.logging = True
        for row in self.device_rows:
            row["id_btn"].configure(state="disabled")
        return True

    def _reset_graph_for_recording(self, start_time: float):
        # Make the current recording start at t=0 on the graph.
        self.graph_t0 = start_time
        self.graph_data.clear()
        self.graph_sample_total = 0
        self.graph_latest.clear()
        self.graph_inputs.clear()
        self.graph_sample_last.clear()
        self.graph_last_draw = 0.0
        while True:
            try:
                self.graph_queue.get_nowait()
            except queue.Empty:
                break
        if self.graph_window is not None and self.graph_window.winfo_exists():
            for speed_line, direction_line in self.graph_lines.values():
                speed_line.set_data([], [])
                direction_line.set_data([], [])
            self.graph_speed_ax.set_xlim(0, 10)
            self.graph_speed_ax.relim()
            self.graph_speed_ax.autoscale_view(scalex=False, scaley=True)
            self.graph_hint.set_visible(True)
            self.graph_status_var.set(self._graph_status_text())
            self.graph_canvas.draw()

    def stop_logging(self):
        # Stop file writing and leave readers running for UI updates.
        writer = self.writer
        if writer is not None:
            session_end_iso = datetime.now(timezone.utc).isoformat()
            self.record_queue_ref["queue"] = None
            writer.stop()
            self.writer = None
            if Workbook is not None and self.current_base_path and self.session_start_iso:
                tag = datetime.fromisoformat(self.session_start_iso).strftime("%Y%m%d_%H%M%S")
                output_path = f"{self.current_base_path}_{tag}_session.xlsx"
                cfgs = self._build_configs()
                device_specs = [
                    (
                        cfg.device_label,
                        list(dict.fromkeys(filter(None, (
                            cfg.device_id,
                            cfg.port.replace("/", "_"),
                        )))),
                    )
                    for cfg in cfgs if cfg.enabled and cfg.port
                ]
                graph_snapshot = [(port, list(samples)) for port, samples in self.graph_data.items()]
                self.status_var.set("Stopped — saving Excel workbook")
                exporter = WorkbookExportWorker(
                    writer, self.current_base_path, self.combined_var.get(), device_specs,
                    self.session_start_iso, session_end_iso, graph_snapshot,
                    self.current_base_path + "_sync.csv", output_path,
                    self.export_status_queue,
                )
                exporter.start()
            else:
                self.status_var.set("Stopped — Excel export unavailable (install openpyxl)")
        self.start_mono = None
        self.start_mono_ref["t0"] = None
        self.record_queue_ref["queue"] = None

        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if writer is None:
            self.status_var.set("Stopped")
        self.logging = False
        for row in self.device_rows:
            row["id_btn"].configure(state="normal")
        # Background readers keep running for UI updates.
        self._ensure_monitor_workers()

    def _on_close(self):
        # Disable TTL reactions before closing so a late serial event cannot reopen logging.
        self.sync_enabled_var.set(False)
        if self.sync_worker is not None:
            self.sync_worker.stop_and_wait(timeout=1.0)
            self.sync_worker = None
        self.stop_logging()
        self.destroy()

    def _poll_ui_queue(self):
        # Process UI updates coming from worker threads.
        latest_ui = {}
        # Bound work per Tk tick. A producer can refill a queue while it is being
        # drained, so an unbounded while-loop can otherwise monopolize the GUI.
        for _ in range(self.ui_queue.maxsize):
            try:
                rec = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if rec and rec.distance_mm is not None:
                latest_ui[rec.port] = rec
        for rec in latest_ui.values():
            self._set_row_position(rec.port, rec.distance_mm)
        # Drain graph samples separately from the live table update queue.
        latest_graph = {}
        for _ in range(self.graph_queue.maxsize):
            try:
                rec = self.graph_queue.get_nowait()
            except queue.Empty:
                break
            if rec is not None and rec.speed_mms is not None:
                latest_graph[rec.port] = rec
        received_mono = time.monotonic()
        for rec in latest_graph.values():
            self.graph_inputs[rec.port] = (
                rec.speed_mms, received_mono, rec.device_label or rec.port
            )
        while True:
            try:
                export_event = self.export_status_queue.get_nowait()
            except queue.Empty:
                break
            self.status_var.set(export_event.get("text", "Excel export"))
        while True:
            try:
                sync_event = self.sync_event_queue.get_nowait()
            except queue.Empty:
                break
            if sync_event.get("kind") == "status":
                self.sync_status_var.set(sync_event.get("text", "TTL sync"))
            elif sync_event.get("kind") == "state":
                active = bool(sync_event["active"])
                if active != self.sync_active_state:
                    self.sync_active_state = active
                    self._handle_sync_state(sync_event)
        self._try_pending_sync_start()
        # Update RX indicators based on how recently data arrived.
        now = time.time()
        for row in self.device_rows:
            port = row["port"].get()
            last = self.last_rx.get(port)
            if last is None or now - last > RX_TIMEOUT_SEC:
                row["rx"].set("●")
                row["rx_label"].configure(foreground="gray40")
            else:
                row["rx"].set("●")
                row["rx_label"].configure(foreground="green")
        # Keep monitor workers in sync with the current UI selection.
        self._ensure_monitor_workers()
        self._ensure_sync_worker()
        self._sample_graph_if_due()
        try:
            self._draw_graph_if_due()
        except Exception as exc:
            if self.graph_status_var is not None:
                self.graph_status_var.set(
                    f"Graph render error: {type(exc).__name__}: {exc}"
                )
            print(f"Live graph render error: {type(exc).__name__}: {exc}")
        self.after(20, self._poll_ui_queue)

    def _sample_graph_if_due(self):
        # Only sample during an active recording; the plot remains frozen after Stop.
        if not self.logging:
            return
        try:
            hz = max(1, min(30, int(self.graph_max_hz.get())))
        except (ValueError, tk.TclError):
            hz = 10
        interval = 1.0 / hz
        now = time.monotonic()
        t = now - self.graph_t0
        if t < 0:
            return

        for row in self.device_rows:
            if not row["enabled"].get():
                continue
            port = row["port"].get().strip()
            if not port or row["status"].get() != "connected":
                continue
            last_sample = self.graph_sample_last.get(port)
            if last_sample is not None and now - last_sample < interval:
                continue

            current = self.graph_inputs.get(port)
            label = row["device_id"].get().strip() or f"DEV{self.device_rows.index(row) + 1}"
            if current is None:
                # No encoder edges while stopped: infer zero after the stop timeout.
                connected_at = self.connected_since.get(port, now)
                if now - connected_at < SPEED_STOP_TIMEOUT_SEC:
                    continue
                signed_speed = 0.0
            else:
                signed_speed, received_at, label = current
                if now - received_at >= SPEED_STOP_TIMEOUT_SEC:
                    signed_speed = 0.0

            speed = abs(signed_speed)
            direction = 1 if signed_speed > 0 else -1 if signed_speed < 0 else 0
            samples = self.graph_data.setdefault(port, deque(maxlen=30000))
            samples.append((t, speed, direction, label))
            self.graph_sample_last[port] = now
            self.graph_sample_total += 1
            self.graph_latest[port] = (speed, direction)

        if getattr(self, "graph_status_var", None) is not None:
            self.graph_status_var.set(self._graph_status_text())
        hint = getattr(self, "graph_hint", None)
        if self.graph_sample_total and hint is not None:
            hint.set_visible(False)

    def _graph_status_text(self):
        if not self.graph_latest:
            return f"PID {os.getpid()} | Graph samples: 0 (waiting for parsed serial data)"
        details = ", ".join(
            f"{next((row['device_id'].get().strip() for row in self.device_rows if row['port'].get().strip() == port), port) or port}: {speed:.3f} mm/s, direction {direction}"
            for port, (speed, direction) in self.graph_latest.items()
        )
        return f"PID {os.getpid()} | Graph samples: {self.graph_sample_total} | {details}"

    def _open_graph(self):
        if matplotlib is None or FigureCanvasTkAgg is None:
            messagebox.showerror(
                "Missing dependency",
                "Matplotlib is not installed. Activate the project venv and run: pip install -r requirements.txt",
            )
            return
        if self.graph_window is not None and self.graph_window.winfo_exists():
            self.graph_window.lift()
            return

        self.graph_window = tk.Toplevel(self)
        self.graph_window.title(f"Treadmill Live Graph (PID {os.getpid()})")
        self.graph_window.geometry("900x680")
        self.graph_window.protocol("WM_DELETE_WINDOW", self._close_graph)
        self.graph_status_var = tk.StringVar(value=self._graph_status_text())
        ttk.Label(self.graph_window, textvariable=self.graph_status_var, anchor="w").pack(
            fill="x", padx=8, pady=(6, 0)
        )
        fig = Figure(figsize=(9, 6), dpi=100, constrained_layout=True)
        self.graph_speed_ax = fig.add_subplot(2, 1, 1)
        self.graph_direction_ax = fig.add_subplot(2, 1, 2, sharex=self.graph_speed_ax)
        self.graph_speed_ax.set_title("Speed")
        self.graph_speed_ax.set_ylabel("Speed (mm/s)")
        self.graph_speed_ax.grid(True, alpha=0.3)
        self.graph_direction_ax.set_title("Direction")
        self.graph_direction_ax.set_ylabel("Direction")
        self.graph_direction_ax.set_xlabel("Time (s)")
        self.graph_direction_ax.set_yticks([-1, 0, 1], labels=["Backward (-1)", "Stopped (0)", "Forward (1)"])
        self.graph_direction_ax.set_ylim(-1.4, 1.4)
        self.graph_direction_ax.grid(True, alpha=0.3)
        self.graph_hint = self.graph_speed_ax.text(
            0.5, 0.5,
            "Waiting for serial samples. Enable a device and select its port in the main window.",
            transform=self.graph_speed_ax.transAxes,
            ha="center", va="center", color="dimgray", wrap=True,
        )
        self.graph_lines = {}
        self.graph_canvas = FigureCanvasTkAgg(fig, master=self.graph_window)
        self.graph_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.graph_canvas.draw()
        self.graph_last_draw = 0.0

    def _close_graph(self):
        if self.graph_window is not None:
            try:
                self.graph_window.destroy()
            except tk.TclError:
                pass
        self.graph_window = None
        self.graph_canvas = None
        self.graph_lines = {}

    def _draw_graph_if_due(self):
        if not self.logging:
            return
        if self.graph_window is None or not self.graph_window.winfo_exists():
            return
        # Sampling frequency controls stored samples. Canvas rendering is capped
        # separately because synchronous Matplotlib redraws are expensive.
        render_hz = 5
        now = time.monotonic()
        if now - self.graph_last_draw < 1.0 / render_hz:
            return
        self.graph_last_draw = now

        has_samples = any(samples for samples in self.graph_data.values())
        if has_samples:
            self.graph_hint.set_visible(False)
        for port, samples in self.graph_data.items():
            if not samples:
                continue
            label = samples[-1][3] or port
            if port not in self.graph_lines:
                speed_line, = self.graph_speed_ax.plot([], [], marker=".", markersize=3, label=label)
                direction_line, = self.graph_direction_ax.step(
                    [], [], where="post", marker=".", markersize=3, label=label
                )
                self.graph_lines[port] = (speed_line, direction_line)
            # Keep the complete session timeline from t=0, but decimate the
            # displayed points to bound Matplotlib's work as recordings grow.
            visible = list(samples)
            stride = max(1, (len(visible) + 1999) // 2000)
            plotted = visible[::stride]
            if plotted[-1] is not visible[-1]:
                plotted.append(visible[-1])
            xs = [sample[0] for sample in plotted]
            self.graph_lines[port][0].set_data(xs, [sample[1] for sample in plotted])
            self.graph_lines[port][1].set_data(xs, [sample[2] for sample in plotted])

        t_now = now - self.graph_t0
        self.graph_speed_ax.set_xlim(0.0, max(10.0, t_now))
        self.graph_speed_ax.relim()
        self.graph_speed_ax.autoscale_view(scalex=False, scaley=True)
        if self.graph_lines:
            self.graph_speed_ax.legend(loc="upper left")
            self.graph_direction_ax.legend(loc="upper left")
        self.graph_canvas.draw_idle()

    def _update_recording_indicator(self):
        # Blink the recording light while logging.
        if self.logging:
            self.recording_blink = not self.recording_blink
            if self.recording_blink:
                self.recording_dot.configure(foreground="red")
            else:
                self.recording_dot.configure(foreground="gray40")
        else:
            self.recording_dot.configure(foreground="gray40")
            self.recording_blink = False
        self.after(500, self._update_recording_indicator)

    def _ensure_sync_worker(self):
        desired_port = self.sync_port_var.get().strip() if self.sync_enabled_var.get() else ""
        if self.sync_enabled_var.get() and not desired_port:
            self.sync_status_var.set("Select the Uno sync port")
            return
        treadmill_ports = {
            row["port"].get().strip() for row in self.device_rows
            if row["enabled"].get() and row["port"].get().strip()
        }
        if desired_port and desired_port in treadmill_ports:
            self.sync_status_var.set("Choose a separate port for the Uno sync input")
            desired_port = ""
        if desired_port == self.sync_worker_port and (
            self.sync_worker is None or self.sync_worker.is_alive()
        ):
            return
        if self.sync_worker is not None:
            self.sync_worker.stop_and_wait(timeout=1.0)
            self.sync_worker = None
            self.sync_worker_port = None
        if not desired_port:
            if not self.sync_enabled_var.get():
                self.sync_status_var.set("TTL sync disabled")
            return
        if serial is None:
            self.sync_status_var.set("pyserial missing")
            return
        self.sync_worker_port = desired_port
        self.sync_active_state = None
        self.sync_worker = SyncWorker(desired_port, self.sync_event_queue)
        self.sync_worker.start()

    def _handle_sync_state(self, event):
        active = bool(event["active"])
        if active:
            self.sync_start_pending = True
            self.pending_sync_start_event = event
            self._try_pending_sync_start()
        else:
            self.sync_start_pending = False
            self.pending_sync_start_event = None
            if self.writer is not None:
                self._write_sync_event(event, "LOW")
                self.stop_logging()
            self.sync_status_var.set("TTL LOW — stopped")

    def _try_pending_sync_start(self):
        if not self.sync_enabled_var.get() or not self.sync_active_state:
            return
        if self.writer is not None:
            if self.sync_start_pending and self.pending_sync_start_event is not None:
                self._write_sync_event(self.pending_sync_start_event, "HIGH")
                self.sync_start_pending = False
                self.pending_sync_start_event = None
            return
        cfgs = self._build_configs()
        if not any(cfg.enabled and cfg.port for cfg in cfgs):
            self.sync_status_var.set("TTL HIGH — enable a treadmill row and select its port")
            return
        if not self.base_name_var.get().strip() or not self.dir_var.get().strip():
            self.sync_status_var.set("TTL HIGH — set the file name and save directory")
            return
        if self.start_logging(from_sync=True) and self.writer is not None:
            if self.pending_sync_start_event is not None:
                self._write_sync_event(self.pending_sync_start_event, "HIGH")
            self.sync_start_pending = False
            self.pending_sync_start_event = None
            self.sync_status_var.set("TTL HIGH — recording")

    def _write_sync_event(self, event, state: str):
        if not self.current_base_path or self.start_mono is None:
            return
        path = self.current_base_path + "_sync.csv"
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        elapsed = max(0.0, event.get("received_mono", time.monotonic()) - self.start_mono)
        with open(path, "a", newline="", encoding="utf-8") as sync_file:
            writer = csv.writer(sync_file)
            if new_file:
                writer.writerow([
                    "session_start_iso", "edge_timestamp_iso", "elapsed_s",
                    "ttl_state", "source_micros",
                ])
            writer.writerow([
                self.session_start_iso,
                event.get("timestamp_iso", datetime.now(timezone.utc).isoformat()),
                f"{elapsed:.6f}",
                state,
                event.get("source_micros") if event.get("source_micros") is not None else "",
            ])

    def _ensure_monitor_workers(self):
        # Ensure background readers are running (used for UI and optional logging).
        if self.monitor_paused:
            self._stop_monitor_workers()
            return

        desired = {}
        for cfg in self._build_configs():
            if cfg.enabled and cfg.port:
                desired[cfg.port] = cfg

        # Stop monitors for ports that are no longer selected.
        for port in list(self.monitor_workers.keys()):
            if port not in desired:
                self.monitor_workers[port].stop()
                self.monitor_workers.pop(port, None)

        # Start monitors for new ports.
        for port, cfg in desired.items():
            if port in self.monitor_workers:
                continue
            worker = SerialWorker(
                cfg, self.record_queue_ref, self._set_row_status,
                self.ui_queue, self.graph_queue, self.start_mono_ref
            )
            self.monitor_workers[port] = worker
            worker.start()

    def _stop_monitor_workers(self):
        # Stop all monitor-only workers.
        for worker in self.monitor_workers.values():
            worker.stop_and_wait(timeout=1.0)
        self.monitor_workers = {}

    def _existing_log_paths(self, base_path: str, cfgs: List[DeviceConfig], combined: bool) -> List[str]:
        # List any files that already exist and would be appended.
        paths = []
        if combined:
            path = base_path + ".csv"
            if os.path.exists(path):
                paths.append(path)
            return paths

        for cfg in cfgs:
            if not cfg.enabled or not cfg.port:
                continue
            key = cfg.device_id if cfg.device_id else cfg.port.replace("/", "_")
            path = f"{base_path}_{key}.csv"
            if os.path.exists(path):
                paths.append(path)
        return paths

    def _id_dialog(self, idx: int):
        # Open a small dialog for reading/setting the device ID.
        if self.logging:
            messagebox.showinfo("Busy", "Stop logging before reading or setting IDs.")
            return
        row = self.device_rows[idx]
        port = row["port"].get().strip()
        if not port:
            messagebox.showerror("Missing", "Select a port first.")
            return
        current_id = row["device_id"].get().strip()

        dialog = tk.Toplevel(self)
        dialog.title(f"Device ID - {port}")
        dialog.geometry("360x160")
        ttk.Label(dialog, text="Device ID (uppercase, no spaces)").pack(pady=(12, 4))
        id_var = tk.StringVar(value=current_id)
        entry = ttk.Entry(dialog, textvariable=id_var, width=24)
        entry.pack()
        entry.focus_set()

        def read_id():
            # Query device for ID and update the row.
            self.monitor_paused = True
            self._stop_monitor_workers()
            try:
                time.sleep(0.2)
                device_id = self._serial_read_id(port)
                if device_id is None:
                    return
                row["device_id"].set(device_id)
                id_var.set(device_id)
                dialog.destroy()
            finally:
                self.monitor_paused = False
                self._ensure_monitor_workers()

        def set_id():
            # Send a new ID to the device.
            self.monitor_paused = True
            self._stop_monitor_workers()
            try:
                value = id_var.get().strip().upper().replace(" ", "")
                if not value:
                    messagebox.showerror("Missing", "ID cannot be empty.")
                    return
                time.sleep(0.2)
                ok = self._serial_set_id(port, value)
                if ok:
                    row["device_id"].set(value)
                    id_var.set(value)
                    dialog.destroy()
            finally:
                self.monitor_paused = False
                self._ensure_monitor_workers()

        btns = ttk.Frame(dialog)
        btns.pack(pady=12)
        ttk.Button(btns, text="Read ID", command=read_id).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Set ID", command=set_id).grid(row=0, column=1, padx=6)
        ttk.Button(btns, text="Close", command=dialog.destroy).grid(row=0, column=2, padx=6)

    def _serial_read_id(self, port: str) -> Optional[str]:
        # Open a temporary serial connection and request the ID.
        if serial is None:
            messagebox.showerror("Missing", "pyserial is not installed.")
            return None
        try:
            with serial.Serial(
                port, DEFAULT_BAUD, timeout=0.2, write_timeout=1.0
            ) as ser:
                # Many ESP32 boards reboot when the port opens; wait briefly.
                time.sleep(1.2)
                ser.reset_input_buffer()
                ser.write(b"ID\r\n")
                ser.flush()

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    # Ignore streaming data lines.
                    if "," in line:
                        continue
                    if line == "?":
                        messagebox.showerror("Error", "Device returned '?' for ID query.")
                        return None
                    return line.upper().replace(" ", "")

                messagebox.showerror("Timeout", "No response to ID query.")
                return None
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to read ID: {exc}")
            return None

    def _serial_set_id(self, port: str, value: str) -> bool:
        # Open a temporary serial connection, write ID, then read it back.
        if serial is None:
            messagebox.showerror("Missing", "pyserial is not installed.")
            return False
        try:
            with serial.Serial(
                port, DEFAULT_BAUD, timeout=0.2, write_timeout=1.0
            ) as ser:
                # Allow reboot on open, then send command.
                time.sleep(1.2)
                ser.reset_input_buffer()
                ser.write(f"ID {value}\r\n".encode("utf-8"))
                ser.flush()
                # Query back to confirm.
                time.sleep(0.1)
                ser.reset_input_buffer()
                ser.write(b"ID\r\n")
                ser.flush()

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    # Ignore streaming data lines.
                    if "," in line:
                        continue
                    if line == "?":
                        messagebox.showerror("Error", "Device returned '?' for ID set.")
                        return False
                    return line.upper().replace(" ", "") == value

            messagebox.showerror("Timeout", "No response after setting ID.")
            return False
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to set ID: {exc}")
            return False


if __name__ == "__main__":
    app = App()
    app.mainloop()




