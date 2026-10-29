import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
TRUSTED_PROXIES = ["10.0.0.5"]
