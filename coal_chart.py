#!/usr/bin/env python3
"""
U.S. EIA Annual Energy Outlook – Coal, Wind & Solar Projections Charts
Uses AEO 2008–2026 vintage reference-case projections vs. actual historical data.

Run: python coal_chart.py
Outputs: output/{coal,wind,solar}_projections.{png,svg,pdf}
"""

import io
import json
import math
import os
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

# ── Global config ──────────────────────────────────────────────────────────────

EIA_API_KEY = os.environ.get("EIA_API_KEY", "put your key here")
CACHE_DIR = Path("cache")
OUTPUT_DIR = Path("output")
AEO_VINTAGES = list(range(2008, 2027))  # 2008–2026 inclusive

# EIA standard heat content: ~20.09 MMBtu/short ton (weighted average all coal types)
MST_TO_QUAD = 0.02009  # million short tons → quadrillion BTU

RETRO_CSV_URL = (
    "https://www.eia.gov/outlooks/aeo/retrospective/csv/dashappdata_allcases.csv"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (energy-projections research project)"}

# ── Per-energy-type configuration ──────────────────────────────────────────────

ENERGY_TYPES: dict[str, dict] = {
    "coal": {
        "title": "U.S. EIA Annual Energy Outlook Projections for Coal",
        "ylabel": "Coal Consumption (quads)",
        "retro_col": "CNSM_NA_NA_NA_CL_NA_NA_MILLTON",
        "retro_multiplier": MST_TO_QUAD,   # million short tons → quads
        "api_series": [                     # (series_id, multiplier) tried in order
            ("cnsm_NA_NA_NA_cl_NA_NA_millton", MST_TO_QUAD),
            ("CNSM_NA_NA_NA_CL_NA_NA_QBTU", 1.0),
            ("CNSM_NA_NA_NA_CL_NA_NA_MILLTON", MST_TO_QUAD),
        ],
        "api_scenario_prefix": "ref",       # ref2024, cb2026, etc.
        "bulk_col_substr": "CNSM_NA_NA_NA_CL_NA_NA",
        "bulk_unit_is_energy": True,        # detect Quad/MILLTON in bulk ZIP
        "ylim": (0, 40),
        "output_stem": "coal_projections",
        "use_dedicated_actuals": True,      # fetch actuals from total-energy API
    },
    "wind": {
        "title": "U.S. EIA Annual Energy Outlook Projections for Wind",
        "ylabel": "Wind Generation (billion kWh)",
        "retro_col": "GEN_NA_ALLS_NA_WND_NA_NA_BLNKWH",
        "retro_multiplier": 1.0,            # already in billion kWh
        "api_series": [
            ("gen_NA_alls_NA_wnd_NA_NA_blnkwh", 1.0),
            ("GEN_NA_ALLS_NA_WND_NA_NA_BLNKWH", 1.0),
        ],
        "api_scenario_prefix": "ref",
        "bulk_col_substr": "GEN_NA_ALLS_NA_WND_NA_NA",
        "bulk_unit_is_energy": False,       # billion kWh
        "ylim": None,                       # auto-scale from data
        "output_stem": "wind_projections",
        "use_dedicated_actuals": False,
    },
    "solar": {
        "title": "U.S. EIA Annual Energy Outlook Projections for Solar",
        "ylabel": "Solar Generation (billion kWh)",
        "retro_col": "GEN_NA_ALLS_NA_SLR_NA_NA_BLNKWH",
        "retro_multiplier": 1.0,
        "api_series": [
            ("gen_NA_alls_NA_slr_NA_NA_blnkwh", 1.0),
            ("GEN_NA_ALLS_NA_SLR_NA_NA_BLNKWH", 1.0),
        ],
        "api_scenario_prefix": "ref",
        "bulk_col_substr": "GEN_NA_ALLS_NA_SLR_NA_NA",
        "bulk_unit_is_energy": False,
        "ylim": None,
        "output_stem": "solar_projections",
        "use_dedicated_actuals": False,
    },
}

# ── Utilities ──────────────────────────────────────────────────────────────────


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


def api_get(endpoint: str, params: dict) -> dict:
    url = f"https://api.eia.gov/v2/{endpoint}"
    params = {"api_key": EIA_API_KEY, "out": "json", **params}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _nice_ylim(projections: dict, actuals: pd.DataFrame) -> tuple[float, float]:
    """Auto-compute a clean y-axis upper limit from data."""
    vals = [v for df in projections.values() for v in df["value"].tolist()]
    if not actuals.empty:
        vals.extend(actuals["value"].tolist())
    if not vals:
        return (0, 100)
    mx = max(v for v in vals if pd.notna(v))
    # Round up to nearest 'nice' increment
    power = 10 ** math.floor(math.log10(mx))
    steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]
    for s in steps:
        candidate = math.ceil(mx / (power * s)) * power * s
        if candidate >= mx * 1.08:
            return (0, candidate)
    return (0, math.ceil(mx * 1.15 / power) * power)


