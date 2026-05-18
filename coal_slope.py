#!/usr/bin/env python3
"""
coal_slope.py — plot the near-term slope of EIA's coal projection vs. AEO vintage year.

For each vintage the slope is the linear-regression coefficient (billion kWh/year)
fitted over the first WINDOW_YEARS of that vintage's reference-case projection.
Early AEOs expected coal to grow (positive slope); recent AEOs project steep decline
(negative slope); somewhere in between the slope crossed zero.

Run: python coal_slope.py
Output: output/coal_slope.png
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pathlib import Path

CACHE_DIR = Path("cache")
OUTPUT_DIR = Path("output")

MST_TO_QUAD    = 0.02009
QUADS_TO_BLNKWH = 293.07
MST_TO_BLNKWH  = MST_TO_QUAD * QUADS_TO_BLNKWH

WINDOW_YEARS = 10   # fit slope over first N projected years per vintage
AEO_VINTAGES = list(range(2008, 2027))

# ── Load retrospective CSV ─────────────────────────────────────────────────────

retro_df = pd.read_csv(CACHE_DIR / "aeo_retrospective.csv", low_memory=False)

legacy_map = {f"REF{y}": "REFERENCE" for y in range(2005, 2027)}
legacy_map.update({f"ref{y}": "REFERENCE" for y in range(2005, 2027)})
retro_df["case_name"] = retro_df["case_name"].replace(legacy_map)

COL = "CNSM_NA_NA_NA_CL_NA_NA_MILLTON"

# ── Compute per-vintage slope ──────────────────────────────────────────────────

slopes = {}
for vintage in AEO_VINTAGES:
    mask = (
        retro_df["edition"].astype(str).str.startswith(str(vintage))
        & (retro_df["case_name"] == "REFERENCE")
    )
    sub = retro_df[mask][["year", COL]].copy()
    sub["year"]  = pd.to_numeric(sub["year"],  errors="coerce")
    sub["value"] = pd.to_numeric(sub[COL],     errors="coerce") * MST_TO_BLNKWH
    sub = sub.dropna().query("year >= @vintage").sort_values("year")
    if len(sub) < 2:
        continue
    window = sub[sub["year"] <= vintage + WINDOW_YEARS]
    if len(window) < 2:
        window = sub
    slope, _ = np.polyfit(window["year"], window["value"], 1)
    slopes[vintage] = slope
    print(f"  AEO {vintage}: slope = {slope:+.1f} billion kWh/yr  ({len(window)} pts)")

# ── Plot ───────────────────────────────────────────────────────────────────────

vlist = sorted(slopes.keys())
slist = [slopes[v] for v in vlist]
missing = [v for v in AEO_VINTAGES if v not in slopes]

BG = "#f0f0f0"
fig, ax = plt.subplots(figsize=(11, 6))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.grid(True, color="white", linewidth=1.0, linestyle="-", zorder=0)
ax.set_axisbelow(True)
for sp in ax.spines.values():
    sp.set_visible(False)
ax.tick_params(length=0, labelsize=9)

# Shaded fill above / below zero
ax.fill_between(vlist, slist, 0,
                where=[s > 0 for s in slist],
                color="#d62728", alpha=0.20, zorder=1)
ax.fill_between(vlist, slist, 0,
                where=[s <= 0 for s in slist],
                color="#1f77b4", alpha=0.20, zorder=1)

# Main line + markers
ax.plot(vlist, slist, "o-", color="#222222", linewidth=2.2, markersize=7, zorder=4)

# Zero baseline
ax.axhline(0, color="#555555", linewidth=1.3, linestyle="--", zorder=3)

# Interpolate zero crossing and annotate
for i in range(len(slist) - 1):
    if slist[i] * slist[i + 1] < 0:
        x0, x1 = vlist[i], vlist[i + 1]
        y0, y1 = slist[i], slist[i + 1]
        x_cross = x0 + (-y0) * (x1 - x0) / (y1 - y0)
        ax.axvline(x_cross, color="#888888", linewidth=1.1, linestyle=":", zorder=2)
        y_range = max(slist) - min(slist)
        ax.text(
            x_cross + 0.15, max(slist) - y_range * 0.06,
            f"slope = 0\n≈ {x_cross:.0f}",
            fontsize=8.5, color="#555555", va="top", ha="left",
        )

# Region labels
y_pos = max(slist) * 0.55
y_neg = min(slist) * 0.55
ax.text(vlist[1], y_pos, "Projected coal\nconsumption rising",
        fontsize=9, color="#d62728", ha="left", va="center",
        path_effects=[pe.withStroke(linewidth=3, foreground=BG)])
ax.text(vlist[-2], y_neg, "Projected coal\nconsumption falling",
        fontsize=9, color="#1f77b4", ha="right", va="center",
        path_effects=[pe.withStroke(linewidth=3, foreground=BG)])

ax.set_xlabel("AEO Vintage Year", fontsize=10.5, labelpad=8)
ax.set_ylabel(
    f"Near-term slope (billion kWh / year)\n"
    f"[linear fit over first {WINDOW_YEARS} projected years]",
    fontsize=10.5, labelpad=8,
)
ax.set_title(
    "Slope of EIA Coal Consumption Projection by AEO Vintage",
    fontsize=13.5, fontweight="bold", pad=14,
)
ax.set_xticks(vlist)
plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

missing_note = (f" AEO {', '.join(str(v) for v in missing)} not available."
                if missing else "")
fig.text(
    0.01, 0.01,
    f"Source: EIA AEO retrospective CSV. "
    f"Slope = linear regression over first {WINDOW_YEARS} projected years.{missing_note}",
    fontsize=7.5, color="#555555",
)

plt.tight_layout(rect=[0, 0.04, 1.0, 1.0])
OUTPUT_DIR.mkdir(exist_ok=True)
out = OUTPUT_DIR / "coal_slope.png"
fig.savefig(out, dpi=300, bbox_inches="tight", facecolor=BG)
print(f"\nSaved: {out}")
plt.close(fig)
