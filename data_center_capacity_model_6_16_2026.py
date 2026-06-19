import pandas as pd
import pyomo.environ as pyo
import xarray as xr
import os

# 1. USER INPUTS

#  Input: 5-digit county FIPS code (e.g., "48453" for Travis County, TX)
county_fips = "48113"  # You can change this to any valid 5-digit FIPS code

#  Input: demand profile option
use_fixed_capacity = True        # Set to True to use a fixed rated capacity, False to read from demand.csv
rated_capacity_kW = 300000        # Fixed rated capacity in kW (only used if use_fixed_capacity = True)

#  Input: hourly demand of the data center (in kW)
if use_fixed_capacity:
    # Use fixed rated capacity for all 8,760 hours
    demand = [rated_capacity_kW] * 8760
else:
    # Read demand profile from demand.csv
    try:
        demand_df = pd.read_csv("input_data/demand.csv", skiprows=1)  # Skip the BEGIN_DATA row
        if demand_df.shape[1] < 5:
            raise ValueError(f"Demand file has only {demand_df.shape[1]} columns, need at least 5.")
        demand = demand_df.iloc[:, 4].values * 1000  # 5th column in MW - convert to kW
        if len(demand) != 8760:
            raise ValueError(f"Demand file must have exactly 8,760 rows (one per hour). Found: {len(demand)}")
    except FileNotFoundError:
        raise FileNotFoundError("Demand file 'input_data/demand.csv' not found.")
    except Exception as e:
        raise ValueError(f"Error reading demand file: {str(e)}") from e

#  Input: battery parameters
round_trip_eff = 0.9             # round-trip efficiency of battery
eta_c = eta_d = round_trip_eff**0.5  # split charge/discharge efficiency equally
duration_hours = 4               # battery duration in hours (e.g., 4-hour battery)

#  Input: financial parameters
discount_rate = 0.07             # discount rate for capital recovery factor (e.g., 0.07 = 7%)

#  Input: technology cost scenario and year
cost_scenario = "Moderate"       # Options: "Advanced", "Moderate", "Conservative"
data_center_year = 2025                 # Options: 2025, 2028, 2030

#  Input: renewable energy cost scenario for emissions data
re_cost_scenario = "MidCase"     # Options: "MidCase", "LowRE", "HighRE"

#  Input: natural gas price source
natural_gas_price_source = "county"  # Options: "county" (utility-level from HIFLD), "state" (state-level historical), "ba" (balancing area-level 2024)
natural_gas_year = 2024             # Options: 2001 to 2024 (only used if natural_gas_price_source = "state")

#  Input: electricity price year
electricity_price_year = 2024    # Options: 2010 to 2024

#  Input: Optimization mode
run_individually = True          # True: Run each enabled technology as separate scenario
                                 # False: Optimize all enabled technologies together in one scenario

#  Input: Technology selections (set to True to include in optimization)
#  If run_individually = True:
#    - Each enabled technology runs as its own scenario (e.g., "Solar+Battery", "Wind+Battery", "NGCC")
#    - Battery is always included as an option with renewable technologies
#  If run_individually = False:
#    - All enabled technologies are optimized together to find the least-cost combination
#    - Example: Solar+Wind+NGCC+Battery will optimize the mix of all four
enable_solar = True              # Include solar PV in optimization
enable_wind = True               # Include wind in optimization
enable_ngcc = True               # Include natural gas combined cycle in optimization
enable_ngcc_ccs = False           # Include natural gas combined cycle with carbon capture in optimization
enable_ngct = False              # Include natural gas combustion turbine in optimization
enable_coal = False              # Include coal in optimization
enable_coal_ccs = False           # Include coal with carbon capture in optimization
enable_nuclear = True           # Include nuclear in optimization
enable_smr = False               # Include small modular reactor (only for 2030)
enable_grid = True              # Include grid purchase option

#  Battery settings (battery is always available as an option, never runs alone)
battery_available = True         # Set to False to disable battery storage entirely

# States with statutory restrictions on new nuclear power plants
NUCLEAR_RESTRICTED_STATES = {'CA', 'CT', 'HI', 'ME', 'MA', 'MN', 'NY', 'OR', 'RI', 'VT'}

# 2. LOAD INPUT DATA

#  Read county-level capacity factors from NetCDF file
print(f"Loading capacity factors for county FIPS: {county_fips}")
try:
    # Load NetCDF file
    cf_ds = xr.open_dataset("input_data/all_counties_capacity_factors.nc")
    
    # Check if county exists in dataset
    if county_fips not in cf_ds.coords['county_fips'].values:
        # Try to load lookup table to provide helpful error message
        try:
            lookup_df = pd.read_csv("input_data/county_fips_lookup.csv", dtype={'county_fips': str})
            available_counties = lookup_df[['county_fips', 'county_name', 'state']].head(10)
            raise ValueError(
                f"County FIPS '{county_fips}' not found in capacity factor dataset.\n"
                f"Example available counties:\n{available_counties.to_string(index=False)}\n"
                f"See 'input_data/county_fips_lookup.csv' for full list."
            )
        except FileNotFoundError:
            raise ValueError(f"County FIPS '{county_fips}' not found in capacity factor dataset.")
    
    # Extract capacity factors for this county
    solar_cf = cf_ds['solar_cf'].sel(county_fips=county_fips).values
    wind_cf = cf_ds['wind_cf'].sel(county_fips=county_fips).values
    
    cf_ds.close()
    
    # Load county information for display
    try:
        lookup_df = pd.read_csv("input_data/county_fips_lookup.csv", dtype={'county_fips': str})
        county_info = lookup_df[lookup_df['county_fips'] == county_fips].iloc[0]
        county_name = county_info['county_name']
        state = county_info['state']
        print(f"County: {county_name}")
        print(f"Location: {county_info['centroid_lat']:.2f}°N, {county_info['centroid_lon']:.2f}°W")
    except Exception as e:
        county_name = f"County {county_fips}"
        state = county_fips[:2]  # First 2 digits are state FIPS
        print(f"Warning: Could not load county lookup information: {e}")
    
except FileNotFoundError:
    raise FileNotFoundError(
        "County capacity factor file not found at 'input_data/all_counties_capacity_factors.nc'.\n"
        "Please download it from the data repository and place it in the 'input_data/' folder."
    )
except Exception as e:
    raise ValueError(f"Error reading county capacity factors: {str(e)}") from e

#  Check data validity
if len(solar_cf) != 8760 or len(wind_cf) != 8760:
    raise ValueError(f"Each capacity factor array must have exactly 8,760 hours. Found: solar={len(solar_cf)}, wind={len(wind_cf)}")

#  Create hourly index
hours = range(8760)

# HELPER FUNCTIONS FOR CLAMPING NUMERICAL NOISE

def _clamp(value: float, eps: float = 1e-9) -> float:
    """Clamp values smaller than epsilon to zero"""
    try:
        return 0.0 if abs(float(value)) < eps else float(value)
    except Exception:
        return value

def clamp_kw(v: float) -> float:
    """kW values from solver can have tiny numerical noise"""
    return _clamp(v, eps=1e-6)

def clamp_kwh(v: float) -> float:
    """kWh values from solver can have tiny numerical noise"""
    return _clamp(v, eps=1e-6)

def clamp_tonnes(v: float) -> float:
    """Emissions in tonnes: clamp tiny +/- numerical artifacts"""
    return _clamp(v, eps=1e-9)

# Map state FIPS to state abbreviation for looking up prices
state_fips_to_abbrev = {
    '01': 'AL', '02': 'AK', '04': 'AZ', '05': 'AR', '06': 'CA', '08': 'CO', '09': 'CT', '10': 'DE',
    '11': 'DC', '12': 'FL', '13': 'GA', '15': 'HI', '16': 'ID', '17': 'IL', '18': 'IN', '19': 'IA',
    '20': 'KS', '21': 'KY', '22': 'LA', '23': 'ME', '24': 'MD', '25': 'MA', '26': 'MI', '27': 'MN',
    '28': 'MS', '29': 'MO', '30': 'MT', '31': 'NE', '32': 'NV', '33': 'NH', '34': 'NJ', '35': 'NM',
    '36': 'NY', '37': 'NC', '38': 'ND', '39': 'OH', '40': 'OK', '41': 'OR', '42': 'PA', '44': 'RI',
    '45': 'SC', '46': 'SD', '47': 'TN', '48': 'TX', '49': 'UT', '50': 'VT', '51': 'VA', '53': 'WA',
    '54': 'WV', '55': 'WI', '56': 'WY'
}

state_fips = county_fips[:2]
if state_fips not in state_fips_to_abbrev:
    raise ValueError(f"Invalid state FIPS code '{state_fips}' from county FIPS '{county_fips}'")
state_abbrev = state_fips_to_abbrev[state_fips]

