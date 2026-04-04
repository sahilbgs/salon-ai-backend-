def predict_wait(token, current, avg_duration=10):
    """Predict wait time in minutes."""
    if token <= current:
        return 0
    return (token - current) * avg_duration

def recommend_salons(salons, user_lat=None, user_lng=None):
    """Score and rank salons by distance, rating, and queue length."""
    from utils.auth_helpers import haversine
    scored = []
    for s in salons:
        score = 0
        score += s.get('rating', 3) * 10  # rating weight
        
        if user_lat and user_lng and 'location' in s:
            dist = haversine(user_lat, user_lng, 
                           s['location'].get('lat',0), s['location'].get('lng',0))
            score -= dist * 2  # closer = better
        
        queue_len = s.get('queue_length', 0)
        score -= queue_len * 3  # shorter queue = better
        
        s['ai_score'] = round(score, 1)
        scored.append(s)
    
    return sorted(scored, key=lambda x: x['ai_score'], reverse=True)
