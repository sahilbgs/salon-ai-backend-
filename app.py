import os
import math
import json
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, auth, firestore, messaging
from datetime import datetime
from services.queue_service import (
    get_or_create_queue, assign_token, advance_queue, 
    skip_token, validate_qr_scan, generate_qr_image
)
from services.ai_service import predict_wait, recommend_salons
from utils.auth_helpers import verify_token, get_user_role, haversine

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
CORS(app, resources={r"/api/*": {"origins": "*"}})

FIREBASE_KEY_PATH = os.environ.get("FIREBASE_KEY_PATH", os.path.join(os.path.dirname(__file__), "serviceAccountKey.json"))
FIREBASE_KEY_JSON = os.environ.get("FIREBASE_KEY_JSON")

db = None

try:
    if FIREBASE_KEY_JSON:
        key_data = json.loads(FIREBASE_KEY_JSON)
        cred = credentials.Certificate(key_data)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase Admin SDK initialized from FIREBASE_KEY_JSON.")
    elif os.path.exists(FIREBASE_KEY_PATH):
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase Admin SDK initialized from FIREBASE_KEY_PATH.")
    else:
        print(f"WARNING: Firebase key not found at {FIREBASE_KEY_PATH} and FIREBASE_KEY_JSON is not set.")
        db = None
except Exception as e:
    print(f"Error initializing Firebase: {e}")
    db = None

def send_notification(token, title, body):
    if not token: return
    try:
        msg = messaging.Message(notification=messaging.Notification(title=title, body=body), token=token)
        messaging.send(msg)
    except Exception as e:
        print(f"FCM Error: {e}")

@app.route('/')
def health_check():
    return jsonify({"status": "ok", "message": "Salon AI backend is running"}), 200

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
    try:
        docs = db.collection('salons').where('status', '==', 'approved').stream()
        salons = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            if lat and lng and 'location' in data:
                slat, slng = data['location'].get('lat'), data['location'].get('lng')
                data['distance'] = haversine(lat, lng, slat, slng) if slat and slng else float('inf')
            else:
                data['distance'] = float('inf')
            # Attach queue info
            _, q = get_or_create_queue(db, doc.id)
            data['queue_length'] = q.get('last_token',0) - q.get('current_token',0)
            data['current_token'] = q.get('current_token', 0)
            salons.append(data)
        if lat and lng:
            salons.sort(key=lambda x: x.get('distance', float('inf')))
        return jsonify(salons), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/salon/<salon_id>', methods=['GET'])
