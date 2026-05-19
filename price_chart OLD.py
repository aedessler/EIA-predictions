#!/usr/bin/env python3
"""
U.S. EIA Annual Energy Outlook – Energy Price Projections Charts
Uses AEO 2008–2026 vintage reference-case projections vs. actual historical data.

Run: python price_chart.py
Outputs: output/{gas,coal,crude_oil,electricity,nuclear_fuel}_price_projections.png

Prices available in the AEO retrospective dataset:
  - Natural gas delivery to electric power  (real $/Mcf)
  - Steam coal to electric power            (real $/MMBtu)
  - Crude oil import price                  (real $/barrel)
  - Retail electricity end-use price        (real ¢/kWh)
  - Nuclear fuel to electric power          (nominal $/MMBtu, fetched per-vintage from API/ZIP)

Solar and wind have zero fuel cost and are not included here.
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

RETRO_CSV_URL = (
    "https://www.eia.gov/outlooks/aeo/retrospective/csv/dashappdata_allcases.csv"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (energy-projections research project)"}

# ── Per-price-type configuration ───────────────────────────────────────────────

# For types with retro_col set: projections and actuals come from the
# AEO retrospective CSV (REFERENCE case rows for projections, ACTUAL rows
# for historical data). No EIA API key is required for these.
#
# For nuclear_fuel: retro_col is None; projections are fetched per-vintage
# from the EIA API or bulk ZIP files (requires EIA_API_KEY for the API path).

PRICE_TYPES: dict[str, dict] = {
    "gas_price": {
        "title": "U.S. EIA Annual Energy Outlook Projections – Natural Gas Price (Electric Power)",
        "ylabel": "Natural Gas to Electric Power (real $/Mcf)",
        "retro_col": "PRCE_DELV_ELEP_NA_NG_NA_USA_RDLRPMCF",
        "api_series": [
            ("PRCE_DELV_ELEP_NA_NG_NA_USA_RDLRPMCF", 1.0),
            ("PRCE_NA_ELEP_NA_NG_NA_USA_Y13DLRPMCF", 1.0),
        ],
        "bulk_col_substr": "PRCE_DELV_ELEP_NA_NG_NA_USA",
        "bulk_unit_substr": "DLRPMCF",
        "ylim": None,
        "output_stem": "gas_price_projections",
    },
    "coal_price": {
        "title": "U.S. EIA Annual Energy Outlook Projections – Steam Coal Price (Electric Power)",
        "ylabel": "Steam Coal to Electric Power (real $/MMBtu)",
        "retro_col": "PRCE_NOM_ELEP_NA_STC_NA_NA_RDLRPMBTU",
        "api_series": [
            ("PRCE_NOM_ELEP_NA_STC_NA_NA_RDLRPMBTU", 1.0),
        ],
        "bulk_col_substr": "PRCE_NOM_ELEP_NA_STC_NA_NA",
        "bulk_unit_substr": "DLRPMBTU",
        "ylim": None,
        "output_stem": "coal_price_projections",
    },
    "crude_oil": {
        "title": "U.S. EIA Annual Energy Outlook Projections – Crude Oil Import Price",
        "ylabel": "Crude Oil Import Price (real $/barrel)",
        "retro_col": "PRCE_NA_NA_NA_CR_IMCO_USA_RDLRPBRL",
        "api_series": [
            ("PRCE_NA_NA_NA_CR_IMCO_USA_RDLRPBRL", 1.0),
        ],
        "bulk_col_substr": "PRCE_NA_NA_NA_CR_IMCO_USA",
        "bulk_unit_substr": "DLRPBRL",
        "ylim": None,
        "output_stem": "crude_oil_price_projections",
    },
    "electricity": {
        "title": "U.S. EIA Annual Energy Outlook Projections – Retail Electricity Price",
        "ylabel": "Retail Electricity Price (real ¢/kWh)",
        "retro_col": "PRCE_NA_ELEP_NA_EDU_NA_USA_RCNTPKWH",
        "api_series": [
            ("PRCE_NA_ELEP_NA_EDU_NA_USA_RCNTPKWH", 1.0),
            ("PRCE_NA_ELEP_NA_EDU_NA_USA_Y13CNTPKWH", 1.0),
        ],
        "bulk_col_substr": "PRCE_NA_ELEP_NA_EDU_NA_USA",
        "bulk_unit_substr": "CNTPKWH",
        "ylim": None,
        "output_stem": "electricity_price_projections",
    },
    "nuclear_fuel": {
        "title": "U.S. EIA Annual Energy Outlook Projections – Nuclear Fuel Price (Electric Power)",
        "ylabel": "Nuclear Fuel to Electric Power ($/MMBtu)",
        "retro_col": None,  # not in retrospective CSV; fetched per-vintage
        "api_series": [
            ("PRCE_NOM_ELEP_NA_U_NA_NA_NDLRPMBTU", 1.0),
            ("PRCE_REAL_ELEP_NA_U_NA_NA_Y13DLRPMMBTU", 1.0),
        ],
        "bulk_col_substr": "PRCE_NOM_ELEP_NA_U_NA_NA",
        "bulk_unit_substr": "DLRPMBTU",
        "ylim": None,
        "output_stem": "nuclear_fuel_price_projections",
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
    vals = [v for df in projections.values() for v in df["value"].tolist()]
    if not actuals.empty:
        vals.extend(actuals["value"].tolist())
    if not vals:
        return (0, 100)
    mx = max(v for v in vals if pd.notna(v))
    power = 10 ** math.floor(math.log10(mx))
    steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]
    for s in steps:
        candidate = math.ceil(mx / (power * s)) * power * s
        if candidate >= mx * 1.08:
            return (0, candidate)
    return (0, math.ceil(mx * 1.15 / power) * power)


# ── Retrospective CSV helpers ──────────────────────────────────────────────────


def load_retro_csv() -> pd.DataFrame | None:
    try:
        data = download(RETRO_CSV_URL, CACHE_DIR / "aeo_retrospective.csv")
        df = pd.read_csv(io.BytesIO(data), low_memory=False)
        print(f"  Loaded retrospective CSV: {df.shape[0]} rows, {df.shape[1]} columns")
        return df
    except Exception as exc:
        print(f"  Retrospective CSV failed: {exc}")
        return None


def actuals_from_retro_csv(retro_df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Extract actual historical values from ACTUAL/HISTORY rows in the retro CSV."""
    if col not in retro_df.columns:
        return pd.DataFrame(columns=["year", "value"])
    mask = retro_df["case_name"].str.upper().isin(["ACTUAL", "MER", "HISTORY"])
    sub = retro_df[mask][["year", col]].copy()
    sub["year"] = pd.to_numeric(sub["year"], errors="coerce")
    sub["value"] = pd.to_numeric(sub[col], errors="coerce")
    result = (sub[["year", "value"]].dropna()
              .sort_values("year").reset_index(drop=True))
    result["year"] = result["year"].astype(int)
    return result


