# Input Data

This folder contains all input data required to run the model. Most files are included directly in this repository. Two sets of large files must be downloaded separately.

## Files Included in This Repository

| File | Size | Description |
|------|------|-------------|
| `cost_assumptions.xlsx` | 16 KB | Technology cost assumptions for 2025, 2028, and 2030 (NREL ATB-based) |
| `county_ng_prices.nc` | 592 KB | County-level industrial natural gas prices ($/Mcf) from HIFLD utility data |
| `county_ng_prices_final.csv` | 380 KB | CSV version of county NG prices |
| `monthly_natural_gas_prices_dollars_per_thousandCubicFeet.csv` | 76 KB | State-level monthly NG prices 2001–2024 (EIA) |
| `demand.csv` | 152 KB | Example hourly demand profile (optional; model defaults to fixed capacity) |
| `county_electricity_prices_2024.nc` | 904 KB | County-level utility electricity prices, 2024 (EIA Form EIA-861M) |
| `county_to_ba_mapping.csv` | 88 KB | County FIPS to NREL balancing area mapping |
| `county_to_ba_mapping.nc` | 904 KB | NetCDF version of county–balancing area mapping |
| `pca_ng_prices_monthly_2024.nc` | 40 KB | Balancing area-level monthly NG prices, 2024 |
| `county_fips_lookup.csv` | 196 KB | County FIPS code to name, state, and centroid location lookup |

## Files to Download

The following large files are hosted separately. Download them and place them in this `input_data/` folder.

### County Renewable Capacity Factors (~433 MB)

**File:** `all_counties_capacity_factors.nc`

**Download:** [DATA REPOSITORY LINK]

Hourly solar PV and wind capacity factors (0–1 fraction) for all US counties at 8,760-hour resolution, computed from ERA5 reanalysis data (2019) using the `atlite` library.

### NREL Cambium Emissions Data (~850 MB total, 9 files)

**Files:**
```
cambium_2025_MidCase.nc
cambium_2025_LowRE.nc
cambium_2025_HighRE.nc
cambium_2030_MidCase.nc
cambium_2030_LowRE.nc
cambium_2030_HighRE.nc
cambium_2035_MidCase.nc
cambium_2035_LowRE.nc
cambium_2035_HighRE.nc
```

**Download:** [DATA REPOSITORY LINK]

Hourly long-run marginal emissions rates (LRMER, kg CO2e/MWh) by NREL balancing area, preprocessed from [NREL Cambium 2023](https://www.nrel.gov/analysis/cambium.html) scenario outputs.

> **Note:** These files are only required when `enable_grid = True` in the model. If you are running without grid electricity purchases, you can skip downloading the Cambium files.
