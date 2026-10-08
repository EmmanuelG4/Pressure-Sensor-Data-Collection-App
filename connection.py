"""
Arduino connection layer for the pressure sensor app.

Contains:
  now()            shared clock used by every part of the app
  find_ports()     lists serial ports and flags the ones that look like an Arduino
  SerialReader     background thread that reads "micros,raw" lines from the Arduino
  ConnectionPanel  the port dropdown, Refresh / Connect buttons and status light

Other parts of the app (live graph, recording) only need to connect to
ConnectionPanel.samples_ready, which delivers batches of
(host_time_s, arduino_us, raw) tuples.
"""
import time

import serial
from serial.tools import list_ports
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QWidget)

# ---------------------------------------------------------------- shared clock
_T0 = time.perf_counter()


def now():
    """Seconds since the app started. Use this for every timestamp so that
    sensor data and mouse events line up."""
    return time.perf_counter() - _T0


# -------------------------------------------------------------- port discovery
# USB vendor IDs used by Arduino boards and common USB-serial chips
_ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86, 0x10C4, 0x0403, 0x239A}
_ARDUINO_HINTS = ("arduino", "ch340", "cp210", "ftdi", "usbmodem",
                  "ttyacm", "usb serial", "usb-serial")


def find_ports():
    """Return a list of (device, label, looks_like_arduino)."""
    ports = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        text = f"{p.device} {p.description or ''} {p.manufacturer or ''}".lower()
        likely = p.vid in _ARDUINO_VIDS or any(h in text for h in _ARDUINO_HINTS)
        desc = p.description if p.description and p.description != "n/a" else ""
        label = f"{p.device} ({desc})" if desc and desc != p.device else p.device
        ports.append((p.device, label, likely))
    return ports


