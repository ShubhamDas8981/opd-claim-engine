import redis
import json
from app.core.config import settings

class RedisCache:
    def __init__(self):
        self.client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            decode_responses=True
        )

    def _get_key(self, cache_type: str, scope: str, identifier: str):
        # cache_type: input | output
        # scope: public | private
        return f"{cache_type}:{scope}:{identifier}"

    def set_cache(self, cache_type: str, scope: str, identifier: str, value: any, ex=3600):
        key = self._get_key(cache_type, scope, identifier)
        self.client.set(key, json.dumps(value), ex=ex)

    def get_cache(self, cache_type: str, scope: str, identifier: str):
        key = self._get_key(cache_type, scope, identifier)
        data = self.client.get(key)
        return json.loads(data) if data else None

    def clear_private_cache(self, user_id: str):
        # Clear all private caches for a specific user
        keys = self.client.keys(f"*:private:{user_id}*")
        if keys:
            self.client.delete(*keys)

cache = RedisCache()
