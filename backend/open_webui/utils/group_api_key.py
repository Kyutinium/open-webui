"""Group API keys — shared credentials owned by a group, not by a person.

Open WebUI authenticates every request as a *user*: permissions, model access,
chat ownership and auditing all resolve from a `UserModel`. A key that belongs
to no user therefore has nothing to authenticate as.

A group key closes that gap with a **service account**: one hidden user row per
group, member of that group and nothing else. Three invariants make that a
boundary rather than a naming convention, and each is enforced somewhere:

1. **It is never an interactive identity.** Every login path that can issue a
   session for an *existing* user rejects it — `create_session_response`
   (signin / LDAP / signup / admin add / token exchange) and the OAuth callback,
   which can otherwise bind an IdP principal to any user row by email merge
   without an `auth` row ever existing. The missing `auth` row is a consequence,
   never the guard.
2. **It belongs to exactly its own group.** Enforced in the membership API
   itself (`Groups.add_users_to_group` / `remove_users_from_group` /
   `set_group_user_ids_by_id`), so SCIM, LDAP group sync, OAuth group management
   and the admin UI all inherit it.
3. **It stays an ordinary `role='user'` account.** Re-checked on every group-key
   request by `group_service_account_drift`, so a key can never silently inherit
   authority that drifted onto its account after the key was issued.
"""

import logging
from typing import Optional

from open_webui.models.group_api_keys import (
    GROUP_API_KEY_PREFIX,
    GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN,
    GROUP_SERVICE_ACCOUNT_ID_PREFIX,
    GROUP_SERVICE_ACCOUNT_INFO_KEY,
    GroupApiKeys,
    group_service_account_email,
    group_service_account_id,
    is_group_service_account_id,
)
from open_webui.models.groups import GroupModel, Groups
from open_webui.models.users import UserModel, Users
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

__all__ = [
    'GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN',
    'GROUP_SERVICE_ACCOUNT_ID_PREFIX',
    'GROUP_SERVICE_ACCOUNT_INFO_KEY',
    'create_group_api_key',
    'delete_group_service_account',
    'ensure_group_service_account',
    'get_group_service_account',
    'group_service_account_drift',
    'group_service_account_email',
    'group_service_account_id',
    'group_service_account_name',
    'is_group_api_key',
    'is_group_service_account',
]

# Role a service account must have: enough to pass `get_verified_user`, never
# more. Anything else is drift and blocks the key.
GROUP_SERVICE_ACCOUNT_ROLE = 'user'


def create_group_api_key() -> str:
    import uuid

    return f'{GROUP_API_KEY_PREFIX}{uuid.uuid4().hex}'


def is_group_api_key(key: str) -> bool:
    return key.startswith(GROUP_API_KEY_PREFIX)


def group_service_account_name(group: GroupModel) -> str:
    return f'{group.name} (Group API)'


def is_group_service_account(user: Optional[UserModel]) -> bool:
    """Whether *user* is a group service account.

    Deliberately OR-shaped over three independent signals (reserved id prefix,
    stored marker, reserved email domain): the callers are guards that must fail
    CLOSED, so any one signal is enough to keep a row out of an interactive
    login. A row that satisfies none of them is an ordinary user.
    """
    if user is None:
        return False

    if is_group_service_account_id(user.id):
        return True

    if isinstance(user.info, dict) and user.info.get(GROUP_SERVICE_ACCOUNT_INFO_KEY):
        return True

    return (user.email or '').endswith(f'@{GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN}')


async def get_group_service_account(group: GroupModel, db: Optional[AsyncSession] = None) -> Optional[UserModel]:
    return await Users.get_user_by_id(group_service_account_id(group.id), db=db)


async def ensure_group_service_account(group: GroupModel, db: Optional[AsyncSession] = None) -> Optional[UserModel]:
    """Return the group's service account, creating it on first use.

    The account is kept a member of its group: that membership is what grants
    a group key the group's permissions (`get_permissions` unions the caller's
    groups), so it is re-asserted on every issuance rather than assumed.
    """
    user_id = group_service_account_id(group.id)
    marker = {GROUP_SERVICE_ACCOUNT_INFO_KEY: {'group_id': group.id}}
    user = await Users.get_user_by_id(user_id, db=db)

    if user is None:
        user = await Users.insert_new_user(
            id=user_id,
            name=group_service_account_name(group),
            email=group_service_account_email(group.id),
            role=GROUP_SERVICE_ACCOUNT_ROLE,
            db=db,
        )
        if user is None:
            log.error(f'Failed to create the service account for group {group.id}')
            return None

        user = (await Users.update_user_by_id(user_id, {'info': marker}, db=db)) or user
    else:
        updated = {}
        if user.name != group_service_account_name(group):
            # Keep the account's display name in step with a renamed group.
            updated['name'] = group_service_account_name(group)
        if not (isinstance(user.info, dict) and user.info.get(GROUP_SERVICE_ACCOUNT_INFO_KEY)):
            # Heal a row created before the marker existed.
            updated['info'] = {**(user.info or {}), **marker}
        if updated:
            user = (await Users.update_user_by_id(user_id, updated, db=db)) or user

    # Idempotent: duplicate memberships are ignored by add_users_to_group.
    await Groups.add_users_to_group(group.id, [user_id], db=db)

    return user


async def group_service_account_drift(
    user: UserModel, group_id: str, db: Optional[AsyncSession] = None
) -> Optional[str]:
    """Why *user* may not act for *group_id*, or `None` when it may.

    Issuance-time checks cannot protect a key that was issued earlier, so a
    group key re-verifies its account on every request: the account is the one
    derived from the key's own group, it is still marked as a service account,
    it is still an ordinary user, and it is a member of that group and no other.
    Extra membership or a raised role would otherwise be inherited silently by
    every key already in circulation.
    """
    expected_id = group_service_account_id(group_id)
    if user.id != expected_id:
        return f'key is bound to {user.id}, which is not the service account of group {group_id}'

    if not is_group_service_account(user):
        return f'{user.id} is no longer marked as a group service account'

    if user.role != GROUP_SERVICE_ACCOUNT_ROLE:
        return f"{user.id} has role '{user.role}', expected '{GROUP_SERVICE_ACCOUNT_ROLE}'"

    group_ids = {group.id for group in await Groups.get_groups_by_member_id(user.id, db=db)}
    if group_ids != {group_id}:
        return f'{user.id} must belong to group {group_id} only, but belongs to {sorted(group_ids) or "no group"}'

    return None


async def delete_group_service_account(group_id: str, db: Optional[AsyncSession] = None) -> None:
    """Drop a group's keys and its service account (used when a group is deleted)."""
    await GroupApiKeys.delete_keys_by_group_id(group_id, db=db)

    user_id = group_service_account_id(group_id)
    if await Users.get_user_by_id(user_id, db=db):
        await Users.delete_user_by_id(user_id, db=db)
