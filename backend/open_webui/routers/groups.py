import os
import time
from pathlib import Path
from typing import Optional
import logging

from open_webui.models.users import Users, UserInfoResponse
from open_webui.models.groups import (
    Groups,
    GroupForm,
    GroupInfoResponse,
    GroupUpdateForm,
    GroupResponse,
    UserIdsForm,
)
from open_webui.models.group_api_keys import (
    MAX_GROUP_API_KEYS,
    GroupApiKeyCreateResponse,
    GroupApiKeyForm,
    GroupApiKeyResponse,
    GroupApiKeys,
    key_hint,
    to_response,
)
from open_webui.utils.group_api_key import create_group_api_key, ensure_group_service_account

from open_webui.config import CACHE_DIR
from open_webui.constants import ERROR_MESSAGES
from fastapi import APIRouter, Depends, HTTPException, Request, status

from open_webui.internal.db import get_async_session
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.utils.auth import get_admin_user, get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()

############################
# GetFunctions
############################


@router.get('/', response_model=list[GroupResponse])
async def get_groups(
    share: Optional[bool] = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    filter = {}

    # Admins can share to all groups regardless of share setting
    if user.role != 'admin':
        filter['member_id'] = user.id
        if share is not None:
            filter['share'] = share

    groups = await Groups.get_groups(filter=filter, db=db)

    return groups


############################
# CreateNewGroup
############################


@router.post('/create', response_model=Optional[GroupResponse])
async def create_new_group(
    form_data: GroupForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        group = await Groups.insert_new_group(user.id, form_data, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error creating group'),
            )
    except Exception as e:
        log.exception(f'Error creating a new group: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# GetGroupById
############################


@router.get('/id/{id}', response_model=Optional[GroupResponse])
async def get_group_by_id(id: str, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    group = await Groups.get_group_by_id(id, db=db)
    if group:
        return GroupResponse(
            **group.model_dump(),
            member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get('/id/{id}/info', response_model=Optional[GroupInfoResponse])
async def get_group_info_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    group = await Groups.get_group_by_id(id, db=db)
    if group:
        return GroupInfoResponse(
            **group.model_dump(),
            member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# ExportGroupById
############################


class GroupExportResponse(GroupResponse):
    user_ids: list[str] = []
    pass


@router.get('/id/{id}/export', response_model=Optional[GroupExportResponse])
async def export_group_by_id(id: str, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    group = await Groups.get_group_by_id(id, db=db)
    if group:
        return GroupExportResponse(
            **group.model_dump(),
            member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
            user_ids=await Groups.get_group_user_ids_by_id(group.id, db=db),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# GetUsersInGroupById
############################


@router.post('/id/{id}/users', response_model=list[UserInfoResponse])
async def get_users_in_group(id: str, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    try:
        users = await Users.get_users_by_group_id(id, db=db)
        return users
    except Exception as e:
        log.exception(f'Error adding users to group {id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# UpdateGroupById
############################


@router.post('/id/{id}/update', response_model=Optional[GroupResponse])
async def update_group_by_id(
    id: str,
    form_data: GroupUpdateForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        group = await Groups.update_group_by_id(id, form_data, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error updating group'),
            )
    except Exception as e:
        log.exception(f'Error updating group {id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# AddUserToGroupByUserIdAndGroupId
############################


@router.post('/id/{id}/users/add', response_model=Optional[GroupResponse])
async def add_user_to_group(
    id: str,
    form_data: UserIdsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        if form_data.user_ids:
            form_data.user_ids = await Users.get_valid_user_ids(form_data.user_ids, db=db)

        group = await Groups.add_users_to_group(id, form_data.user_ids, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error adding users to group'),
            )
    except Exception as e:
        log.exception(f'Error adding users to group {id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


@router.post('/id/{id}/users/remove', response_model=Optional[GroupResponse])
async def remove_users_from_group(
    id: str,
    form_data: UserIdsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        group = await Groups.remove_users_from_group(id, form_data.user_ids, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=await Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error removing users from group'),
            )
    except Exception as e:
        log.exception(f'Error removing users from group {id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# Group API Keys
#
# A shared, group-owned credential: it authenticates as the group's service
# account instead of a person, so it survives staff changes and never carries
# an individual's identity. Admin-managed, like every other group setting.
############################


async def _get_group_or_404(id: str, db: AsyncSession):
    group = await Groups.get_group_by_id(id, db=db)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return group


@router.get('/id/{id}/api_keys', response_model=list[GroupApiKeyResponse])
async def get_group_api_keys(id: str, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    await _get_group_or_404(id, db)
    keys = await GroupApiKeys.get_keys_by_group_id(id, db=db)
    return [to_response(key) for key in keys]


@router.post('/id/{id}/api_keys', response_model=GroupApiKeyCreateResponse)
async def create_group_api_key_by_group_id(
    request: Request,
    id: str,
    form_data: GroupApiKeyForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    if not request.app.state.config.ENABLE_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.API_KEY_CREATION_NOT_ALLOWED,
        )

    group = await _get_group_or_404(id, db)

    if await GroupApiKeys.count_keys_by_group_id(id, db=db) >= MAX_GROUP_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(f'A group can hold at most {MAX_GROUP_API_KEYS} API keys. Revoke one first.'),
        )

    if form_data.expires_at is not None and form_data.expires_at <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('The expiration date must be in the future.'),
        )

    name = (form_data.name or '').strip()[:100] or None

    # The service account is created on first issuance and re-joined to the
    # group every time, since its membership is what carries the permissions.
    service_account = await ensure_group_service_account(group, db=db)
    if service_account is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ERROR_MESSAGES.CREATE_API_KEY_ERROR,
        )

    api_key = await GroupApiKeys.insert_new_key(
        group_id=id,
        user_id=service_account.id,
        key=create_group_api_key(),
        name=name,
        expires_at=form_data.expires_at,
        created_by=user.id,
        db=db,
    )

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ERROR_MESSAGES.CREATE_API_KEY_ERROR,
        )

    # The only response that carries the secret; afterwards only the hint is
    # ever returned, so the admin must copy it now.
    return GroupApiKeyCreateResponse(
        **api_key.model_dump(),
        key_hint=key_hint(api_key.key),
    )


@router.delete('/id/{id}/api_keys/{key_id}', response_model=bool)
async def delete_group_api_key_by_id(
    id: str,
    key_id: str,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _get_group_or_404(id, db)

    api_key = await GroupApiKeys.get_key_by_id(key_id, db=db)
    # Scoped to the group in the path, so a key can never be revoked through
    # another group's endpoint.
    if api_key is None or api_key.group_id != id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    return await GroupApiKeys.delete_key_by_id(key_id, db=db)


############################
# DeleteGroupById
############################


@router.delete('/id/{id}/delete', response_model=bool)
async def delete_group_by_id(id: str, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    try:
        result = await Groups.delete_group_by_id(id, db=db)
        if result:
            return result
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error deleting group'),
            )
    except Exception as e:
        log.exception(f'Error deleting group {id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )
