"""Rate limiter setup (slowapi, in-memory storage).

Pilot scope: single-instance app, no Redis (BRD §5.2/9.2). In-memory is fine.
If we ever scale out to multiple Container Apps replicas, swap `storage_uri`
to a Redis backend — the API surface for `Limiter.limit(...)` doesn't change.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
