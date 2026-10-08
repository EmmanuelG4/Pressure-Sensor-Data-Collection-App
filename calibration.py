"""
Calibration for the pressure sensor app.

Contains:
  Calibration       the conversion settings (baseline, units, full-scale force)
  CalibrationPanel  Zero / Reset buttons, unit picker and full-scale setting

How SingleTact readings work (from the SingleTact user manual):
  * The I2C output has a fixed offset: with no load it reads about 255.
  * The sensor's rated full-scale force is 511 counts above that offset (~767).
  * Readings above full scale are possible but outside the calibrated range.
  * Force = (reading - baseline) / 511 * full-scale force, assuming the load
    is spread evenly over the sensor head.

Calibration only changes what is displayed. Recordings should always store the
raw readings plus Calibration.to_dict(), so nothing is lost if a setting is wrong.
"""
import time

import numpy as np
from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton)

SINGLETACT_OFFSET = 255      # no-load reading from the factory
FULL_SCALE_COUNTS = 511      # counts between no load and rated full-scale force


class Calibration:
    """Converts raw readings into the chosen display unit."""

    UNITS = {
        "raw": "Raw reading",
        "counts": "Counts above baseline",
        "percent": "% of full scale",
        "newtons": "Force (N)",
    }

    def __init__(self):
        self.baseline = None        # measured no-load reading, None until zeroed
        self.noise = None           # standard deviation during zeroing
        self.zeroed_at = None       # wall-clock time of the last zero
        self.unit = "raw"
        self.full_scale_n = 10.0    # the sensor's rated force

    # -- conversion -----------------------------------------------------------
    def effective_baseline(self):
        return self.baseline if self.baseline is not None else SINGLETACT_OFFSET

    def scale(self):
        """Display units per raw count."""
        return {"raw": 1.0, "counts": 1.0,
                "percent": 100.0 / FULL_SCALE_COUNTS,
                "newtons": self.full_scale_n / FULL_SCALE_COUNTS}[self.unit]

    def convert(self, raw):
        """Raw reading(s) -> display units. Works on numbers and numpy arrays."""
        if self.unit == "raw":
            return raw
        return (np.asarray(raw, dtype=float) - self.effective_baseline()) * self.scale()

    def axis_label(self):
        return {"raw": "Pressure (raw)", "counts": "Pressure (counts above baseline)",
                "percent": "Pressure (% of full scale)", "newtons": "Force (N)"}[self.unit]

    def format(self, value):
        if self.unit in ("raw", "counts"):
            return f"{value:.0f}"
        if self.unit == "percent":
            return f"{value:.1f} %"
        decimals = 3 if self.full_scale_n < 5 else 2
        return f"{value:.{decimals}f} N"

    def is_over_range(self, raw):
        return raw > self.effective_baseline() + FULL_SCALE_COUNTS

    def to_dict(self):
        """Settings to save alongside a recording."""
        return {
            "baseline_raw": self.baseline,
            "baseline_source": "measured" if self.baseline is not None else "factory offset",
            "effective_baseline_raw": self.effective_baseline(),
            "baseline_noise_std": self.noise,
            "zeroed_at": self.zeroed_at,
            "display_unit": self.unit,
            "full_scale_force_n": self.full_scale_n,
            "full_scale_counts": FULL_SCALE_COUNTS,
        }


