#!/usr/bin/env python3
"""
Lazard LCOE History Charts

Downloads Lazard LCOE Analysis data (v1.0–v17.0, 2008–2023) from DataHub,
augments with v18.0 (2025) partial data, and generates:
  - Per-technology LCOE range charts (low–high shaded band + midpoint line)
  - A combined chart of midpoint LCOE for all technologies

Run: python price_chart2.py
Outputs: output/prices2/*_lcoe_lazard.png
"""

import io
import math
import time
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

# ── Config ─────────────────────────────────────────────────────────────────────

CACHE_DIR = Path("cache")
OUTPUT_DIR = Path("output/prices2")
HEADERS = {"User-Agent": "Mozilla/5.0 (energy-projections research project)"}

LAZARD_CSV_URL = (
    "https://datahub.io/climate-and-environment/"
    "lazard-levelized-cost-of-energy/_r/-/data/lcoe.csv"
)

# v18.0 (2025) values not yet in DataHub — hard-coded from Lazard's June 2025 report
LAZARD_V18 = [
    {"year": 2025, "version": "v18.0", "technology_id": "utility_pv",
     "technology": "Utility-Scale Solar PV", "lcoe_low": 38, "lcoe_high": 78},
    {"year": 2025, "version": "v18.0", "technology_id": "onshore_wind",
     "technology": "Onshore Wind", "lcoe_low": 37, "lcoe_high": 86},
    {"year": 2025, "version": "v18.0", "technology_id": "gas_cc",
     "technology": "Gas Combined Cycle", "lcoe_low": 48, "lcoe_high": 109},
    {"year": 2025, "version": "v18.0", "technology_id": "coal",
     "technology": "Coal", "lcoe_low": 71, "lcoe_high": 173},
    {"year": 2025, "version": "v18.0", "technology_id": "offshore_wind",
     "technology": "Offshore Wind", "lcoe_low": 72, "lcoe_high": 140},
    {"year": 2025, "version": "v18.0", "technology_id": "nuclear",
     "technology": "Nuclear", "lcoe_low": 141, "lcoe_high": 220},
    {"year": 2025, "version": "v18.0", "technology_id": "gas_peaker",
     "technology": "Gas Peaker", "lcoe_low": 149, "lcoe_high": 251},
]

# Colors per technology (for combined chart and individual charts)
TECH_COLORS: dict[str, str] = {
    "Utility-Scale Solar PV": "#e69520",
    "Onshore Wind":           "#3a80c8",
    "Gas Combined Cycle":     "#888888",
    "Coal":                   "#5a3a1a",
    "Nuclear":                "#9b59b6",
    "Offshore Wind":          "#1a8e8e",
    "Gas Peaker":             "#c0392b",
}

# Per-technology chart config
LAZARD_TYPES: dict[str, dict] = {
    "solar_pv": {
        "technology": "Utility-Scale Solar PV",
        "title": "Lazard LCOE Analysis – Utility-Scale Solar PV",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "solar_pv_lcoe_lazard",
        "ylim": None,
        "atb_technology": "UtilityPV",
        "atb_techdetail": "Class5",
    },
    "onshore_wind": {
        "technology": "Onshore Wind",
        "title": "Lazard LCOE Analysis – Onshore Wind",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "onshore_wind_lcoe_lazard",
        "ylim": None,
        "atb_technology": "LandbasedWind",
        "atb_techdetail": "Class4",
    },
    "gas_cc": {
        "technology": "Gas Combined Cycle",
        "title": "Lazard LCOE Analysis – Gas Combined Cycle",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "gas_cc_lcoe_lazard",
        "ylim": None,
        "atb_technology": None,
        "atb_techdetail": None,
    },
    "coal": {
        "technology": "Coal",
        "title": "Lazard LCOE Analysis – Coal",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "coal_lcoe_lazard",
        "ylim": None,
        "atb_technology": None,
        "atb_techdetail": None,
    },
    "nuclear": {
        "technology": "Nuclear",
        "title": "Lazard LCOE Analysis – Nuclear",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "nuclear_lcoe_lazard",
        "ylim": None,
        "atb_technology": "Nuclear",
        "atb_techdetail": None,
    },
    "offshore_wind": {
        "technology": "Offshore Wind",
        "title": "Lazard LCOE Analysis – Offshore Wind",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "offshore_wind_lcoe_lazard",
        "ylim": None,
        "atb_technology": "OffShoreWind",
        "atb_techdetail": "Class3",
    },
    "gas_peaker": {
        "technology": "Gas Peaker",
        "title": "Lazard LCOE Analysis – Gas Peaker",
        "ylabel": "LCOE ($/MWh, unsubsidized)",
        "output_stem": "gas_peaker_lcoe_lazard",
        "ylim": None,
        "atb_technology": None,
        "atb_techdetail": None,
    },
}

