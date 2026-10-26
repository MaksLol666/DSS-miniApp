import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from api_services.captcha import COOKIE_NAME, SITE_KEY, PRIVATE_KEY, verify_session

RATE_WINDOW_SECONDS = max(5, int(os.getenv("SECURITY_RATE_WINDOW_SECONDS", "30")))
RATE_MAX_REQUESTS = max(10, int(os.getenv("SECURITY_RATE_MAX_REQUESTS", "45")))

# Per-process baseline limiter. Use a shared store for multiple workers.
_requests_by_ip: dict[str, deque[float]] = defaultdict(deque)
_cart_adds_by_ip: dict[str, deque[float]] = defaultdict(deque)
CAPTCHA_EXEMPT_PATHS = {"/api/health", "/api/captcha/config", "/api/captcha/status", "/api/captcha/verify", "/api/auth"}
SUSPICIOUS_CART_ADD_COUNT = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_CART_ADDS", "8")))
SUSPICIOUS_CART_ADD_WINDOW = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_WINDOW_SECONDS", "20")))


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()

    # Require a signed, HttpOnly CAPTCHA session for protected API routes.
    if SITE_KEY and PRIVATE_KEY and path not in CAPTCHA_EXEMPT_PATHS:
        from api_services.auth import verify_init_data
        init_data = request.headers.get("X-Init-Data", "")
        telegram_user = verify_init_data(init_data)
        if not telegram_user or not verify_session(
            request.cookies.get(COOKIE_NAME), telegram_user["id"]
        ):
            response = JSONResponse(status_code=428, content={"detail": "CAPTCHA_REQUIRED"})
            response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="none")
            return response

    # Repeated add-to-cart calls in a short interval trigger a fresh challenge.
    if SITE_KEY and PRIVATE_KEY and request.method == "POST" and path == "/api/cart/add":
        action_bucket = _cart_adds_by_ip[ip]
        while action_bucket and now - action_bucket[0] > SUSPICIOUS_CART_ADD_WINDOW:
            action_bucket.popleft()
        action_bucket.append(now)
        if len(action_bucket) > SUSPICIOUS_CART_ADD_COUNT:
            action_bucket.clear()
            response = JSONResponse(status_code=428, content={"detail": "CAPTCHA_REQUIRED"})
            response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="none")
            return response

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