def projections_from_retro_csv(
    retro_df: pd.DataFrame, col: str
) -> dict[int, pd.DataFrame]:
    """Extract per-vintage REFERENCE-case projections from the retro CSV."""
    if col not in retro_df.columns:
        print(f"  WARNING: column '{col}' not in retrospective CSV.")
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
        sub["year"] = pd.to_numeric(sub["year"], errors="coerce")
        sub["value"] = pd.to_numeric(sub[col], errors="coerce")
        sub = sub.dropna()
        sub = sub[sub["year"] >= vintage]
        if sub.empty:
            continue
        projections[vintage] = (sub[["year", "value"]]
                                .sort_values("year").reset_index(drop=True))
    return projections


# ── Nuclear fuel: per-vintage API / bulk-ZIP fetching ─────────────────────────


def _nuclear_from_api(vintage: int, ecfg: dict) -> pd.DataFrame | None:
    scenario_candidates = [f"ref{vintage}", f"cb{vintage}"]
    for series_id, mult in ecfg["api_series"]:
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
                df["value"] = pd.to_numeric(df["value"], errors="coerce") * mult
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


def _nuclear_from_bulk_zip(vintage: int, ecfg: dict) -> pd.DataFrame | None:
    url = f"https://www.eia.gov/opendata/bulk/AEO{vintage}.zip"
    cache_path = CACHE_DIR / f"AEO{vintage}.zip"
    try:
        data = download(url, cache_path)
    except Exception as exc:
        print(f"    Bulk ZIP unavailable for {vintage}: {exc}")
        return None

    col_substr = ecfg["bulk_col_substr"]
    unit_substr = ecfg["bulk_unit_substr"]
    own_ref_tokens = {f"REF{vintage}", f"CB{vintage}", f"ref{vintage}"}
    exclude_tokens = {f"AEO{vintage - 1}REF", f"REF{vintage - 1}", f"CB{vintage - 1}"}

    def _scan(fh, strict: bool) -> pd.DataFrame | None:
        for line in fh:
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            sid = obj.get("series_id", "")
            sid_up = sid.upper()
            if col_substr not in sid_up:
                continue
            if any(tok in sid_up for tok in exclude_tokens):
                continue
            if strict and not any(tok in sid_up for tok in own_ref_tokens):
                continue
            if not strict and not any(x in sid_up for x in ("REF", "CB")):
                continue
            units = obj.get("units", "")
            if unit_substr.lower() not in (units + sid).lower():
                continue
            raw = obj.get("data", [])
            if not raw:
                continue
            pairs = []
            for item in raw:
                try:
                    pairs.append((int(item[0]), float(item[1])))
                except (ValueError, TypeError, IndexError):
                    continue
            if not pairs:
                continue
            df = pd.DataFrame(pairs, columns=["year", "value"])
            result = (df[df["year"] >= vintage][["year", "value"]]
                      .sort_values("year").reset_index(drop=True))
            if not result.empty:
                print(f"    {len(result)} pts via bulk ZIP (…{sid[-45:]})")
                return result
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for pass_strict in (True, False):
                for fname in zf.namelist():
                    with zf.open(fname) as fh:
                        result = _scan(fh, pass_strict)
                        if result is not None:
                            return result
    except Exception as exc:
        print(f"    Bulk ZIP parse error for {vintage}: {exc}")
    return None


