import redis

# --- Constants ---
LIBRARIES = {
    "comics": "/downloads/comics",
    "artbooks": "/downloads/artbooks",
    "manga": "/downloads/manga"
}
WATCHED_URLS_REDIS_KEY = "gallery_dl_watched_urls"

# --- Clients ---
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)