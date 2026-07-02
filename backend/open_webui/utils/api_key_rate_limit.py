"""Per-user, per-model request rate limiting for API-key traffic.

Open WebUI has no usage cap on API keys, so a leaked or abused key can hammer
the chat-completion endpoint unchecked. This guard throttles requests that
authenticated via an API key (UI sessions set ``request.state.auth_type`` to
``"session"`` and are exempt; admins are exempt too).

A global default (day/night limits over a shared window, with the active tier
chosen by the current hour in a configurable timezone) is set from admin
config. Individual models may override the window / day / night *limits* via
``meta.api_key_rate_limit`` (edited in the Models screen); the day/night
boundary and timezone stay global. A per-model limit of 0 (or a global limit of
0) means unlimited for that tier. Counters are keyed per (user, model).
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


def _is_daytime(config) -> bool:
    """Whether now falls in the configured daytime window.

    Daytime is ``[day_start, day_end)``; a range that wraps past midnight
    (start > end) is handled so e.g. 22→6 still means "night hours".
    """
    start = config.API_KEY_RATE_LIMIT_DAY_START
    end = config.API_KEY_RATE_LIMIT_DAY_END
    hour = _current_hour(config.API_KEY_RATE_LIMIT_TZ)
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _coalesce_int(value, default) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _model_override(request, model_id) -> dict:
    """Per-model ``meta.api_key_rate_limit`` override, or ``{}``.

    Read from the in-memory model registry (no DB round-trip). Models without a
    workspace entry simply have no override and fall back to the global config.
    """
    if not model_id:
        return {}
    models = getattr(request.app.state, 'MODELS', None) or {}
    entry = models.get(model_id) or {}
    meta = (entry.get('info') or {}).get('meta') or {}
    override = meta.get('api_key_rate_limit')
    return override if isinstance(override, dict) else {}


def check_api_key_rate_limit(request, user, model_id=None) -> None:
    """Raise HTTP 429 if this API-key request exceeds the active rate limit.

    No-op for UI sessions, admins, when the feature is disabled, or when the
    effective limit/window is non-positive (unlimited).
    """
    config = request.app.state.config

    if not getattr(config, 'API_KEY_RATE_LIMIT_ENABLED', False):
        return
    if getattr(request.state, 'auth_type', None) != 'api_key':
        return
    # Admins are exempt — their keys are for operational/automation use.
    if getattr(user, 'role', None) == 'admin':
        return

    override = _model_override(request, model_id)
    window = _coalesce_int(override.get('window'), config.API_KEY_RATE_LIMIT_WINDOW)
    day = _coalesce_int(override.get('day'), config.API_KEY_RATE_LIMIT_DAY)
    night = _coalesce_int(override.get('night'), config.API_KEY_RATE_LIMIT_NIGHT)

    limit = day if _is_daytime(config) else night
    if limit <= 0 or window <= 0:
        return

    limiter = RateLimiter(get_redis_client(), limit=limit, window=window)
    # Per (user, model) so each model has its own budget.
    key = f'apikey:{user.id}:{model_id or "*"}'

    # Decide from the current count WITHOUT incrementing, so a rejected (429)
    # request does not consume quota; only an allowed request is recorded.
    if limiter.get_count(key) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='API key rate limit exceeded. Please slow down and try again later.',
            headers={'Retry-After': str(window)},
        )

    limiter.is_limited(key)
