from urllib.parse import quote_plus

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi


username = "zico_cybok"
password = "T7uF10RDgC5Im7Wp"
uri = (
    f"mongodb+srv://{quote_plus(username)}:{quote_plus(password)}"
    "@cluster0.a77dwo1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)

client = MongoClient(uri, server_api=ServerApi("1"))

try:
    client.admin.command("ping")
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print("MongoDB connection error:", e)

db = client["speedlink"]