# ── Actual historical data ─────────────────────────────────────────────────────


def _actuals_from_total_energy_api() -> pd.DataFrame:
    """Coal actuals: EIA Total Energy API, MSN CLTCPUS (Thousand Short Tons)."""
    resp = api_get("total-energy/data/", {
        "frequency": "annual",
        "data[0]": "value",
        "facets[msn][]": "CLTCPUS",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": 100,
    })
    rows = resp.get("response", {}).get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    unit = df["unit"].iloc[0] if "unit" in df.columns else ""
    print(f"    CLTCPUS unit: '{unit}'")
    df["year"] = pd.to_numeric(df["period"], errors="coerce")
    df["raw"] = pd.to_numeric(df["value"], errors="coerce")
    if "Thousand Short Ton" in unit:
        df["value"] = df["raw"] * MST_TO_QUAD / 1000
    elif "Trillion Btu" in unit:
        df["value"] = df["raw"] / 1000
    elif "Quadrillion Btu" in unit or "Quad Btu" in unit:
        df["value"] = df["raw"]
    else:
        print(f"    Unknown unit '{unit}', dividing by 1000")
        df["value"] = df["raw"] / 1000
    result = df[["year", "value"]].dropna().sort_values("year").reset_index(drop=True)
    result["year"] = result["year"].astype(int)
    return result


def _actuals_from_mer_t0103() -> pd.DataFrame:
    """Coal actuals fallback: MER Table 1.3."""
    url = "https://www.eia.gov/totalenergy/data/browser/csv.php?tbl=T01.03&freq=a"
    data = download(url, CACHE_DIR / "mer_t0103.csv")
    df = pd.read_csv(io.BytesIO(data), low_memory=False)
    if "MSN" not in df.columns:
        return pd.DataFrame()
    for msn in ("CLTCPUS", "CLACPUS"):
        sub = df[df["MSN"] == msn].copy()
        if sub.empty:
            continue
        sub = sub[sub["YYYYMM"].astype(str).str.endswith("13")]
        sub["year"] = sub["YYYYMM"].astype(str).str[:4].astype(int)
        sub["raw"] = pd.to_numeric(sub["Value"], errors="coerce")
        unit = sub["Unit"].iloc[0] if "Unit" in sub.columns else ""
        sub["value"] = sub["raw"] / 1000 if "Trillion" in unit else sub["raw"]
        result = sub[["year", "value"]].dropna().sort_values("year").reset_index(drop=True)
        if not result.empty:
            return result
    return pd.DataFrame()