def fetch_nuclear_projections(ecfg: dict) -> dict[int, pd.DataFrame]:
    """Fetch nuclear fuel price projections per vintage (API → bulk ZIP fallback)."""
    projections: dict[int, pd.DataFrame] = {}
    for vintage in AEO_VINTAGES:
        print(f"  AEO {vintage}:")
        df = _nuclear_from_api(vintage, ecfg)
        if df is None or df.empty:
            df = _nuclear_from_bulk_zip(vintage, ecfg)
        if df is not None and not df.empty:
            projections[vintage] = df
        else:
            print(f"    No data found for AEO {vintage}.")
    return projections


def fetch_nuclear_actuals() -> pd.DataFrame:
    """
    Fetch historical nuclear fuel cost to electric power ($/MMBtu) from EIA API.
    EIA Electricity Annual Table 8.4 reports nuclear fuel cost; we try the
    electricity/electric-power-operational-data endpoint for fuel cost data.
    Returns empty DataFrame if unavailable.
    """
    # Nuclear fuel cost is reported as average cents/kWh in EIA MER; convert
    # via the average heat rate for nuclear (≈10,500 BTU/kWh → 10.5 MMBtu/MWh)
    # to get $/MMBtu: cost_dollar_per_mmbtu = cost_cents_per_kwh / 100 / 10.5e-3
    # But EIA also reports nuclear fuel receipts cost in $/MMBtu directly via the
    # electricity/fuel-receipts API (2001+).
    try:
        resp = api_get("electricity/electric-power-operational-data/data/", {
            "frequency": "annual",
            "data[0]": "cost-per-btu",
            "facets[fueltypeid][]": "NUC",
            "facets[location][]": "US",
            "facets[sectorid][]": "99",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 200,
        })
        rows = resp.get("response", {}).get("data", [])
        if rows:
            df = pd.DataFrame(rows)
            df["year"] = pd.to_numeric(
                df["period"].astype(str).str[:4], errors="coerce"
            )
            df["raw"] = pd.to_numeric(df["cost-per-btu"], errors="coerce")
            # cost-per-btu is in $/MMBtu (EIA reports per million BTU)
            annual = (df.groupby("year")["raw"].mean().reset_index()
                      .rename(columns={"raw": "value"}))
            annual = annual.dropna()
            annual["year"] = annual["year"].astype(int)
            if not annual.empty:
                print(f"    Nuclear fuel actuals: {len(annual)} rows via elec API, "
                      f"latest = {annual['value'].iloc[-1]:.3f} $/MMBtu")
                return annual.sort_values("year").reset_index(drop=True)
    except Exception as exc:
        print(f"    Nuclear fuel actuals API failed: {exc}")

    # Fallback: MER total-energy API for nuclear fuel expenditure
    try:
        resp = api_get("total-energy/data/", {
            "frequency": "annual",
            "data[0]": "value",
            "facets[msn][]": "NUETPUS",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 100,
        })
        rows = resp.get("response", {}).get("data", [])
        if rows:
            df = pd.DataFrame(rows)
            unit = df["unit"].iloc[0] if "unit" in df.columns else ""
            print(f"    NUETPUS unit: '{unit}' (quantity, not price — skipping)")
    except Exception:
        pass

    print("    No nuclear fuel price actuals available.")
    return pd.DataFrame(columns=["year", "value"])


