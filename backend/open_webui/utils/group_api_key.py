"""Group API keys — shared credentials owned by a group, not by a person.

Open WebUI authenticates every request as a *user*: permissions, model access,
chat ownership and auditing all resolve from a `UserModel`. A key that belongs
to no user therefore has nothing to authenticate as.

A group key closes that gap with a **service account**: one hidden user row per
group, member of that group and nothing else, with no `auth` row (so it can
never sign in through the login form, LDAP or OAuth). The key resolves to that
account, so the whole permission stack downstream keeps working unchanged while
the credential itself is issued, listed and revoked per group by an admin.
"""

import logging
import uuid
from typing import Optional

from open_webui.models.group_api_keys import GROUP_API_KEY_PREFIX, GroupApiKeys
from open_webui.models.groups import GroupModel, Groups
from open_webui.models.users import UserModel, Users
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Emails are unique in the user table; this domain is reserved so a service
# account can never collide with (or be claimed by) a real directory account.
GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN = 'group-api.local'


def create_group_api_key() -> str:
    return f'{GROUP_API_KEY_PREFIX}{uuid.uuid4().hex}'


def is_group_api_key(key: str) -> bool:
    return key.startswith(GROUP_API_KEY_PREFIX)


def group_service_account_id(group_id: str) -> str:
    return f'group-api-{group_id}'


def group_service_account_email(group_id: str) -> str:
    return f'{group_id}@{GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN}'


def group_service_account_name(group: GroupModel) -> str:
    return f'{group.name} (Group API)'


def is_group_service_account(user: UserModel) -> bool:
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
    user = await Users.get_user_by_id(user_id, db=db)

    if user is None:
        user = await Users.insert_new_user(
            id=user_id,
            name=group_service_account_name(group),
            email=group_service_account_email(group.id),
            role='user',
            db=db,
        )
        if user is None:
            log.error(f'Failed to create the service account for group {group.id}')
            return None
    elif user.name != group_service_account_name(group):
        # Keep the account's display name in step with a renamed group.
        user = (await Users.update_user_by_id(user_id, {'name': group_service_account_name(group)}, db=db)) or user

    # Idempotent: duplicate memberships are ignored by add_users_to_group.
    await Groups.add_users_to_group(group.id, [user_id], db=db)

    return user


async def delete_group_service_account(group_id: str, db: Optional[AsyncSession] = None) -> None:
    """Drop a group's keys and its service account (used when a group is deleted)."""
    await GroupApiKeys.delete_keys_by_group_id(group_id, db=db)

    user_id = group_service_account_id(group_id)
    if await Users.get_user_by_id(user_id, db=db):
        await Users.delete_user_by_id(user_id, db=db)
