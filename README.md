# EIA Annual Energy Outlook Projections – Coal, Wind & Solar

Reproduces and extends the RethinkX "EIA AEO Projections for Coal" chart style, updated through AEO 2026, and applies the same treatment to wind and solar. Each chart overlays one thin colored line per AEO vintage (reference case) against a thick black line of actual historical data, illustrating how EIA's forecasts have evolved over time.

---

## Program

**`coal_chart.py`** — the single script that fetches data, processes it, and writes all three charts.

Run with:
```bash
python coal_chart.py
```

Output PNGs (300 DPI) are written to `output/`:
- `output/coal_projections.png`
- `output/wind_projections.png`
- `output/solar_projections.png`

Downloaded data is cached in `cache/` and reused on subsequent runs. To force a re-download, delete the relevant file from `cache/`.

The script is parameterized by an `ENERGY_TYPES` dict near the top of the file. Adding a new energy series requires only a new entry in that dict with the appropriate EIA column name and API series ID.

---

## Data Sources

### AEO Vintage Projections

All three energy types draw vintage projections from the same primary source, with fallbacks for any missing vintages.

**Primary — EIA AEO Retrospective CSV**
```
https://www.eia.gov/outlooks/aeo/retrospective/csv/dashappdata_allcases.csv
```
A single CSV maintained by EIA that contains reference-case projections from every AEO edition alongside actual historical values. It covers AEO vintages from roughly 2005 onward. This is the workhorse of the data pipeline: one download provides projections for all three energy types across most vintages.

Relevant columns:
| Column | Description |
|--------|-------------|
| `CNSM_NA_NA_NA_CL_NA_NA_MILLTON` | Total coal consumption, all sectors (million short tons) |
| `GEN_NA_ALLS_NA_WND_NA_NA_BLNKWH` | Wind electricity generation, all sectors (billion kWh) |
| `GEN_NA_ALLS_NA_SLR_NA_NA_BLNKWH` | Solar electricity generation, all sectors (billion kWh) |

**Fallback — EIA API v2**
```
https://api.eia.gov/v2/aeo/{vintage_year}/data/
```
Used for any vintage missing from the retrospective CSV. Scenario facet is `ref{year}` for most vintages; AEO 2026 uses `cb2026` ("Current Baseline 2026"). Requires a free API key set as the environment variable `EIA_API_KEY`.

**Tertiary fallback — EIA bulk ZIP files**
```
https://www.eia.gov/opendata/bulk/AEO{year}.zip
```
Newline-delimited JSON bundles available for most vintages. No API key required.

### Historical Actuals

**Coal** — EIA Total Energy API, MSN code `CLTCPUS` (total U.S. coal consumption, Thousand Short Tons):
```
https://api.eia.gov/v2/total-energy/data/
```
Fallback: EIA Monthly Energy Review Table 1.3 (`https://www.eia.gov/totalenergy/data/browser/csv.php?tbl=T01.03&freq=a`).

**Wind and Solar** — extracted directly from the retrospective CSV's `ACTUAL` rows (same file as the projections). Wind actuals run from ~1983 through 2024; solar from ~2005 through 2024.

For manual cross-checking, AEO supplement tables are published at:
```
https://www.eia.gov/outlooks/aeo/tables_ref.php
```
Relevant tables: Table 2 (Energy Consumption by Sector and Source) and Table 15 (Coal Supply, Disposition, and Price).

---

## Metrics

| Chart | Metric | Unit |
|-------|--------|------|
| Coal | Total U.S. coal consumption, all sectors | Quadrillion BTU (quads) |
| Wind | U.S. wind electricity generation, all sectors | Billion kWh |
| Solar | U.S. solar electricity generation, all sectors (utility-scale + distributed) | Billion kWh |

Coal is **consumption**, not production or electricity generation specifically — it includes electric power, industrial, residential/commercial, and coke plant uses.

---

## Unit Conversions

**Million short tons → quads (coal only)**
```
1 million short ton × 0.02009 = 1 quadrillion BTU
```
Based on EIA's weighted-average heat content for U.S. coal (bituminous, subbituminous, and lignite blend). Calibration: the ~1,100 million short ton peak in 2007 converts to ~22.1 quads, consistent with historical records.
Reference: https://www.eia.gov/tools/faqs/faq.php?id=72&t=2

**Trillion Btu → quads**: divide by 1,000 (1 quad = 1 quadrillion Btu = 1,000 trillion Btu).

Wind and solar data are already in billion kWh throughout and require no conversion.

---

## Caveats

**AEO 2024 is unavailable.** EIA's API v2, retrospective CSV, and bulk file archive all skip from 2023 to 2025. No data for AEO 2024 was found through any EIA channel, consistent with reports of reduced EIA output capacity that year. All three charts cover AEO 2008–2023 and 2025–2026, with 2024 explicitly absent.

**AEO 2026 uses a different scenario name.** EIA restructured AEO 2026 around a "Current Baseline" scenario labeled `CB2026` rather than the traditional `REF2026`. The script detects this automatically. The prior-year reference (`AEO2025REF`), bundled inside the 2026 data for comparison, is excluded.

**Coal sector definitions shifted across vintages.** AEO 2008–2013 may aggregate coal slightly differently (e.g., treatment of coke breeze or coal-to-liquids) compared to later editions. Discrepancies are estimated at less than 1 quad.

**AEO 2020** was a preliminary release delayed by COVID-19 and may show a minor discontinuity relative to adjacent vintages.

**IRA inflection in recent AEO editions.** The Inflation Reduction Act (2022) clean energy incentives are more fully incorporated in AEO 2023 and later, which accounts for the sharp downward revision in coal projections (from ~3.3 quads by 2050 in AEO 2023 to ~0.5 quads in AEO 2025/2026) and correspondingly larger upward revisions in wind and solar projections.

**Each vintage line starts at its publication year.** Historical data included in each AEO publication is excluded from the projection lines; only forward-looking values are plotted.
