"""
Live display for the pressure sensor app.

Contains:
  MouseClickWatcher  listens for real mouse clicks anywhere on screen (pynput)
  LiveDisplay        scrolling pressure graph with click markers and controls

Feed it data by connecting ConnectionPanel.samples_ready to LiveDisplay.add_samples.
Call set_calibration() so it plots in the chosen units; the buffer keeps raw values.
Each mouse click is drawn as a shaded band from press to release, so you can
check whether clicks line up with pressure spikes.
"""
import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)
import pyqtgraph as pg   # imported after PySide6 so pyqtgraph uses it

from connection import now

pg.setConfigOptions(background="w", foreground="#333333")


# ------------------------------------------------------------ mouse clicks
class MouseClickWatcher(QObject):
    """Reports every mouse press and release, timestamped with the app clock.

    Signals
      clicked(float, str, bool)   (host_time_s, button name, pressed)
      failed(str)                 the listener couldn't start
    """
    clicked = Signal(float, str, bool)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._listener = None

    def start(self):
        if self._listener is not None:
            return True
        try:
            from pynput import mouse
            self._listener = mouse.Listener(on_click=self._on_click)
            self._listener.start()
            return True
        except Exception as exc:
            self._listener = None
            self.failed.emit(
                f"Can't watch mouse clicks ({str(exc).splitlines()[0][:120]}). "
                "On macOS, allow Accessibility "
                "access for your terminal or editor in System Settings.")
            return False

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_click(self, x, y, button, pressed):
        # Runs on pynput's thread; Qt delivers the signal on the main thread.
        self.clicked.emit(now(), getattr(button, "name", str(button)), bool(pressed))