def _actuals_from_retro_csv(retro_df: pd.DataFrame, ecfg: dict) -> pd.DataFrame:
    """Wind/solar actuals: ACTUAL rows from the retrospective CSV."""
    col = ecfg["retro_col"]
    mult = ecfg["retro_multiplier"]
    if col not in retro_df.columns:
        return pd.DataFrame()
    mask = retro_df["case_name"].str.upper().isin(["ACTUAL", "MER", "HISTORY"])
    sub = retro_df[mask][["year", col]].copy()
    sub = sub.rename(columns={col: "raw"})
    sub["year"] = pd.to_numeric(sub["year"], errors="coerce")
    sub["raw"] = pd.to_numeric(sub["raw"], errors="coerce")
    sub = sub.dropna()
    sub["value"] = sub["raw"] * mult
    return sub[["year", "value"]].sort_values("year").reset_index(drop=True)


def fetch_actuals(ecfg: dict, retro_df: pd.DataFrame | None) -> pd.DataFrame:
    """Fetch historical actuals for the given energy type."""
    if ecfg["use_dedicated_actuals"]:
        # Coal: use total energy API → MER fallback
        print("  Trying Total Energy API (CLTCPUS)…")
        try:
            result = _actuals_from_total_energy_api()
            if not result.empty:
                peak = result["value"].max()
                print(f"    {len(result)} rows, peak = {peak:.2f} quads")
                if 12 < peak < 35:
                    return result
                print(f"    Peak {peak:.1f} outside expected 12–35, trying MER fallback.")
        except Exception as exc:
            print(f"    Total Energy API failed: {exc}")
        print("  Trying MER Table 1.3…")
        try:
            return _actuals_from_mer_t0103()
        except Exception as exc:
            print(f"    MER fallback failed: {exc}")
        return pd.DataFrame(columns=["year", "value"])
    else:
        # Wind/Solar: use retrospective CSV ACTUAL rows
        if retro_df is None:
            print("  WARNING: no retrospective CSV for actuals.")
            return pd.DataFrame(columns=["year", "value"])
        result = _actuals_from_retro_csv(retro_df, ecfg)
        if not result.empty:
            print(f"  {len(result)} actual rows from retrospective CSV "
                  f"({int(result['year'].min())}–{int(result['year'].max())}), "
                  f"latest = {result['value'].iloc[-1]:.1f} billion kWh")
        return result


# ── AEO Projections ────────────────────────────────────────────────────────────


def _aeo_from_api(vintage: int, ecfg: dict) -> pd.DataFrame | None:
    """EIA API v2 for one AEO vintage, reference case."""
    scenario_candidates = [f"ref{vintage}", f"cb{vintage}"]
    for series_id, multiplier in ecfg["api_series"]:
        for scenario in scenario_candidates:
            try:
                resp = api_get(f"aeo/{vintage}/data/", {
                    "frequency": "annual",
                    "data[0]": "value",
                    "facets[scenario][]": scenario,
                    "facets[seriesId][]": series_id,
                    "sort[0][column]": "period",
                    "sort[0][direction]": "asc",
                    "length": 100,
                })
                rows = resp.get("response", {}).get("data", [])
                if not rows:
                    continue
                df = pd.DataFrame(rows)
                df["year"] = pd.to_numeric(df["period"], errors="coerce").astype("Int64")
                df["value"] = pd.to_numeric(df["value"], errors="coerce") * multiplier
                result = df[["year", "value"]].dropna()
                result["year"] = result["year"].astype(int)
                result = (result[result["year"] >= vintage]
                          .sort_values("year").reset_index(drop=True))
                if not result.empty:
                    print(f"    {len(result)} pts via API ({series_id}, {scenario})")
                    return result
            except Exception:
                pass
    return None


