"""
Recording for the pressure sensor app.

Contains:
  MouseLogger      writes every mouse move, press, release and scroll to mouse.csv
  Recording        one recording session on disk
  RecordingPanel   session details, Start/Stop, markers and save location

Each recording is saved in its own folder, named by date, participant and task:
  sensor.csv    host_time_s, arduino_us, raw        (raw readings, never converted)
  mouse.csv     host_time_s, event, x, y, button, scroll_dx, scroll_dy
  markers.csv   host_time_s, number, label          (moments you flagged)
  session.json  session details, calibration, connection events, screen setup

host_time_s uses the same app clock in every file, so they line up directly.
session.json is written when recording starts (status "recording") and
rewritten when it stops (status "complete"), so an unexpected crash still
leaves the session details behind.
"""
import csv
import json
import platform
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy)

from connection import now

FORMAT_VERSION = 1


def _iso(wall_time):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(wall_time))


def _slug(text):
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-")
    return text[:40]


# ------------------------------------------------------------ mouse logger
class MouseLogger:
    """Logs all mouse activity on pynput's own thread, straight to CSV."""

    def __init__(self, path):
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["host_time_s", "event", "x", "y", "button",
                               "scroll_dx", "scroll_dy"])
        self._lock = threading.Lock()
        self._listener = None
        self.counts = {"move": 0, "press": 0, "release": 0, "scroll": 0}
        self.error = None

    def start(self):
        try:
            from pynput import mouse
            self._listener = mouse.Listener(on_move=self._on_move,
                                            on_click=self._on_click,
                                            on_scroll=self._on_scroll)
            self._listener.start()
            return True
        except Exception as exc:
            self._listener = None
            self.error = str(exc)
            return False

    def _write(self, row, kind):
        with self._lock:
            if self._file.closed:
                return
            self._writer.writerow(row)
            self.counts[kind] += 1

    def _on_move(self, x, y):
        self._write([f"{now():.6f}", "move", x, y, "", "", ""], "move")

    def _on_click(self, x, y, button, pressed):
        kind = "press" if pressed else "release"
        self._write([f"{now():.6f}", kind, x, y,
                     getattr(button, "name", str(button)), "", ""], kind)

    def _on_scroll(self, x, y, dx, dy):
        self._write([f"{now():.6f}", "scroll", x, y, "", dx, dy], "scroll")

    def flush(self):
        with self._lock:
            if not self._file.closed:
                self._file.flush()

    def close(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        with self._lock:
            self._file.close()


# ------------------------------------------------------------ one recording
class Recording:
    def __init__(self, folder, session, calibration, port, log_mouse=True):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.session = session
        self.port = port
        self.start_wall = time.time()
        self.start_t = now()
        self.calibration_at_start = calibration
        self.calibration_changes = []
        self.connection_events = []
        self.samples = 0
        self.first_sample_t = None
        self.last_sample_t = None
        self.markers = 0
        self.read_errors = 0

        self._sensor_file = open(self.folder / "sensor.csv", "w", newline="")
        self._sensor = csv.writer(self._sensor_file)
        self._sensor.writerow(["host_time_s", "arduino_us", "raw"])

        self._marker_file = open(self.folder / "markers.csv", "w", newline="",
                                 encoding="utf-8")
        self._marker_writer = csv.writer(self._marker_file)
        self._marker_writer.writerow(["host_time_s", "number", "label"])

        self.mouse = None
        self.mouse_ok = False
        if log_mouse:
            self.mouse = MouseLogger(self.folder / "mouse.csv")
            self.mouse_ok = self.mouse.start()

        self._write_session(status="recording")

    # -- data in --------------------------------------------------------------
    def add_samples(self, batch):
        for t, arduino_us, raw in batch:
            self._sensor.writerow([f"{t:.6f}", arduino_us, raw])
        if self.first_sample_t is None:
            self.first_sample_t = batch[0][0]
        self.last_sample_t = batch[-1][0]
        self.samples += len(batch)

    def add_marker(self, label=""):
        self.markers += 1
        self._marker_writer.writerow([f"{now():.6f}", self.markers, label])
        self._marker_file.flush()
        return self.markers

    def log_connection(self, state, message):
        self.connection_events.append({"host_time_s": round(now(), 6),
                                       "state": state, "message": message})

    def log_calibration(self, calibration):
        self.calibration_changes.append({"host_time_s": round(now(), 6),
                                         "calibration": calibration})

    # -- status ---------------------------------------------------------------
    def elapsed(self):
        return now() - self.start_t

    def clicks(self):
        return self.mouse.counts["press"] if self.mouse else 0

    def flush(self):
        self._sensor_file.flush()
        if self.mouse:
            self.mouse.flush()

    def stop(self):
        self.end_t = now()
        self.end_wall = time.time()
        self._sensor_file.close()
        self._marker_file.close()
        if self.mouse:
            self.mouse.close()
        self._write_session(status="complete")

    # -- session.json ---------------------------------------------------------
    def _write_session(self, status):
        complete = status == "complete"
        span = ((self.last_sample_t - self.first_sample_t)
                if self.samples > 1 else None)
        info = {
            "format_version": FORMAT_VERSION,
            "status": status,
            "session": self.session,
            "started_at": _iso(self.start_wall),
            "ended_at": _iso(self.end_wall) if complete else None,
            "duration_s": round(self.end_t - self.start_t, 3) if complete else None,
            "clock": {
                "description": ("host_time_s in every file is seconds since the app "
                                "started, from the computer's high-resolution clock"),
                "start_host_time_s": round(self.start_t, 6),
                "end_host_time_s": round(self.end_t, 6) if complete else None,
            },
            "sensor": {
                "port": self.port,
                "samples": self.samples,
                "first_sample_host_time_s": self.first_sample_t,
                "last_sample_host_time_s": self.last_sample_t,
                "mean_rate_hz": round((self.samples - 1) / span, 1) if span else None,
                "read_errors": self.read_errors,
                "note": ("raw is the SingleTact I2C reading. arduino_us is the "
                         "Arduino's micros() and wraps around after about 71 minutes."),
            },
            "mouse": {
                "logged": self.mouse_ok,
                "error": self.mouse.error if self.mouse else "mouse logging turned off",
                "event_counts": dict(self.mouse.counts) if self.mouse else None,
                "note": "x and y are screen pixels as reported by the operating system",
            },
            "markers": self.markers,
            "calibration_at_start": self.calibration_at_start,
            "calibration_changes": self.calibration_changes,
            "connection_events": self.connection_events,
            "screens": [
                {"name": s.name(),
                 "x": s.geometry().x(), "y": s.geometry().y(),
                 "width": s.geometry().width(), "height": s.geometry().height(),
                 "device_pixel_ratio": s.devicePixelRatio()}
                for s in QGuiApplication.screens()
            ],
            "system": {"platform": platform.platform(),
                       "python": platform.python_version()},
        }
        with open(self.folder / "session.json", "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)


# ------------------------------------------------------------ panel
class RecordingPanel(QGroupBox):
    """Session details, Start/Stop recording, markers and save location.

    Signals
      recording_changed(bool)
    """
    recording_changed = Signal(bool)

    PLACEMENTS = ["Under center of pad", "Under hand-rest area",
                  "Under left side", "Under right side"]
    TASKS = ["Free use", "Click test", "Movement only", "Baseline (no use)"]

    def __init__(self, calibration, connection, parent=None):
        super().__init__("Recording", parent)
        self.cal = calibration
        self.connection = connection
        self.recording = None
        self.last_folder = None
        self._receiving = False
        self._errors_at_start = 0
        self._settings = QSettings("PressureSensorApp", "Recording")
        default_dir = str(Path.home() / "PressureRecordings")
        self.base_dir = Path(self._settings.value("base_dir", default_dir))

        # Session details
        self.participant = QLineEdit(self._settings.value("participant", ""))
        self.participant.setPlaceholderText("e.g. P01")
        self.participant.setMaximumWidth(110)
        self.placement = QComboBox()
        self.placement.setEditable(True)
        self.placement.addItems(self.PLACEMENTS)
        self.placement.setCurrentText(self._settings.value("placement", self.PLACEMENTS[0]))
        self.task = QComboBox()
        self.task.setEditable(True)
        self.task.addItems(self.TASKS)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Anything worth remembering about this session")
        self.log_mouse = QCheckBox("Log mouse movement and clicks")
        self.log_mouse.setChecked(True)

        # Controls
        self.record_btn = QPushButton("Start recording")
        self.record_btn.setMinimumWidth(140)
        self.record_btn.setToolTip("Start or stop recording (Ctrl+R)")
        self.marker_label = QLineEdit()
        self.marker_label.setPlaceholderText("Marker label (optional)")
        self.marker_label.setMaximumWidth(200)
        self.marker_btn = QPushButton("Add marker")
        self.marker_btn.setToolTip("Flag this moment in the recording (Ctrl+M). The "
                                   "shortcut avoids adding a mouse click to the data.")
        self.status = QLabel("Not recording")
        self.status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # Save location
        self.path_label = QLabel()
        self.path_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.folder_btn = QPushButton("Change folder…")
        self.open_btn = QPushButton("Open last recording")
        self.open_btn.setEnabled(False)

        grid = QGridLayout(self)
        details = QHBoxLayout()
        details.addWidget(QLabel("Participant"))
        details.addWidget(self.participant)
        details.addWidget(QLabel("Sensor placement"))
        details.addWidget(self.placement)
        details.addWidget(QLabel("Task"))
        details.addWidget(self.task)
        details.addWidget(QLabel("Notes"))
        details.addWidget(self.notes, 1)
        grid.addLayout(details, 0, 0)

        controls = QHBoxLayout()
        controls.addWidget(self.record_btn)
        controls.addWidget(self.marker_label)
        controls.addWidget(self.marker_btn)
        controls.addSpacing(12)
        controls.addWidget(self.status, 1)
        controls.addWidget(self.log_mouse)
        grid.addLayout(controls, 1, 0)

        location = QHBoxLayout()
        location.addWidget(QLabel("Save to"))
        location.addWidget(self.path_label, 1)
        location.addWidget(self.folder_btn)
        location.addWidget(self.open_btn)
        grid.addLayout(location, 2, 0)

        self.record_btn.clicked.connect(self.toggle_recording)
        self.marker_btn.clicked.connect(self.add_marker)
        self.marker_label.returnPressed.connect(self.add_marker)
        self.folder_btn.clicked.connect(self.choose_folder)
        self.open_btn.clicked.connect(self.open_last)
        QShortcut(QKeySequence("Ctrl+R"), self, self.toggle_recording,
                  context=Qt.ApplicationShortcut)
        QShortcut(QKeySequence("Ctrl+M"), self, self.add_marker,
                  context=Qt.ApplicationShortcut)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._update_status)
        self.flush_timer = QTimer(self)
        self.flush_timer.timeout.connect(self._flush)

        self._show_path()
        self._update_enabled()

    # -- inputs from the app --------------------------------------------------
    def add_samples(self, batch):
        if self.recording:
            self.recording.add_samples(batch)

    def on_connection_state(self, state, message):
        self._receiving = state == "receiving"
        if self.recording:
            self.recording.log_connection(state, message)
            if not self._receiving:
                self._set_status("Connection interrupted. Still recording; sensor "
                                 "data resumes when you reconnect.", "warn")
        self._update_enabled()

    def on_calibration_changed(self):
        if self.recording:
            self.recording.log_calibration(self.cal.to_dict())

    def shutdown(self):
        if self.recording:
            self.stop()

    # -- start / stop ---------------------------------------------------------
    def toggle_recording(self):
        if self.recording:
            self.stop()
        elif self._receiving:
            self.start()

    def start(self):
        session = {
            "participant": self.participant.text().strip(),
            "sensor_placement": self.placement.currentText().strip(),
            "task": self.task.currentText().strip(),
            "notes": self.notes.text().strip(),
        }
        name_parts = [time.strftime("%Y%m%d_%H%M%S"),
                      _slug(session["participant"]), _slug(session["task"])]
        folder = self.base_dir / "_".join(p for p in name_parts if p)
        reader = self.connection.reader
        try:
            self.recording = Recording(folder, session, self.cal.to_dict(),
                                       port=reader.port if reader else None,
                                       log_mouse=self.log_mouse.isChecked())
        except OSError as exc:
            self.recording = None
            self._set_status(f"Couldn't create the recording folder. Choose another "
                             f"folder and try again. ({exc})", "error")
            return

        self._errors_at_start = reader.i2c_errors if reader else 0
        self._marker_note = ""
        self._settings.setValue("participant", session["participant"])
        self._settings.setValue("placement", session["sensor_placement"])
        self.ui_timer.start(250)
        self.flush_timer.start(1000)
        self._update_enabled()
        self._update_status()
        self.recording_changed.emit(True)

    def stop(self):
        rec = self.recording
        reader = self.connection.reader
        if reader is not None:
            rec.read_errors = max(0, reader.i2c_errors - self._errors_at_start)
        rec.stop()
        self.recording = None
        self.ui_timer.stop()
        self.flush_timer.stop()
        self.last_folder = rec.folder
        self.open_btn.setEnabled(True)
        self._update_enabled()
        clicks = f", {rec.clicks()} clicks" if rec.mouse_ok else ""
        self._set_status(f"Saved {rec.end_t - rec.start_t:.1f} s, {rec.samples:,} "
                         f"samples{clicks} to {rec.folder.name}", "ok")
        self.recording_changed.emit(False)

    def add_marker(self):
        if not self.recording:
            return
        label = self.marker_label.text().strip()
        number = self.recording.add_marker(label)
        self.marker_label.clear()
        self._marker_note = f"Marker {number} added" + (f": {label}" if label else "")
        QTimer.singleShot(3000, self._clear_marker_note)

    def _clear_marker_note(self):
        self._marker_note = ""

    # -- save location --------------------------------------------------------
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Save recordings in",
                                                  str(self.base_dir))
        if folder:
            self.base_dir = Path(folder)
            self._settings.setValue("base_dir", str(self.base_dir))
            self._show_path()

    def open_last(self):
        if self.last_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_folder)))

    # -- internal -------------------------------------------------------------
    def _flush(self):
        if self.recording:
            self.recording.flush()

    def _update_enabled(self):
        recording = self.recording is not None
        self.record_btn.setText("Stop recording" if recording else "Start recording")
        self.record_btn.setStyleSheet(
            "background:#d93025; color:white; font-weight:bold;" if recording else "")
        self.record_btn.setEnabled(recording or self._receiving)
        self.marker_btn.setEnabled(recording)
        self.marker_label.setEnabled(recording)
        for widget in (self.participant, self.placement, self.task, self.notes,
                       self.log_mouse, self.folder_btn):
            widget.setEnabled(not recording)
        if not recording and self.last_folder is None:
            if self._receiving:
                self._set_status("Ready to record.")
            else:
                self._set_status("Connect to the Arduino to start recording.")

    _marker_note = ""

    def _update_status(self):
        rec = self.recording
        if not rec:
            return
        if not self._receiving:
            return  # keep the interruption warning visible
        seconds = int(rec.elapsed())
        parts = [f"Recording {seconds // 60:02d}:{seconds % 60:02d}",
                 f"{rec.samples:,} samples"]
        if rec.mouse_ok:
            parts.append(f"{rec.clicks()} clicks")
        parts.append(f"{rec.markers} marker" + ("" if rec.markers == 1 else "s"))
        text = "   ".join(parts)
        if rec.mouse is not None and not rec.mouse_ok:
            text += "   (mouse not logged: allow mouse access, see notes)"
            self.status.setToolTip(rec.mouse.error or "")
        if self._marker_note:
            text += f"   {self._marker_note}"
        self._set_status(text, "rec")

    def _show_path(self):
        full = str(self.base_dir)
        metrics = self.path_label.fontMetrics()
        self.path_label.setText(metrics.elidedText(full, Qt.ElideMiddle, 520))
        self.path_label.setToolTip(full)

    def _set_status(self, text, kind="info"):
        color = {"info": "gray", "ok": "#2e9e44", "warn": "#c26a00",
                 "error": "#d93025", "rec": "#d93025"}.get(kind, "gray")
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)