import uuid
import json
import io
import qrcode
from firebase_admin import firestore

def get_or_create_queue(db, salon_id, date_str=None):
    """Get queue doc for a salon and date, create if doesn't exist."""
    import datetime
    if not date_str:
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    doc_id = f"{salon_id}_{date_str}"
    ref = db.collection('queues').document(doc_id)
    doc = ref.get()
    
    if doc.exists:
        return ref, doc.to_dict()

    initial = {
        "salon_id": salon_id,
        "date": date_str,
        "current_token": 0,
        "last_token": 0,
        "qr_session": str(uuid.uuid4())[:8],
        "status": "active",
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    ref.set(initial)
    return ref, initial

def assign_token(db, salon_id, date_str=None):
    """Assign next token number for a specific date. Each day starts from 1."""
    ref, queue = get_or_create_queue(db, salon_id, date_str)
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

def advance_queue(db, salon_id, date_str=None):
    """Move to next token for a specific date (default today)."""
    ref, queue = get_or_create_queue(db, salon_id, date_str)
    current = queue.get('current_token', 0)
    last = queue.get('last_token', 0)
    if not date_str:
        import datetime
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')

    if current >= last:
        return None, "No more tokens in queue"

    new_current = current + 1
    new_session = str(uuid.uuid4())[:8]

    ref.update({
        "current_token": new_current,
        "qr_session": new_session,
        "updated_at": firestore.SERVER_TIMESTAMP
    })

    # Filter bookings by date to avoid massive scans
    all_bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id)
        .where('date', '==', date_str).stream())

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

def skip_token(db, salon_id, date_str=None):
    """Skip current token (no-show), mark as no_show."""
    ref, queue = get_or_create_queue(db, salon_id, date_str)
    current = queue.get('current_token', 0)
    if not date_str:
        import datetime
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    # Mark current as no_show
    bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id)
        .where('date', '==', date_str)
        .where('token_number', '==', current)
        .stream())
    for b in bookings:
        if b.to_dict().get('status') not in ['completed', 'cancelled']:
            b.reference.update({"status": "no_show"})
    
    return advance_queue(db, salon_id, date_str)

def finish_current_token(db, salon_id, date_str=None):
    """Mark the current token as completed explicitly without advancing."""
    ref, queue = get_or_create_queue(db, salon_id, date_str)
    current = queue.get('current_token', 0)
    if not date_str:
        import datetime
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    if current == 0:
        return None, "No active customer to finish"

    all_bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id)
        .where('date', '==', date_str).stream())
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
    Scans always happen on current date.
    """
    import datetime
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    ref, queue = get_or_create_queue(db, salon_id, today_str)
    current = queue.get('current_token', 0)
    last = queue.get('last_token', 0)
    qr_session = queue.get('qr_session', '')
    next_token = current + 1

    if session != qr_session:
        return None, "Invalid or expired QR code"
    if last == 0:
        return None, "No bookings in queue"
    if token_number < next_token:
        return None, f"Token #{token_number} has already been called"
    if token_number > next_token:
        return None, f"Please wait. Now calling #{next_token}, you are #{token_number}"

    # Mark the previous token as completed if it was in_progress
    all_bookings = list(db.collection('bookings')
        .where('salon_id', '==', salon_id)
        .where('date', '==', today_str).stream())
    target_booking = None
    for b in all_bookings:
        b_dict = b.to_dict()
        if b_dict.get('token_number') == current and b_dict.get('status') in ['arrived', 'in_progress']:
            b.reference.update({"status": "completed"})
        if b_dict.get('token_number') == token_number:
            target_booking = b

    if not target_booking:
        return None, "No booking found for this token"

    target_booking.reference.update({
        "status": "in_progress",
        "qr_scanned_at": firestore.SERVER_TIMESTAMP
    })

    # Auto-advance queue
    new_session = str(uuid.uuid4())[:8]
    ref.update({
        "current_token": token_number,
        "qr_session": new_session,
        "updated_at": firestore.SERVER_TIMESTAMP
    })

    # Notify upcoming customers
    try:
        from utils.notification_helpers import notify_queue_update
        notify_queue_update(db, salon_id, token_number)
    except Exception as e:
        print(f"Notification error in scan: {e}")

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