def _aeo_from_bulk_zip(vintage: int, ecfg: dict) -> pd.DataFrame | None:
    """Parse EIA bulk ZIP for one vintage."""
    url = f"https://www.eia.gov/opendata/bulk/AEO{vintage}.zip"
    cache_path = CACHE_DIR / f"AEO{vintage}.zip"
    try:
        data = download(url, cache_path)
    except Exception as exc:
        print(f"    Bulk ZIP unavailable for {vintage}: {exc}")
        return None

    col_substr = ecfg["bulk_col_substr"]
    is_energy = ecfg["bulk_unit_is_energy"]   # True = Quad/MILLTON, False = billion kWh
    mult = ecfg["retro_multiplier"]

    own_ref_tokens = {f"REF{vintage}", f"CB{vintage}", f"ref{vintage}"}
    exclude_tokens  = {f"AEO{vintage - 1}REF", f"REF{vintage - 1}", f"CB{vintage - 1}"}

    def _to_df(obj: dict, sid: str) -> pd.DataFrame | None:
        units = obj.get("units", "")
        raw = obj.get("data", [])
        if not raw:
            return None
        pairs = []
        for item in raw:
            try:
                pairs.append((int(item[0]), float(item[1])))
            except (ValueError, TypeError, IndexError):
                continue
        if not pairs:
            return None
        df = pd.DataFrame(pairs, columns=["year", "v"])
        if is_energy:
            if "Quad" in units or "QBTU" in sid.upper():
                df["value"] = df["v"]
            elif "MMst" in units or "MILLTON" in sid.upper() or "Short Ton" in units:
                df["value"] = df["v"] * mult
            else:
                return None
        else:
            # electricity generation in billion kWh (or similar)
            if "Billion kWh" in units or "BLNKWH" in sid.upper() or "kilowatthour" in units.lower():
                df["value"] = df["v"] * mult
            else:
                return None
        result = (df[df["year"] >= vintage][["year", "value"]]
                  .sort_values("year").reset_index(drop=True))
        return result if not result.empty else None

    def _scan(fh, strict: bool) -> pd.DataFrame | None:
        for line in fh:
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            sid = obj.get("series_id", "")
            if col_substr not in sid:
                continue
            sid_up = sid.upper()
            if any(tok in sid_up for tok in exclude_tokens):
                continue
            if strict:
                if not any(tok in sid_up for tok in own_ref_tokens):
                    continue
            else:
                if not any(x in sid_up for x in ("REF", "CB")):
                    continue
            result = _to_df(obj, sid)
            if result is not None:
                print(f"    {len(result)} pts via bulk ZIP (…{sid[-45:]})")
                return result
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            for pass_strict in (True, False):
                for fname in names:
                    with zf.open(fname) as fh:
                        result = _scan(fh, strict=pass_strict)
                        if result is not None:
                            return result
    except Exception as exc:
        print(f"    Bulk ZIP parse error for {vintage}: {exc}")
    return None


def _parse_retro_csv(retro_df: pd.DataFrame, ecfg: dict) -> dict[int, pd.DataFrame]:
    """Extract per-vintage reference-case projections from the retrospective CSV."""
    col = ecfg["retro_col"]
    mult = ecfg["retro_multiplier"]
    if col not in retro_df.columns:
        print(f"  WARNING: column '{col}' not found in retrospective CSV.")
        return {}

    legacy_map = {f"REF{y}": "REFERENCE" for y in range(2005, 2027)}
    legacy_map.update({f"ref{y}": "REFERENCE" for y in range(2005, 2027)})

    df = retro_df.copy()
    df["case_name"] = df["case_name"].replace(legacy_map)

    projections: dict[int, pd.DataFrame] = {}
    for edition_raw in df["edition"].unique():
        try:
            vintage = int(str(edition_raw).split("-")[0].split("_")[0])
        except (ValueError, AttributeError):
            continue
        if vintage not in AEO_VINTAGES:
            continue

        mask = (df["edition"].astype(str).str.startswith(str(vintage))
                & (df["case_name"] == "REFERENCE"))
        sub = df[mask][["year", col]].copy()
        sub = sub.rename(columns={col: "raw"})
        sub["year"] = pd.to_numeric(sub["year"], errors="coerce")
        sub["raw"] = pd.to_numeric(sub["raw"], errors="coerce")
        sub = sub.dropna()
        sub = sub[sub["year"] >= vintage]
        if sub.empty:
            continue
        sub["value"] = sub["raw"] * mult
        projections[vintage] = (sub[["year", "value"]]
                                .sort_values("year").reset_index(drop=True))
    return projections


