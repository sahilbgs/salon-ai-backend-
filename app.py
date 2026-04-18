import os
import math
import json
import time
import threading
import requests as http_requests
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_compress import Compress
import firebase_admin
from firebase_admin import credentials, auth, firestore, messaging
from datetime import datetime, timedelta
from services.queue_service import (
    get_or_create_queue, assign_token, advance_queue, 
    skip_token, validate_qr_scan, generate_qr_image
)
from services.ai_service import predict_wait, recommend_salons
from utils.auth_helpers import verify_token, get_user_role, haversine
# BUG 20 FIX: Single notification import — no more duplicate inline send_notification
from utils.notification_helpers import send_push_notification, notify_queue_update

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
CORS(app, resources={r"/api/*": {"origins": "*"}})
Compress(app)  # Shrink JSON responses by 60-80% with gzip compression

# ── Render free-plan keep-alive: ping /api/health every 14 min ──
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://salon-ai-backend-306m.onrender.com")

def _keep_alive():
    """Prevents Render free-plan spin-down by self-pinging every 14 minutes."""
    time.sleep(60)  # wait for app to fully boot first
    while True:
        try:
            if RENDER_URL:
                http_requests.get(f"{RENDER_URL}/api/health", timeout=10)
                print("[keep-alive] pinged /api/health")
        except Exception as e:
            print(f"[keep-alive] ping failed: {e}")
        time.sleep(840)  # 14 minutes

threading.Thread(target=_keep_alive, daemon=True).start()

# ── Simple 30-second in-memory cache for /api/salons (non-location requests) ──
_salons_cache = {"data": None, "ts": 0}
_salons_cache_lock = threading.Lock()  # BUG 11 FIX: Thread-safe cache access

# ── Per-salon detail cache (5 min TTL) ──
_salon_detail_cache = {}  # {salon_id: {"data": ..., "ts": ...}}
_salon_detail_lock = threading.Lock()
_SALON_DETAIL_TTL = 300  # 5 minutes

# ── Services cache (2 min TTL, keyed by salon_id) ──
_services_cache = {}  # {salon_id: {"data": [...], "ts": float}}
_services_cache_lock = threading.Lock()
_SERVICES_TTL = 120  # 2 minutes

FIREBASE_KEY_PATH = os.environ.get("FIREBASE_KEY_PATH", os.path.join(os.path.dirname(__file__), "serviceAccountKey.json"))
# Support multiple env var names for flexibility
FIREBASE_KEY_JSON = os.environ.get("FIREBASE_KEY_JSON") or os.environ.get("FIREBASE_CREDENTIALS") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

db = None
_init_method = "none"

try:
    if FIREBASE_KEY_JSON:
        print(f"Found Firebase JSON env var ({len(FIREBASE_KEY_JSON)} chars), parsing...")
        key_data = json.loads(FIREBASE_KEY_JSON)
        print(f"Parsed JSON successfully. Project: {key_data.get('project_id', 'unknown')}")
        cred = credentials.Certificate(key_data)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        _init_method = "env_var"
        print("Firebase Admin SDK initialized from environment variable.")
    elif os.path.exists(FIREBASE_KEY_PATH):
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        _init_method = "file"
        print("Firebase Admin SDK initialized from FIREBASE_KEY_PATH.")
    else:
        print(f"WARNING: Firebase key not found at {FIREBASE_KEY_PATH}")
        print(f"  FIREBASE_KEY_JSON set: {bool(os.environ.get('FIREBASE_KEY_JSON'))}")
        print(f"  FIREBASE_CREDENTIALS set: {bool(os.environ.get('FIREBASE_CREDENTIALS'))}")
        db = None
except json.JSONDecodeError as e:
    print(f"ERROR: Failed to parse Firebase JSON: {e}")
    db = None
except Exception as e:
    print(f"Error initializing Firebase: {e}")
    import traceback
    traceback.print_exc()
    db = None

@app.route('/api/health')
def health_check():
    return jsonify({
        "status": "ok",
        "db_initialized": db is not None,
        "init_method": _init_method,
        "env_vars_present": {
            "FIREBASE_KEY_JSON": bool(os.environ.get("FIREBASE_KEY_JSON")),
            "FIREBASE_CREDENTIALS": bool(os.environ.get("FIREBASE_CREDENTIALS")),
            "FIREBASE_KEY_PATH": os.environ.get("FIREBASE_KEY_PATH", "(default)")
        }
    }), 200

def send_notification(token, title, body):
    """Wrapper for backward compatibility — delegates to send_push_notification."""
    return send_push_notification(token, title, body)


def send_topic_notification(topic, title, body, data=None):
    """Send FCM notification to a topic (reaches all subscribed devices, even when app is closed)."""
    try:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    channel_id='luxe_salon_bookings',
                    default_sound=True,
                    default_vibrate_timings=True,
                )
            ),
            data=data or {},
            topic=topic,
        )
        messaging.send(msg)
        print(f"Topic notification sent to: {topic}")
    except Exception as e:
        print(f"FCM Topic Error: {e}")