class CalibrationPanel(QGroupBox):
    """Zero / Reset, unit picker and full-scale setting.

    Signals
      changed()   the baseline or display settings changed
    """
    changed = Signal()

    ZERO_SECONDS = 2.0       # how long to average when zeroing
    ZERO_TIMEOUT_MS = 6000   # give up if not enough data arrives
    MIN_ZERO_SAMPLES = 20
    UNSTEADY_SPREAD = 25     # counts; a wider spread suggests something touched the pad

    def __init__(self, calibration, parent=None):
        super().__init__("Calibration", parent)
        self.cal = calibration
        self._settings = QSettings("PressureSensorApp", "Calibration")
        self._zeroing = False
        self._zero_t = []
        self._zero_y = []
        self._receiving = False
        self._over_range = False

        self.zero_btn = QPushButton("Zero")
        self.zero_btn.setToolTip("Measure the no-load reading. Leave the mouse pad in "
                                 "place, but keep your hand and the mouse off it.")
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Forget the measured baseline and use the factory offset (255).")
        self.baseline_label = QLabel()
        self.unit_combo = QComboBox()
        for key, label in Calibration.UNITS.items():
            self.unit_combo.addItem(label, key)
        self.fsr_spin = QDoubleSpinBox()
        self.fsr_spin.setRange(0.1, 1000.0)
        self.fsr_spin.setDecimals(1)
        self.fsr_spin.setSuffix(" N")
        self.fsr_spin.setToolTip("Your sensor's rated force range, from its label or "
                                 "order details (for example 1, 10, 45 or 100 N).")
        self.fsr_label = QLabel("Sensor rating")
        self.status = QLabel()
        self.status.setWordWrap(True)

        row = QHBoxLayout(self)
        row.addWidget(self.zero_btn)
        row.addWidget(self.reset_btn)
        row.addWidget(self.baseline_label)
        row.addSpacing(16)
        row.addWidget(QLabel("Show as"))
        row.addWidget(self.unit_combo)
        row.addWidget(self.fsr_label)
        row.addWidget(self.fsr_spin)
        row.addSpacing(16)
        row.addWidget(self.status, 1)

        # Restore saved preferences (the baseline itself is re-measured each session)
        self.cal.unit = self._settings.value("unit", "raw")
        self.cal.full_scale_n = float(self._settings.value("full_scale_n", 10.0))
        self.unit_combo.setCurrentIndex(max(0, self.unit_combo.findData(self.cal.unit)))
        self.fsr_spin.setValue(self.cal.full_scale_n)

        self.zero_btn.clicked.connect(self.start_zero)
        self.reset_btn.clicked.connect(self.reset)
        self.unit_combo.currentIndexChanged.connect(self._on_unit_changed)
        self.fsr_spin.valueChanged.connect(self._on_fsr_changed)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._zero_timed_out)

        self._refresh_labels()
        self.set_receiving(False)

    # -- inputs from the app --------------------------------------------------
    def set_receiving(self, receiving):
        self._receiving = receiving
        self.zero_btn.setEnabled(receiving and not self._zeroing)
        if not receiving and self._zeroing:
            self._finish_zero(failed="Connection stopped while zeroing. Try again.")

    def add_samples(self, batch):
        """Slot for ConnectionPanel.samples_ready."""
        if self._zeroing:
            for t, _, raw in batch:
                self._zero_t.append(t)
                self._zero_y.append(raw)
            if self._zero_t and self._zero_t[-1] - self._zero_t[0] >= self.ZERO_SECONDS:
                self._finish_zero()
            return

        over = self.cal.is_over_range(batch[-1][2])
        if over != self._over_range:
            self._over_range = over
            if over:
                self._set_status("Above the sensor's full-scale range. Readings here "
                                 "aren't calibrated.", "warn")
            else:
                self._set_status("")

    # -- zeroing --------------------------------------------------------------
    def start_zero(self):
        self._zeroing = True
        self._zero_t, self._zero_y = [], []
        self.zero_btn.setEnabled(False)
        self.zero_btn.setText("Zeroing…")
        self._set_status(f"Hold still: hand and mouse off the pad for "
                         f"{self.ZERO_SECONDS:.0f} seconds.", "info")
        self._timeout.start(self.ZERO_TIMEOUT_MS)

    def _zero_timed_out(self):
        if self._zeroing:
            self._finish_zero(failed="Not enough sensor data arrived. Check the "
                                     "connection and try again.")

    def _finish_zero(self, failed=None):
        self._zeroing = False
        self._timeout.stop()
        self.zero_btn.setText("Zero")
        self.zero_btn.setEnabled(self._receiving)

        if failed is None and len(self._zero_y) < self.MIN_ZERO_SAMPLES:
            failed = "Not enough sensor data arrived. Check the connection and try again."
        if failed:
            self._set_status(failed, "error")
            return

        values = np.asarray(self._zero_y, dtype=float)
        self.cal.baseline = float(values.mean())
        self.cal.noise = float(values.std())
        self.cal.zeroed_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_labels()
        self.changed.emit()

        spread = float(values.max() - values.min())
        if spread > self.UNSTEADY_SPREAD:
            self._set_status(f"Baseline saved, but the reading moved by {spread:.0f} "
                             "counts while zeroing. If anything touched the pad, "
                             "zero again.", "warn")
        else:
            self._set_status("Zeroed.", "ok")

    def reset(self):
        self.cal.baseline = None
        self.cal.noise = None
        self.cal.zeroed_at = None
        self._refresh_labels()
        self._set_status("Using the factory offset (255). Press Zero for a measured baseline.")
        self.changed.emit()

    # -- settings -------------------------------------------------------------
    def _on_unit_changed(self):
        self.cal.unit = self.unit_combo.currentData()
        self._settings.setValue("unit", self.cal.unit)
        self._refresh_labels()
        self.changed.emit()

    def _on_fsr_changed(self, value):
        self.cal.full_scale_n = float(value)
        self._settings.setValue("full_scale_n", self.cal.full_scale_n)
        self.changed.emit()

    def _refresh_labels(self):
        if self.cal.baseline is None:
            self.baseline_label.setText("Baseline: not zeroed")
        else:
            self.baseline_label.setText(
                f"Baseline: {self.cal.baseline:.1f} (noise ±{self.cal.noise:.1f})")
        newtons = self.cal.unit == "newtons"
        self.fsr_label.setVisible(newtons)
        self.fsr_spin.setVisible(newtons)

    def _set_status(self, text, kind="info"):
        color = {"info": "gray", "ok": "#2e9e44", "warn": "#c26a00",
                 "error": "#d93025"}.get(kind, "gray")
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)