def fetch_projections(retro_df: pd.DataFrame | None, ecfg: dict) -> dict[int, pd.DataFrame]:
    """Build the full projections dict for one energy type."""
    projections: dict[int, pd.DataFrame] = {}

    if retro_df is not None:
        try:
            retro_proj = _parse_retro_csv(retro_df, ecfg)
            found = sorted(retro_proj.keys())
            print(f"  Retrospective CSV yielded vintages: {found}")
            projections.update(retro_proj)
        except Exception as exc:
            print(f"  Retrospective CSV parse failed: {exc}")

    missing = [v for v in AEO_VINTAGES if v not in projections]
    if missing:
        print(f"  Still missing: {missing}. Fetching individually…")
        for vintage in missing:
            print(f"  AEO {vintage}:")
            df = _aeo_from_api(vintage, ecfg)
            if df is None or df.empty:
                df = _aeo_from_bulk_zip(vintage, ecfg)
            if df is not None and not df.empty:
                projections[vintage] = df
            else:
                print(f"    WARNING: no data found for AEO {vintage}.")

    return projections


# ── Plotting ───────────────────────────────────────────────────────────────────


def _nudge_by_group(label_entries: list[tuple], min_gap: float = 0.0,
                    y_min: float = 0.0, y_max: float = 1e9) -> dict[int, float]:
    """
    Nudge label y-values only within clusters that share the same x (end year).
    Returns {index: adjusted_y}.
    """
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


