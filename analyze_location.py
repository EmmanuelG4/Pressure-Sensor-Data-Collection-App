"""
Summarize a location-test recording: the average force at each grid square.

Usage:
  python analyze_location.py                       (uses your most recent recording)
  python analyze_location.py "C:\\Users\\you\\PressureRecordings\\20261008_021924_EG_Location-test"

Expects markers labeled with the square name (A1 ... C3), typed while the mouse
sits on that square. It doesn't matter whether you press Enter right after
placing the mouse or after waiting: the script finds the steady stretch of
signal around each marker (between the moments the mouse was picked up and put
down) and averages only that. A label with extra words, like "B2 hand", is
treated as a separate condition and gets its own heat map.

Writes into the recording folder:
  location_results.csv   one row per square and condition
  location_heatmap.png   3x3 heat map of average force (one per condition)
  location_trace.png     the whole recording with each measurement window shaded
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

BIN_S = 0.05            # signal is summarized in 50 ms steps for finding steady stretches
SMOOTH_BINS = 7         # 0.35 s rolling median
STEP_COUNTS = 2.0       # a change this big (in raw counts), held for 0.2 s, starts a new stretch
MIN_STEADY_S = 0.8      # shorter stretches are treated as the mouse being moved
EDGE_TRIM_S = 0.2       # skip this much at each end of a steady stretch
MAX_REACH_S = 3.0       # average at most this far before and after the marker
ROWS, COLS = "ABC", "123"

BLUE = LinearSegmentedColormap.from_list(
    "blue", ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])
INK, MUTED, GRID = "#1f1f1f", "#6b6b6b", "#e4e4e4"


# ------------------------------------------------------------------ loading
def latest_recording():
    root = Path.home() / "PressureRecordings"
    folders = sorted(p for p in root.glob("*") if (p / "sensor.csv").exists())
    if not folders:
        sys.exit(f"No recordings found in {root}")
    return folders[-1]


def load(folder):
    folder = Path(folder)
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    data = np.loadtxt(folder / "sensor.csv", delimiter=",", skiprows=1)
    t, raw = data[:, 0], data[:, 2]
    keep = raw >= 0                      # drop sensor read errors (-1)
    t, raw = t[keep], raw[keep]
    markers = []
    with open(folder / "markers.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            markers.append((float(row["host_time_s"]), row["label"].strip()))
    return session, t, raw, markers


def parse_label(label):
    """'B2' -> ('B2', 'mouse only'); 'b2 hand' -> ('B2', 'hand')."""
    m = re.match(r"\s*([A-Ca-c])\s*([1-3])\b\s*(.*)", label)
    if not m:
        return None, None
    square = m.group(1).upper() + m.group(2)
    condition = m.group(3).strip().lower() or "mouse only"
    return square, condition


# ------------------------------------------------------------------ analysis
def steady_stretches(t, raw, step_counts):
    """Split the recording wherever the level changes, return [(start, end), ...]."""
    edges = np.arange(t[0], t[-1] + BIN_S, BIN_S)
    idx = np.digitize(t, edges) - 1
    centers, values = [], []
    for b in np.unique(idx):
        sel = idx == b
        centers.append(edges[b] + BIN_S / 2)
        values.append(np.median(raw[sel]))
    centers, values = np.array(centers), np.array(values)
    half = SMOOTH_BINS // 2
    smooth = np.array([np.median(values[max(0, i - half): i + half + 1])
                       for i in range(len(values))])

    starts = [0]
    run = 0
    for i in range(1, len(smooth)):
        s0 = starts[-1]
        ref = np.median(smooth[max(s0, i - 40): i]) if i - s0 >= 4 else smooth[s0]
        run = run + 1 if abs(smooth[i] - ref) >= step_counts else 0
        if run >= 4:
            starts.append(i - 3)
            run = 0
    bounds = starts + [len(smooth)]
    stretches = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        start, end = centers[a] - BIN_S / 2, centers[b - 1] + BIN_S / 2
        if end - start >= MIN_STEADY_S:
            stretches.append((start, end))
    return stretches


def window_for_marker(tm, stretches):
    """The steady stretch holding the marker (or the nearest one), trimmed and
    limited to MAX_REACH_S on either side of the marker."""
    if not stretches:
        return None
    def distance(st):
        return 0.0 if st[0] <= tm <= st[1] else min(abs(tm - st[0]), abs(tm - st[1]))
    start, end = min(stretches, key=distance)
    if distance((start, end)) > 2.0:
        return None
    start, end = start + EDGE_TRIM_S, end - EDGE_TRIM_S
    return max(start, tm - MAX_REACH_S), min(end, tm + MAX_REACH_S)


def analyze(session, t, raw, markers):
    cal = session["calibration_at_start"]
    baseline = cal["effective_baseline_raw"]
    n_per_count = cal["full_scale_force_n"] / cal["full_scale_counts"]
    noise_counts = cal.get("baseline_noise_std") or float(np.std(raw[: min(len(raw), 500)]))
    noise_n = noise_counts * n_per_count

    stretches = steady_stretches(t, raw, max(STEP_COUNTS, 2 * noise_counts))
    windows, skipped = [], []
    for tm, label in markers:
        square, cond = parse_label(label)
        window = window_for_marker(tm, stretches)
        if square is None or window is None:
            skipped.append(label)
            continue
        start, end = window
        sel = (t >= start) & (t <= end)
        if sel.sum() < 50:
            skipped.append(label)
            continue
        force = (np.mean(raw[sel]) - baseline) * n_per_count   # mean resolves below 1 count
        windows.append({"square": square, "condition": cond, "marker": tm, "start": start,
                        "end": end, "force_n": force, "samples": int(sel.sum())})

    grouped = defaultdict(list)
    for w in windows:
        grouped[(w["square"], w["condition"])].append(w["force_n"])
    results = []
    for (square, cond), values in sorted(grouped.items()):
        mean = float(np.mean(values))
        results.append({
            "square": square, "condition": cond, "passes": len(values),
            "mean_force_n": mean,
            "spread_between_passes_n": float(np.std(values)) if len(values) > 1 else None,
            "signal_to_noise": mean / noise_n if noise_n else None,
        })
    return {"windows": windows, "results": results, "skipped": skipped,
            "baseline": baseline, "n_per_count": n_per_count, "noise_n": noise_n}


# ------------------------------------------------------------------ outputs
def write_csv(folder, a):
    path = Path(folder) / "location_results.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["square", "condition", "passes", "mean_force_n",
                    "spread_between_passes_n", "signal_to_noise"])
        for r in a["results"]:
            w.writerow([r["square"], r["condition"], r["passes"], f"{r['mean_force_n']:.4f}",
                        "" if r["spread_between_passes_n"] is None
                        else f"{r['spread_between_passes_n']:.4f}",
                        f"{r['signal_to_noise']:.1f}"])
    return path


def plot_heatmaps(folder, a, session):
    conditions = sorted({r["condition"] for r in a["results"]},
                        key=lambda c: (c != "mouse only", c))
    noise = a["noise_n"]
    # Keep the color scale at least 5x the noise wide, so noise can't look like a pattern.
    top = max([r["mean_force_n"] for r in a["results"]] + [5 * noise])
    fig, axes = plt.subplots(1, len(conditions), figsize=(4.2 * len(conditions) + 1, 4.6),
                             squeeze=False)
    for ax, cond in zip(axes[0], conditions):
        grid = np.full((3, 3), np.nan)
        for r in a["results"]:
            if r["condition"] == cond:
                grid[ROWS.index(r["square"][0]), COLS.index(r["square"][1])] = r["mean_force_n"]
        im = ax.imshow(np.clip(grid, 0, None), cmap=BLUE, vmin=0, vmax=top)
        for i in range(3):
            for j in range(3):
                v = grid[i, j]
                text = "–" if np.isnan(v) else f"{v:.2f} N"
                shade = 0 if np.isnan(v) else max(v, 0) / top
                ax.text(j, i, f"{ROWS[i]}{COLS[j]}\n{text}", ha="center", va="center",
                        fontsize=10, color="white" if shade > 0.55 else INK)
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_title(f"Mouse on each square ({cond})", fontsize=11, color=INK, loc="left")
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cb.set_label("Average force above baseline (N)", color=MUTED, fontsize=9)
        cb.outline.set_visible(False)
    placement = session["session"].get("sensor_placement", "")
    fig.suptitle(f"Sensor: {placement}\nNoise: ±{noise:.3f} N  (a real signal is about "
                 f"{3 * noise:.2f} N or more)", fontsize=9, color=MUTED, x=0.02, ha="left")
    fig.tight_layout()
    path = Path(folder) / "location_heatmap.png"
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)
    return path


def plot_trace(folder, a, t, raw):
    # Median per 50 ms keeps the plot light without hiding real changes.
    bins = np.floor((t - t[0]) / 0.05).astype(int)
    order = np.unique(bins)
    tt = np.array([t[bins == b].mean() for b in order])
    ff = np.array([np.median(raw[bins == b]) for b in order])
    ff = (ff - a["baseline"]) * a["n_per_count"]

    fig, ax = plt.subplots(figsize=(12, 4))
    for w in a["windows"]:
        ax.axvspan(w["start"], w["end"], color="#9ec5f4", alpha=0.45, lw=0, zorder=1)
        ax.axvline(w["marker"], color=MUTED, lw=0.8, ls=":", zorder=2)
        ax.text((w["start"] + w["end"]) / 2, 1.0, w["square"] +
                ("" if w["condition"] == "mouse only" else f"\n{w['condition']}"),
                transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=9, color=INK)
    ax.axhspan(-3 * a["noise_n"], 3 * a["noise_n"], color="#ecebe7", lw=0, zorder=0)
    ax.axhline(0, color=MUTED, lw=1, ls="--")
    ax.plot(tt, ff, color="#184f95", lw=1.5, zorder=3)
    lo = min(ff.min(), -5 * a["noise_n"])
    hi = max(ff.max(), 5 * a["noise_n"])
    ax.set_ylim(lo - 0.1 * (hi - lo), hi + 0.1 * (hi - lo))
    ax.set_xlabel("Time since app start (s)", color=MUTED)
    ax.set_ylabel("Force above baseline (N)", color=MUTED)
    ax.grid(axis="y", color=GRID, lw=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.set_title("Whole recording: shaded = steady stretch averaged, dotted = marker, "
                 "gray band = ±3× noise",
                 fontsize=10, color=MUTED, loc="left", pad=28)
    fig.tight_layout()
    path = Path(folder) / "location_trace.png"
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)
    return path


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_recording()
    session, t, raw, markers = load(folder)
    a = analyze(session, t, raw, markers)
    if not a["results"]:
        sys.exit("No markers named like A1 ... C3 were found in markers.csv.")

    print(f"Recording: {folder.name}")
    print(f"Noise level: ±{a['noise_n']:.3f} N  (a square needs about 3x this to count as a real signal)\n")
    print(f"{'square':<7}{'condition':<14}{'passes':>6}{'force (N)':>12}{'x noise':>10}")
    for r in a["results"]:
        print(f"{r['square']:<7}{r['condition']:<14}{r['passes']:>6}"
              f"{r['mean_force_n']:>12.3f}{r['signal_to_noise']:>10.1f}")
    if a["skipped"]:
        print(f"\nSkipped markers: {', '.join(a['skipped'])}")
    for p in (write_csv(folder, a), plot_heatmaps(folder, a, session),
              plot_trace(folder, a, t, raw)):
        print(f"Saved {p}")


if __name__ == "__main__":
    main()