# Apply state-level nuclear moratorium
if enable_nuclear and state_abbrev in NUCLEAR_RESTRICTED_STATES:
    print(f"Note: Nuclear disabled for {state_abbrev} (state moratorium on new nuclear plants).")
    enable_nuclear = False

#  Economic assumptions - read from Excel file (sheet based on year)
try:
    cost_assumptions = pd.read_excel("input_data/cost_assumptions.xlsx", sheet_name=str(data_center_year), index_col=0, header=[0, 1])
except FileNotFoundError:
    raise FileNotFoundError("Cost assumptions file 'input_data/cost_assumptions.xlsx' not found.")
except ValueError as e:
    raise ValueError(f"Error reading Excel sheet '{data_center_year}'. Check that this sheet exists in cost_assumptions.xlsx and has multi-level headers.") from e

# Load natural gas prices (state-level or BA-level)
heat_rate_ngcc_btu_per_kWh = 7000
heat_rate_ngct_btu_per_kWh = 10500
btu_per_thousand_cf = 1037000
mmbtu_to_mcf = 1.037  # 1 Mcf = 1.037 MMBtu

# Emissions factors for fossil fuel technologies (kg CO2e per MWh)
# Source: NREL (https://data.nrel.gov/submissions/171)
# NGCC: ~450 kg CO2/MWh (efficient combined cycle)
# NGCC-CCS: ~111 kg CO2/MWh (95% capture rate, reflecting parasitic load and upstream CH4 emissions)
# NGCT: ~605 kg CO2/MWh (simple cycle combustion turbine)
# Coal: ~1000 kg CO2/MWh (subcritical coal)
# Coal-CCS: ~220 kg CO2/MWh (95% capture rate, reflecting parasitic load and upstream CH4 emissions)
emissions_ngcc_kg_per_MWh = 450
emissions_ngcc_ccs_kg_per_MWh = 111
emissions_ngct_kg_per_MWh = 605
emissions_coal_kg_per_MWh = 1000
emissions_coal_ccs_kg_per_MWh = 220
emissions_nuclear_kg_per_MWh = 0  # Nuclear considered zero-emission
emissions_smr_kg_per_MWh = 0  # SMR also zero-emission

if natural_gas_price_source.lower() == "county":
    # Load county-level utility prices from NetCDF (HIFLD spatial data)
    print(f"Loading natural gas prices from county-level utility data...")
    try:
        ng_county_ds = xr.open_dataset("input_data/county_ng_prices.nc")
        
        # Check if county exists in dataset
        if county_fips not in ng_county_ds.coords['county_fips'].values:
            raise ValueError(
                f"County FIPS '{county_fips}' not found in county NG prices dataset. "
                f"Available counties: {ng_county_ds.coords['county_fips'].values[:10]}"
            )
        
        # Extract price for this county ($/Mcf which equals $/thousand cubic feet)
        ng_price_per_thousand_cf = float(ng_county_ds['ng_price_per_mcf'].sel(county_fips=county_fips).values)
        ng_price_per_mmbtu = float(ng_county_ds['ng_price_per_mmbtu'].sel(county_fips=county_fips).values)
        
        ng_county_ds.close()
        print(f"  County '{county_name}': ${ng_price_per_thousand_cf:.2f}/thousand cubic feet (${ng_price_per_mmbtu:.2f}/MMBtu)")
        
    except FileNotFoundError:
        raise FileNotFoundError(
            "County-level NG price file 'input_data/county_ng_prices.nc' not found. "
            "Please download it from the data repository or set natural_gas_price_source='state'."
        )
    except Exception as e:
        raise ValueError(f"Error loading county-level natural gas prices: {str(e)}") from e

elif natural_gas_price_source.lower() == "ba":
    # Load BA-level monthly prices from NetCDF (2024 data only)
    print(f"Loading natural gas prices from BA-level data (2024)...")
    try:
        ng_pca_ds = xr.open_dataset("input_data/pca_ng_prices_monthly_2024.nc")
        
        # Get BA mapping for this county
        ba_mapping_df = pd.read_csv("input_data/county_to_ba_mapping.csv", dtype={'county_fips': str})
        county_ba_row = ba_mapping_df[ba_mapping_df['county_fips'] == county_fips]
        if len(county_ba_row) == 0:
            raise ValueError(f"County FIPS '{county_fips}' not found in BA mapping.")
        
        balancing_area = county_ba_row.iloc[0]['balancing_area']
        if balancing_area == 'NONE':
            raise ValueError(f"County FIPS '{county_fips}' is not mapped to a balancing area.")
        
        # PCA region ID is the balancing area value directly (e.g., 'p64')
        pca_region_id = balancing_area
        
        # Load annual average price from NetCDF ($/MMBtu)
        if pca_region_id not in ng_pca_ds.coords['pca_region'].values:
            raise ValueError(f"PCA region '{pca_region_id}' not found in BA-level price dataset.")
        
        # Get monthly average and convert to annual
        monthly_prices_mmbtu = ng_pca_ds['ng_price'].sel(pca_region=pca_region_id).values
        ng_price_per_mmbtu = float(monthly_prices_mmbtu.mean())  # Annual average in $/MMBtu
        
        # Convert from $/MMBtu to $/thousand cubic feet
        ng_price_per_thousand_cf = ng_price_per_mmbtu * mmbtu_to_mcf
        
        ng_pca_ds.close()
        print(f"  County '{county_name}' (PCA region '{pca_region_id}'): ${ng_price_per_thousand_cf:.2f}/thousand cubic feet (${ng_price_per_mmbtu:.2f}/MMBtu)")
        
    except FileNotFoundError:
        raise FileNotFoundError(
            "BA-level price file 'input_data/pca_ng_prices_monthly_2024.nc' not found. "
            "Please download it from the data repository or set natural_gas_price_source='state'."
        )
    except Exception as e:
        raise ValueError(f"Error loading BA-level natural gas prices: {str(e)}") from e

else:
    # Load state-level historical prices (default)
    print(f"Loading natural gas prices from state-level data ({natural_gas_year})...")
    try:
        ng_prices_df = pd.read_csv("input_data/monthly_natural_gas_prices_dollars_per_thousandCubicFeet.csv")
        if state_abbrev not in ng_prices_df.columns:
            available_states = [col for col in ng_prices_df.columns if col != 'Date']
            raise ValueError(f"State '{state_abbrev}' not found in natural gas prices file. Available states: {available_states}")
        if 'Date' not in ng_prices_df.columns:
            raise ValueError("Natural gas prices file missing 'Date' column.")
        
        # Parse Date column to datetime for proper year filtering
        ng_prices_df['Date'] = pd.to_datetime(ng_prices_df['Date'], format='mixed', errors='coerce')
        if ng_prices_df['Date'].isna().any():
            raise ValueError("Some dates in natural gas prices file could not be parsed. Check date format.")
        
    except FileNotFoundError:
        raise FileNotFoundError("Natural gas prices file 'input_data/monthly_natural_gas_prices_dollars_per_thousandCubicFeet.csv' not found.")
    except Exception as e:
        raise ValueError(f"Error reading natural gas prices: {str(e)}") from e

    # Filter to the selected year and get state-specific prices
    ng_prices_year = ng_prices_df[ng_prices_df['Date'].dt.year == natural_gas_year]
    if len(ng_prices_year) == 0:
        raise ValueError(f"No natural gas price data found for year {natural_gas_year} in the prices file.")
    # Calculate average annual price for the state ($/thousand cubic feet)
    ng_price_per_thousand_cf = ng_prices_year[state_abbrev].mean()
    if pd.isna(ng_price_per_thousand_cf):
        raise ValueError(f"Natural gas price for state '{state_abbrev}' in year {natural_gas_year} is missing or invalid.")
    
    print(f"  State '{state_abbrev}': ${ng_price_per_thousand_cf:.2f}/thousand cubic feet")

# Convert natural gas price to $/kWh
ng_fuel_cost_ngcc_kWh = (ng_price_per_thousand_cf / btu_per_thousand_cf) * heat_rate_ngcc_btu_per_kWh
ng_fuel_cost_ngct_kWh = (ng_price_per_thousand_cf / btu_per_thousand_cf) * heat_rate_ngct_btu_per_kWh