# ------------------------------------------------------------ live display
class LiveDisplay(QWidget):
    WINDOWS = [("5 s", 5), ("10 s", 10), ("30 s", 30), ("60 s", 60)]
    BUFFER_SECONDS = 70          # how much history is kept for pausing and panning
    MAX_RATE = 2000              # samples/s the buffer is sized for
    MIN_Y_SPAN = 20              # stops auto-scale from magnifying tiny noise
    REFRESH_MS = 33              # about 30 frames per second
    CLICK_COLORS = {"left": (37, 99, 235), "right": (217, 48, 37),
                    "middle": (46, 158, 68)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cap = self.BUFFER_SECONDS * self.MAX_RATE
        self._t = np.zeros(self._cap)
        self._y = np.zeros(self._cap)
        self._i = 0          # next write position
        self._n = 0          # number of valid samples
        self.paused = False
        self._markers = []   # (end_time, plot item)
        self._pending = {}   # button -> (press_time, line item)
        self.calibration = None

        # Controls
        self.window_combo = QComboBox()
        for label, seconds in self.WINDOWS:
            self.window_combo.addItem(label, seconds)
        self.window_combo.setCurrentIndex(1)
        self.autoscale_box = QCheckBox("Auto-scale")
        self.autoscale_box.setChecked(True)
        self.clicks_box = QCheckBox("Show mouse clicks")
        self.clicks_box.setChecked(True)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.clear_btn = QPushButton("Clear")
        self.note = QLabel()
        self.note.setStyleSheet("color: gray;")

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show last"))
        controls.addWidget(self.window_combo)
        controls.addSpacing(12)
        controls.addWidget(self.autoscale_box)
        controls.addWidget(self.clicks_box)
        controls.addStretch(1)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.clear_btn)

        # Plot
        self.plot = pg.PlotWidget()
        item = self.plot.getPlotItem()
        item.showGrid(x=True, y=True, alpha=0.25)
        item.setLabel("left", "Pressure (raw)")
        item.getAxis("left").enableAutoSIPrefix(False)   # show 0.7, not 700 (x0.001)
        item.setLabel("bottom", "Time since app start (s)")
        item.setDownsampling(auto=True, mode="peak")
        item.setClipToView(True)
        item.setMouseEnabled(x=False, y=False)
        item.hideButtons()
        self.curve = item.plot(pen=pg.mkPen((35, 35, 35), width=1.5), connect="finite")
        self.zero_line = pg.InfiniteLine(angle=0, movable=False,
                                         pen=pg.mkPen((140, 140, 140), width=1,
                                                      style=Qt.DashLine))
        self.zero_line.setZValue(-5)
        self.zero_line.setVisible(False)
        self.plot.addItem(self.zero_line)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.note)

        # Mouse clicks
        self.clicks = MouseClickWatcher(self)
        self.clicks.clicked.connect(self.add_click)
        self.clicks.failed.connect(self._on_click_watch_failed)

        self.clicks_box.toggled.connect(self._toggle_clicks)
        self.pause_btn.toggled.connect(self._toggle_pause)
        self.clear_btn.clicked.connect(self.clear)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.redraw)
        self.timer.start(self.REFRESH_MS)
        self._toggle_clicks(True)

    # -- data in --------------------------------------------------------------
    def add_samples(self, batch):
        """Slot for ConnectionPanel.samples_ready."""
        data = np.asarray(batch, dtype=float)
        self._append(data[:, 0], data[:, 2])

    def mark_gap(self):
        """Break the line, e.g. when reconnecting, so a gap isn't drawn as a slope."""
        if self._n:
            self._append(np.array([now()]), np.array([np.nan]))

    def add_click(self, t, button, pressed):
        if not self.clicks_box.isChecked():
            return
        color = self.CLICK_COLORS.get(button, (120, 120, 120))
        if pressed:
            line = pg.InfiniteLine(pos=t, angle=90, movable=False,
                                   pen=pg.mkPen(color, width=1.5))
            self.plot.addItem(line)
            self._pending[button] = (t, line)
        else:
            start = self._pending.pop(button, None)
            if start is None:
                return
            t_press, line = start
            self.plot.removeItem(line)
            brush = pg.mkBrush(*color, 60)
            band = pg.LinearRegionItem(values=(t_press, t), movable=False,
                                       brush=brush, hoverBrush=brush,
                                       pen=pg.mkPen(color, width=1))
            band.setZValue(-10)
            self.plot.addItem(band)
            self._markers.append((t, band))

    # -- calibration ----------------------------------------------------------
    def set_calibration(self, calibration):
        self.calibration = calibration
        self.refresh_calibration()

    def refresh_calibration(self):
        """Call after the calibration changes (connected to CalibrationPanel.changed)."""
        cal = self.calibration
        if cal is None:
            return
        self.plot.getPlotItem().setLabel("left", cal.axis_label())
        show_line = cal.baseline is not None or cal.unit != "raw"
        self.zero_line.setVisible(show_line)
        if show_line:
            self.zero_line.setPos(float(cal.convert(cal.effective_baseline())))
        if self.paused:
            t, y = self._ordered()
            self.curve.setData(t.copy(), self._to_units(y))
            self.plot.getPlotItem().enableAutoRange(axis="y")

    def _to_units(self, y):
        if self.calibration is None:
            return y
        return np.array(self.calibration.convert(y), dtype=float, copy=True)

    # -- controls -------------------------------------------------------------
    def clear(self):
        self._i = self._n = 0
        self._remove_markers()
        self.curve.setData([], [])

    def shutdown(self):
        self.timer.stop()
        self.clicks.stop()

    # -- drawing --------------------------------------------------------------
    def redraw(self):
        self._expire_markers()
        if self.paused:
            return
        span = self.window_combo.currentData()
        t_now = now()
        t, y = self._ordered()
        if t.size:
            first = np.searchsorted(t, t_now - span - 1)
            t, y = t[first:], self._to_units(y[first:])
        self.curve.setData(t, y)
        self.plot.setXRange(t_now - span, t_now, padding=0)

        if self.autoscale_box.isChecked() and y.size:
            visible = y[np.isfinite(y)]
            if visible.size:
                lo, hi = float(visible.min()), float(visible.max())
                scale = self.calibration.scale() if self.calibration else 1.0
                half = max((hi - lo) / 2 * 1.15, self.MIN_Y_SPAN * scale / 2)
                mid = (lo + hi) / 2
                self.plot.setYRange(mid - half, mid + half, padding=0)

    # -- internal -------------------------------------------------------------
    def _append(self, t, y):
        k = len(t)
        if k == 0:
            return
        if k >= self._cap:
            t, y, k = t[-self._cap:], y[-self._cap:], self._cap
        end = self._i + k
        if end <= self._cap:
            self._t[self._i:end] = t
            self._y[self._i:end] = y
        else:
            first = self._cap - self._i
            self._t[self._i:] = t[:first]
            self._y[self._i:] = y[:first]
            self._t[:k - first] = t[first:]
            self._y[:k - first] = y[first:]
        self._i = end % self._cap
        self._n = min(self._n + k, self._cap)

    def _ordered(self):
        """Buffered samples, oldest first."""
        if self._n < self._cap:
            return self._t[:self._n], self._y[:self._n]
        return (np.concatenate((self._t[self._i:], self._t[:self._i])),
                np.concatenate((self._y[self._i:], self._y[:self._i])))

    def _toggle_pause(self, paused):
        self.paused = paused
        self.pause_btn.setText("Resume" if paused else "Pause")
        self.plot.getPlotItem().setMouseEnabled(x=paused, y=paused)
        if paused:
            t, y = self._ordered()
            self.curve.setData(t.copy(), self._to_units(y))   # whole buffer, for panning back
            self.note.setText(f"Paused. Drag to pan and scroll to zoom through the last "
                              f"{self.BUFFER_SECONDS} s. Data is still being received.")
        else:
            self.note.setText("")

    def _toggle_clicks(self, on):
        if on:
            self.clicks.start()
        else:
            self.clicks.stop()
            self._remove_markers()

    def _on_click_watch_failed(self, message):
        self.clicks_box.blockSignals(True)
        self.clicks_box.setChecked(False)
        self.clicks_box.blockSignals(False)
        self.note.setText(message)

    def _expire_markers(self):
        cutoff = now() - self.BUFFER_SECONDS
        while self._markers and self._markers[0][0] < cutoff:
            self.plot.removeItem(self._markers.pop(0)[1])

    def _remove_markers(self):
        for _, item in self._markers:
            self.plot.removeItem(item)
        for _, line in self._pending.values():
            self.plot.removeItem(line)
        self._markers.clear()
        self._pending.clear()