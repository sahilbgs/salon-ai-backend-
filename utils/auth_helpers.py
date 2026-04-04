import math
from flask import request, jsonify
from firebase_admin import auth, firestore

def verify_token():
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, "No token"
    try:
        return auth.verify_id_token(auth_header.split(" ")[1]), None
    except Exception as e:
        return None, str(e)

def get_user_role(db, uid):
    if not db: return None
    doc = db.collection('users').document(uid).get()
    return doc.to_dict().get('role', 'user') if doc.exists else None

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        decoded, err = verify_token()
        if err: return jsonify({"error": err}), 401
        return f(decoded, *args, **kwargs)
    return decorated

def require_role(db, role):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            decoded, err = verify_token()
            if err: return jsonify({"error": err}), 401
            if get_user_role(db, decoded['uid']) != role:
                return jsonify({"error": f"{role}s only"}), 403
            return f(decoded, *args, **kwargs)
        return decorated
    return decorator

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