# Read county-level electricity prices from NetCDF file (annual utility-based prices)
print(f"Loading county-level electricity prices from NetCDF...")
try:
    # Load county electricity prices from NetCDF
    elec_prices_ds = xr.open_dataset("input_data/county_electricity_prices_2024.nc")
    
    # Check if county FIPS code exists in dataset
    county_fips_str = str(county_fips)
    if county_fips_str not in elec_prices_ds.coords['county_fips'].values:
        raise ValueError(
            f"County FIPS '{county_fips}' not found in county electricity prices dataset. "
            f"Available counties: {elec_prices_ds.coords['county_fips'].values[:10]}"
        )
    
    # Extract annual county price (in cents/kWh)
    county_price_cents_per_kwh = float(elec_prices_ds['price_cents_per_kwh'].sel(county_fips=county_fips_str).values)
    
    # Get utility information for display
    try:
        county_name_from_nc = str(elec_prices_ds['county_name'].sel(county_fips=county_fips_str).values)
        utility_name_from_nc = str(elec_prices_ds['utility_name'].sel(county_fips=county_fips_str).values)
        print(f"  County: {county_name_from_nc}")
        print(f"  Utility: {utility_name_from_nc}")
    except:
        pass
    
    # Convert from cents/kWh to $/kWh
    county_price_per_kwh = county_price_cents_per_kwh / 100
    
    elec_prices_ds.close()
    
    # Create hourly electricity price array (8,760 hours)
    # Since this is an annual average price, repeat it for all 12 months
    hourly_elec_price = [county_price_per_kwh] * 8760
    
    print(f"  Annual county price: {county_price_cents_per_kwh:.2f}¢/kWh (${county_price_per_kwh:.4f}/kWh)")
    
except FileNotFoundError:
    raise FileNotFoundError(
        "County electricity prices file 'input_data/county_electricity_prices_2024.nc' not found. "
        "Please download it from the data repository and place it in the 'input_data/' folder."
    )
except Exception as e:
    raise ValueError(f"Error reading county electricity prices: {str(e)}") from e

# Load emissions data from Cambium if grid is enabled
if enable_grid:
    try:
        # Map data_center_year to Cambium year (2025 and 2028 use 2025 data, 2030 uses 2030 data)
        if data_center_year in [2025, 2028]:
            cambium_year = 2025
        elif data_center_year == 2030:
            cambium_year = 2030
        else:
            raise ValueError(f"No Cambium data available for year {data_center_year}. Available years: 2025, 2028, 2030")
        
        # Load county to balancing area mapping
        print(f"Loading emissions data for {re_cost_scenario} scenario, year {cambium_year}...")
        ba_mapping_ds = xr.open_dataset("input_data/county_to_ba_mapping.nc")
        
        # Get balancing area for this county
        # The NetCDF has 'county_fips' as a data variable (not a dimension coordinate)
        # So we need to find the matching row by searching the county_fips values
        fips_matches = (ba_mapping_ds['county_fips'].values == county_fips)
        
        if not fips_matches.any():
            raise ValueError(f"County FIPS {county_fips} not found in BA mapping dataset")
        
        match_idx = fips_matches.argmax()
        county_ba_data = ba_mapping_ds.isel(county=match_idx)
        balancing_area = str(county_ba_data['balancing_area'].values)
        
        if balancing_area == 'NONE':
            raise ValueError(f"County {county_fips} ({county_name}, {state}) is not mapped to any NREL balancing area. This typically occurs for Alaska, Hawaii, and offshore territories.")
        
        print(f"County {county_fips} mapped to balancing area: {balancing_area}")
        ba_mapping_ds.close()
        
        # Load Cambium emissions data for the appropriate year and scenario
        cambium_file = f"input_data/cambium_{cambium_year}_{re_cost_scenario}.nc"
        cambium_ds = xr.open_dataset(cambium_file)
        
        # Extract hourly LRMER CO2e emissions factors (kg/MWh) for this balancing area
        ba_emissions_data = cambium_ds.sel(balancing_area=balancing_area)
        lrmer_co2e_kg_per_MWh = ba_emissions_data['lrmer_co2e'].values  # kg CO2e per MWh
        
        # Validate emissions data
        if len(lrmer_co2e_kg_per_MWh) != 8760:
            raise ValueError(f"Expected 8760 hourly emissions factors, got {len(lrmer_co2e_kg_per_MWh)}")
        
        print(f"Average LRMER CO2e rate: {lrmer_co2e_kg_per_MWh.mean():.1f} kg/MWh")
        
        cambium_ds.close()
        
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Emissions data file not found: {str(e)}. Download the Cambium NetCDF files from the data repository and place them in 'input_data/'.") from e
    except Exception as e:
        raise ValueError(f"Error loading emissions data: {str(e)}") from e
else:
    # If grid not enabled, create dummy emissions array
    lrmer_co2e_kg_per_MWh = [0] * 8760

# Read overnight capital costs for the selected scenario
try:
    occ_solar_kW = cost_assumptions.loc['Solar', (cost_scenario, 'OCC_dollars_kW')]
    occ_wind_kW = cost_assumptions.loc['Wind', (cost_scenario, 'OCC_dollars_kW')]
    occ_batt_power_kW = cost_assumptions.loc['Utility-scale battery - Power', (cost_scenario, 'OCC_dollars_kW')]
    occ_batt_energy_kWh = cost_assumptions.loc['Utility-scale battery - Energy', (cost_scenario, 'OCC_dollars_kW')]  # Note: units are $/kWh for energy row
    occ_ngcc_kW = cost_assumptions.loc['NGCC', (cost_scenario, 'OCC_dollars_kW')]
    occ_ngcc_ccs_kW = cost_assumptions.loc['NGCC-CCS', (cost_scenario, 'OCC_dollars_kW')]
    occ_ngct_kW = cost_assumptions.loc['NGCT', (cost_scenario, 'OCC_dollars_kW')]
    occ_coal_kW = cost_assumptions.loc['Coal', (cost_scenario, 'OCC_dollars_kW')]
    occ_coal_ccs_kW = cost_assumptions.loc['Coal-CCS', (cost_scenario, 'OCC_dollars_kW')]
    occ_nuclear_kW = cost_assumptions.loc['Nuclear', (cost_scenario, 'OCC_dollars_kW')]
    if data_center_year == 2030:
        occ_smr_kW = cost_assumptions.loc['SMR', (cost_scenario, 'OCC_dollars_kW')]
except KeyError as e:
    raise ValueError(f"Missing technology or column in cost assumptions. Check that scenario '{cost_scenario}' exists and all required technologies are present in sheet '{data_center_year}'.") from e

