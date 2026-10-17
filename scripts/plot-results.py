"""Render the final paired-resource figure (requires matplotlib 3.11.2)."""

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
data = json.loads((ROOT / "reports/final-metrics.json").read_text())
rows = data["tasks"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                     "svg.hashsalt": "terminal-bench-final"})
fig, axes = plt.subplots(1, 2, figsize=(12.5, 11.6), sharey=True)
fig.patch.set_facecolor("#ffffff")
for ax, key, title in zip(axes, ["time_ratio", "token_ratio"], ["Solve time", "Input + output tokens"]):
    ax.set_xscale("log", base=2)
    ax.set_xlim(0.0625, 8)
    ax.axvspan(0.0625, 1, color="#edf7f4")
    ax.axvline(1, color="#667085", lw=1)
    for i, row in enumerate(rows):
        value = row[key]
        color = "#7255a5" if row["task"] == "photonic-waveguide-routing" else "#18786b" if value < 1 else "#b34b32"
        ax.plot([1, value], [i, i], color=color, lw=2, solid_capstyle="round")
        ax.scatter(value, i, color=color, s=27, zorder=3)
        ax.annotate(f"{value:.2f}×", (value, i), xytext=(-7 if value < 1 else 7, 0),
                    textcoords="offset points", ha="right" if value < 1 else "left", va="center", fontsize=8.5, color=color)
    ax.xaxis.set_major_locator(FixedLocator([0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}×"))
    ax.grid(axis="y", color="#eef0f3", lw=0.5)
    ax.set_axisbelow(True)
    ax.set_title(title, fontsize=13, loc="left", pad=12, fontweight="bold")
    ax.set_xlabel("Cuppet / Codex · logarithmic scale", labelpad=12)
    ax.tick_params(axis="y", length=0)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#cbd0d8")
axes[0].set_yticks(range(len(rows)), [row["task"] for row in rows])
axes[0].invert_yaxis()
fig.suptitle("Cuppet / Codex on 27 completed, graded task pairs", x=0.03, ha="left", fontsize=17, fontweight="bold", y=0.98)
fig.text(0.03, 0.943, "Below 1×: Cuppet used less. Above 1×: Cuppet used more. Photonic is highlighted in purple.", fontsize=10, color="#475467")
fig.text(0.03, 0.025, "Retained solves only. Tokens include cached input; reasoning is included in output.\nInterrupted attempts and setup / verification costs are accounted for separately in REPORT.md.", fontsize=9, color="#475467")
fig.subplots_adjust(left=0.255, right=0.97, top=0.90, bottom=0.13, wspace=0.12)
(ROOT / "assets").mkdir(exist_ok=True)
(ROOT / ".cache").mkdir(exist_ok=True)
fig.savefig(ROOT / "assets/task-ratios.svg", metadata={"Date": None})
fig.savefig(ROOT / ".cache/report-task-ratios.png", dpi=135)
plt.close(fig)
print(ROOT / "assets/task-ratios.svg")
