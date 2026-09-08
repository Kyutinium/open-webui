import logging
import time
import uuid
from typing import Optional

from open_webui.internal.db import Base, get_async_db_context
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

####################
# GroupApiKey DB Schema
#
# A group API key is a shared credential owned by a GROUP rather than by a
# person. It authenticates as the group's service account (a hidden user row,
# see `open_webui.utils.group_api_key`) so every downstream permission check
# keeps working unchanged, while issuance, listing and revocation are managed
# per group by an admin.
####################

# Group keys carry their own prefix so the auth path can tell them apart from
# personal keys without a database round-trip. It still starts with `sk-` —
# that is what `get_current_user` uses to route a token to API-key auth.
GROUP_API_KEY_PREFIX = 'sk-grp-'

# Upper bound on live keys per group, so a rotation loop cannot grow unbounded.
MAX_GROUP_API_KEYS = 20

####################
# Service account identity
#
# These are deliberately pure (no user/group imports) so the membership layer in
# `models.groups` can enforce the service-account invariants without a circular
# import. `open_webui.utils.group_api_key` builds the user-facing helpers on top.
####################

# Reserved id prefix. Deterministic per group, so the binding between a key and
# its account can be re-derived and verified rather than trusted.
GROUP_SERVICE_ACCOUNT_ID_PREFIX = 'group-api-'

# Reserved email domain: emails are unique in the user table, so a directory
# account can never collide with (or claim) a service account.
GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN = 'group-api.local'

# Marker written into `user.info`, so "this is a service account" is an explicit
# stored fact and not only an inference from the id or email shape.
GROUP_SERVICE_ACCOUNT_INFO_KEY = 'group_api_service_account'


def group_service_account_id(group_id: str) -> str:
    return f'{GROUP_SERVICE_ACCOUNT_ID_PREFIX}{group_id}'


def group_service_account_email(group_id: str) -> str:
    return f'{group_id}@{GROUP_SERVICE_ACCOUNT_EMAIL_DOMAIN}'


def is_group_service_account_id(user_id: Optional[str]) -> bool:
    return bool(user_id) and user_id.startswith(GROUP_SERVICE_ACCOUNT_ID_PREFIX)


class GroupApiKey(Base):
    __tablename__ = 'group_api_key'

    id = Column(Text, primary_key=True, unique=True)
    group_id = Column(
        Text,
        ForeignKey('group.id', ondelete='CASCADE'),
        nullable=False,
    )
    # Service account the key authenticates as (one per group).
    user_id = Column(Text, nullable=False)
    # Admin who issued the key; kept for auditing, never used for authorization.
    created_by = Column(Text, nullable=True)

    name = Column(Text, nullable=True)
    key = Column(Text, unique=True, nullable=False)

    expires_at = Column(BigInteger, nullable=True)
    last_used_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index('idx_group_api_key_group_id', 'group_id'),)


class GroupApiKeyModel(BaseModel):
    id: str
    group_id: str
    user_id: str
    created_by: Optional[str] = None

    name: Optional[str] = None
    key: str

    expires_at: Optional[int] = None
    last_used_at: Optional[int] = None
    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)

    def is_expired(self, at: Optional[int] = None) -> bool:
        if not self.expires_at:
            return False
        return self.expires_at <= (at if at is not None else int(time.time()))


####################
# Forms
####################


class GroupApiKeyForm(BaseModel):
    name: Optional[str] = None
    expires_at: Optional[int] = None


class GroupApiKeyResponse(BaseModel):
    """A key as listed in the admin UI — the secret itself is never returned."""

    id: str
    group_id: str
    user_id: str
    created_by: Optional[str] = None

    name: Optional[str] = None
    key_hint: str

    expires_at: Optional[int] = None
    last_used_at: Optional[int] = None
    created_at: int
    updated_at: int


class GroupApiKeyCreateResponse(GroupApiKeyResponse):
    """Returned once, at creation time — the only time the secret is shown."""

    key: str


def key_hint(key: str) -> str:
    """Masked form of a key, safe to show in a list (prefix + last 4 chars)."""
    return f'{key[: len(GROUP_API_KEY_PREFIX)]}...{key[-4:]}'


def to_response(api_key: GroupApiKeyModel) -> GroupApiKeyResponse:
    return GroupApiKeyResponse(
        **api_key.model_dump(exclude={'key'}),
        key_hint=key_hint(api_key.key),
    )


class GroupApiKeysTable:
    async def insert_new_key(
        self,
        group_id: str,
        user_id: str,
        key: str,
        name: Optional[str] = None,
        expires_at: Optional[int] = None,
        created_by: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[GroupApiKeyModel]:
        try:
            async with get_async_db_context(db) as db:
                now = int(time.time())
                api_key = GroupApiKey(
                    id=str(uuid.uuid4()),
                    group_id=group_id,
                    user_id=user_id,
                    created_by=created_by,
                    name=name,
                    key=key,
                    expires_at=expires_at,
                    last_used_at=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(api_key)
                await db.commit()
                await db.refresh(api_key)
                return GroupApiKeyModel.model_validate(api_key)
        except Exception as e:
            log.exception(f'Error creating an API key for group {group_id}: {e}')
            return None

    async def get_key_by_key(self, key: str, db: Optional[AsyncSession] = None) -> Optional[GroupApiKeyModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(GroupApiKey).filter_by(key=key))
                api_key = result.scalars().first()
                return GroupApiKeyModel.model_validate(api_key) if api_key else None
        except Exception:
            return None

    async def get_key_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[GroupApiKeyModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(GroupApiKey).filter_by(id=id))
                api_key = result.scalars().first()
                return GroupApiKeyModel.model_validate(api_key) if api_key else None
        except Exception:
            return None

    async def get_keys_by_group_id(self, group_id: str, db: Optional[AsyncSession] = None) -> list[GroupApiKeyModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(GroupApiKey).filter_by(group_id=group_id).order_by(GroupApiKey.created_at.desc())
            )
            return [GroupApiKeyModel.model_validate(api_key) for api_key in result.scalars().all()]

    async def count_keys_by_group_id(self, group_id: str, db: Optional[AsyncSession] = None) -> int:
        return len(await self.get_keys_by_group_id(group_id, db=db))

    async def update_last_used_by_id(self, id: str, db: Optional[AsyncSession] = None) -> bool:
        try:
            async with get_async_db_context(db) as db:
                await db.execute(update(GroupApiKey).filter_by(id=id).values(last_used_at=int(time.time())))
                await db.commit()
                return True
        except Exception:
            return False

    async def delete_key_by_id(self, id: str, db: Optional[AsyncSession] = None) -> bool:
        try:
            async with get_async_db_context(db) as db:
                await db.execute(delete(GroupApiKey).filter_by(id=id))
                await db.commit()
                return True
        except Exception:
            return False

    async def delete_keys_by_group_id(self, group_id: str, db: Optional[AsyncSession] = None) -> bool:
        try:
            async with get_async_db_context(db) as db:
                await db.execute(delete(GroupApiKey).filter_by(group_id=group_id))
                await db.commit()
                return True
        except Exception:
            return False

    async def delete_keys_by_user_id(self, user_id: str, db: Optional[AsyncSession] = None) -> bool:
        try:
            async with get_async_db_context(db) as db:
                await db.execute(delete(GroupApiKey).filter_by(user_id=user_id))
                await db.commit()
                return True
        except Exception:
            return False


GroupApiKeys = GroupApiKeysTable()