# Lazard v1–v5 (published 2008–2011) reported subsidized LCOE;
# v6+ (2012 onward) switched to unsubsidized. Mark the cutoff year.
SUBSIDIZED_CUTOFF_YEAR = 2011

# ── NREL ATB config ────────────────────────────────────────────────────────────

ATB_VINTAGES = list(range(2019, 2025))

ATB_S3_URLS: dict[int, str] = {
    2024: "https://oedi-data-lake.s3.amazonaws.com/ATB/electricity/csv/2024/v3.0.0/ATBe.csv",
    **{y: f"https://oedi-data-lake.s3.amazonaws.com/ATB/electricity/csv/{y}/ATBe.csv"
       for y in range(2019, 2024)},
}

ATB_BASE_YEAR: dict[int, int] = {
    2019: 2017, 2020: 2018, 2021: 2019,
    2022: 2020, 2023: 2021, 2024: 2022,
}


# ── Download helper ────────────────────────────────────────────────────────────


def download(url: str, path: Path, force: bool = False) -> bytes:
    if path.exists() and not force:
        print(f"  [cache] {path.name}")
        return path.read_bytes()
    print(f"  [fetch] {url}")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120)
            if r.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"    Rate-limited, waiting {wait}s…")
                time.sleep(wait)
                continue
            r.raise_for_status()
            path.write_bytes(r.content)
            return r.content
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            print(f"    Attempt {attempt + 1} failed ({exc}), retrying…")
            time.sleep(2)


# ── Data loading ───────────────────────────────────────────────────────────────


def load_lazard_csv() -> pd.DataFrame:
    data = download(LAZARD_CSV_URL, CACHE_DIR / "lazard_lcoe.csv")
    df = pd.read_csv(io.BytesIO(data))
    print(f"  Loaded Lazard CSV: {df.shape[0]} rows, columns: {list(df.columns)}")

    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["lcoe_low"] = pd.to_numeric(df["lcoe_low"], errors="coerce")
    df["lcoe_high"] = pd.to_numeric(df["lcoe_high"], errors="coerce")
    df = df.dropna(subset=["year", "lcoe_low", "lcoe_high"])
    df["year"] = df["year"].astype(int)

    # Append hard-coded v18.0 rows
    v18_df = pd.DataFrame(LAZARD_V18)
    df = pd.concat([df, v18_df], ignore_index=True)

    # Midpoint
    df["lcoe_mid"] = (df["lcoe_low"] + df["lcoe_high"]) / 2

    print(f"  Technologies: {sorted(df['technology'].unique())}")
    print(f"  Years: {sorted(df['year'].unique())}")
    return df


# ── NREL ATB helpers ──────────────────────────────────────────────────────────


def fetch_atb_csv(vintage: int) -> pd.DataFrame | None:
    url = ATB_S3_URLS[vintage]
    cache_path = CACHE_DIR / f"ATBe_{vintage}.csv"
    try:
        data = download(url, cache_path)
        df = pd.read_csv(io.BytesIO(data), low_memory=False)
        print(f"  ATB {vintage}: {df.shape[0]} rows loaded")
        return df
    except Exception as exc:
        print(f"  ATB {vintage} failed: {exc}")
        return None


