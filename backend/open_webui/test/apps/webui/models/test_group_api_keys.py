"""Group API keys: issuance, authentication, expiry, revocation and cleanup.

Runs against a throwaway SQLite database rather than the Postgres integration
harness, so it needs no Docker. The database URL has to be in the environment
*before* `open_webui.internal.db` is imported, since that module builds its
engines at import time.
"""

import os
import tempfile
import time
import uuid

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix='owui-group-api-key-test-')
os.environ.setdefault('DATA_DIR', _TMP_DIR)
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP_DIR}/test.db')
os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret')
# `open_webui.config` imports the chroma client eagerly for the default vector
# DB; this test touches none of it, so point it elsewhere to keep the import light.
os.environ.setdefault('VECTOR_DB', 'qdrant')

from types import SimpleNamespace  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from open_webui.internal.db import Base, engine  # noqa: E402
import open_webui.routers.groups as routers_groups  # noqa: E402
from open_webui.models.group_api_keys import (  # noqa: E402
    GROUP_API_KEY_PREFIX,
    MAX_GROUP_API_KEYS,
    GroupApiKeyForm,
    GroupApiKeys,
    key_hint,
)
from open_webui.models.groups import GroupForm, Groups  # noqa: E402
from open_webui.models.users import Users  # noqa: E402
from open_webui.utils.auth import get_current_user_by_api_key  # noqa: E402
from open_webui.utils.group_api_key import (  # noqa: E402
    create_group_api_key,
    ensure_group_service_account,
    group_service_account_id,
)


@pytest.fixture(scope='module', autouse=True)
def _database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _request(
    enable_api_keys=True,
    enable_api_keys_config=True,
    endpoint_restrictions=False,
    allowed_endpoints='',
    path='/api/chat/completions',
):
    """Minimal stand-in for the pieces of `Request` these paths read.

    `enable_api_keys` is the per-request snapshot the auth path checks;
    `enable_api_keys_config` is the admin setting the issuing endpoint checks.
    """
    return SimpleNamespace(
        state=SimpleNamespace(enable_api_keys=enable_api_keys),
        url=SimpleNamespace(path=path),
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    ENABLE_API_KEYS=enable_api_keys_config,
                    ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS=endpoint_restrictions,
                    API_KEYS_ALLOWED_ENDPOINTS=allowed_endpoints,
                    # Deliberately withheld: a group key must authenticate even
                    # when no one has the personal API-key permission.
                    USER_PERMISSIONS={'features': {'api_keys': False}},
                )
            )
        ),
    )


async def _make_group(name='Team'):
    owner_id = str(uuid.uuid4())
    await Users.insert_new_user(id=owner_id, name='Owner', email=f'{owner_id}@example.com', role='admin')
    group = await Groups.insert_new_group(owner_id, GroupForm(name=name, description=''))
    assert group is not None
    return group


async def _issue_key(group, expires_at=None):
    service_account = await ensure_group_service_account(group)
    assert service_account is not None

    api_key = await GroupApiKeys.insert_new_key(
        group_id=group.id,
        user_id=service_account.id,
        key=create_group_api_key(),
        name='ci',
        expires_at=expires_at,
    )
    assert api_key is not None
    return service_account, api_key


async def test_service_account_is_created_once_and_joins_the_group():
    group = await _make_group('Alpha')

    first = await ensure_group_service_account(group)
    second = await ensure_group_service_account(group)

    assert first is not None
    assert first.id == second.id == group_service_account_id(group.id)
    # It is a member of its own group — that membership is what carries the
    # group's permissions into a key's requests.
    assert await Groups.get_group_user_ids_by_id(group.id) == [first.id]

    # A renamed group renames its service account.
    renamed = await Groups.update_group_by_id(group.id, GroupForm(name='Alpha Renamed', description=''))
    third = await ensure_group_service_account(renamed)
    assert third is not None and 'Alpha Renamed' in third.name


async def test_group_key_authenticates_as_the_service_account():
    group = await _make_group('Bravo')
    service_account, api_key = await _issue_key(group)

    assert api_key.key.startswith(GROUP_API_KEY_PREFIX)
    # The list view only ever exposes a masked form.
    assert api_key.key not in key_hint(api_key.key)

    request = _request()
    user = await get_current_user_by_api_key(request, api_key.key)

    assert user.id == service_account.id
    assert request.state.api_key_group_id == group.id
    assert request.state.group_api_key_id == api_key.id

    # Usage is recorded on the key itself, not only on the account.
    assert (await GroupApiKeys.get_key_by_id(api_key.id)).last_used_at is not None


async def test_expired_group_key_is_rejected():
    group = await _make_group('Charlie')
    _, api_key = await _issue_key(group, expires_at=int(time.time()) - 1)

    with pytest.raises(HTTPException) as exc:
        await get_current_user_by_api_key(_request(), api_key.key)

    assert exc.value.status_code == 401


async def test_revoked_group_key_is_rejected():
    group = await _make_group('Delta')
    _, api_key = await _issue_key(group)

    assert await GroupApiKeys.delete_key_by_id(api_key.id)

    with pytest.raises(HTTPException) as exc:
        await get_current_user_by_api_key(_request(), api_key.key)

    assert exc.value.status_code == 401


