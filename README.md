# Data Center Capacity Optimization Model

This repository contains the optimization model accompanying the paper:

> **[Paper Title]**
> [Authors], [Journal], [Year]

## Overview

`data_center_capacity_model_6_16_2026.py` is a single-county data center energy supply optimization model. It uses mixed-integer linear programming (MILP) to determine the least-cost combination of electricity supply sources for a data center at any US county.

**Technologies modeled:**
- Renewables: Solar PV, Wind
- Thermal (dispatchable): Natural gas combined cycle (NGCC), NGCC with carbon capture (NGCC-CCS), natural gas combustion turbine (NGCT), Coal, Coal with CCS, Nuclear, Small Modular Reactor (SMR, 2030 only)
- Storage: Utility-scale battery (4-hour duration)
- Grid: Direct utility electricity purchases

The model optimizes capacity (kW) and hourly dispatch across all 8,760 hours of a year, minimizing total annualized cost (capital + fixed O&M + fuel + variable O&M + grid electricity).

## Repository Structure

```
data_center_capacity_github/
├── data_center_capacity_model_6_16_2026.py   # Main optimization model
├── README.md
├── requirements.txt
├── input_data/                               # All input data (see input_data/README.md)
│   ├── cost_assumptions.xlsx                 # Technology cost assumptions (2025/2028/2030)
│   ├── county_ng_prices.nc                   # County-level natural gas prices
│   ├── county_ng_prices_final.csv            # CSV version of county NG prices
│   ├── monthly_natural_gas_prices_*.csv      # State-level historical NG prices
│   ├── demand.csv                            # Example hourly demand profile
│   ├── county_electricity_prices_2024.nc     # County utility electricity prices
│   ├── county_to_ba_mapping.csv             # County FIPS to balancing area mapping
│   ├── county_to_ba_mapping.nc              # NetCDF version of BA mapping
│   ├── pca_ng_prices_monthly_2024.nc        # BA-level natural gas prices
│   ├── county_fips_lookup.csv               # County FIPS/name/state lookup
│   ├── all_counties_capacity_factors.nc     # [DOWNLOAD REQUIRED - see input_data/README.md]
│   └── cambium_{year}_{scenario}.nc         # [DOWNLOAD REQUIRED - see input_data/README.md]
└── output/                                   # Model outputs written here
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

A commercial or open-source MILP solver is also required. The model tries solvers in this order: **Gurobi** (commercial, free academic license available) → **CBC** → **GLPK**. Install at least one:

```bash
# CBC (open-source, recommended)
conda install -c conda-forge coincbc

# GLPK (open-source, alternative)
conda install -c conda-forge glpk
```

### 2. Download large data files

Two sets of large files must be downloaded separately and placed in `input_data/`. See [input_data/README.md](input_data/README.md) for details.

**County renewable capacity factors** (~433 MB):
- Download `all_counties_capacity_factors.nc` from: [DATA REPOSITORY LINK]
- Place in: `input_data/`

**NREL Cambium emissions data** (~850 MB total, 9 files):
- Download all `cambium_*.nc` files from: [DATA REPOSITORY LINK]
- Place in: `input_data/`
- Only required if `enable_grid = True` (grid electricity purchases with emissions tracking)

## Running the Model

Open `data_center_capacity_model_6_16_2026.py` and configure the parameters at the top of the file under **Section 1 (USER INPUTS)**:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `county_fips` | 5-digit county FIPS code | `"48113"` (Galveston, TX) |
| `rated_capacity_kW` | Data center load in kW | `300000` (300 MW) |
| `cost_scenario` | Technology cost assumptions | `"Moderate"` |
| `data_center_year` | Cost year | `2025` |
| `natural_gas_price_source` | NG price resolution | `"county"` |
| `enable_solar`, `enable_wind`, etc. | Technology switches | varies |

Then run:

```bash
python data_center_capacity_model_6_16_2026.py
```

Results are written to `output/results_county_{FIPS}.csv`.

## Data Sources

| Data | Source |
|------|--------|
| Technology cost assumptions | NREL Annual Technology Baseline (ATB) |
| Solar/wind capacity factors | ERA5 reanalysis (2019) via `atlite` |
| County electricity prices | EIA Form EIA-861M (2024) |
| County natural gas prices | HIFLD natural gas utility service territories |
| State natural gas prices | EIA Natural Gas Navigator |
| Grid emissions factors | NREL Cambium 2023 (LRMER, hourly by balancing area) |
| County–balancing area mapping | NREL ReEDS balancing area boundaries |

## Citation

If you use this model, please cite:

> [Full citation]
