import os
import json
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore, messaging

# 🔥 1. INITIALIZE FIREBASE  (mirrors app.py — uses env var on Render, falls back to local file)
FIREBASE_KEY_JSON = (
    os.environ.get("FIREBASE_KEY_JSON")
    or os.environ.get("FIREBASE_CREDENTIALS")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
)
FIREBASE_KEY_PATH = os.environ.get(
    "FIREBASE_KEY_PATH",
    os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")
)

try:
    # Check if Firebase is already initialized (avoids crash when imported from app.py)
    if not firebase_admin._apps:
        if FIREBASE_KEY_JSON:
            key_data = json.loads(FIREBASE_KEY_JSON)
            cred = credentials.Certificate(key_data)
        elif os.path.exists(FIREBASE_KEY_PATH):
            cred = credentials.Certificate(FIREBASE_KEY_PATH)
        else:
            raise FileNotFoundError(f"No Firebase credentials found (tried env var and {FIREBASE_KEY_PATH})")
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Database connected for Reminder Cron Job.")
except Exception as e:
    print(f"Firebase error: {e}")
    exit()

def run_reminder_job():
    """
    Search for bookings happening exactly within the next 1 Hour and send a Free FCM Push Notification
    """
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    target_time_str = (now + timedelta(hours=1)).strftime('%H:%M')

    print(f"Scanning for verified bookings on {today_str} scheduled near {target_time_str}...")

    bookings = db.collection('bookings')\
        .where('date', '==', today_str)\
        .where('status', '==', 'confirmed')\
        .stream()

    for b in bookings:
        booking = b.to_dict()
        b_time = booking.get('time')
        
        # Compare full HH:MM (not just hour prefix which mismatches '1' vs '10','11','12')
        if b_time and b_time[:5] == target_time_str[:5]:
            
            user_doc = db.collection('users').document(booking.get('user_id')).get()
            if not user_doc.exists: continue
            
            user_data = user_doc.to_dict()
            fcm_token = user_data.get('fcm_token')
            
            salon_name = "The Salon"
            s_doc = db.collection('salons').document(booking.get('salon_id')).get()
            if s_doc.exists:
                salon_name = s_doc.to_dict().get('name')

            # SEND PUSH NOTIFICATION (FCM) - THIS IS 100% FREE!
            if fcm_token:
                try:
                    msg = messaging.Message(
                        notification=messaging.Notification(
                            title="Appointment Reminder", 
                            body=f"Your appointment at {salon_name} is coming up at {b_time}! Please arrive on time as slots reflect service durations."
                        ),
                        token=fcm_token
                    )
                    messaging.send(msg)
                    print(f"Sent Push Notification reminder to {user_data.get('name')}")
                except Exception as e:
                    print(f"Failed to send push notification: {e}")

def send_morning_reminders():
    """Notify users about their appointments scheduled for today."""
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    print(f"Sending morning reminders for {today_str}...")

    bookings = db.collection('bookings')\
        .where('date', '==', today_str)\
        .where('status', '==', 'confirmed')\
        .stream()

    for b in bookings:
        booking = b.to_dict()
        fcm_token = booking.get('fcm_token')
        token_num = booking.get('token_number')
        
        if not fcm_token:
            # Try to get from user doc if missing from booking
            user_doc = db.collection('users').document(booking.get('user_id')).get()
            if user_doc.exists:
                fcm_token = user_doc.to_dict().get('fcm_token')

        if fcm_token:
            salon_name = "the Salon"
            s_doc = db.collection('salons').document(booking.get('salon_id')).get()
            if s_doc.exists:
                salon_name = s_doc.to_dict().get('name')

            try:
                msg = messaging.Message(
                    notification=messaging.Notification(
                        title="Today's Appointment 📅", 
                        body=f"You have an appointment at {salon_name} today! Your token number is #{token_num}. We'll notify you when it's almost your turn."
                    ),
                    token=fcm_token
                )
                messaging.send(msg)
                print(f"Sent morning reminder for Token #{token_num}")
            except Exception as e:
                print(f"Failed: {e}")

if __name__ == "__main__":
    # If run in the morning (e.g., 8-9 AM), send morning reminders
    if 8 <= datetime.now().hour <= 10:
        send_morning_reminders()
    else:
        run_reminder_job()