async def test_group_key_honours_the_global_switch_and_endpoint_restrictions():
    group = await _make_group('Echo')
    _, api_key = await _issue_key(group)

    with pytest.raises(HTTPException) as exc:
        await get_current_user_by_api_key(_request(enable_api_keys=False), api_key.key)
    assert exc.value.status_code == 403

    blocked = _request(endpoint_restrictions=True, allowed_endpoints='/api/models', path='/api/chat/completions')
    with pytest.raises(HTTPException) as exc:
        await get_current_user_by_api_key(blocked, api_key.key)
    assert exc.value.status_code == 403

    allowed = _request(endpoint_restrictions=True, allowed_endpoints='/api/models', path='/api/models')
    assert (await get_current_user_by_api_key(allowed, api_key.key)) is not None


async def test_deleting_a_group_revokes_its_keys_and_service_account():
    group = await _make_group('Foxtrot')
    service_account, api_key = await _issue_key(group)

    assert await Groups.delete_group_by_id(group.id)

    assert await GroupApiKeys.get_key_by_key(api_key.key) is None
    assert await Users.get_user_by_id(service_account.id) is None

    with pytest.raises(HTTPException) as exc:
        await get_current_user_by_api_key(_request(), api_key.key)
    assert exc.value.status_code == 401


async def test_deleting_the_service_account_revokes_its_keys():
    group = await _make_group('Golf')
    service_account, api_key = await _issue_key(group)

    assert await Users.delete_user_by_id(service_account.id)
    assert await GroupApiKeys.get_key_by_key(api_key.key) is None


async def test_personal_api_keys_still_resolve_to_their_owner():
    user_id = str(uuid.uuid4())
    await Users.insert_new_user(id=user_id, name='Member', email=f'{user_id}@example.com', role='admin')

    personal_key = f'sk-{uuid.uuid4().hex}'
    assert await Users.update_user_api_key_by_id(user_id, personal_key)

    # Admin, so the withheld `features.api_keys` permission does not block it.
    user = await get_current_user_by_api_key(_request(), personal_key)
    assert user.id == user_id


####################
# Admin endpoints
#
# The handlers are called directly (their dependencies are plain arguments), so
# these cover the route guards without standing up the whole app.
####################


def _admin():
    return SimpleNamespace(id='admin-user', role='admin')


async def test_endpoints_issue_list_and_revoke():
    group = await _make_group('Hotel')
    request = _request()

    created = await routers_groups.create_group_api_key_by_group_id(
        request, group.id, GroupApiKeyForm(name='deploy bot'), user=_admin(), db=None
    )
    assert created.key.startswith(GROUP_API_KEY_PREFIX)
    assert created.created_by == 'admin-user'

    listed = await routers_groups.get_group_api_keys(group.id, user=_admin(), db=None)
    assert [key.id for key in listed] == [created.id]
    # The secret is returned exactly once, at creation.
    assert not hasattr(listed[0], 'key')
    assert listed[0].key_hint == key_hint(created.key)

    assert await routers_groups.delete_group_api_key_by_id(group.id, created.id, user=_admin(), db=None)
    assert await routers_groups.get_group_api_keys(group.id, user=_admin(), db=None) == []


async def test_endpoints_reject_unknown_group_and_cross_group_revocation():
    group = await _make_group('India')
    other = await _make_group('Juliett')

    with pytest.raises(HTTPException) as exc:
        await routers_groups.get_group_api_keys('does-not-exist', user=_admin(), db=None)
    assert exc.value.status_code == 404

    created = await routers_groups.create_group_api_key_by_group_id(
        _request(), group.id, GroupApiKeyForm(), user=_admin(), db=None
    )

    # A key can only be revoked through the group that owns it.
    with pytest.raises(HTTPException) as exc:
        await routers_groups.delete_group_api_key_by_id(other.id, created.id, user=_admin(), db=None)
    assert exc.value.status_code == 404
    assert await GroupApiKeys.get_key_by_id(created.id) is not None


async def test_create_endpoint_rejects_past_expiry_and_respects_the_global_switch():
    group = await _make_group('Kilo')

    with pytest.raises(HTTPException) as exc:
        await routers_groups.create_group_api_key_by_group_id(
            _request(),
            group.id,
            GroupApiKeyForm(expires_at=int(time.time()) - 60),
            user=_admin(),
            db=None,
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await routers_groups.create_group_api_key_by_group_id(
            _request(enable_api_keys_config=False), group.id, GroupApiKeyForm(), user=_admin(), db=None
        )
    assert exc.value.status_code == 403


async def test_create_endpoint_caps_the_number_of_keys_per_group():
    group = await _make_group('Lima')

    for _ in range(MAX_GROUP_API_KEYS):
        await routers_groups.create_group_api_key_by_group_id(
            _request(), group.id, GroupApiKeyForm(), user=_admin(), db=None
        )

    with pytest.raises(HTTPException) as exc:
        await routers_groups.create_group_api_key_by_group_id(
            _request(), group.id, GroupApiKeyForm(), user=_admin(), db=None
        )
    assert exc.value.status_code == 400