# ==================== AUTH ====================

@app.route('/api/auth', methods=['POST'])
def update_user():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    data = request.json
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        ref = db.collection('users').document(uid)
        doc = ref.get()
        role = data.get('role', 'user')
        if not doc.exists:
            ref.set({
                "uid": uid, "name": decoded.get('name',''), "email": decoded.get('email',''),
                "fcm_token": data.get('fcm_token',''), "role": role,
                "created_at": firestore.SERVER_TIMESTAMP
            })
        else:
            updates = {"role": role}
            if data.get('fcm_token'): updates["fcm_token"] = data['fcm_token']
            ref.update(updates)
        return jsonify({"message": "OK", "role": role}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== CUSTOMER ====================

@app.route('/api/salons', methods=['GET'])
def get_salons():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if db is None: return jsonify({"error": "DB not initialized"}), 500

    # Return cached response for non-location requests (avoids Firestore reads)
    now = time.time()
    if not lat and not lng:
        with _salons_cache_lock:
            if _salons_cache["data"] and now - _salons_cache["ts"] < 30:
                return jsonify(_salons_cache["data"]), 200

    try:
        # Fetch all approved salons (we will sort by is_open in Python)
        salon_docs = list(db.collection('salons')
                         .where('status', '==', 'approved')
                         .stream())

        # ── Batch-fetch all queue docs in ONE read instead of N reads ──
        salon_ids = [doc.id for doc in salon_docs]
        today_str = datetime.now().strftime('%Y-%m-%d')
        queue_refs = [db.collection('queues').document(f"{sid}_{today_str}") for sid in salon_ids]
        queue_snap_map = {}
        if queue_refs:
            for q_doc in db.get_all(queue_refs):
                if q_doc.exists:
                    # BUG 13 FIX: Use rsplit to only strip trailing date suffix
                    salon_key = q_doc.id.rsplit(f"_{today_str}", 1)[0]
                    queue_snap_map[salon_key] = q_doc.to_dict()

        salons = []
        for doc in salon_docs:
            data = doc.to_dict()
            data['id'] = doc.id
            if lat and lng and 'location' in data:
                slat, slng = data['location'].get('lat'), data['location'].get('lng')
                # BUG 12 FIX: Use 'is not None' so lat/lng=0 (valid coords) aren't skipped
                data['distance'] = haversine(lat, lng, slat, slng) if (slat is not None and slng is not None) else float('inf')
            else:
                data['distance'] = float('inf')
            # Attach queue info from pre-fetched batch
            q = queue_snap_map.get(doc.id, {})
            data['queue_length'] = q.get('last_token', 0) - q.get('current_token', 0)
            data['current_token'] = q.get('current_token', 0)
            salons.append(data)

        # Sort: Open salons first, then by distance
        salons.sort(key=lambda x: (not x.get('is_open', True), x.get('distance', float('inf'))))

        # Update cache only for non-location responses
        if not lat and not lng:
            with _salons_cache_lock:
                _salons_cache.update({"data": salons, "ts": now})

        return jsonify(salons), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/salon/<salon_id>', methods=['GET'])
def get_salon(salon_id):
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    now = time.time()
    # Check in-memory cache first
    with _salon_detail_lock:
        cached = _salon_detail_cache.get(salon_id)
        if cached and now - cached['ts'] < _SALON_DETAIL_TTL:
            resp = jsonify(cached['data'])
            resp.headers['Cache-Control'] = 'public, max-age=60'
            return resp, 200
    try:
        salon_doc = db.collection('salons').document(salon_id).get()
        if not salon_doc.exists: return jsonify({"error": "Not found"}), 404
        data = salon_doc.to_dict()
        data['id'] = salon_doc.id
        services_docs = db.collection('services').where('salon_id', '==', salon_id).stream()
        data['services'] = [{"id": d.id, **d.to_dict()} for d in services_docs]
        _, q = get_or_create_queue(db, salon_id)
        data['queue'] = q
        # Store in cache
        with _salon_detail_lock:
            _salon_detail_cache[salon_id] = {"data": data, "ts": now}
        resp = jsonify(data)
        resp.headers['Cache-Control'] = 'public, max-age=60'
        return resp, 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/book', methods=['POST'])
def book_appointment():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    data = request.json
    salon_id, service_id = data.get('salon_id'), data.get('service_id')
    date, time_slot = data.get('date'), data.get('time')
    visitor_name = data.get('visitor_name', '').strip() or decoded.get('name', '')
    fcm_token = data.get('fcm_token')
    if not all([salon_id, service_id, date, time_slot]):
        return jsonify({"error": "Missing fields"}), 400
    try:
        salon_doc = db.collection('salons').document(salon_id).get()
        if not salon_doc.exists:
            return jsonify({"error": "Salon not found"}), 404
        
        salon_data = salon_doc.to_dict()
        
        # Check if today is a closed day
        booking_date_obj = datetime.strptime(date, '%Y-%m-%d')
        # Only allow today and tomorrow
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        if booking_date_obj.date() < today or booking_date_obj.date() > tomorrow:
            return jsonify({"error": "You can only book for today or tomorrow."}), 400

        # weekday() is 0-6 (Mon-Sun)
        if booking_date_obj.weekday() in salon_data.get('closed_days', []):
            return jsonify({"error": f"Sorry, the salon is closed on this day ({booking_date_obj.strftime('%A')})"}), 400
        # No slot conflict check — shifts allow multiple bookings.
        # Everyone gets their own token in the queue.
        token_number = assign_token(db, salon_id, date)

        # Determine booking status based on auto-confirm limit
        # BUG 3 FIX: Reuse salon_doc from line 238 instead of re-fetching
        auto_confirm_limit = salon_data.get('auto_confirm_limit', 10)

        # BUG 2 FIX: Use date-suffixed queue doc ID (matches get_or_create_queue format)
        queue_ref = db.collection('queues').document(f"{salon_id}_{date}")
        queue_doc = queue_ref.get()
        slot_key = f"confirmed_count_{date}_{time_slot.replace(':', '')}"
        confirmed_count = 0
        if queue_doc.exists:
            confirmed_count = queue_doc.to_dict().get(slot_key, 0)

        # Auto-confirm if under limit, else pending
        booking_status = 'confirmed' if confirmed_count < auto_confirm_limit else 'pending'

        new_booking = {
            "user_id": uid, "salon_id": salon_id, "service_id": service_id,
            "date": date, "time": time_slot, "status": booking_status,
            "token_number": token_number, "fcm_token": fcm_token,
            "visitor_name": visitor_name,
            "created_at": firestore.SERVER_TIMESTAMP
        }

        # Resolve service name for notification — batch fetch if multiple IDs
        service_name = ''
        service_names = []
        service_ids = data.get('service_ids') or ([service_id] if service_id else [])
        if service_ids:
            # Batch read all services in ONE call instead of N individual reads
            svc_refs = [db.collection('services').document(sid) for sid in service_ids]
            svc_docs = db.get_all(svc_refs)
            for svc_doc in svc_docs:
                if svc_doc.exists:
                    svc_name = svc_doc.to_dict().get('name', '')
                    if svc_name:
                        service_names.append(svc_name)
        service_name = ', '.join(service_names) if service_names else service_id or ''

        # Persist service names in the booking document
        new_booking['services'] = service_names if service_names else ([service_id] if service_id else [])
        new_booking['service_name'] = service_name

        _, doc_ref = db.collection('bookings').add(new_booking)

        # Increment per-slot confirmed counter on the date-suffixed queue doc
        if booking_status in ('confirmed', 'in_progress', 'arrived'):
            db.collection('queues').document(f"{salon_id}_{date}").set(
                {slot_key: firestore.Increment(1)}, merge=True
            )

        # ── Notify owner via FCM topic (works even when app is closed) ──
        topic = f'salon_{salon_id}'
        notif_title = '📅 New Appointment' if booking_status == 'confirmed' else '⚠️ Pending Approval'
        notif_body_parts = [f'Token #{token_number}']
        if visitor_name: notif_body_parts.append(visitor_name)
        if service_name: notif_body_parts.append(service_name)
        notif_body = ' · '.join(notif_body_parts)

        send_topic_notification(
            topic=topic,
            title=notif_title,
            body=notif_body,
            data={
                'token_number': str(token_number),
                'customer_name': visitor_name,
                'service': service_name,
                'salon_id': salon_id,
                'booking_status': booking_status,
            }
        )

        # Also send to individual FCM token (legacy fallback)
        owner_id = salon_data.get('owner_id')
        if owner_id:
            owner_user = db.collection('users').document(owner_id).get()
            if owner_user.exists:
                send_notification(owner_user.to_dict().get('fcm_token'), notif_title, notif_body)

        status_msg = "Booked successfully!" if booking_status == 'confirmed' else "Booking submitted — waiting for owner approval."
        return jsonify({
            "message": status_msg, "id": doc_ref.id,
            "token_number": token_number, "visitor_name": visitor_name,
            "status": booking_status
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/my-bookings', methods=['GET'])
def my_bookings():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    try:
        docs = db.collection('bookings').where('user_id', '==', decoded['uid']).stream()
        bookings = []
        queue_cache = {}  # Avoid re-fetching same queue doc for multiple bookings
        for d in docs:
            b = {"id": d.id, **d.to_dict()}
            if b.get('hidden_by_customer'): continue  # Skip cleared history
            # Add queue position info with local cache
            if b.get('salon_id') and b.get('token_number') and b.get('date'):
                cache_key = f"{b['salon_id']}_{b['date']}"
                if cache_key not in queue_cache:
                    _, q = get_or_create_queue(db, b['salon_id'], b['date'])
                    queue_cache[cache_key] = q
                q = queue_cache[cache_key]
                b['current_token'] = q.get('current_token', 0)
                b['wait_estimate'] = predict_wait(b['token_number'], q.get('current_token', 0))
            bookings.append(b)
        # Sort by date descending for better UX
        bookings.sort(key=lambda x: x.get('date', ''), reverse=True)
        return jsonify(bookings), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cancel', methods=['POST'])
def cancel_booking():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    try:
        booking_id = request.json.get('booking_id')
        doc_ref = db.collection('bookings').document(booking_id)
        doc = doc_ref.get()
        if not doc.exists or doc.to_dict().get('user_id') != decoded['uid']:
            return jsonify({"error": "Unauthorized"}), 403
        doc_ref.update({"status": "cancelled"})

        # Notify owner about customer cancellation via topic
        booking_data = doc.to_dict()
        salon_id_from_booking = booking_data.get('salon_id', '')
        visitor = booking_data.get('visitor_name', 'A customer')
        token_num = booking_data.get('token_number', '')
        svc = booking_data.get('service_name', '')

        if salon_id_from_booking:
            cancel_body_parts = [f'Token #{token_num}'] if token_num else []
            cancel_body_parts.append(f'{visitor} cancelled')
            if svc: cancel_body_parts.append(svc)
            send_topic_notification(
                topic=f'salon_{salon_id_from_booking}',
                title='❌ Appointment Cancelled',
                body=' · '.join(cancel_body_parts),
                data={
                    'token_number': str(token_num),
                    'customer_name': visitor,
                    'service': svc,
                    'booking_status': 'cancelled',
                }
            )

        return jsonify({"message": "Cancelled"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/customer/clear-history', methods=['POST'])
def customer_clear_history():
    """Customer: hide completed/cancelled bookings from their view."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    try:
        docs = db.collection('bookings').where('user_id', '==', decoded['uid']).stream()
        count = 0
        for d in docs:
            bd = d.to_dict()
            if bd.get('status') in ['completed', 'cancelled', 'rejected', 'no_show']:
                d.reference.update({"hidden_by_customer": True})
                count += 1
        return jsonify({"message": f"Cleared {count} bookings from history"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/clear-queue', methods=['POST'])
def owner_clear_queue():
    """Owner: reset today's queue counter and archive completed bookings."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'owner':
        return jsonify({"error": "Owners only"}), 403
    salon_id = request.json.get('salon_id')
    if not salon_id: return jsonify({"error": "salon_id required"}), 400
    salon_doc = db.collection('salons').document(salon_id).get()
    if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != decoded['uid']:
        return jsonify({"error": "Not your salon"}), 403
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        # Reset queue counters for today
        ref = db.collection('queues').document(f"{salon_id}_{today_str}")
        ref.set({
            "current_token": 0, "last_token": 0,
            "qr_session": str(__import__('uuid').uuid4())[:8],
            "updated_at": firestore.SERVER_TIMESTAMP
        })

        # Archive/Cancel active bookings for TODAY
        all_docs = db.collection('bookings')\
            .where('salon_id', '==', salon_id)\
            .where('date', '==', today_str).stream()
        
        count = 0
        # BUG 20 FIX: Use top-level import instead of inline import
        
        for d in all_docs:
            bd = d.to_dict()
            status = bd.get('status')
            
            # If active, mark as cancelled and notify
            if status in ['confirmed', 'arrived', 'in_progress']:
                d.reference.update({"status": "cancelled", "archived": True, "cancel_reason": "Queue reset by Salon"})
                count += 1
                
                fcm_token = bd.get('fcm_token')
                if fcm_token:
                    send_push_notification(
                        fcm_token,
                        "Appointment Cancelled ⚠️",
                        f"Your appointment at {salon_doc.to_dict().get('name')} was cancelled due to a queue reset. Please book again if needed.",
                        data={"type": "cancellation"}
                    )
            else:
                # Just archive non-active ones
                d.reference.update({"archived": True})

        return jsonify({"message": f"Queue reset. {count} active bookings cancelled and notified."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== QUEUE ====================

@app.route('/api/queue/<salon_id>', methods=['GET'])
def get_queue(salon_id):
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        date_param = request.args.get('date')
        today_str = date_param if date_param else datetime.now().strftime('%Y-%m-%d')
        
        _, q = get_or_create_queue(db, salon_id, today_str)
        # Get all active bookings for this date
        all_docs = db.collection('bookings')\
            .where('salon_id', '==', salon_id)\
            .where('date', '==', today_str).stream()
        bookings = []
        for b in all_docs:
            bd = b.to_dict()
            if bd.get('status') not in ['cancelled', 'no_show', 'archived', 'completed']:
                bookings.append({"token": bd.get('token_number'), "status": bd.get('status'), "time": bd.get('time')})
        bookings.sort(key=lambda x: x.get('token', 0))
        q['bookings'] = bookings
        return jsonify(q), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/my-position/<salon_id>', methods=['GET'])
def my_queue_position(salon_id):
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    try:
        _, q = get_or_create_queue(db, salon_id)
        token_param = request.args.get('token', type=int)
        today_str = datetime.now().strftime('%Y-%m-%d')
        # Single-field query to avoid composite index requirement
        all_user_bookings = db.collection('bookings')\
            .where('user_id', '==', decoded['uid']).stream()

        # BUG 15 FIX: Filter by salon AND today's date to avoid stale position data
        bookings = [b.to_dict() | {'id': b.id} for b in all_user_bookings
                    if b.to_dict().get('salon_id') == salon_id
                    and b.to_dict().get('date', '') == today_str]

        if not bookings:
            return jsonify({"error": "No booking found at this salon"}), 404

        # Pick the booking matching the requested token, or fall back to first active
        my = None
        if token_param:
            my = next((b for b in bookings if str(b.get('token_number')) == str(token_param)), None)
        if not my:
            active = [b for b in bookings
                      if b.get('status') not in ['cancelled', 'completed', 'no_show', 'rejected']]
            my = active[0] if active else bookings[0]
            
        try:
            my_token = int(my.get('token_number', 0))
        except (ValueError, TypeError):
            my_token = 0

        current = q.get('current_token', 0)
        try:
            current = int(current)
        except (ValueError, TypeError):
            current = 0

        return jsonify({
            "my_token": my_token,
            "current_token": current,
            "position": max(0, my_token - (current + 1)),
            "wait_estimate": predict_wait(my_token, current),
            "status": my.get('status'),
            "visitor_name": my.get('visitor_name', '')
        }), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/queue/next', methods=['POST'])
def queue_next():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    salon_id = request.json.get('salon_id')
    if not salon_id: return jsonify({"error": "salon_id required"}), 400
    # Verify ownership
    salon_doc = db.collection('salons').document(salon_id).get()
    if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
        return jsonify({"error": "Not your salon"}), 403
    result, err = advance_queue(db, salon_id)
    if err: return jsonify({"error": err}), 400
    return jsonify(result), 200

@app.route('/api/queue/finish', methods=['POST'])
def queue_finish():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    salon_id = request.json.get('salon_id')
    if not salon_id: return jsonify({"error": "salon_id required"}), 400
    from services.queue_service import finish_current_token
    # Verify ownership
    salon_doc = db.collection('salons').document(salon_id).get()
    if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
        return jsonify({"error": "Not your salon"}), 403
    result, err = finish_current_token(db, salon_id)
    if err: return jsonify({"error": err}), 400
    return jsonify(result), 200

@app.route('/api/queue/skip', methods=['POST'])
def queue_skip():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    salon_id = request.json.get('salon_id')
    if not salon_id: return jsonify({"error": "salon_id required"}), 400
    salon_doc = db.collection('salons').document(salon_id).get()
    if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
        return jsonify({"error": "Not your salon"}), 403
    result, err = skip_token(db, salon_id)
    if err: return jsonify({"error": err}), 400
    return jsonify(result), 200

@app.route('/api/queue/scan', methods=['POST'])
def queue_scan():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    data = request.json
    salon_id = data.get('salon_id')
    token_number = data.get('token')
    session = data.get('session')
    if not all([salon_id, token_number, session]):
        return jsonify({"error": "Missing QR data"}), 400
    result, err = validate_qr_scan(db, salon_id, int(token_number), session)
    if err: return jsonify({"error": err}), 400
    return jsonify(result), 200

# ==================== QR ====================

@app.route('/api/qr/<salon_id>', methods=['GET'])
def get_qr(salon_id):
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        _, q = get_or_create_queue(db, salon_id)
        buf = generate_qr_image(salon_id, q.get('current_token', 0), q.get('qr_session', ''))
        return send_file(buf, mimetype='image/png', download_name='qr.png')
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/qr/data/<salon_id>', methods=['GET'])
def get_qr_data(salon_id):
    """Return QR data as JSON (for frontend JS-based QR rendering)."""
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        _, q = get_or_create_queue(db, salon_id)
        return jsonify({
            "salon_id": salon_id,
            "token": q.get('current_token', 0),
            "session": q.get('qr_session', ''),
            "last_token": q.get('last_token', 0)
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== OWNER ====================

@app.route('/api/create-salon', methods=['POST'])
def create_salon():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    try:
        new_salon = {
            "name": data.get('name'), "address": data.get('address'),
            "location": data.get('location', {"lat": 0.0, "lng": 0.0}),
            "opening_time": data.get('opening_time', "09:00"),
            "closing_time": data.get('closing_time', "20:00"),
            "slot_duration": data.get('slot_duration', 30),
            "owner_id": uid, "status": "pending", "rating": 5.0,
            "is_open": True
        }
        _, doc_ref = db.collection('salons').add(new_salon)
        return jsonify({"message": "Salon created, pending approval", "id": doc_ref.id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/add-service', methods=['POST'])
def add_service():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    salon_id = data.get('salon_id')
    try:
        salon_doc = db.collection('salons').document(salon_id).get()
        if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        svc = {
            "salon_id": salon_id,
            "name": data.get('name'),
            "price": data.get('price'),
            "duration": data.get('duration'),
            "category": data.get('category', ''),
            "description": data.get('description', ''),
            "image_url": data.get('image_url', ''),
        }
        _, doc_ref = db.collection('services').add(svc)
        # Invalidate caches for this salon
        with _salon_detail_lock:
            _salon_detail_cache.pop(salon_id, None)
        with _services_cache_lock:
            _services_cache.pop(salon_id, None)
        with _salons_cache_lock:
            _salons_cache.update({"data": None, "ts": 0})
        return jsonify({"message": "Service added", "id": doc_ref.id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/services', methods=['GET'])
def get_all_services():
    """
    Get all services across all approved salons.
    Optional query params:
      - salon_id : filter by a specific salon
      - category : filter by category name (case-insensitive)
    Returns each service with its salon's name and address attached.
    """
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    salon_id_filter = request.args.get('salon_id')
    category_filter = request.args.get('category', '').strip().lower()
    try:
        # Fetch services
        if salon_id_filter:
            svc_docs = db.collection('services').where('salon_id', '==', salon_id_filter).stream()
        else:
            # If no filter, but we are an owner, maybe we should only see our own?
            # For now, let's keep it as is for customers, but owner app should pass salon_id.
            svc_docs = db.collection('services').stream()

        services = []
        salon_cache = {}  # avoid re-fetching the same salon doc

        for doc in svc_docs:
            svc = {"id": doc.id, **doc.to_dict()}

            # Apply category filter in Python (avoids composite index requirement)
            if category_filter and svc.get('category', '').lower() != category_filter:
                continue

            # Attach salon name + address for display in the Services page
            sid = svc.get('salon_id', '')
            if sid:
                if sid not in salon_cache:
                    s_doc = db.collection('salons').document(sid).get()
                    salon_cache[sid] = s_doc.to_dict() if s_doc.exists else {}
                salon_info = salon_cache[sid]
                svc['salon_name'] = salon_info.get('name', '')
                svc['salon_address'] = salon_info.get('address', '')
                svc['salon_status'] = salon_info.get('status', '')

            # Only surface services from approved salons (unless salon_id_filter is provided by owner)
            if not salon_id_filter and svc.get('salon_status', 'approved') != 'approved':
                continue

            services.append(svc)

        return jsonify(services), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/services/<salon_id>', methods=['GET'])
def get_salon_services(salon_id):
    """Get all services for a specific salon (dedicated endpoint, no auth required)."""
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    # Check services cache
    now = time.time()
    with _services_cache_lock:
        cached = _services_cache.get(salon_id)
        if cached and now - cached['ts'] < _SERVICES_TTL:
            resp = jsonify(cached['data'])
            resp.headers['Cache-Control'] = 'public, max-age=60'
            return resp, 200
    try:
        docs = db.collection('services').where('salon_id', '==', salon_id).stream()
        services = [{"id": d.id, **d.to_dict()} for d in docs]
        # Update cache
        with _services_cache_lock:
            _services_cache[salon_id] = {"data": services, "ts": now}
        resp = jsonify(services)
        resp.headers['Cache-Control'] = 'public, max-age=60'
        return resp, 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/service/<service_id>', methods=['DELETE'])
def delete_service(service_id):
    """Owner: delete one of their services."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    try:
        doc_ref = db.collection('services').document(service_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Service not found"}), 404
        # Verify the service belongs to one of the owner's salons
        salon_doc = db.collection('salons').document(doc.to_dict().get('salon_id', '')).get()
        if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        doc_ref.delete()
        # Invalidate caches for this salon
        salon_id = doc.to_dict().get('salon_id', '')
        with _salon_detail_lock:
            _salon_detail_cache.pop(salon_id, None)
        with _services_cache_lock:
            _services_cache.pop(salon_id, None)
        return jsonify({"message": "Service deleted"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/service/<service_id>', methods=['PUT'])
def update_service(service_id):
    """Owner: update an existing service."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    try:
        doc_ref = db.collection('services').document(service_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "Service not found"}), 404
        salon_doc = db.collection('salons').document(doc.to_dict().get('salon_id', '')).get()
        if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        allowed = ['name', 'price', 'duration', 'category', 'description', 'image_url']
        updates = {k: v for k, v in data.items() if k in allowed}
        doc_ref.update(updates)
        return jsonify({"message": "Service updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/api/owner/manual-book', methods=['POST'])
def manual_book():
    """Owner: manually add a customer who doesn't have the app/mobile."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    
    data = request.json
    salon_id = data.get('salon_id')
    visitor_name = data.get('visitor_name', 'Walk-in Customer')
    service_ids = data.get('service_ids', [])
    date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
    time_slot = data.get('time', datetime.now().strftime('%H:%M'))

    if not salon_id: return jsonify({"error": "salon_id required"}), 400

    try:
        salon_doc = db.collection('salons').document(salon_id).get()
        if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403

        token_number = assign_token(db, salon_id, date)
        
        # Resolve service names
        service_names = []
        for sid in service_ids:
            svc_doc = db.collection('services').document(sid).get()
            if svc_doc.exists:
                service_names.append(svc_doc.to_dict().get('name', ''))

        new_booking = {
            "user_id": f"manual_{uid}_{int(time.time())}", 
            "salon_id": salon_id,
            "visitor_name": visitor_name,
            "service_ids": service_ids,
            "services": service_names,
            "service_name": ', '.join(service_names),
            "date": date,
            "time": time_slot,
            "status": "confirmed", # Manual bookings are auto-confirmed
            "token_number": token_number,
            "is_manual": True,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        
        _, doc_ref = db.collection('bookings').add(new_booking)
        
        # BUG 2 FIX: Use date-suffixed queue doc ID (matches get_or_create_queue format)
        slot_key = f"confirmed_count_{date}_{time_slot.replace(':', '')}"
        db.collection('queues').document(f"{salon_id}_{date}").set(
            {slot_key: firestore.Increment(1)}, merge=True
        )

        return jsonify({
            "message": "Manual booking added",
            "id": doc_ref.id,
            "token_number": token_number
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/salons', methods=['GET'])
def get_owner_salons():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    try:
        salons = list(db.collection('salons').where('owner_id', '==', uid).stream())
        result = []
        for s in salons:
            data = {"id": s.id, **s.to_dict()}
            today_str = datetime.now().strftime('%Y-%m-%d')
            _, q = get_or_create_queue(db, s.id, today_str)
            data['queue'] = q
            result.append(data)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/update-salon', methods=['POST'])
def update_salon():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    salon_id = data.get('salon_id')
    try:
        doc_ref = db.collection('salons').document(salon_id)
        doc = doc_ref.get()
        if not doc.exists or doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
            
        allowed = ['name', 'address', 'opening_time', 'closing_time', 'slot_duration', 'closed_days', 'auto_confirm_limit']
        updates = {k: v for k, v in data.items() if k in allowed}
        doc_ref.update(updates)
        # Invalidate caches so clients see fresh data
        with _salon_detail_lock:
            _salon_detail_cache.pop(salon_id, None)
        with _salons_cache_lock:
            _salons_cache.update({"data": None, "ts": 0})
        return jsonify({"message": "Salon info updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/set-auto-confirm', methods=['POST'])
def set_auto_confirm():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    salon_id = data.get('salon_id')
    limit = data.get('limit', 10)
    try:
        doc_ref = db.collection('salons').document(salon_id)
        doc = doc_ref.get()
        if not doc.exists or doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        doc_ref.update({"auto_confirm_limit": limit})
        with _salon_detail_lock:
            _salon_detail_cache.pop(salon_id, None)
        return jsonify({"message": f"Auto-confirm limit set to {limit}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/owner/update-salon-status', methods=['POST'])
def update_salon_status_owner():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    salon_id = data.get('salon_id')
    is_open = data.get('is_open', True)
    try:
        doc_ref = db.collection('salons').document(salon_id)
        doc = doc_ref.get()
        if not doc.exists or doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        doc_ref.update({"is_open": is_open})
        with _salon_detail_lock:
            _salon_detail_cache.pop(salon_id, None)
        with _salons_cache_lock:
            _salons_cache.update({"data": None, "ts": 0})
        return jsonify({"message": f"Salon status updated to {'Open' if is_open else 'Closed'}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/bookings', methods=['GET'])
def get_owner_bookings():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    try:
        salons = list(db.collection('salons').where('owner_id', '==', uid).stream())
        salon_ids = [s.id for s in salons]
        if not salon_ids: return jsonify([]), 200
        # BUG 22 FIX: Filter by date (default today) to avoid fetching all historical bookings
        date_filter = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        bookings = []
        for s_id in salon_ids:
            b_docs = db.collection('bookings')\
                .where('salon_id', '==', s_id)\
                .where('date', '==', date_filter).stream()
            bookings.extend([{"id": d.id, **d.to_dict()} for d in b_docs])
        return jsonify(bookings), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/booking/update-status', methods=['POST'])
def update_booking_status():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    uid = decoded['uid']
    if get_user_role(db, uid) != 'owner': return jsonify({"error": "Owners only"}), 403
    data = request.json
    booking_id, status = data.get('booking_id'), data.get('status')
    # BUG 17 FIX: Validate status against allowed values
    ALLOWED_STATUSES = {'confirmed', 'cancelled', 'completed', 'in_progress', 'arrived', 'pending', 'rejected', 'no_show'}
    if status not in ALLOWED_STATUSES:
        return jsonify({"error": f"Invalid status '{status}'. Allowed: {', '.join(sorted(ALLOWED_STATUSES))}"}), 400
    try:
        doc_ref = db.collection('bookings').document(booking_id)
        doc = doc_ref.get()
        if not doc.exists: return jsonify({"error": "Not found"}), 404
        salon_doc = db.collection('salons').document(doc.to_dict().get('salon_id')).get()
        if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != uid:
            return jsonify({"error": "Unauthorized"}), 403
        doc_ref.update({"status": status})
        user_doc = db.collection('users').document(doc.to_dict().get('user_id')).get()
        if user_doc.exists and user_doc.to_dict().get('fcm_token'):
            send_notification(user_doc.to_dict().get('fcm_token'), "Booking Update", f"Your booking is now {status}.")
        return jsonify({"message": f"Updated to {status}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== ADMIN ====================

@app.route('/api/admin/salons', methods=['GET'])
def admin_salons():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'admin': return jsonify({"error": "Admins only"}), 403
    try:
        docs = db.collection('salons').stream()
        return jsonify([{"id": d.id, **d.to_dict()} for d in docs]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/<action>-salon', methods=['POST'])
def admin_salon_action(action):
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'admin': return jsonify({"error": "Admins only"}), 403
    if action not in ['approve', 'reject']: return jsonify({"error": "Invalid action"}), 400
    salon_id = request.json.get('salon_id')
    try:
        status = "approved" if action == "approve" else "rejected"
        db.collection('salons').document(salon_id).update({"status": status})
        return jsonify({"message": f"Salon {status}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users', methods=['GET'])
def admin_users():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'admin': return jsonify({"error": "Admins only"}), 403
    try:
        docs = db.collection('users').stream()
        return jsonify([{"id": d.id, **d.to_dict()} for d in docs]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'admin': return jsonify({"error": "Admins only"}), 403
    try:
        # BUG 16 FIX: Only count today's bookings instead of loading entire history into memory
        users = len(list(db.collection('users').stream()))
        salons = len(list(db.collection('salons').stream()))
        today_str = datetime.now().strftime('%Y-%m-%d')
        today_bookings = list(db.collection('bookings').where('date', '==', today_str).stream())
        total_today = len(today_bookings)
        active = sum(1 for b in today_bookings if b.to_dict().get('status') in ['confirmed','in_progress','arrived'])
        return jsonify({
            "total_users": users, "total_salons": salons,
            "total_bookings_today": total_today, "active_bookings": active
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== AI ====================

@app.route('/api/ai/recommend', methods=['GET'])
def ai_recommend():
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        # Reuse salon cache if available to avoid redundant Firestore reads
        now = time.time()
        with _salons_cache_lock:
            cached = _salons_cache.get('data')
            cache_fresh = cached and now - _salons_cache.get('ts', 0) < 30
        
        if cache_fresh and cached:
            salons = [{**s} for s in cached]  # shallow copy
        else:
            docs = db.collection('salons').where('status', '==', 'approved').stream()
            today_str = datetime.now().strftime('%Y-%m-%d')
            salons = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                _, q = get_or_create_queue(db, doc.id, today_str)
                data['queue_length'] = q.get('last_token', 0) - q.get('current_token', 0)
                salons.append(data)
        result = recommend_salons(salons, lat, lng)
        return jsonify(result[:5]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ai/wait-time/<salon_id>', methods=['GET'])
def ai_wait_time(salon_id):
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    try:
        _, q = get_or_create_queue(db, salon_id)
        token = request.args.get('token', type=int, default=q.get('last_token', 0))
        wait = predict_wait(token, q.get('current_token', 0))
        return jsonify({"wait_minutes": wait, "current_token": q.get('current_token',0)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== SERVE FRONTEND ====================

@app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/<path:path>')
def serve_frontend(path):
    file_path = os.path.join(FRONTEND_DIR, path)
    if os.path.isfile(file_path):
        return send_from_directory(FRONTEND_DIR, path)
    # For SPA-like behavior, serve index.html for unknown paths
    return send_from_directory(FRONTEND_DIR, 'index.html')

if __name__ == '__main__':
    # Use debug=True only locally; on Render, gunicorn is the entrypoint.
    # Start command: gunicorn app:app --workers 1 --threads 2 --timeout 120
    app.run(host='0.0.0.0', debug=False, port=5000)
