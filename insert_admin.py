from db import db
from werkzeug.security import generate_password_hash

# Access the users collection
users_col = db["users"]

# Hardcoded credentials
admin_data = {
    "username": "admin",
    "password": generate_password_hash("1234"),  # Hashed version of '1234'
    "role": "admin",
    "name": "Administrator",
    "email": "admin@example.com",
    "status": "active"
}

# Check if admin already exists
existing = users_col.find_one({"username": "admin"})
if existing:
    print("Admin user already exists.")
else:
    users_col.insert_one(admin_data)
    print("✅ Admin user inserted.")
