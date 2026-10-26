import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

RATE_WINDOW_SECONDS = max(5, int(os.getenv("SECURITY_RATE_WINDOW_SECONDS", "30")))
RATE_MAX_REQUESTS = max(10, int(os.getenv("SECURITY_RATE_MAX_REQUESTS", "45")))

# Per-process baseline limiter. Use a shared store for multiple workers.
_requests_by_ip: dict[str, deque[float]] = defaultdict(deque)


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/health":
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _requests_by_ip[ip]
    while bucket and now - bucket[0] > RATE_WINDOW_SECONDS:
        bucket.popleft()

    if len(bucket) >= RATE_MAX_REQUESTS:
        retry_after = max(1, int(RATE_WINDOW_SECONDS - (now - bucket[0])))
        return JSONResponse(
            status_code=429,
            content={"detail": "Слишком много запросов. Попробуй чуть позже."},
            headers={"Retry-After": str(retry_after)},
        )

    bucket.append(now)
    if len(_requests_by_ip) > 5000:
        cutoff = now - RATE_WINDOW_SECONDS
        for old_ip in list(_requests_by_ip):
            old_bucket = _requests_by_ip[old_ip]
            while old_bucket and now - old_bucket[0] > RATE_WINDOW_SECONDS:
                old_bucket.popleft()
            if not old_bucket:
                _requests_by_ip.pop(old_ip, None)

    return await call_next(request)
