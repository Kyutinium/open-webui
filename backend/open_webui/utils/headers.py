from urllib.parse import quote

from open_webui.env import (
    FORWARD_USER_INFO_HEADER_USER_NAME,
    FORWARD_USER_INFO_HEADER_USER_ID,
    FORWARD_USER_INFO_HEADER_USER_EMAIL,
    FORWARD_USER_INFO_HEADER_USER_ROLE,
    FORWARD_USER_INFO_HEADER_USER_D_INDEX,
)


def include_user_info_headers(headers, user):
    d_index = getattr(user, 'd_index', None)

    return {
        **headers,
        FORWARD_USER_INFO_HEADER_USER_NAME: quote(user.name, safe=' '),
        FORWARD_USER_INFO_HEADER_USER_ID: user.id,
        FORWARD_USER_INFO_HEADER_USER_EMAIL: user.email,
        FORWARD_USER_INFO_HEADER_USER_ROLE: user.role,
        # Left out entirely while the index is unresolved, so a receiver can tell
        # 'not resolved yet' (header absent) from 'belongs to no candidate
        # department' (0) without inventing a sentinel value.
        **({FORWARD_USER_INFO_HEADER_USER_D_INDEX: str(d_index)} if d_index is not None else {}),
    }
