"""Per-user / per-group, per-model request rate limiting for API-key traffic.

Open WebUI has no usage cap on API keys, so a leaked or abused key can hammer
the chat-completion endpoint unchecked. This guard throttles requests that
authenticated via an API key (UI sessions set ``request.state.auth_type`` to
``"session"`` and are exempt; admins are exempt too).

With ``API_KEY_RATE_LIMIT_BY_GROUP`` on, counting is attributed to the user's
group so a team shares one budget: a single-group user is charged
automatically (no client change); a multi-group user names the group via the
``X-RateLimit-Group`` header; users in no group fall back to per-user.

A global default (day/night limits over a shared window, with the active tier
chosen by the current hour in a configurable timezone) is set from admin
config. Individual models may override the window / day / night *limits* via
``meta.api_key_rate_limit`` (edited in the Models screen); the day/night
boundary and timezone stay global. Limit semantics for a tier: ``-1`` (or any
negative) means unlimited, ``0`` blocks all API-key traffic, ``>0`` allows that
many requests per window. Counters are keyed per (user, model).
"""

from datetime import datetime

from fastapi import HTTPException, status

from open_webui.models.groups import Groups
from open_webui.utils.rate_limit import RateLimiter
from open_webui.utils.redis import get_redis_client

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

# Header a multi-group caller uses to name which group's budget to charge.
RATE_LIMIT_GROUP_HEADER = 'x-ratelimit-group'


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


async def _counter_keys(request, user, model_id) -> list:
    """Counter key(s) this request draws from (team-budget attribution).

    Per (user, model) by default. When ``API_KEY_RATE_LIMIT_BY_GROUP`` is on:
    a user in exactly one group draws from that group's shared counter (no
    client change); a user in no group still counts per-user; a user in
    multiple groups must name which group's budget to charge via the
    ``X-RateLimit-Group`` header (400 if missing/unknown/ambiguous).
    """
    suffix = model_id or '*'
    config = request.app.state.config

    if not getattr(config, 'API_KEY_RATE_LIMIT_BY_GROUP', False):
        return [f'apikey:{user.id}:{suffix}']

    try:
        groups = await Groups.get_groups_by_member_id(user.id)
    except Exception:
        groups = []

    if not groups:
        # No group to attribute to — count per-user.
        return [f'apikey:{user.id}:{suffix}']
    if len(groups) == 1:
        # Single group: attribute automatically, no client change needed.
        return [f'apikey:group:{groups[0].id}:{suffix}']

    # Multiple groups: the caller must say which group's budget to charge.
    names = [g.name for g in groups]
    requested = (request.headers.get(RATE_LIMIT_GROUP_HEADER) or '').strip()
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"You belong to multiple groups; set the '{RATE_LIMIT_GROUP_HEADER}' "
                f"header to the group whose rate-limit budget to use. One of: "
                f"{', '.join(names)}."
            ),
        )
    matches = [g for g in groups if g.name == requested]
    if len(matches) == 1:
        return [f'apikey:group:{matches[0].id}:{suffix}']
    if not matches:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{requested}' is not one of your groups. One of: {', '.join(names)}."
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Group name '{requested}' is ambiguous (multiple groups share it); "
            f"give the groups unique names."
        ),
    )


async def check_api_key_rate_limit(request, user, model_id=None) -> None:
    """Raise HTTP 429 if this API-key request exceeds the active rate limit.

    No-op for UI sessions, admins, when the feature is disabled, or when the
    effective limit is negative (unlimited). A limit of 0 blocks all traffic.
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
    # Negative limit = unlimited; a non-positive window is treated as unlimited.
    if limit < 0 or window <= 0:
        return

    def _reject():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='API key rate limit exceeded. Please slow down and try again later.',
            headers={'Retry-After': str(window)},
        )

    # limit == 0 blocks all API-key traffic for this model/tier.
    if limit == 0:
        _reject()

    # RateLimiter buckets the window (default 60s) and sums num_buckets+1, so a
    # sub-60s window would collapse to one bucket and larger windows round up by
    # a full 60s. Scale the bucket to ~10 per window: sub-60s windows work and
    # the effective window overshoots by only ~10% (≈11 keys read per request).
    bucket_size = max(1, window // 10)
    limiter = RateLimiter(get_redis_client(), limit=limit, window=window, bucket_size=bucket_size)

    # Resolve the counter(s) — per-user, or the attributed group (may 400 if a
    # multi-group caller didn't name a group).
    keys = await _counter_keys(request, user, model_id)

    # Decide from the current count WITHOUT incrementing, so a rejected (429)
    # request does not consume quota; only record the hit once there's room.
    if any(limiter.get_count(k) >= limit for k in keys):
        _reject()

    for k in keys:
        limiter.is_limited(k)
