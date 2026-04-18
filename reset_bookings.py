"""
Reset script — deletes ALL bookings and queues from Firestore.
Keeps: users, salons, services, admins.
Run once from the backend directory.
"""
import os, sys
import firebase_admin
from firebase_admin import credentials, firestore

# Load credentials the same way app.py does
CRED_PATH = os.path.join(os.path.dirname(__file__), 'serviceAccountKey.json')
if not os.path.exists(CRED_PATH):
    print(f"ERROR: Credentials not found at {CRED_PATH}")
    sys.exit(1)

cred = credentials.Certificate(CRED_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

def delete_collection(col_name):
    docs = list(db.collection(col_name).stream())
    if not docs:
        print(f"  {col_name}: already empty")
        return
    for doc in docs:
        doc.reference.delete()
    print(f"  {col_name}: deleted {len(docs)} documents")

# BUG 23 FIX: Add confirmation prompt to prevent accidental data wipe
print("\n⚠️  WARNING: This will DELETE all bookings and queues from Firestore!")
print("   Users, salons, and services will NOT be affected.")
confirm = input("\nType 'DELETE' to confirm: ").strip()
if confirm != 'DELETE':
    print("Aborted. No data was changed.")
    sys.exit(0)

print("\nResetting Firestore data...")
print("Deleting bookings...")
delete_collection('bookings')
print("Deleting queues...")
delete_collection('queues')
print("\n Done! Users, salons, services untouched.")
