import requests
import urllib3
from tabulate import tabulate  # pip install tabulate

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_KEY = "68b5ec28de8abe4f99a77a5434e032fc78b8d2d8"
URL = "https://toppily.com/api/v1/fetch-data-packages"

headers = {
    "x-api-key": API_KEY
}

# Fetch the data
response = requests.get(URL, headers=headers, verify=False)
data = response.json()

# Prepare table rows
table_data = []
for pkg in data:
    table_data.append([
        pkg.get("id"),
        pkg.get("network"),
        pkg.get("network_id"),
        pkg.get("volume"),
        pkg.get("status")
    ])

# Print as table
print(tabulate(table_data, headers=["Package ID", "Network", "Network ID", "Volume", "Status"], tablefmt="grid"))