def build_chart(
    projections: dict[int, pd.DataFrame],
    actuals: pd.DataFrame,
    ecfg: dict,
) -> None:
    BG = "#f0f0f0"
    fig, ax = plt.subplots(figsize=(13.5, 7.8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.grid(True, color="white", linewidth=1.0, linestyle="-", zorder=0)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0, labelsize=9)

    # Determine y-axis limits before drawing
    ylim = ecfg["ylim"]
    if ylim is None:
        ylim = _nice_ylim(projections, actuals)
    y_min_label, y_max_label = ylim[0] + ylim[1] * 0.01, ylim[1] * 0.99

    # Colormap: plasma 0.05–0.78 avoids pale yellow; oldest=deep violet, newest=warm orange
    vintages = sorted(projections.keys())
    n = len(vintages)
    cmap = matplotlib.colormaps["plasma"]
    colors = {v: cmap(0.05 + i / max(n - 1, 1) * 0.73) for i, v in enumerate(vintages)}

    label_entries: list[tuple] = []

    for vintage in vintages:
        vdf = projections[vintage].sort_values("year")
        if vdf.empty:
            continue
        c = colors[vintage]
        ax.plot(vdf["year"], vdf["value"], color=c, linewidth=1.1, alpha=0.85, zorder=2)
        last = vdf.iloc[-1]
        label_entries.append((float(last["year"]), float(last["value"]), str(vintage), c))

    if not actuals.empty:
        act = actuals.sort_values("year")
        ax.plot(
            act["year"], act["value"],
            color="black", linewidth=2.6,
            marker="o", markersize=4.5,
            markerfacecolor="black", markeredgewidth=0,
            zorder=6, solid_capstyle="round",
        )
        last_act = act.iloc[-1]
        label_entries.append(
            (float(last_act["year"]), float(last_act["value"]), "Actual", "black")
        )

    # Place labels inline at line tips
    adj_y = _nudge_by_group(label_entries, min_gap=ylim[1] * 0.016,
                            y_min=y_min_label, y_max=y_max_label)
    X_OFFSET = 0.6
    for i, (orig_x, orig_y, text, color) in enumerate(label_entries):
        is_actual = text == "Actual"
        txt = ax.text(
            orig_x + X_OFFSET, adj_y[i], text,
            fontsize=10 if not is_actual else 11.5,
            fontweight="bold",
            color=color,
            va="center", ha="left",
            fontfamily="DejaVu Sans",
            clip_on=False,
        )
        txt.set_path_effects([pe.withStroke(linewidth=3, foreground="white")])

    ax.set_xlim(2000, 2053)
    ax.set_ylim(*ylim)
    ax.set_xticks(range(2000, 2055, 5))
    # Y-ticks: pick a sensible step
    y_range = ylim[1] - ylim[0]
    y_step = max(1, round(y_range / 8))
    # round y_step to a "nice" number
    for nice in [1, 2, 5, 10, 25, 50, 100, 200, 250, 500, 1000]:
        if nice >= y_step:
            y_step = nice
            break
    ax.set_yticks(np.arange(ylim[0], ylim[1] + y_step * 0.5, y_step))

    ax.set_xlabel("Year", fontsize=10.5, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_ylabel(ecfg["ylabel"], fontsize=10.5, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_title(ecfg["title"], fontsize=14.5, fontweight="bold",
                 fontfamily="DejaVu Sans", pad=14)

    latest_vintage = max(projections.keys()) if projections else 2026
    missing_vv = [v for v in AEO_VINTAGES if v not in projections]
    missing_note = (f" (AEO {', '.join(str(v) for v in missing_vv)} not publicly available)"
                    if missing_vv else "")
    latest_actual = int(actuals["year"].max()) if not actuals.empty else "N/A"
    fig.text(
        0.01, 0.01,
        (f"Source: U.S. EIA Annual Energy Outlook (AEO 2008–{latest_vintage}){missing_note}; "
         f"EIA Monthly Energy Review. Actuals through {latest_actual}."),
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


# ── Validation ─────────────────────────────────────────────────────────────────


def validate(projections: dict, actuals: pd.DataFrame, label: str) -> None:
    print(f"\n── Validation [{label}] ──")
    if not actuals.empty:
        peak_val = actuals["value"].max()
        peak_yr  = int(actuals.loc[actuals["value"].idxmax(), "year"])
        latest_yr  = int(actuals["year"].max())
        latest_val = float(actuals.loc[actuals["year"] == latest_yr, "value"].values[0])
        print(f"  Actuals peak  : {peak_val:.2f} in {peak_yr}")
        print(f"  Latest actual : {latest_val:.2f} in {latest_yr}")
    available = sorted(projections.keys())
    missing   = [v for v in AEO_VINTAGES if v not in projections]
    print(f"  Vintages found : {available}")
    if missing:
        print(f"  Missing        : {missing}")
    else:
        print(f"  All {len(AEO_VINTAGES)} vintages present ✓")


# ── Main ────────────────────────────────────────────────────────────────────────


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("═══ Fetching EIA AEO Retrospective CSV… ═══")
    retro_df: pd.DataFrame | None = None
    try:
        data = download(RETRO_CSV_URL, CACHE_DIR / "aeo_retrospective.csv")
        retro_df = pd.read_csv(io.BytesIO(data), low_memory=False)
        print(f"  Loaded: {retro_df.shape[0]} rows, {retro_df.shape[1]} columns")
    except Exception as exc:
        print(f"  Retrospective CSV failed: {exc}")

    for energy_key, ecfg in ENERGY_TYPES.items():
        print(f"\n{'═'*55}")
        print(f"  {energy_key.upper()} PROJECTIONS")
        print(f"{'═'*55}")

        print(f"\n[1] Actuals…")
        actuals = fetch_actuals(ecfg, retro_df)

        print(f"\n[2] AEO projections…")
        projections = fetch_projections(retro_df, ecfg)

        validate(projections, actuals, energy_key)

        if not projections:
            print(f"  ERROR: No projection data — skipping {energy_key} chart.")
            continue

        print(f"\n[3] Generating chart…")
        build_chart(projections, actuals, ecfg)

    print("\n═══ Done. Outputs in:", OUTPUT_DIR, "═══")


if __name__ == "__main__":
    main()