def get_salon(salon_id):
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        salon_doc = db.collection('salons').document(salon_id).get()
        if not salon_doc.exists: return jsonify({"error": "Not found"}), 404
        data = salon_doc.to_dict()
        data['id'] = salon_doc.id
        services_docs = db.collection('services').where('salon_id', '==', salon_id).stream()
        data['services'] = [{"id": d.id, **d.to_dict()} for d in services_docs]
        _, q = get_or_create_queue(db, salon_id)
        data['queue'] = q
        return jsonify(data), 200
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
    if not all([salon_id, service_id, date, time_slot]):
        return jsonify({"error": "Missing fields"}), 400
    try:
        # No slot conflict check — shifts allow multiple bookings.
        # Everyone gets their own token in the queue.
        token_number = assign_token(db, salon_id)

        # Determine booking status based on auto-confirm limit
        salon_doc = db.collection('salons').document(salon_id).get()
        auto_confirm_limit = 10  # default
        if salon_doc.exists:
            auto_confirm_limit = salon_doc.to_dict().get('auto_confirm_limit', 10)

        # Count existing confirmed bookings for this shift today (single-field query, filter in Python)
        all_salon_bookings = db.collection('bookings').where('salon_id', '==', salon_id).stream()
        confirmed_count = sum(
            1 for b in all_salon_bookings
            if b.to_dict().get('date') == date
            and b.to_dict().get('time') == time_slot
            and b.to_dict().get('status') in ['confirmed', 'in_progress', 'arrived']
        )

        # Auto-confirm if under limit, else pending
        booking_status = 'confirmed' if confirmed_count < auto_confirm_limit else 'pending'

        new_booking = {
            "user_id": uid, "salon_id": salon_id, "service_id": service_id,
            "date": date, "time": time_slot, "status": booking_status,
            "token_number": token_number,
            "visitor_name": visitor_name,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        _, doc_ref = db.collection('bookings').add(new_booking)

        # Notify owner
        if salon_doc.exists:
            owner_id = salon_doc.to_dict().get('owner_id')
            if owner_id:
                owner_user = db.collection('users').document(owner_id).get()
                if owner_user.exists:
                    notif_title = "New Booking" if booking_status == 'confirmed' else "⚠️ Pending Approval Required"
                    send_notification(owner_user.to_dict().get('fcm_token'),
                        notif_title, f"Token #{token_number} — {visitor_name} on {date} at {time_slot}")

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
        for d in docs:
            b = {"id": d.id, **d.to_dict()}
            if b.get('hidden_by_customer'): continue  # Skip cleared history
            # Add queue position info
            if b.get('salon_id') and b.get('token_number'):
                _, q = get_or_create_queue(db, b['salon_id'])
                b['current_token'] = q.get('current_token', 0)
                b['wait_estimate'] = predict_wait(b['token_number'], q.get('current_token', 0))
            bookings.append(b)
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
        # Reset queue counters
        ref = db.collection('queues').document(salon_id)
        ref.set({
            "current_token": 0, "last_token": 0,
            "qr_session": str(__import__('uuid').uuid4())[:8],
            "updated_at": firestore.SERVER_TIMESTAMP
        })
        # Archive all non-archived bookings during testing phase
        all_docs = db.collection('bookings').where('salon_id', '==', salon_id).stream()
        count = 0
        for d in all_docs:
            d.reference.update({"archived": True})
            count += 1
        return jsonify({"message": f"Queue reset. {count} bookings archived."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/owner/set-auto-confirm', methods=['POST'])
def set_auto_confirm():
    """Owner: set the auto-confirm limit per shift for their salon."""
    decoded, err = verify_token()
    if err: return jsonify({"error": err}), 401
    if get_user_role(db, decoded['uid']) != 'owner':
        return jsonify({"error": "Owners only"}), 403
    data = request.json
    salon_id = data.get('salon_id')
    limit = data.get('limit')
    if salon_id is None or limit is None:
        return jsonify({"error": "salon_id and limit required"}), 400
    salon_doc = db.collection('salons').document(salon_id).get()
    if not salon_doc.exists or salon_doc.to_dict().get('owner_id') != decoded['uid']:
        return jsonify({"error": "Not your salon"}), 403
    try:
        db.collection('salons').document(salon_id).update({
            "auto_confirm_limit": int(limit)
        })
        return jsonify({"message": f"Auto-confirm limit set to {limit}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== QUEUE ====================

@app.route('/api/queue/<salon_id>', methods=['GET'])
def get_queue(salon_id):
    if db is None: return jsonify({"error": "DB not initialized"}), 500
    try:
        _, q = get_or_create_queue(db, salon_id)
        # Get all active bookings for testing phase
        all_docs = db.collection('bookings').where('salon_id', '==', salon_id).stream()
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
        # Single-field query to avoid composite index requirement
        all_user_bookings = db.collection('bookings')\
            .where('user_id', '==', decoded['uid']).stream()

        # Testing phase: allow all dates
        bookings = [b.to_dict() | {'id': b.id} for b in all_user_bookings
                    if b.to_dict().get('salon_id') == salon_id]
        
        print(f"DEBUG my_queue_position: salon_id={salon_id}, uid={decoded['uid']}")
        print(f"DEBUG my_queue_position: bookings length={len(bookings)}")

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
            
        print(f"DEBUG my_queue_position: selected booking={my.get('id')} with status={my.get('status')}")

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
            "owner_id": uid, "status": "pending", "rating": 5.0
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
        svc = {"salon_id": salon_id, "name": data.get('name'),
               "price": data.get('price'), "duration": data.get('duration')}
        _, doc_ref = db.collection('services').add(svc)
        return jsonify({"message": "Service added", "id": doc_ref.id}), 201
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
            _, q = get_or_create_queue(db, s.id)
            data['queue'] = q
            result.append(data)
        return jsonify(result), 200
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
        bookings = []
        for s_id in salon_ids:
            b_docs = db.collection('bookings').where('salon_id', '==', s_id).stream()
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
        users = len(list(db.collection('users').stream()))
        salons = len(list(db.collection('salons').stream()))
        bookings = list(db.collection('bookings').stream())
        total_bookings = len(bookings)
        active = sum(1 for b in bookings if b.to_dict().get('status') in ['confirmed','in_progress','arrived'])
        return jsonify({
            "total_users": users, "total_salons": salons,
            "total_bookings": total_bookings, "active_bookings": active
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
        docs = db.collection('salons').where('status', '==', 'approved').stream()
        salons = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            _, q = get_or_create_queue(db, doc.id)
            data['queue_length'] = q.get('last_token',0) - q.get('current_token',0)
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
    app.run(host='0.0.0.0', debug=True, port=5000)