def extract_atb_projection(df: pd.DataFrame, atb_tech: str,
                           atb_techdetail: str | None, vintage: int) -> pd.DataFrame:
    if "core_metric_parameter" not in df.columns or "technology" not in df.columns:
        return pd.DataFrame(columns=["year", "value"])

    scenario_candidates = ["Moderate", "Mid"]
    sub = pd.DataFrame()
    for scenario_name in scenario_candidates:
        mask = (
            (df["core_metric_parameter"] == "LCOE") &
            (df["technology"] == atb_tech) &
            (df["scenario"] == scenario_name)
        )
        sub = df[mask].copy()
        if not sub.empty:
            break
    if sub.empty:
        return pd.DataFrame(columns=["year", "value"])

    if "crpyears" in sub.columns:
        for crp in [30, 20]:
            cand = sub[sub["crpyears"] == crp]
            if not cand.empty:
                sub = cand
                break

    if "tax_credit_case" in sub.columns:
        no_tax = sub[sub["tax_credit_case"].isna()]
        if not no_tax.empty:
            sub = no_tax

    if "core_metric_case" in sub.columns:
        for case in ["R&D", "Market"]:
            cand = sub[sub["core_metric_case"] == case]
            if not cand.empty:
                sub = cand
                break

    if atb_techdetail and "techdetail" in sub.columns:
        cand = sub[sub["techdetail"] == atb_techdetail]
        if not cand.empty:
            sub = cand
        else:
            classes = sorted(sub["techdetail"].dropna().unique())
            if classes:
                sub = sub[sub["techdetail"] == classes[len(classes) // 2]]

    result = (
        sub.groupby("core_metric_variable")["value"]
        .mean().reset_index()
        .rename(columns={"core_metric_variable": "year"})
    )
    result["year"] = pd.to_numeric(result["year"], errors="coerce")
    result = result.dropna(subset=["year", "value"])
    result["year"] = result["year"].astype(int)
    base = ATB_BASE_YEAR.get(vintage, vintage - 2)
    result = result[result["year"] >= base]
    return result.sort_values("year").reset_index(drop=True)


def fetch_atb_projections(atb_tech: str,
                          atb_techdetail: str | None) -> dict[int, pd.DataFrame]:
    projections: dict[int, pd.DataFrame] = {}
    for vintage in ATB_VINTAGES:
        df = fetch_atb_csv(vintage)
        if df is None:
            continue
        proj = extract_atb_projection(df, atb_tech, atb_techdetail, vintage)
        if not proj.empty:
            projections[vintage] = proj
            print(f"    ATB {vintage}: {len(proj)} projection years")
        else:
            print(f"    ATB {vintage}: no data for {atb_tech}")
    return projections


# ── Y-axis helper ──────────────────────────────────────────────────────────────


def _nice_ylim(low_vals, high_vals,
               atb_projections: dict | None = None) -> tuple[float, float]:
    vals = [v for v in list(low_vals) + list(high_vals) if pd.notna(v)]
    if atb_projections:
        for proj_df in atb_projections.values():
            vals.extend(proj_df["value"].dropna().tolist())
    if not vals:
        return (0, 100)
    mx = max(vals)
    power = 10 ** math.floor(math.log10(mx))
    for s in [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]:
        candidate = math.ceil(mx / (power * s)) * power * s
        if candidate >= mx * 1.08:
            return (0, candidate)
    return (0, math.ceil(mx * 1.15 / power) * power)


def _nice_y_step(ylim: tuple[float, float]) -> float:
    y_range = ylim[1] - ylim[0]
    rough = max(1, round(y_range / 8))
    for nice in [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500]:
        if nice >= rough:
            return float(nice)
    return float(rough)


# ── Label nudge (for combined chart) ──────────────────────────────────────────


def _nudge_by_group(
    label_entries: list[tuple], min_gap: float = 0.0,
    y_min: float = 0.0, y_max: float = 1e9,
) -> dict[int, float]:
    adj: dict[int, float] = {i: e[1] for i, e in enumerate(label_entries)}
    by_x: dict[int, list[int]] = defaultdict(list)
    for i, (x, y, *_) in enumerate(label_entries):
        by_x[round(x)].append(i)
    for indices in by_x.values():
        if len(indices) <= 1:
            continue
        grp = sorted(indices, key=lambda i: adj[i])
        for _ in range(80):
            moved = False
            for k in range(1, len(grp)):
                ia, ib = grp[k - 1], grp[k]
                ya, yb = adj[ia], adj[ib]
                if yb - ya < min_gap:
                    mid = (ya + yb) / 2
                    adj[ia] = max(y_min, mid - min_gap / 2)
                    adj[ib] = min(y_max, adj[ia] + min_gap)
                    moved = True
            if not moved:
                break
    return adj


# ── Per-technology band chart ──────────────────────────────────────────────────


def build_lazard_chart(
    df_tech: pd.DataFrame,
    ecfg: dict,
    atb_projections: dict[int, pd.DataFrame] | None = None,
) -> None:
    tech_name = ecfg["technology"]
    color = TECH_COLORS.get(tech_name, "#444444")
    has_atb = bool(atb_projections)

    df_sorted = df_tech.sort_values("year").reset_index(drop=True)
    years = df_sorted["year"].values
    low = df_sorted["lcoe_low"].values
    high = df_sorted["lcoe_high"].values
    mid = df_sorted["lcoe_mid"].values

    BG = "#f0f0f0"
    fig, ax = plt.subplots(figsize=(13.5, 7.8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.grid(True, color="white", linewidth=1.0, linestyle="-", zorder=0)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0, labelsize=12)

    ylim = ecfg["ylim"]
    if ylim is None:
        ylim = _nice_ylim(low, high, atb_projections)
    ax.set_ylim(*ylim)
    y_min_label = ylim[0] + ylim[1] * 0.01
    y_max_label = ylim[1] * 0.99

    x_min = 2006
    x_max = 2053 if has_atb else 2027
    ax.set_xlim(x_min, x_max)
    if has_atb:
        ax.set_xticks(range(2010, 2055, 5))
    else:
        ax.set_xticks(range(2008, 2026, 2))

    y_step = _nice_y_step(ylim)
    ax.set_yticks(np.arange(ylim[0], ylim[1] + y_step * 0.5, y_step))

    # Shade the subsidized years (2008–2011)
    ax.axvspan(x_min, SUBSIDIZED_CUTOFF_YEAR + 0.5, alpha=0.10,
               color="#aaaaaa", zorder=0, lw=0)
    ax.axvline(SUBSIDIZED_CUTOFF_YEAR + 0.5, color="#999999",
               linewidth=0.8, linestyle="--", zorder=1)
    ax.text(
        SUBSIDIZED_CUTOFF_YEAR - 1.5, ylim[1] * 0.97,
        "← subsidized", fontsize=9, color="#777777",
        ha="right", va="top", fontstyle="italic",
    )

    # ── NREL ATB projection lines (behind Lazard band) ────────────────────────
    atb_label_entries: list[tuple] = []
    if has_atb:
        vintages = sorted(atb_projections.keys())
        n = len(vintages)
        cmap = matplotlib.colormaps["plasma"]
        atb_colors = {
            v: cmap(0.05 + i / max(n - 1, 1) * 0.73)
            for i, v in enumerate(vintages)
        }
        for vintage in vintages:
            vdf = atb_projections[vintage].sort_values("year")
            if vdf.empty:
                continue
            c = atb_colors[vintage]
            ax.plot(vdf["year"], vdf["value"], color=c,
                    linewidth=1.1, alpha=0.85, zorder=2)
            last = vdf.iloc[-1]
            atb_label_entries.append(
                (float(last["year"]), float(last["value"]), str(vintage), c)
            )

    # ── Lazard band (on top of ATB lines) ────────────────────────────────────
    ax.fill_between(years, low, high, alpha=0.30, color=color, zorder=3)
    ax.plot(years, low, color=color, linewidth=0.9, linestyle="--",
            alpha=0.6, zorder=4)
    ax.plot(years, high, color=color, linewidth=0.9, linestyle="--",
            alpha=0.6, zorder=4)
    ax.plot(years, mid, color=color, linewidth=2.5,
            marker="o", markersize=5, markerfacecolor=color, markeredgewidth=0,
            zorder=5, solid_capstyle="round")

    # ── Labels ────────────────────────────────────────────────────────────────
    # ATB vintage labels at line ends
    if atb_label_entries:
        adj_y = _nudge_by_group(
            atb_label_entries, min_gap=ylim[1] * 0.016,
            y_min=y_min_label, y_max=y_max_label,
        )
        for i, (orig_x, orig_y, text, c) in enumerate(atb_label_entries):
            txt = ax.text(
                orig_x + 0.6, adj_y[i], text,
                fontsize=10, fontweight="bold", color=c,
                va="center", ha="left", clip_on=False,
            )
            txt.set_path_effects([pe.withStroke(linewidth=3, foreground="white")])

    # Lazard band labels
    lazard_end_x = float(years[-1])
    lazard_label_x = lazard_end_x + (1.5 if has_atb else 0.35)
    for label, vals, fs in [
        ("Lazard low", low, 9.5),
        ("Lazard high", high, 9.5),
        ("Lazard midpoint", mid, 11),
    ]:
        txt = ax.text(
            lazard_label_x, vals[-1], label,
            fontsize=fs, fontweight="bold", color=color,
            va="center", ha="left", clip_on=False,
        )
        txt.set_path_effects([pe.withStroke(linewidth=3, foreground="white")])

    ax.set_xlabel("Year", fontsize=13, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_ylabel(ecfg["ylabel"], fontsize=13, labelpad=8,
                  fontfamily="DejaVu Sans")
    ax.set_title(ecfg["title"], fontsize=16, fontweight="bold",
                 fontfamily="DejaVu Sans", pad=14)

    latest_year = int(df_sorted["year"].max())
    atb_note = (
        " NREL ATB (2019–2024) projection lines shown for comparison."
        if has_atb else ""
    )
    fig.text(
        0.01, 0.01,
        (f"Source: Lazard Levelized Cost of Energy Analysis "
         f"(v1.0–v18.0, 2008–{latest_year}) via DataHub.io.{atb_note} "
         "Shaded band = Lazard reported low–high range; line = midpoint. "
         "Gray shading: v1–v5 (2008–2011) used subsidized LCOE; "
         "v6+ (2012 onward) unsubsidized."),
        fontsize=7.5, color="#555555", fontfamily="DejaVu Sans",
    )

    plt.tight_layout(rect=[0, 0.04, 1.0, 1.0])
    out = OUTPUT_DIR / f"{ecfg['output_stem']}.png"
    try:
        fig.savefig(out, dpi=300, bbox_inches="tight", facecolor=BG)
        print(f"  Saved: {out}")
    except Exception as exc:
        print(f"  Could not save PNG: {exc}")
    plt.close(fig)


# ── Combined midpoint comparison chart ────────────────────────────────────────


def build_combined_chart(df: pd.DataFrame) -> None:
    """All-technology midpoint LCOE on one chart for comparison."""
    BG = "#f0f0f0"
    fig, ax = plt.subplots(figsize=(13.5, 7.8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.grid(True, color="white", linewidth=1.0, linestyle="-", zorder=0)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0, labelsize=12)

    # Collect all midpoints to compute ylim
    all_mids = df["lcoe_mid"].dropna().tolist()
    ylim = _nice_ylim(all_mids, all_mids)
    ax.set_ylim(*ylim)

    x_min, x_max = 2006, 2027
    ax.set_xlim(x_min, x_max)
    ax.set_xticks(range(2008, 2026, 2))

    y_step = _nice_y_step(ylim)
    ax.set_yticks(np.arange(ylim[0], ylim[1] + y_step * 0.5, y_step))

    # Subsidized shading
    ax.axvspan(x_min, SUBSIDIZED_CUTOFF_YEAR + 0.5, alpha=0.10,
               color="#aaaaaa", zorder=0, lw=0)
    ax.axvline(SUBSIDIZED_CUTOFF_YEAR + 0.5, color="#999999",
               linewidth=0.8, linestyle="--", zorder=1)
    ax.text(
        SUBSIDIZED_CUTOFF_YEAR - 1.5, ylim[1] * 0.97,
        "← subsidized", fontsize=9, color="#777777",
        ha="right", va="top", fontstyle="italic",
    )

    label_entries: list[tuple] = []

    for tech_name, color in TECH_COLORS.items():
        sub = df[df["technology"] == tech_name].sort_values("year")
        if sub.empty:
            continue
        years = sub["year"].values
        mid = sub["lcoe_mid"].values
        ax.plot(years, mid, color=color, linewidth=2.0,
                marker="o", markersize=4, markerfacecolor=color,
                markeredgewidth=0, zorder=3, solid_capstyle="round",
                alpha=0.9)
        last_y = float(years[-1])
        last_v = float(mid[-1])
        label_entries.append((last_y, last_v, tech_name, color))

    adj_y = _nudge_by_group(
        label_entries, min_gap=ylim[1] * 0.03,
        y_min=ylim[0] + ylim[1] * 0.01,
        y_max=ylim[1] * 0.99,
    )
    for i, (orig_x, orig_y, text, color) in enumerate(label_entries):
        txt = ax.text(
            orig_x + 0.35, adj_y[i], text,
            fontsize=10, fontweight="bold", color=color,
            va="center", ha="left", clip_on=False,
        )
        txt.set_path_effects([pe.withStroke(linewidth=3, foreground="white")])

    ax.set_xlabel("Year", fontsize=13, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_ylabel("LCOE Midpoint ($/MWh)", fontsize=13, labelpad=8,
                  fontfamily="DejaVu Sans")
    ax.set_title(
        "Lazard LCOE Analysis – All Technologies (Midpoint)",
        fontsize=16, fontweight="bold", fontfamily="DejaVu Sans", pad=14,
    )

    latest_year = int(df["year"].max())
    fig.text(
        0.01, 0.01,
        (f"Source: Lazard Levelized Cost of Energy Analysis "
         f"(v1.0–v18.0, 2008–{latest_year}) via DataHub.io. "
         "Lines show midpoint of reported low–high range. "
         "Gray shading: v1–v5 (2008–2011) used subsidized LCOE; "
         "v6+ (2012 onward) unsubsidized."),
        fontsize=7.5, color="#555555", fontfamily="DejaVu Sans",
    )

    plt.tight_layout(rect=[0, 0.04, 1.0, 1.0])
    out = OUTPUT_DIR / "all_technologies_lcoe_lazard.png"
    try:
        fig.savefig(out, dpi=300, bbox_inches="tight", facecolor=BG)
        print(f"  Saved: {out}")
    except Exception as exc:
        print(f"  Could not save PNG: {exc}")
    plt.close(fig)


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("═══ Loading Lazard LCOE CSV… ═══")
    try:
        df = load_lazard_csv()
    except Exception as exc:
        print(f"ERROR: Could not load Lazard CSV: {exc}")
        return

    # Pre-fetch ATB projections for the four technologies that have ATB data
    print("\n═══ Loading NREL ATB projections… ═══")
    atb_cache: dict[str, dict[int, pd.DataFrame]] = {}
    for key, ecfg in LAZARD_TYPES.items():
        atb_tech = ecfg.get("atb_technology")
        if not atb_tech or atb_tech in atb_cache:
            continue
        print(f"\n  ATB: {atb_tech}")
        atb_cache[atb_tech] = fetch_atb_projections(
            atb_tech, ecfg.get("atb_techdetail")
        )

    for key, ecfg in LAZARD_TYPES.items():
        tech_name = ecfg["technology"]
        print(f"\n── {tech_name} ──")
        df_tech = df[df["technology"] == tech_name].copy()
        if df_tech.empty:
            print(f"  WARNING: no data found for '{tech_name}' — skipping.")
            continue
        df_tech = df_tech.sort_values("year").reset_index(drop=True)
        print(f"  {len(df_tech)} data points "
              f"({int(df_tech['year'].min())}–{int(df_tech['year'].max())})")
        atb_tech = ecfg.get("atb_technology")
        atb_projections = atb_cache.get(atb_tech) if atb_tech else None
        build_lazard_chart(df_tech, ecfg, atb_projections)

    print("\n── All Technologies (combined) ──")
    build_combined_chart(df)

    print(f"\n═══ Done. Outputs in: {OUTPUT_DIR} ═══")


if __name__ == "__main__":
    main()
