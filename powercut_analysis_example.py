import pandas as pd
from influxdb import InfluxDBClient
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import config as conf

### Variables and files ---------
INPUT_CSV = "devices_locations.csv"
OUTPUT_CSV = "photometer_magtess_measurements.csv"
TIMEZONE = "Europe/Madrid"
MEASUREMENT = "sensor_measurement"
MAG_FIELD = "mag_field"
MAG_ERR = "mag_err"
INPUT_FILE = "photometers_data.csv"

VARIABLE_0 = conf.VARIABLE_0
VARIABLE_1 = conf.VARIABLE_1
VARIABLE_2 = conf.VARIABLE_2
LAT_MIN = conf.LAT_MIN
LAT_MAX = conf.LAT_MAX
LON_MIN = conf.LON_MIN
LON_MAX = conf.LON_MAX

### Load environment variables ------
load_dotenv()
host = os.getenv("HOST")
port = int(os.getenv("PORT"))
username = os.getenv("USER")
password = os.getenv("PASS")
database = os.getenv("DBNAME")

### Connect to InfluxDB ------------
client = InfluxDBClient(host=host, port=port, username=username, password=password, database=database)

### Load photometer info ----------
print("Loading data...")
df_photometers = pd.read_csv(INPUT_CSV)
print("Sample of original lat/lon values:")
print(df_photometers[['ID', 'Device', 'Lat', 'Lon', 'Place']].head(10))
print("Data types before conversion:")
print(df_photometers.dtypes)

### Check for column names and extract needed data --------
expected_cols = ['ID', 'Device', 'Lat', 'Lon', 'Place']
if not all(col in df_photometers.columns for col in expected_cols):
    raise ValueError(f"CSV file must contain columns: {expected_cols}")

df_photometers = df_photometers[['ID', 'Device', 'Lat', 'Lon', 'Place']]

### Check available photometers in InfluxDB database ----------
print("Checking available photometers in InfluxDB...")
query_names = f'SHOW TAG VALUES FROM "{MEASUREMENT}" WITH KEY = "name"'
names_result = client.query(query_names)
available_names = set(entry['value'] for entry in names_result.get_points())


### Keep only available photometers by matching 'id' or 'Device' ------
matched_rows = []
for _, row in df_photometers.iterrows():
    if row['ID'] in available_names or row['Device'] in available_names:
        print(f"found {row['ID']} or {row['Device']}")
        matched_rows.append(row)

df_photometers = pd.DataFrame(matched_rows)
print(f"Available photometers found: {len(df_photometers)}")

### Match photometers and create ID to Place map -----
matched_rows = []
id_to_location = {}
for _, row in df_photometers.iterrows():
    if row['ID'] in available_names or row['Device'] in available_names:
        print(f"found {row['ID']} or {row['Device']}")
        matched_rows.append(row)
        key = row['ID'] if row['ID'] in available_names else row['Device']
        id_to_location[key] = row['Place']

df_photometers = pd.DataFrame(matched_rows)
print(f"Available photometers found: {len(df_photometers)}")

### Replace comma decimal separator with period -----------
df_photometers['Lat'] = df_photometers['Lat'].str.replace(',', '.', regex=False)
df_photometers['Lon'] = df_photometers['Lon'].str.replace(',', '.', regex=False)

### Converting lat and lon to numeric format ----------
df_photometers['Lat'] = pd.to_numeric(df_photometers['Lat'], errors='coerce')
df_photometers['Lon'] = pd.to_numeric(df_photometers['Lon'], errors='coerce')

df_photometers = df_photometers[
    (df_photometers['Lat'] >= LAT_MIN) &
    (df_photometers['Lat'] <= LAT_MAX) &
    (df_photometers['Lon'] >= LON_MIN) &
    (df_photometers['Lon'] <= LON_MAX)
]

print(f"Photometers within bounds: {len(df_photometers)}")


### Define time range --------
now = datetime.now(pytz.timezone(TIMEZONE))
yesterday = now - timedelta(days=1)
start_time_local = yesterday.replace(hour=18, minute=0, second=0, microsecond=0)
end_time_local = now.replace(hour=18, minute=0, second=0, microsecond=0)

### Convert to UTC timestamps for InfluxDB -------
start_time_utc = start_time_local.astimezone(pytz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
end_time_utc = end_time_local.astimezone(pytz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

print(f"Querying data from {start_time_local} to {end_time_local} (Spain local time)")

### Query and collect measurements -----------
all_data = []

for _, row in df_photometers.iterrows():
    photometer_id = row['ID'] if row['ID'] in available_names else row['Device']
    query = f'''
    SELECT "{MAG_FIELD}", "{MAG_ERR}" FROM "{MEASUREMENT}"
    WHERE "name" = '{photometer_id}'
    AND time >= '{start_time_utc}' AND time <= '{end_time_utc}'
    '''
    result = client.query(query)
    points = list(result.get_points())

    if points:
        for point in points:
            all_data.append({
                'photometer_id': photometer_id,
                'time': point['time'],
                'mag': point.get(MAG_FIELD, None),
                'mag_err': point.get(MAG_ERR, None),
                'lat': row['Lat'],
                'lon': row['Lon'],
                'place': row['Place']
            })
        print(f"Data found for photometer {photometer_id} in the time window.")
    else:
        print(f"No data found for photometer {photometer_id} in the time window.")

### Save results --------
times = pd.to_datetime([d['time'] for d in all_data])
print(f"Queried data range according to available data: {times.min()} to {times.max()}")
print(f"Saving results to {OUTPUT_CSV}...")
df_result = pd.DataFrame(all_data)

if not df_result.empty:
    df_result['time'] = pd.to_datetime(df_result['time'])
    df_result = df_result.sort_values(by=['photometer_id', 'time'])
    df_result.to_csv(OUTPUT_CSV, index=False)
    print("Done!")
else:
    print("No data found for any photometer in the given time window.")

### Filtering -------
print("Applying filter ...")
df_result = df_result.set_index('time')
filtered_data = []

for photometer_id, group in df_result.groupby('photometer_id'):
    rolling_std = group['mag'].rolling(VARIABLE_0).std()
    condition = (rolling_std < VARIABLE_1)
    filtered_group = group[condition]
    filtered_data.append(filtered_group)

df_filtered = pd.concat(filtered_data).reset_index()
print(f"Data points after cloudlessness filtering: {len(df_filtered)}")

### Plotting -------
custom_labels = {
    'DEVICE1': 'Place1',
    'DEVICE2': 'Place2',
    'DEVICE3': 'Place3',
    # Add more as needed
}

if not df_filtered.empty:
    print("Plotting magnitude vs time ...")
    plt.figure(figsize=(12, 7))
    for name, group in df_filtered.groupby("photometer_id"):
        label = custom_labels.get(name, name)
        plt.errorbar(
            group['time'],
            group['mag'],
            yerr=group['mag_err'],
            fmt='o',
            label=label,
            capsize=2,
            markersize=4,
            linestyle='-',
            alpha=0.8
        )

    plt.xlabel("Local Time", size=15)
    plt.ylabel("Magnitude (mag/arcsec²)", size= 15)
    plt.title("Night Sky Brightness recorded in Spain during blackout with ground-based NeXT photometers", size=17)
    plt.legend(title="Location", loc='best', fontsize=12)
    plt.grid(True)
    plt.tight_layout()
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d/%m %H:%M'))
    plt.xticks(fontsize=12, rotation=0)
    plt.yticks(fontsize=12)

    ### Save the figure before showing it
    plt.savefig("photometer_plot.png", dpi=300, bbox_inches='tight')
    plt.show()
