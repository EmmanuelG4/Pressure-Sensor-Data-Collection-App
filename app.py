"""
Pressure sensor app: main window.

Hosts the Arduino connection panel, sensor readouts, calibration, recording
and the live graph.

Install:  pip install -r requirements.txt
Run:      python app.py
"""
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from calibration import Calibration, CalibrationPanel
from connection import ConnectionPanel
from live_display import LiveDisplay
from recording import RecordingPanel


def readout(title, value_label):
    """A small titled number, e.g. 'Sample rate / 450 samples/s'."""
    box = QWidget()
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 18, 0)
    caption = title if isinstance(title, QLabel) else QLabel(title)
    caption.setStyleSheet("color: gray;")
    col.addWidget(caption)
    col.addWidget(value_label)
    return box


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pressure Sensor")
        self.resize(1150, 820)

        # Arduino connection
        self.connection = ConnectionPanel()

        # Readouts
        self.value_label = QLabel("–")
        big = QFont()
        big.setPointSize(22)
        big.setBold(True)
        self.value_label.setFont(big)
        self.value_label.setMinimumWidth(90)
        self.rate_label = QLabel("–")
        self.total_label = QLabel("0")
        self.error_label = QLabel("0")

        readouts = QHBoxLayout()
        self.value_caption = QLabel("Current reading")
        readouts.addWidget(readout(self.value_caption, self.value_label))
        readouts.addWidget(readout("Sample rate", self.rate_label))
        readouts.addWidget(readout("Samples received", self.total_label))
        readouts.addWidget(readout("Sensor read errors", self.error_label))
        readouts.addStretch(1)

        # Calibration
        self.calibration = Calibration()
        self.calibration_panel = CalibrationPanel(self.calibration)

        # Recording
        self.recording_panel = RecordingPanel(self.calibration, self.connection)

        # Live graph
        self.display = LiveDisplay()
        self.display.set_calibration(self.calibration)

        # Connection log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        log_box = QWidget()
        log_col = QVBoxLayout(log_box)
        log_col.setContentsMargins(0, 0, 0, 0)
        log_col.addWidget(QLabel("Connection log"))
        log_col.addWidget(self.log)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.display)
        splitter.addWidget(log_box)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 110])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.connection)
        layout.addLayout(readouts)
        layout.addWidget(self.calibration_panel)
        layout.addWidget(self.recording_panel)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        # Wiring
        self.connection.samples_ready.connect(self.on_samples)
        self.connection.samples_ready.connect(self.display.add_samples)
        self.connection.samples_ready.connect(self.calibration_panel.add_samples)
        self.connection.samples_ready.connect(self.recording_panel.add_samples)
        self.connection.state_changed.connect(self.recording_panel.on_connection_state)
        self.calibration_panel.changed.connect(self.recording_panel.on_calibration_changed)
        self.recording_panel.recording_changed.connect(self.on_recording_changed)
        self.calibration_panel.changed.connect(self.display.refresh_calibration)
        self.calibration_panel.changed.connect(self.update_value_caption)
        self.connection.state_changed.connect(self.on_state_changed)

        self._last_raw = None
        self.update_value_caption()

        self._samples_this_second = 0
        self.rate_timer = QTimer(self)
        self.rate_timer.timeout.connect(self.update_rate)
        self.rate_timer.start(1000)

    def on_samples(self, batch):
        self._samples_this_second += len(batch)
        self._last_raw = batch[-1][2]
        self.show_value()

    def show_value(self):
        if self._last_raw is None:
            self.value_label.setText("–")
        else:
            cal = self.calibration
            self.value_label.setText(cal.format(cal.convert(self._last_raw)))

    def update_value_caption(self):
        captions = {"raw": "Current reading (raw)",
                    "counts": "Current reading (counts above baseline)",
                    "percent": "Current reading (% of full scale)",
                    "newtons": "Current force"}
        self.value_caption.setText(captions[self.calibration.unit])
        self.show_value()

    def update_rate(self):
        reader = self.connection.reader
        if self.connection.is_connected():
            self.rate_label.setText(f"{self._samples_this_second} samples/s")
        else:
            self.rate_label.setText("–")
        if reader is not None:
            self.total_label.setText(f"{reader.total_samples:,}")
            self.error_label.setText(f"{reader.i2c_errors:,}")
        self._samples_this_second = 0

    def on_state_changed(self, state, message):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")
        if state == "connecting":
            self.display.mark_gap()
        self.calibration_panel.set_receiving(state == "receiving")
        if state != "receiving":
            self._last_raw = None
            self.value_label.setText("–")

    def on_recording_changed(self, recording):
        self.setWindowTitle("Pressure Sensor (recording)" if recording
                            else "Pressure Sensor")

    def closeEvent(self, event):
        self.recording_panel.shutdown()
        self.display.shutdown()
        self.connection.shutdown()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())