# ── Plotting ───────────────────────────────────────────────────────────────────


def _nudge_by_group(
    label_entries: list[tuple], min_gap: float = 0.0,
    y_min: float = 0.0, y_max: float = 1e9
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
    ax.tick_params(length=0, labelsize=12)

    ylim = ecfg["ylim"]
    if ylim is None:
        ylim = _nice_ylim(projections, actuals)
    y_min_label = ylim[0] + ylim[1] * 0.01
    y_max_label = ylim[1] * 0.99

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

    adj_y = _nudge_by_group(
        label_entries, min_gap=ylim[1] * 0.016,
        y_min=y_min_label, y_max=y_max_label
    )
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

    y_range = ylim[1] - ylim[0]
    y_step = max(1, round(y_range / 8))
    for nice in [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500]:
        if nice >= y_step:
            y_step = nice
            break
    ax.set_yticks(np.arange(ylim[0], ylim[1] + y_step * 0.5, y_step))

    ax.set_xlabel("Year", fontsize=13, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_ylabel(ecfg["ylabel"], fontsize=13, labelpad=8, fontfamily="DejaVu Sans")
    ax.set_title(ecfg["title"], fontsize=16, fontweight="bold",
                 fontfamily="DejaVu Sans", pad=14)

    latest_vintage = max(projections.keys()) if projections else 2026
    missing_vv = [v for v in AEO_VINTAGES if v not in projections]
    missing_note = (
        f" (AEO {', '.join(str(v) for v in missing_vv)} not publicly available)"
        if missing_vv else ""
    )
    latest_actual = int(actuals["year"].max()) if not actuals.empty else "N/A"
    fig.text(
        0.01, 0.01,
        (f"Source: U.S. EIA Annual Energy Outlook (AEO 2008–{latest_vintage})"
         f"{missing_note}; EIA Monthly Energy Review. Actuals through {latest_actual}."),
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


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("═══ Fetching EIA AEO Retrospective CSV… ═══")
    retro_df = load_retro_csv()

    for price_key, ecfg in PRICE_TYPES.items():
        print(f"\n{'═'*55}")
        print(f"  {price_key.upper()} PRICE PROJECTIONS")
        print(f"{'═'*55}")

        col = ecfg["retro_col"]

        if col is not None:
            # ── Types present in the retrospective CSV ─────────────────────────
            if retro_df is None:
                print("  ERROR: No retrospective CSV — skipping.")
                continue

            print("\n[1] Actuals from retrospective CSV…")
            actuals = actuals_from_retro_csv(retro_df, col)
            if not actuals.empty:
                print(f"  {len(actuals)} actual rows "
                      f"({int(actuals['year'].min())}–{int(actuals['year'].max())}), "
                      f"latest = {actuals['value'].iloc[-1]:.3f}")
            else:
                print("  WARNING: no actual rows found.")

            print("\n[2] AEO projections from retrospective CSV…")
            projections = projections_from_retro_csv(retro_df, col)
            found = sorted(projections.keys())
            print(f"  Vintages: {found}")

        else:
            # ── Nuclear fuel: per-vintage API / ZIP ────────────────────────────
            print("\n[1] Actuals for nuclear fuel price…")
            actuals = fetch_nuclear_actuals()

            print("\n[2] AEO projections for nuclear fuel price…")
            projections = fetch_nuclear_projections(ecfg)
            found = sorted(projections.keys())
            print(f"  Vintages with data: {found}")

        if not projections:
            print(f"  ERROR: No projection data — skipping {price_key} chart.")
            continue

        missing = [v for v in AEO_VINTAGES if v not in projections]
        if missing:
            print(f"  Missing vintages: {missing}")

        print("\n[3] Generating chart…")
        build_chart(projections, actuals, ecfg)

    print("\n═══ Done. Outputs in:", OUTPUT_DIR, "═══")


if __name__ == "__main__":
    main()
