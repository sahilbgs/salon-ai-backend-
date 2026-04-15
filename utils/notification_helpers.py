from firebase_admin import messaging
from datetime import datetime

def send_push_notification(fcm_token, title, body, data=None):
    """Send a push notification to a specific device."""
    if not fcm_token:
        return False
    
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body
            ),
            data=data or {},
            token=fcm_token
        )
        response = messaging.send(message)
        print(f"Successfully sent message: {response}")
        return True
    except Exception as e:
        print(f"Error sending push notification: {e}")
        return False

def notify_queue_update(db, salon_id, current_token):
    """Notify upcoming customers in the queue."""
    # Notify 5 tokens ahead
    target5 = current_token + 5
    # Notify 2 tokens ahead
    target2 = current_token + 2
    
    # Query bookings for this salon today with these token numbers
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # Fetch upcoming bookings
    bookings = db.collection('bookings') \
        .where('salon_id', '==', salon_id) \
        .where('date', '==', today_str) \
        .where('token_number', 'in', [target2, target5]) \
        .stream()
        
    for b in bookings:
        data = b.to_dict()
        token_num = data.get('token_number')
        fcm_token = data.get('fcm_token')
        
        if not fcm_token:
            continue
            
        if token_num == target5:
            send_push_notification(
                fcm_token,
                "Head to Salon! 🏪",
                f"Only 5 people ahead of you (Token #{token_num}). Start heading to the salon now!"
            )
        elif token_num == target2:
            send_push_notification(
                fcm_token,
                "Almost your turn! 🏃‍♂️",
                f"Only 2 people ahead (Token #{token_num}). Reach the salon ASAP and get your QR ready!"
            )