# ----------------------------------------------------------- background reader
class SerialReader(QThread):
    """Reads the Arduino stream on a background thread so the window never freezes.

    Signals
      samples_ready(list)        batches of (host_time_s, arduino_us, raw), ~30 per second
      status_changed(str, str)   (state, message); state is one of
                                 connecting, receiving, stalled, error, disconnected
    """
    samples_ready = Signal(list)
    status_changed = Signal(str, str)

    BATCH_INTERVAL = 0.03    # seconds between batches sent to the window
    STALL_TIMEOUT = 3.0      # seconds without valid data before warning
    RESET_WAIT = 2.0         # most Arduinos reboot when the port opens

    def __init__(self, port, baud=115200, parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self._running = True
        self.total_samples = 0
        self.i2c_errors = 0      # lines where the sketch reported -1
        self.bad_lines = 0       # lines that could not be parsed

    def stop(self):
        self._running = False

    def run(self):
        self.status_changed.emit("connecting", f"Opening {self.port}")
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except serial.SerialException as exc:
            text = str(exc).lower()
            if "no such file" in text or "cannot find" in text or "filenotfound" in text:
                hint = "The port is gone. Check the USB cable, then press Refresh."
            else:
                hint = ("Close the Arduino Serial Monitor or any other program "
                        "using it, then try again.")
            self.status_changed.emit("error", f"Couldn't open {self.port}. {hint} ({exc})")
            return

        try:
            self.status_changed.emit("connecting", "Waiting for the Arduino to restart")
            deadline = time.monotonic() + self.RESET_WAIT
            while self._running and time.monotonic() < deadline:
                self.msleep(50)
            ser.reset_input_buffer()

            batch = []
            state = "connecting"
            last_emit = last_valid = time.monotonic()
            errors_since_valid = 0

            while self._running:
                line = ser.readline()
                t = now()
                if line:
                    parsed = self._parse(line)
                    if parsed is not None:
                        arduino_us, raw = parsed
                        if raw < 0:
                            self.i2c_errors += 1
                            errors_since_valid += 1
                        else:
                            batch.append((t, arduino_us, raw))
                            self.total_samples += 1
                            last_valid = time.monotonic()
                            errors_since_valid = 0
                            if state != "receiving":
                                state = "receiving"
                                self.status_changed.emit(
                                    "receiving", f"Receiving data on {self.port}")

                mono = time.monotonic()
                if batch and mono - last_emit >= self.BATCH_INTERVAL:
                    self.samples_ready.emit(batch)
                    batch = []
                    last_emit = mono

                if mono - last_valid > self.STALL_TIMEOUT and state != "stalled":
                    state = "stalled"
                    if errors_since_valid:
                        msg = ("Arduino connected, but it can't read the sensor. "
                               "Check the SDA, SCL, power and ground wires.")
                    else:
                        msg = ("Port open, but no sensor data. Check that the "
                               "SingleTact sketch is uploaded and uses 115200 baud.")
                    self.status_changed.emit("stalled", msg)

            if batch:
                self.samples_ready.emit(batch)
        except serial.SerialException as exc:
            self.status_changed.emit(
                "error", f"Lost connection to {self.port}. Check the USB cable. ({exc})")
            return
        finally:
            try:
                ser.close()
            except Exception:
                pass

        self.status_changed.emit("disconnected", "Disconnected")

    def _parse(self, line):
        try:
            a, b = line.decode("ascii", errors="ignore").strip().split(",")
            return int(a), int(b)
        except ValueError:
            self.bad_lines += 1
            return None


# ------------------------------------------------------------ connection panel
_STATE_COLORS = {
    "disconnected": "#9e9e9e",
    "connecting": "#f2b705",
    "receiving": "#2e9e44",
    "stalled": "#f28705",
    "error": "#d93025",
}


class ConnectionPanel(QWidget):
    """Port picker, Refresh and Connect buttons, and a status light.

    Signals
      samples_ready(list)        forwarded from the reader
      state_changed(str, str)    (state, message)
    """
    samples_ready = Signal(list)
    state_changed = Signal(str, str)

    def __init__(self, baud=115200, parent=None):
        super().__init__(parent)
        self.baud = baud
        self.reader = None
        self.state = "disconnected"

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(280)
        self.refresh_btn = QPushButton("Refresh")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumWidth(100)
        self.status_light = QLabel()
        self.status_light.setFixedSize(14, 14)
        self.status_text = QLabel()
        self.status_text.setWordWrap(True)
        self.status_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Port"))
        row.addWidget(self.port_combo)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.connect_btn)
        row.addSpacing(12)
        row.addWidget(self.status_light)
        row.addWidget(self.status_text, 1)

        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.toggle_connection)

        self.refresh_ports()
        self._set_state("disconnected", "Not connected")

    # -- public ---------------------------------------------------------------
    def is_connected(self):
        return self.reader is not None and self.reader.isRunning()

    def refresh_ports(self):
        previous = self.port_combo.currentData()
        self.port_combo.clear()
        ports = find_ports()
        for device, label, likely in ports:
            self.port_combo.addItem(label + ("  [Arduino?]" if likely else ""), device)

        if not ports:
            self.port_combo.addItem("No serial ports found. Plug in the Arduino "
                                    "and press Refresh.", None)
            self.connect_btn.setEnabled(False)
            return

        self.connect_btn.setEnabled(True)
        index = self.port_combo.findData(previous) if previous else -1
        if index < 0:   # otherwise pick the first port that looks like an Arduino
            index = next((i for i, p in enumerate(ports) if p[2]), 0)
        self.port_combo.setCurrentIndex(index)

    def toggle_connection(self):
        if self.is_connected():
            self.disconnect_port()
        else:
            self.connect_port()

    def connect_port(self):
        port = self.port_combo.currentData()
        if not port:
            return
        reader = SerialReader(port, self.baud)
        reader.samples_ready.connect(self.samples_ready)
        reader.status_changed.connect(self._set_state)
        reader.finished.connect(lambda r=reader: self._on_reader_finished(r))
        self.reader = reader
        reader.start()

        self.connect_btn.setText("Disconnect")
        self.port_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)

    def disconnect_port(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(3000)

    def shutdown(self):
        """Call when the window closes so the port is released cleanly."""
        self.disconnect_port()

    # -- internal -------------------------------------------------------------
    def _on_reader_finished(self, reader):
        if reader is not self.reader:   # an older connection finishing late
            return
        self.connect_btn.setText("Connect")
        self.port_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)

    def _set_state(self, state, message):
        self.state = state
        color = _STATE_COLORS.get(state, "#9e9e9e")
        self.status_light.setStyleSheet(f"background:{color}; border-radius:7px;")
        self.status_light.setToolTip(state)
        self.status_text.setText(message)
        self.state_changed.emit(state, message)