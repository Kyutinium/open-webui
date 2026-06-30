"""Per-user request rate limiting for API-key traffic.

Open WebUI has no built-in usage cap on API keys, so a leaked or abused key can
hammer the chat-completion endpoint unchecked. This guard throttles requests
that authenticated via an API key (UI sessions are exempt — they set
``request.state.auth_type`` to ``"session"``).

Two tiers are supported so daytime can be tighter than night: the active
per-window request limit is picked from the current hour in the configured
timezone. All knobs are ``PersistentConfig`` and tunable live from the admin
config endpoint — no restart needed.
"""

from datetime import datetime

from fastapi import HTTPException, status

from open_webui.utils.rate_limit import RateLimiter
from open_webui.utils.redis import get_redis_client

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


def _current_hour(tz_name: str) -> int:
    """Hour-of-day (0-23) in *tz_name*, falling back to local time."""
    tz = None
    if ZoneInfo and tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    return datetime.now(tz).hour


def _active_limit(config) -> int:
    """Pick the day or night request limit for the current hour.

    Daytime is ``[day_start, day_end)``; a range that wraps past midnight
    (start > end) is handled so e.g. 22→6 still means "night hours".
    """
    start = config.API_KEY_RATE_LIMIT_DAY_START
    end = config.API_KEY_RATE_LIMIT_DAY_END
    hour = _current_hour(config.API_KEY_RATE_LIMIT_TZ)

    if start <= end:
        is_day = start <= hour < end
    else:
        is_day = hour >= start or hour < end

    return config.API_KEY_RATE_LIMIT_DAY if is_day else config.API_KEY_RATE_LIMIT_NIGHT


def check_api_key_rate_limit(request, user) -> None:
    """Raise HTTP 429 if this API-key request exceeds the active rate limit.

    No-op for UI sessions, when the feature is disabled, or when the active
    limit/window is non-positive.
    """
    config = request.app.state.config

    if not getattr(config, 'API_KEY_RATE_LIMIT_ENABLED', False):
        return
    if getattr(request.state, 'auth_type', None) != 'api_key':
        return

    limit = _active_limit(config)
    window = int(config.API_KEY_RATE_LIMIT_WINDOW or 0)
    if limit <= 0 or window <= 0:
        return

    limiter = RateLimiter(get_redis_client(), limit=limit, window=window)
    if limiter.is_limited(f'apikey:{user.id}'):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='API key rate limit exceeded. Please slow down and try again later.',
            headers={'Retry-After': str(window)},
        )