# Read fixed O&M costs for the selected scenario
try:
    fom_solar_kW_year = cost_assumptions.loc['Solar', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_wind_kW_year = cost_assumptions.loc['Wind', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_batt_power_kW_year = cost_assumptions.loc['Utility-scale battery - Power', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_ngcc_kW_year = cost_assumptions.loc['NGCC', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_ngcc_ccs_kW_year = cost_assumptions.loc['NGCC-CCS', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_ngct_kW_year = cost_assumptions.loc['NGCT', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_coal_kW_year = cost_assumptions.loc['Coal', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_coal_ccs_kW_year = cost_assumptions.loc['Coal-CCS', (cost_scenario, 'FOM_dollars_kw_year')]
    fom_nuclear_kW_year = cost_assumptions.loc['Nuclear', (cost_scenario, 'FOM_dollars_kw_year')]
    if data_center_year == 2030:
        fom_smr_kW_year = cost_assumptions.loc['SMR', (cost_scenario, 'FOM_dollars_kw_year')]
except KeyError as e:
    raise ValueError(f"Missing FOM data in cost assumptions for scenario '{cost_scenario}'.") from e

# Read lifetimes (same for all scenarios)
# Lifetime is under the last scenario column in the Excel (Conservative)
try:
    lifetime_solar = cost_assumptions.loc['Solar', ('Conservative', 'Lifetime')]
    lifetime_wind = cost_assumptions.loc['Wind', ('Conservative', 'Lifetime')]
    lifetime_batt_power = cost_assumptions.loc['Utility-scale battery - Power', ('Conservative', 'Lifetime')]
    lifetime_batt_energy = cost_assumptions.loc['Utility-scale battery - Energy', ('Conservative', 'Lifetime')]
    lifetime_ngcc = cost_assumptions.loc['NGCC', ('Conservative', 'Lifetime')]
    lifetime_ngcc_ccs = cost_assumptions.loc['NGCC-CCS', ('Conservative', 'Lifetime')]
    lifetime_ngct = cost_assumptions.loc['NGCT', ('Conservative', 'Lifetime')]
    lifetime_coal = cost_assumptions.loc['Coal', ('Conservative', 'Lifetime')]
    lifetime_coal_ccs = cost_assumptions.loc['Coal-CCS', ('Conservative', 'Lifetime')]
    lifetime_nuclear = cost_assumptions.loc['Nuclear', ('Conservative', 'Lifetime')]
    if data_center_year == 2030:
        lifetime_smr = cost_assumptions.loc['SMR', ('Conservative', 'Lifetime')]
except KeyError as e:
    raise ValueError(f"Missing Lifetime data in cost assumptions. Check that 'Conservative' scenario exists with 'Lifetime' column.") from e

# Read variable operating and fuel costs for the selected scenario (convert from $/MWh to $/kWh)
# For natural gas technologies, use calculated fuel costs from CSV prices
try:
    cost_ngcc_fuel_kWh = ng_fuel_cost_ngcc_kWh
    cost_ngcc_vom_kWh = cost_assumptions.loc['NGCC', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    cost_ngcc_ccs_fuel_kWh = ng_fuel_cost_ngcc_kWh  # NGCC-CCS uses same fuel (natural gas)
    cost_ngcc_ccs_vom_kWh = cost_assumptions.loc['NGCC-CCS', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    cost_ngct_fuel_kWh = ng_fuel_cost_ngct_kWh
    cost_ngct_vom_kWh = cost_assumptions.loc['NGCT', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    # For other technologies, use Excel values
    cost_coal_fuel_kWh = cost_assumptions.loc['Coal', (cost_scenario, 'fuel_dollars_MWh')] / 1000
    cost_coal_vom_kWh = cost_assumptions.loc['Coal', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    cost_coal_ccs_fuel_kWh = cost_assumptions.loc['Coal-CCS', (cost_scenario, 'fuel_dollars_MWh')] / 1000
    cost_coal_ccs_vom_kWh = cost_assumptions.loc['Coal-CCS', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    cost_nuclear_fuel_kWh = cost_assumptions.loc['Nuclear', (cost_scenario, 'fuel_dollars_MWh')] / 1000
    cost_nuclear_vom_kWh = cost_assumptions.loc['Nuclear', (cost_scenario, 'VOM_dollars_MWh')] / 1000
    if data_center_year == 2030:
        cost_smr_fuel_kWh = cost_assumptions.loc['SMR', (cost_scenario, 'fuel_dollars_MWh')] / 1000
        cost_smr_vom_kWh = cost_assumptions.loc['SMR', (cost_scenario, 'VOM_dollars_MWh')] / 1000
except KeyError as e:
    raise ValueError(f"Missing fuel or VOM data in cost assumptions for scenario '{cost_scenario}'.") from e

# Calculate capital recovery factor (CRF) for each technology
# CRF = r * (1 + r)^n / ((1 + r)^n - 1) where r = discount rate, n = lifetime
def calculate_crf(discount_rate, lifetime):
    if discount_rate == 0:
        return 1 / lifetime
    return discount_rate * (1 + discount_rate)**lifetime / ((1 + discount_rate)**lifetime - 1)

crf_solar = calculate_crf(discount_rate, lifetime_solar)
crf_wind = calculate_crf(discount_rate, lifetime_wind)
crf_batt_power = calculate_crf(discount_rate, lifetime_batt_power)
crf_batt_energy = calculate_crf(discount_rate, lifetime_batt_energy)
crf_ngcc = calculate_crf(discount_rate, lifetime_ngcc)
crf_ngcc_ccs = calculate_crf(discount_rate, lifetime_ngcc_ccs)
crf_ngct = calculate_crf(discount_rate, lifetime_ngct)
crf_coal = calculate_crf(discount_rate, lifetime_coal)
crf_coal_ccs = calculate_crf(discount_rate, lifetime_coal_ccs)
crf_nuclear = calculate_crf(discount_rate, lifetime_nuclear)
if data_center_year == 2030:
    crf_smr = calculate_crf(discount_rate, lifetime_smr)

# Calculate annualized capital costs (OCC * CRF + FOM)
annualized_solar_kW = occ_solar_kW * crf_solar + fom_solar_kW_year
annualized_wind_kW = occ_wind_kW * crf_wind + fom_wind_kW_year
annualized_batt_power_kW = occ_batt_power_kW * crf_batt_power + fom_batt_power_kW_year
annualized_batt_energy_kWh = occ_batt_energy_kWh * crf_batt_energy  # No FOM for battery energy
annualized_ngcc_kW = occ_ngcc_kW * crf_ngcc + fom_ngcc_kW_year
annualized_ngcc_ccs_kW = occ_ngcc_ccs_kW * crf_ngcc_ccs + fom_ngcc_ccs_kW_year
annualized_ngct_kW = occ_ngct_kW * crf_ngct + fom_ngct_kW_year
annualized_coal_kW = occ_coal_kW * crf_coal + fom_coal_kW_year
annualized_coal_ccs_kW = occ_coal_ccs_kW * crf_coal_ccs + fom_coal_ccs_kW_year
annualized_nuclear_kW = occ_nuclear_kW * crf_nuclear + fom_nuclear_kW_year
if data_center_year == 2030:
    annualized_smr_kW = occ_smr_kW * crf_smr + fom_smr_kW_year

# 3. FUNCTION TO BUILD AND SOLVE MODEL

def build_and_solve_thermal_model(tech_name, occ_kW, crf, fom_kW_year, fuel_cost_kWh, vom_cost_kWh):
    """
    Generalized function to build and solve optimization model for thermal generation scenarios.
    Simple model with no renewables or batteries - just thermal capacity to meet demand.
    
    Args:
        tech_name: Name of the technology (e.g., 'NGCC', 'NGCT', 'Coal', 'Nuclear')
        occ_kW: Overnight capital cost in $/kW
        crf: Capital recovery factor (dimensionless)
        fom_kW_year: Fixed O&M cost in $/kW/year
        fuel_cost_kWh: Fuel cost in $/kWh
        vom_cost_kWh: Variable O&M cost in $/kWh
    
    Returns:
        Dictionary with results
    """
    scenario_name = tech_name
    print(f"\n{'-'*60}")
    print(f"Solving: {scenario_name}")
    print(f"{'-'*60}")
    
    # For thermal generation, we just need enough capacity to meet peak demand
    peak_demand = max(demand)
    
    # Annualized capital cost + annual fixed O&M + annual fuel cost + variable O&M cost
    # Assuming the thermal plant runs at full demand all year (8760 hours)
    total_annual_generation = sum(demand)  # kWh
    
    # Separate CAPEX (annualized capital) and FOM
    # annualized_cost_kW = occ_kW * CRF + fom_kW_year
    annual_capex_cost = occ_kW * crf * peak_demand
    annual_fom_cost = fom_kW_year * peak_demand
    annual_fuel_cost = fuel_cost_kWh * total_annual_generation
    annual_vom_cost = vom_cost_kWh * total_annual_generation
    total_annual_cost = annual_capex_cost + annual_fom_cost + annual_fuel_cost + annual_vom_cost
    
    result_dict = {
        'Scenario': scenario_name,
        'County_FIPS': county_fips,
        'County_Name': county_name,
        'State': state_abbrev,
        'Solar_Capacity_kW': 0.0,
        'Wind_Capacity_kW': 0.0,
        'Total_Generation_Capacity_kW': peak_demand,
        'Battery_Power_kW': 0.0,
        'Battery_Energy_kWh': 0.0,
        f'{tech_name}_Capacity_kW': peak_demand,
        'Annual_Electricity_Cost_USD': 0.0,
        'Annual_CAPEX_Cost_USD': annual_capex_cost,
        'Annual_FOM_Cost_USD': annual_fom_cost,
        'Annual_Fuel_Cost_USD': annual_fuel_cost,
        'Annual_VOM_Cost_USD': annual_vom_cost,
        'Total_Annual_Cost_USD': total_annual_cost
    }
    
    print(f"{tech_name} capacity:       {peak_demand:8.2f} kW")
    print(f"Total generation:    {peak_demand:8.2f} kW")
    print(f"Annual fuel cost: ${annual_fuel_cost:,.0f}")
    print(f"Total annual cost: ${total_annual_cost:,.0f}")
    
    return result_dict

def build_and_solve_grid_model():
    """
    Calculate cost for grid-only scenario where all electricity is purchased from the grid.
    No generation or storage - just pure grid electricity purchases.
    
    Returns:
        Dictionary with results
    """
    scenario_name = "Grid"
    print(f"\n{'-'*60}")
    print(f"Solving: {scenario_name}")
    print(f"{'-'*60}")
    
    # Calculate total annual electricity cost from grid
    # Each hour: demand[hour] * hourly_elec_price[hour]
    total_annual_elec_cost = sum(demand[h] * hourly_elec_price[h] for h in range(8760))
    
    result_dict = {
        'Scenario': scenario_name,
        'County_FIPS': county_fips,
        'County_Name': county_name,
        'State': state_abbrev,
        'Solar_Capacity_kW': 0.0,
        'Wind_Capacity_kW': 0.0,
        'Total_Generation_Capacity_kW': 0.0,
        'Battery_Power_kW': 0.0,
        'Battery_Energy_kWh': 0.0,
        'Grid_Capacity_kW': 0.0,
        'Annual_Electricity_Cost_USD': total_annual_elec_cost,
        'Annual_CAPEX_Cost_USD': 0.0,
        'Annual_FOM_Cost_USD': 0.0,
        'Annual_Fuel_Cost_USD': 0.0,
        'Annual_VOM_Cost_USD': 0.0,
        'Total_Annual_Cost_USD': total_annual_elec_cost
    }
    
    print(f"Grid electricity only")
    print(f"Annual electricity cost: ${total_annual_elec_cost:,.0f}")
    print(f"Total annual cost: ${total_annual_elec_cost:,.0f}")
    
    return result_dict

def build_and_solve_model(scenario_name, include_solar=True, include_wind=True, include_battery=True,
                          include_ngcc=False, include_ngcc_ccs=False, include_ngct=False, 
                          include_coal=False, include_coal_ccs=False, include_nuclear=False, 
                          include_smr=False, include_grid=False):
    """
    Build and solve optimization model for any combination of technologies.
    
    Args:
        scenario_name: Name of the scenario
        include_solar: Whether to include solar capacity
        include_wind: Whether to include wind capacity
        include_battery: Whether to include battery storage
        include_ngcc: Whether to include NGCC
        include_ngcc_ccs: Whether to include NGCC with CCS
        include_ngct: Whether to include NGCT
        include_coal: Whether to include coal
        include_coal_ccs: Whether to include coal with CCS
        include_nuclear: Whether to include nuclear
        include_smr: Whether to include SMR
        include_grid: Whether to include grid purchases
    
    Returns:
        Dictionary with results
    """
    print(f"\n{'-'*60}")
    print(f"Solving: {scenario_name}")
    print(f"{'-'*60}")
    
    #  Create a Pyomo model
    model = pyo.ConcreteModel()

    #  Sets
    model.T = pyo.RangeSet(1, 8760)  # 8,760 hours (1-indexed)
    model.T_SOC = pyo.RangeSet(0, 8760)  # SOC includes initial state at t=0

    #  Decision variables - Renewables
    model.solar_cap = pyo.Var(within=pyo.NonNegativeReals)     # kW of installed solar
    model.wind_cap  = pyo.Var(within=pyo.NonNegativeReals)     # kW of installed wind
    
    #  Decision variables - Battery
    model.batt_power = pyo.Var(within=pyo.NonNegativeReals)    # kW battery power capacity
    model.batt_energy = pyo.Var(within=pyo.NonNegativeReals)   # kWh battery energy capacity
    model.charge = pyo.Var(model.T, within=pyo.NonNegativeReals)     # kW charging power
    model.discharge = pyo.Var(model.T, within=pyo.NonNegativeReals)  # kW discharging power
    model.soc = pyo.Var(model.T_SOC, within=pyo.NonNegativeReals)    # kWh energy stored
    
    #  Decision variables - Thermal generation
    model.ngcc_cap = pyo.Var(within=pyo.NonNegativeReals)      # kW of NGCC capacity
    model.ngcc_ccs_cap = pyo.Var(within=pyo.NonNegativeReals)  # kW of NGCC-CCS capacity
    model.ngct_cap = pyo.Var(within=pyo.NonNegativeReals)      # kW of NGCT capacity
    model.coal_cap = pyo.Var(within=pyo.NonNegativeReals)      # kW of coal capacity
    model.coal_ccs_cap = pyo.Var(within=pyo.NonNegativeReals)  # kW of Coal-CCS capacity
    model.nuclear_cap = pyo.Var(within=pyo.NonNegativeReals)   # kW of nuclear capacity
    model.smr_cap = pyo.Var(within=pyo.NonNegativeReals)       # kW of SMR capacity
    
    #  Binary variables for minimum capacity constraints (if built, must be >= 10% of demand)
    model.has_solar = pyo.Var(within=pyo.Binary)
    model.has_wind = pyo.Var(within=pyo.Binary)
    model.has_batt = pyo.Var(within=pyo.Binary)
    model.has_ngcc = pyo.Var(within=pyo.Binary)
    model.has_ngcc_ccs = pyo.Var(within=pyo.Binary)
    model.has_ngct = pyo.Var(within=pyo.Binary)
    model.has_coal = pyo.Var(within=pyo.Binary)
    model.has_coal_ccs = pyo.Var(within=pyo.Binary)
    model.has_nuclear = pyo.Var(within=pyo.Binary)
    model.has_smr = pyo.Var(within=pyo.Binary)
    
    #  Decision variables - Hourly generation from each thermal source
    model.ngcc_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.ngcc_ccs_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.ngct_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.coal_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.coal_ccs_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.nuclear_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    model.smr_gen = pyo.Var(model.T, within=pyo.NonNegativeReals)
    
    #  Decision variables - Grid
    model.grid_purchase = pyo.Var(model.T, within=pyo.NonNegativeReals)  # kW purchased from grid each hour

    # ───────────────────────────────
    # CONSTRAINTS
    # ───────────────────────────────

    #  Battery power limits
    def charge_limit_rule(m, t):
        return m.charge[t] <= m.batt_power
    model.charge_limit = pyo.Constraint(model.T, rule=charge_limit_rule)

    def discharge_limit_rule(m, t):
        return m.discharge[t] <= m.batt_power
    model.discharge_limit = pyo.Constraint(model.T, rule=discharge_limit_rule)
    
    #  Thermal generation limits
    def ngcc_gen_limit_rule(m, t):
        return m.ngcc_gen[t] <= m.ngcc_cap
    model.ngcc_gen_limit = pyo.Constraint(model.T, rule=ngcc_gen_limit_rule)
    
    def ngcc_ccs_gen_limit_rule(m, t):
        return m.ngcc_ccs_gen[t] <= m.ngcc_ccs_cap
    model.ngcc_ccs_gen_limit = pyo.Constraint(model.T, rule=ngcc_ccs_gen_limit_rule)
    
    def ngct_gen_limit_rule(m, t):
        return m.ngct_gen[t] <= m.ngct_cap
    model.ngct_gen_limit = pyo.Constraint(model.T, rule=ngct_gen_limit_rule)
    
    def coal_gen_limit_rule(m, t):
        return m.coal_gen[t] <= m.coal_cap
    model.coal_gen_limit = pyo.Constraint(model.T, rule=coal_gen_limit_rule)
    
    def coal_ccs_gen_limit_rule(m, t):
        return m.coal_ccs_gen[t] <= m.coal_ccs_cap
    model.coal_ccs_gen_limit = pyo.Constraint(model.T, rule=coal_ccs_gen_limit_rule)
    
    def nuclear_gen_limit_rule(m, t):
        return m.nuclear_gen[t] <= m.nuclear_cap
    model.nuclear_gen_limit = pyo.Constraint(model.T, rule=nuclear_gen_limit_rule)
    
    def smr_gen_limit_rule(m, t):
        return m.smr_gen[t] <= m.smr_cap
    model.smr_gen_limit = pyo.Constraint(model.T, rule=smr_gen_limit_rule)

    #  Energy balance: ensure all generation sources + battery discharge >= demand + battery charge
    def energy_balance_rule(m, t):
        renewable_gen = solar_cf[t-1] * m.solar_cap + wind_cf[t-1] * m.wind_cap  # t-1 because arrays are 0-indexed
        thermal_gen = (m.ngcc_gen[t] + m.ngcc_ccs_gen[t] + m.ngct_gen[t] + m.coal_gen[t] + 
                      m.coal_ccs_gen[t] + m.nuclear_gen[t] + m.smr_gen[t])
        return renewable_gen + thermal_gen + m.discharge[t] + m.grid_purchase[t] - m.charge[t] >= demand[t-1]
    model.energy_balance = pyo.Constraint(model.T, rule=energy_balance_rule)

    #  State of charge dynamics (SOC[t] = SOC[t-1] + ηc*charge[t] - (1/ηd)*discharge[t])
    def soc_balance_rule(m, t):
        return m.soc[t] == m.soc[t-1] + eta_c * m.charge[t] - (1/eta_d) * m.discharge[t]
    model.soc_balance = pyo.Constraint(model.T, rule=soc_balance_rule)
    
    # Initial SOC
    model.initial_soc = pyo.Constraint(expr=model.soc[0] == 0.5 * model.batt_energy)

    #  SOC must remain within energy capacity
    def soc_limit_rule(m, t):
        return m.soc[t] <= m.batt_energy
    model.soc_limit = pyo.Constraint(model.T_SOC, rule=soc_limit_rule)

    #  Nonnegative SOC
    def soc_nonneg_rule(m, t):
        return m.soc[t] >= 0
    model.soc_nonneg = pyo.Constraint(model.T_SOC, rule=soc_nonneg_rule)

    #  End of year SOC = initial SOC (energy neutrality)
    def soc_cycle_rule(m):
        return m.soc[8760] == m.soc[0]
    model.soc_cycle = pyo.Constraint(rule=soc_cycle_rule)
    
    # --- Battery duration constraint (e.g., 4-hour battery)
    model.batt_duration = pyo.Constraint(expr=model.batt_energy == duration_hours * model.batt_power)
    
    # Technology-specific constraints (disable technologies not selected)
    if not include_solar:
        model.solar_cap.fix(0)
        model.has_solar.fix(0)
    if not include_wind:
        model.wind_cap.fix(0)
        model.has_wind.fix(0)
    if not include_battery:
        model.batt_power.fix(0)
        model.batt_energy.fix(0)
        model.has_batt.fix(0)
    if not include_ngcc:
        model.ngcc_cap.fix(0)
        model.has_ngcc.fix(0)
    if not include_ngcc_ccs:
        model.ngcc_ccs_cap.fix(0)
        model.has_ngcc_ccs.fix(0)
    if not include_ngct:
        model.ngct_cap.fix(0)
        model.has_ngct.fix(0)
    if not include_coal:
        model.coal_cap.fix(0)
        model.has_coal.fix(0)
    if not include_coal_ccs:
        model.coal_ccs_cap.fix(0)
        model.has_coal_ccs.fix(0)
    if not include_nuclear:
        model.nuclear_cap.fix(0)
        model.has_nuclear.fix(0)
    if not include_smr:
        model.smr_cap.fix(0)
        model.has_smr.fix(0)
    if not include_grid:
        for t in model.T:
            model.grid_purchase[t].fix(0)
    
    # Minimum capacity constraints: if built, must be >= 10% of rated capacity
    # This prevents unrealistically small behind-the-meter installations
    min_cap_threshold = 0.10 * rated_capacity_kW  # 10% of data center demand
    Big_M_cap = 50 * rated_capacity_kW  # Upper bound on capacity (50x demand)
    
    # Solar: cap <= Big_M * has_solar, cap >= min_threshold * has_solar
    model.solar_cap_ub = pyo.Constraint(expr=model.solar_cap <= Big_M_cap * model.has_solar)
    model.solar_cap_lb = pyo.Constraint(expr=model.solar_cap >= min_cap_threshold * model.has_solar)
    
    # Wind
    model.wind_cap_ub = pyo.Constraint(expr=model.wind_cap <= Big_M_cap * model.has_wind)
    model.wind_cap_lb = pyo.Constraint(expr=model.wind_cap >= min_cap_threshold * model.has_wind)
    
    # Battery (power rating)
    model.batt_cap_ub = pyo.Constraint(expr=model.batt_power <= Big_M_cap * model.has_batt)
    model.batt_cap_lb = pyo.Constraint(expr=model.batt_power >= min_cap_threshold * model.has_batt)
    
    # NGCC
    model.ngcc_cap_ub = pyo.Constraint(expr=model.ngcc_cap <= Big_M_cap * model.has_ngcc)
    model.ngcc_cap_lb = pyo.Constraint(expr=model.ngcc_cap >= min_cap_threshold * model.has_ngcc)
    
    # NGCC-CCS
    model.ngcc_ccs_cap_ub = pyo.Constraint(expr=model.ngcc_ccs_cap <= Big_M_cap * model.has_ngcc_ccs)
    model.ngcc_ccs_cap_lb = pyo.Constraint(expr=model.ngcc_ccs_cap >= min_cap_threshold * model.has_ngcc_ccs)
    
    # NGCT
    model.ngct_cap_ub = pyo.Constraint(expr=model.ngct_cap <= Big_M_cap * model.has_ngct)
    model.ngct_cap_lb = pyo.Constraint(expr=model.ngct_cap >= min_cap_threshold * model.has_ngct)
    
    # Coal
    model.coal_cap_ub = pyo.Constraint(expr=model.coal_cap <= Big_M_cap * model.has_coal)
    model.coal_cap_lb = pyo.Constraint(expr=model.coal_cap >= min_cap_threshold * model.has_coal)
    
    # Coal-CCS
    model.coal_ccs_cap_ub = pyo.Constraint(expr=model.coal_ccs_cap <= Big_M_cap * model.has_coal_ccs)
    model.coal_ccs_cap_lb = pyo.Constraint(expr=model.coal_ccs_cap >= min_cap_threshold * model.has_coal_ccs)
    
    # Nuclear
    model.nuclear_cap_ub = pyo.Constraint(expr=model.nuclear_cap <= Big_M_cap * model.has_nuclear)
    model.nuclear_cap_lb = pyo.Constraint(expr=model.nuclear_cap >= min_cap_threshold * model.has_nuclear)
    
    # SMR
    model.smr_cap_ub = pyo.Constraint(expr=model.smr_cap <= Big_M_cap * model.has_smr)
    model.smr_cap_lb = pyo.Constraint(expr=model.smr_cap >= min_cap_threshold * model.has_smr)

    # ───────────────────────────────
    # OBJECTIVE FUNCTION
    # ───────────────────────────────

    #  Define total cost: annualized capital costs + annual operating costs
    def objective_rule(m):
        # Annualized capital costs
        capital_cost = (
            annualized_solar_kW * m.solar_cap +
            annualized_wind_kW * m.wind_cap +
            annualized_batt_power_kW * m.batt_power +
            annualized_batt_energy_kWh * m.batt_energy +
            annualized_ngcc_kW * m.ngcc_cap +
            annualized_ngcc_ccs_kW * m.ngcc_ccs_cap +
            annualized_ngct_kW * m.ngct_cap +
            annualized_coal_kW * m.coal_cap +
            annualized_coal_ccs_kW * m.coal_ccs_cap +
            annualized_nuclear_kW * m.nuclear_cap
        )
        
        # Add SMR if enabled
        if include_smr and data_center_year == 2030:
            capital_cost += annualized_smr_kW * m.smr_cap
        
        # Annual operating costs (fuel + VOM + grid purchases)
        operating_cost = sum(
            (cost_ngcc_fuel_kWh + cost_ngcc_vom_kWh) * m.ngcc_gen[t] +
            (cost_ngcc_ccs_fuel_kWh + cost_ngcc_ccs_vom_kWh) * m.ngcc_ccs_gen[t] +
            (cost_ngct_fuel_kWh + cost_ngct_vom_kWh) * m.ngct_gen[t] +
            (cost_coal_fuel_kWh + cost_coal_vom_kWh) * m.coal_gen[t] +
            (cost_coal_ccs_fuel_kWh + cost_coal_ccs_vom_kWh) * m.coal_ccs_gen[t] +
            (cost_nuclear_fuel_kWh + cost_nuclear_vom_kWh) * m.nuclear_gen[t] +
            hourly_elec_price[t-1] * m.grid_purchase[t]
            for t in m.T
        )
        
        # Add SMR operating costs if enabled
        if include_smr and data_center_year == 2030:
            operating_cost += sum((cost_smr_fuel_kWh + cost_smr_vom_kWh) * m.smr_gen[t] for t in m.T)
        
        return capital_cost + operating_cost
    
    model.objective = pyo.Objective(rule=objective_rule, sense=pyo.minimize)

    # ───────────────────────────────
    # SOLVE THE MODEL
    # ───────────────────────────────

    #  Define a solver with fallback options
    solver = None
    solver_name = None
    for solver_option in ['cbc', 'glpk', 'gurobi']:
        try:
            solver = pyo.SolverFactory(solver_option)
            if solver.available():
                solver_name = solver_option
                break
        except:
            continue
    
    if solver is None or not solver.available():
        raise RuntimeError(
            "No LP solver found. Please install Gurobi (recommended), CBC, or GLPK.\n"
            "To install CBC: conda install -c conda-forge coincbc\n"
            "To install GLPK: conda install -c conda-forge glpk"
        )

    #  Solve the optimization
    results = solver.solve(model, tee=False)
    
    # Check if solution is optimal
    if results.solver.termination_condition != pyo.TerminationCondition.optimal:
        print(f"WARNING: Solution not optimal for {scenario_name}")
        return None

    # ───────────────────────────────
    # EXTRACT RESULTS
    # ───────────────────────────────
    
    solar_cap = pyo.value(model.solar_cap)
    wind_cap = pyo.value(model.wind_cap)
    ngcc_cap = pyo.value(model.ngcc_cap)
    ngcc_ccs_cap = pyo.value(model.ngcc_ccs_cap)
    ngct_cap = pyo.value(model.ngct_cap)
    coal_cap = pyo.value(model.coal_cap)
    coal_ccs_cap = pyo.value(model.coal_ccs_cap)
    nuclear_cap = pyo.value(model.nuclear_cap)
    smr_cap = pyo.value(model.smr_cap)
    
    # Calculate total generation from each source
    total_renewable_gen = solar_cap + wind_cap
    total_thermal_gen = ngcc_cap + ngcc_ccs_cap + ngct_cap + coal_cap + coal_ccs_cap + nuclear_cap + smr_cap
    total_gen_cap = total_renewable_gen + total_thermal_gen
    
    # Calculate annual operating costs
    annual_fuel_cost = sum(
        pyo.value(model.ngcc_gen[t]) * cost_ngcc_fuel_kWh +
        pyo.value(model.ngcc_ccs_gen[t]) * cost_ngcc_ccs_fuel_kWh +
        pyo.value(model.ngct_gen[t]) * cost_ngct_fuel_kWh +
        pyo.value(model.coal_gen[t]) * cost_coal_fuel_kWh +
        pyo.value(model.coal_ccs_gen[t]) * cost_coal_ccs_fuel_kWh +
        pyo.value(model.nuclear_gen[t]) * cost_nuclear_fuel_kWh +
        (pyo.value(model.smr_gen[t]) * cost_smr_fuel_kWh if include_smr and data_center_year == 2030 else 0)
        for t in model.T
    )
    
    annual_vom_cost = sum(
        pyo.value(model.ngcc_gen[t]) * cost_ngcc_vom_kWh +
        pyo.value(model.ngcc_ccs_gen[t]) * cost_ngcc_ccs_vom_kWh +
        pyo.value(model.ngct_gen[t]) * cost_ngct_vom_kWh +
        pyo.value(model.coal_gen[t]) * cost_coal_vom_kWh +
        pyo.value(model.coal_ccs_gen[t]) * cost_coal_ccs_vom_kWh +
        pyo.value(model.nuclear_gen[t]) * cost_nuclear_vom_kWh +
        (pyo.value(model.smr_gen[t]) * cost_smr_vom_kWh if include_smr and data_center_year == 2030 else 0)
        for t in model.T
    )
    
    # Calculate annual CAPEX (annualized capital costs only)
    annual_capex_cost = (
        occ_solar_kW * crf_solar * solar_cap +
        occ_wind_kW * crf_wind * wind_cap +
        occ_batt_power_kW * crf_batt_power * pyo.value(model.batt_power) +
        occ_batt_energy_kWh * crf_batt_energy * pyo.value(model.batt_energy) +
        occ_ngcc_kW * crf_ngcc * ngcc_cap +
        occ_ngcc_ccs_kW * crf_ngcc_ccs * ngcc_ccs_cap +
        occ_ngct_kW * crf_ngct * ngct_cap +
        occ_coal_kW * crf_coal * coal_cap +
        occ_coal_ccs_kW * crf_coal_ccs * coal_ccs_cap +
        occ_nuclear_kW * crf_nuclear * nuclear_cap
    )
    
    if include_smr and data_center_year == 2030:
        annual_capex_cost += occ_smr_kW * crf_smr * smr_cap
    
    # Calculate annual FOM (fixed O&M costs)
    annual_fom_cost = (
        fom_solar_kW_year * solar_cap +
        fom_wind_kW_year * wind_cap +
        fom_batt_power_kW_year * pyo.value(model.batt_power) +
        # No FOM for battery energy (already accounted for in power)
        fom_ngcc_kW_year * ngcc_cap +
        fom_ngcc_ccs_kW_year * ngcc_ccs_cap +
        fom_ngct_kW_year * ngct_cap +
        fom_coal_kW_year * coal_cap +
        fom_coal_ccs_kW_year * coal_ccs_cap +
        fom_nuclear_kW_year * nuclear_cap
    )
    
    if include_smr and data_center_year == 2030:
        annual_fom_cost += fom_smr_kW_year * smr_cap
    
    annual_grid_cost = sum(pyo.value(model.grid_purchase[t]) * hourly_elec_price[t-1] for t in model.T)
    
    # Calculate annual emissions from grid purchases (kg CO2e)
    # Grid purchases are in kW, emissions factors are in kg/MWh
    # Convert: kW * (1 MWh / 1000 kW) * (kg CO2e / MWh) = kg CO2e, then divide by 1000 for tonnes
    emissions_from_grid = sum(
        (pyo.value(model.grid_purchase[t]) / 1000) * lrmer_co2e_kg_per_MWh[t-1]
        for t in model.T
    ) / 1000
    
    # Calculate emissions from generation technologies (kg CO2e per MWh * MWh generated, then convert to tonnes)
    emissions_from_ngcc = sum(
        (pyo.value(model.ngcc_gen[t]) / 1000) * emissions_ngcc_kg_per_MWh
        for t in model.T
    ) / 1000
    
    emissions_from_ngcc_ccs = sum(
        (pyo.value(model.ngcc_ccs_gen[t]) / 1000) * emissions_ngcc_ccs_kg_per_MWh
        for t in model.T
    ) / 1000
    
    emissions_from_ngct = sum(
        (pyo.value(model.ngct_gen[t]) / 1000) * emissions_ngct_kg_per_MWh
        for t in model.T
    ) / 1000
    
    emissions_from_coal = sum(
        (pyo.value(model.coal_gen[t]) / 1000) * emissions_coal_kg_per_MWh
        for t in model.T
    ) / 1000
    
    emissions_from_coal_ccs = sum(
        (pyo.value(model.coal_ccs_gen[t]) / 1000) * emissions_coal_ccs_kg_per_MWh
        for t in model.T
    ) / 1000
    
    # Clamp tiny numerical noise
    emissions_from_grid = clamp_tonnes(emissions_from_grid)
    emissions_from_ngcc = clamp_tonnes(emissions_from_ngcc)
    emissions_from_ngcc_ccs = clamp_tonnes(emissions_from_ngcc_ccs)
    emissions_from_ngct = clamp_tonnes(emissions_from_ngct)
    emissions_from_coal = clamp_tonnes(emissions_from_coal)
    emissions_from_coal_ccs = clamp_tonnes(emissions_from_coal_ccs)
    
    total_emissions = clamp_tonnes(
        emissions_from_grid + emissions_from_ngcc + emissions_from_ngcc_ccs +
        emissions_from_ngct + emissions_from_coal + emissions_from_coal_ccs
    )
    
    result_dict = {
        'Scenario': scenario_name,
        'County_FIPS': county_fips,
        'County_Name': county_name,
        'State': state_abbrev,
        'Solar_Capacity_kW': clamp_kw(solar_cap),
        'Wind_Capacity_kW': clamp_kw(wind_cap),
        'Battery_Power_kW': clamp_kw(pyo.value(model.batt_power)),
        'Battery_Energy_kWh': clamp_kwh(pyo.value(model.batt_energy)),
        'NGCC_Capacity_kW': clamp_kw(ngcc_cap),
        'NGCC_CCS_Capacity_kW': clamp_kw(ngcc_ccs_cap),
        'NGCT_Capacity_kW': clamp_kw(ngct_cap),
        'Coal_Capacity_kW': clamp_kw(coal_cap),
        'Coal_CCS_Capacity_kW': clamp_kw(coal_ccs_cap),
        'Nuclear_Capacity_kW': clamp_kw(nuclear_cap),
        'SMR_Capacity_kW': clamp_kw(smr_cap),
        'Total_Generation_Capacity_kW': clamp_kw(total_gen_cap),
        'Annual_Electricity_Cost_USD': annual_grid_cost,
        'Annual_CAPEX_Cost_USD': annual_capex_cost,
        'Annual_FOM_Cost_USD': annual_fom_cost,
        'Annual_Fuel_Cost_USD': annual_fuel_cost,
        'Annual_VOM_Cost_USD': annual_vom_cost,
        'Total_Annual_Cost_USD': pyo.value(model.objective),
        'Annual_NGCC_Emissions_kg_CO2e': clamp_tonnes(emissions_from_ngcc * 1000),
        'Annual_NGCC_Emissions_tonnes_CO2e': emissions_from_ngcc,
        'Annual_NGCC_CCS_Emissions_kg_CO2e': clamp_tonnes(emissions_from_ngcc_ccs * 1000),
        'Annual_NGCC_CCS_Emissions_tonnes_CO2e': emissions_from_ngcc_ccs,
        'Annual_NGCT_Emissions_kg_CO2e': clamp_tonnes(emissions_from_ngct * 1000),
        'Annual_NGCT_Emissions_tonnes_CO2e': emissions_from_ngct,
        'Annual_Coal_Emissions_kg_CO2e': clamp_tonnes(emissions_from_coal * 1000),
        'Annual_Coal_Emissions_tonnes_CO2e': emissions_from_coal,
        'Annual_Coal_CCS_Emissions_kg_CO2e': clamp_tonnes(emissions_from_coal_ccs * 1000),
        'Annual_Coal_CCS_Emissions_tonnes_CO2e': emissions_from_coal_ccs,
        'Annual_Grid_Emissions_kg_CO2e': clamp_tonnes(emissions_from_grid * 1000),
        'Annual_Grid_Emissions_tonnes_CO2e': emissions_from_grid,
        'Annual_Total_Emissions_kg_CO2e': clamp_tonnes(total_emissions * 1000),
        'Annual_Total_Emissions_tonnes_CO2e': total_emissions
    }
    
    # Print results
    if solar_cap > 0:
        print(f"Solar capacity:      {solar_cap:10.2f} kW")
    if wind_cap > 0:
        print(f"Wind capacity:       {wind_cap:10.2f} kW")
    if ngcc_cap > 0:
        print(f"NGCC capacity:       {ngcc_cap:10.2f} kW")
    if ngcc_ccs_cap > 0:
        print(f"NGCC-CCS capacity:   {ngcc_ccs_cap:10.2f} kW")
    if ngct_cap > 0:
        print(f"NGCT capacity:       {ngct_cap:10.2f} kW")
    if coal_cap > 0:
        print(f"Coal capacity:       {coal_cap:10.2f} kW")
    if coal_ccs_cap > 0:
        print(f"Coal-CCS capacity:   {coal_ccs_cap:10.2f} kW")
    if nuclear_cap > 0:
        print(f"Nuclear capacity:    {nuclear_cap:10.2f} kW")
    if smr_cap > 0:
        print(f"SMR capacity:        {smr_cap:10.2f} kW")
    if pyo.value(model.batt_power) > 0:
        print(f"Battery power:       {pyo.value(model.batt_power):10.2f} kW")
        print(f"Battery energy:      {pyo.value(model.batt_energy):10.2f} kWh")
    if total_gen_cap > 0:
        print(f"Total generation:    {total_gen_cap:10.2f} kW")
    if annual_grid_cost > 0:
        print(f"Annual grid cost:    ${annual_grid_cost:,.0f}")
    if annual_fuel_cost > 0:
        print(f"Annual fuel cost:    ${annual_fuel_cost:,.0f}")
    if annual_vom_cost > 0:
        print(f"Annual VOM cost:     ${annual_vom_cost:,.0f}")
    if emissions_from_ngcc > 0:
        print(f"Annual NGCC emissions: {emissions_from_ngcc:,.1f} tonnes CO2e")
    if emissions_from_ngcc_ccs > 0:
        print(f"Annual NGCC-CCS emissions: {emissions_from_ngcc_ccs:,.1f} tonnes CO2e")
    if emissions_from_ngct > 0:
        print(f"Annual NGCT emissions: {emissions_from_ngct:,.1f} tonnes CO2e")
    if emissions_from_coal > 0:
        print(f"Annual Coal emissions: {emissions_from_coal:,.1f} tonnes CO2e")
    if emissions_from_coal_ccs > 0:
        print(f"Annual Coal-CCS emissions: {emissions_from_coal_ccs:,.1f} tonnes CO2e")
    if emissions_from_grid > 0:
        print(f"Annual Grid emissions: {emissions_from_grid:,.1f} tonnes CO2e")
    if total_emissions > 0:
        print(f"TOTAL annual emissions: {total_emissions:,.1f} tonnes CO2e")
    print(f"Total annual cost:   ${pyo.value(model.objective):,.0f}")
    
    return result_dict

# 4. RUN OPTIMIZATION WITH SELECTED TECHNOLOGIES

results_list = []

# Collect enabled generation technologies (excluding battery and grid)
generation_techs = []
if enable_solar:
    generation_techs.append(('Solar', enable_solar, False, False, False, False, False, False, False, False))
if enable_wind:
    generation_techs.append(('Wind', False, enable_wind, False, False, False, False, False, False, False))
if enable_ngcc:
    generation_techs.append(('NGCC', False, False, True, False, False, False, False, False, False))
if enable_ngcc_ccs:
    generation_techs.append(('NGCC-CCS', False, False, False, True, False, False, False, False, False))
if enable_ngct:
    generation_techs.append(('NGCT', False, False, False, False, True, False, False, False, False))
if enable_coal:
    generation_techs.append(('Coal', False, False, False, False, False, True, False, False, False))
if enable_coal_ccs:
    generation_techs.append(('Coal-CCS', False, False, False, False, False, False, True, False, False))
if enable_nuclear:
    generation_techs.append(('Nuclear', False, False, False, False, False, False, False, True, False))
if enable_smr and data_center_year == 2030:
    generation_techs.append(('SMR', False, False, False, False, False, False, False, False, True))

if len(generation_techs) == 0 and not enable_grid:
    raise ValueError("No technologies enabled. Please enable at least one generation technology or grid option.")

if run_individually:
    # Run each enabled technology as a separate scenario
    for tech_name, inc_solar, inc_wind, inc_ngcc, inc_ngcc_ccs, inc_ngct, inc_coal, inc_coal_ccs, inc_nuclear, inc_smr in generation_techs:
        # For renewable technologies, include battery if available
        # For thermal/dispatchable technologies, battery is optional (model decides)
        if tech_name in ['Solar', 'Wind']:
            scenario_name = f"{tech_name}+Battery" if battery_available else tech_name
        else:
            scenario_name = tech_name
        
        result = build_and_solve_model(
            scenario_name,
            include_solar=inc_solar,
            include_wind=inc_wind,
            include_battery=battery_available if tech_name in ['Solar', 'Wind'] else battery_available,
            include_ngcc=inc_ngcc,
            include_ngcc_ccs=inc_ngcc_ccs,
            include_ngct=inc_ngct,
            include_coal=inc_coal,
            include_coal_ccs=inc_coal_ccs,
            include_nuclear=inc_nuclear,
            include_smr=inc_smr,
            include_grid=False  # Don't include grid in individual scenarios
        )
        if result:
            results_list.append(result)
    
    # Add grid-only scenario if enabled
    if enable_grid:
        scenario_name = "Grid"
        result = build_and_solve_model(
            scenario_name,
            include_solar=False,
            include_wind=False,
            include_battery=False,
            include_ngcc=False,
            include_ngcc_ccs=False,
            include_ngct=False,
            include_coal=False,
            include_coal_ccs=False,
            include_nuclear=False,
            include_smr=False,
            include_grid=True
        )
        if result:
            results_list.append(result)
else:
    # Run all enabled technologies together in one optimization
    enabled_techs = []
    if enable_solar:
        enabled_techs.append("Solar")
    if enable_wind:
        enabled_techs.append("Wind")
    if battery_available:
        enabled_techs.append("Battery")
    if enable_ngcc:
        enabled_techs.append("NGCC")
    if enable_ngcc_ccs:
        enabled_techs.append("NGCC-CCS")
    if enable_ngct:
        enabled_techs.append("NGCT")
    if enable_coal:
        enabled_techs.append("Coal")
    if enable_coal_ccs:
        enabled_techs.append("Coal-CCS")
    if enable_nuclear:
        enabled_techs.append("Nuclear")
    if enable_smr and data_center_year == 2030:
        enabled_techs.append("SMR")
    if enable_grid:
        enabled_techs.append("Grid")
    
    scenario_name = "+".join(enabled_techs)
    
    result = build_and_solve_model(
        scenario_name,
        include_solar=enable_solar,
        include_wind=enable_wind,
        include_battery=battery_available,
        include_ngcc=enable_ngcc,
        include_ngcc_ccs=enable_ngcc_ccs,
        include_ngct=enable_ngct,
        include_coal=enable_coal,
        include_coal_ccs=enable_coal_ccs,
        include_nuclear=enable_nuclear,
        include_smr=enable_smr and data_center_year == 2030,
        include_grid=enable_grid
    )
    
    if result:
        results_list.append(result)

# 5. SAVE RESULTS TO CSV

if len(results_list) == 0:
    raise ValueError("No scenarios were successfully solved. Check your scenarios_to_run list and model inputs.")

results_df = pd.DataFrame(results_list)

# Reorder columns
base_columns = ['Scenario', 'County_FIPS', 'County_Name', 'State']
capacity_columns = ['Solar_Capacity_kW', 'Wind_Capacity_kW', 'Battery_Power_kW', 'Battery_Energy_kWh',
                   'NGCC_Capacity_kW', 'NGCC_CCS_Capacity_kW', 'NGCT_Capacity_kW', 
                   'Coal_Capacity_kW', 'Coal_CCS_Capacity_kW', 'Nuclear_Capacity_kW', 'SMR_Capacity_kW',
                   'Total_Generation_Capacity_kW']
cost_columns = ['Annual_Electricity_Cost_USD', 'Annual_CAPEX_Cost_USD', 'Annual_FOM_Cost_USD', 'Annual_Fuel_Cost_USD', 'Annual_VOM_Cost_USD', 'Total_Annual_Cost_USD']
emissions_columns = ['Annual_Grid_Emissions_kg_CO2e', 'Annual_Grid_Emissions_tonnes_CO2e']

# Only include columns that exist in results_df
column_order = base_columns + [col for col in capacity_columns if col in results_df.columns] + \
               [col for col in cost_columns if col in results_df.columns] + \
               [col for col in emissions_columns if col in results_df.columns]
results_df = results_df[column_order]

# Create output directory if it doesn't exist
os.makedirs("output", exist_ok=True)

output_file = f"output/results_county_{county_fips}.csv"
results_df.to_csv(output_file, index=False)

print(f"\n{'-'*60}")
print(f"All optimizations complete!")
print(f"Results saved to: {output_file}")
print(f"{'-'*60}")
print("\nSummary:")
print(results_df.to_string(index=False))

