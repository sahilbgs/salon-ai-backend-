import uuid
import json
import io
import qrcode
from firebase_admin import firestore

def get_or_create_queue(db, salon_id):
    """Get queue doc for a salon, create if doesn't exist. Resets daily."""
    import datetime
    ref = db.collection('queues').document(salon_id)
    doc = ref.get()
    
    now = datetime.datetime.now()
    
    if doc.exists:
        data = doc.to_dict()
        last_update = data.get('updated_at')
        
        # Check if reset is needed (different day)
        is_new_day = False
        if last_update:
            # Handle both datetime objects and timestamps
            if isinstance(last_update, datetime.datetime):
                ut = last_update
            else:
                # Firestore timestamp helper usually returns datetime in Python SDK
                ut = last_update
            
            if ut.date() != now.date():
                is_new_day = True
        
        if is_new_day:
            reset_data = {
                "current_token": 0,
                "last_token": 0,
                "qr_session": str(uuid.uuid4())[:8],
                "updated_at": firestore.SERVER_TIMESTAMP
            }
            ref.update(reset_data)
            return ref, {**data, **reset_data}
            
        return ref, data

    initial = {
        "salon_id": salon_id,
        "current_token": 0,
        "last_token": 0,
        "qr_session": str(uuid.uuid4())[:8],
        "status": "active",
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    ref.set(initial)
    return ref, initial

def assign_token(db, salon_id):
    """Assign next token number for a new booking. Ensures qr_session exists for QR display."""
    ref, queue = get_or_create_queue(db, salon_id)
    new_token = queue.get('last_token', 0) + 1
    update = {
        "last_token": new_token,
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    # Ensure qr_session is set so QR displays immediately for token #1
    if not queue.get('qr_session'):
        update["qr_session"] = str(uuid.uuid4())[:8]
    ref.update(update)
    return new_token

def advance_queue(db, salon_id):
    """Move to next token, rotate QR session."""
    ref, queue = get_or_create_queue(db, salon_id)
    current = queue.get('current_token', 0)
    last = queue.get('last_token', 0)

    if current >= last:
        return None, "No more tokens in queue"

    new_current = current + 1
    new_session = str(uuid.uuid4())[:8]

    ref.update({
        "current_token": new_current,
        "qr_session": new_session,
        "updated_at": firestore.SERVER_TIMESTAMP
    })

    # Query by salon_id only, filter token + status in Python (avoids composite index)
    all_bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id).stream())

    # Mark previous token as completed if exists
    for b in all_bookings:
        bd = b.to_dict()
        if bd.get('token_number') == current and bd.get('status') in ['arrived', 'in_progress']:
            b.reference.update({"status": "completed"})

    # Mark new current as in_progress
    for b in all_bookings:
        bd = b.to_dict()
        if bd.get('token_number') == new_current and bd.get('status') in ['confirmed', 'arrived']:
            b.reference.update({"status": "in_progress"})

    # Notify upcoming customers
    try:
        from utils.notification_helpers import notify_queue_update
        notify_queue_update(db, salon_id, new_current)
    except Exception as e:
        print(f"Notification error: {e}")

    return {"current_token": new_current, "qr_session": new_session, "last_token": last}, None

def skip_token(db, salon_id):
    """Skip current token (no-show), mark as no_show."""
    ref, queue = get_or_create_queue(db, salon_id)
    current = queue.get('current_token', 0)
    
    # Mark current as no_show
    bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id)
        .where('token_number', '==', current)
        .stream())
    for b in bookings:
        if b.to_dict().get('status') not in ['completed', 'cancelled']:
            b.reference.update({"status": "no_show"})
    
    return advance_queue(db, salon_id)

def finish_current_token(db, salon_id):
    """Mark the current token as completed explicitly without advancing."""
    ref, queue = get_or_create_queue(db, salon_id)
    current = queue.get('current_token', 0)
    
    if current == 0:
        return None, "No active customer to finish"

    all_bookings = list(db.collection('bookings').where('salon_id', '==', salon_id).stream())
    found = False
    for b in all_bookings:
        bd = b.to_dict()
        if bd.get('token_number') == current and bd.get('status') in ['arrived', 'in_progress']:
            b.reference.update({"status": "completed"})
            found = True

    if not found:
        return None, f"Token #{current} is already completed or not active"

    return {"message": "Current service completed", "current_token": current}, None

def validate_qr_scan(db, salon_id, token_number, session):
    """Validate a QR scan. QR encodes the NEXT token to be served (current+1).
    On success: mark booking as in_progress, advance queue so QR rotates to current+2.
    """
    ref, queue = get_or_create_queue(db, salon_id)
    current = queue.get('current_token', 0)
    last = queue.get('last_token', 0)
    qr_session = queue.get('qr_session', '')
    next_token = current + 1  # The token the QR is displayed for

    if session != qr_session:
        return None, "Invalid or expired QR code"
    if last == 0:
        return None, "No bookings in queue"
    if token_number < next_token:
        return None, f"Token #{token_number} has already been called"
    if token_number > next_token:
        return None, f"Please wait. Now calling #{next_token}, you are #{token_number}"

    # Mark the previous token as completed if it was in_progress
    all_bookings = list(db.collection('bookings').where('salon_id', '==', salon_id).stream())
    target_booking = None
    for b in all_bookings:
        b_dict = b.to_dict()
        # Mark old token as completed
        if b_dict.get('token_number') == current and b_dict.get('status') in ['arrived', 'in_progress']:
            b.reference.update({"status": "completed"})
        # Identify the new token to mark as in_progress
        if b_dict.get('token_number') == token_number:
            target_booking = b

    if not target_booking:
        return None, "No booking found for this token"

    target_booking.reference.update({
        "status": "in_progress",
        "qr_scanned_at": firestore.SERVER_TIMESTAMP
    })

    # Auto-advance queue: current_token → token_number, generate new session for next customer
    new_session = str(uuid.uuid4())[:8]
    ref.update({
        "current_token": token_number,
        "qr_session": new_session,
        "updated_at": firestore.SERVER_TIMESTAMP
    })

    return {
        "message": f"✅ Token #{token_number} — You are now being served!",
        "token": token_number,
        "next_token": token_number + 1 if token_number < last else None
    }, None

def generate_qr_image(salon_id, token, session):
    """Generate QR code image as bytes."""
    qr_data = json.dumps({
        "salon_id": salon_id,
        "token": token,
        "session": session
    })
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="white", back_color="#0f0c29")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf
