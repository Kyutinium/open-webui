# Custom Changes Log

이 문서는 upstream open-webui에서 커스텀으로 추가/변경한 내역을 기록합니다.
업그레이드나 머지 시 이 변경사항들이 유지되어야 합니다.

---

## 1. Image Gallery Sidebar

이미지 서버의 이미지를 오른쪽 사이드바에서 넘겨볼 수 있는 기능.
문서 썸네일 클릭 시 해당 문서의 **모든 페이지를 넘겨볼 수 있는** 이미지 뷰어.

### Backend
- **`backend/open_webui/routers/image_proxy.py`** (new)
  - `GET /api/v1/image_proxy/get_image_list` - 이미지 서버에 폴더 내 이미지 목록 프록시
  - `GET /api/v1/image_proxy/get_image` - 개별 이미지 프록시
  - 환경변수: `IMAGE_SERVER_BASE`, `IMAGE_INTERNAL_SECRET`, `IMAGE_TLS_VERIFY`
- **`backend/open_webui/main.py`** - `image_proxy` router import 및 등록

### Frontend
- **`src/lib/components/chat/ImageGallerySidebar.svelte`** (new)
  - panzoom 줌/팬 지원
  - 이전/다음 키보드 네비게이션 (Arrow Left/Right, Escape)
  - **Lazy page discovery**: URL 패턴 기반 (`/page/1.png` → `2.png`, `3.png`...) 순차 탐색
    - 클릭한 페이지 즉시 표시, 주변 페이지 백그라운드 탐색
    - 100+ 페이지도 로딩 지연 없음
  - 하단 썸네일 스트립 (auto-scroll + drag-scroll, 드래그 후 2초간 auto-scroll 차단)
  - 두 가지 모드: Direct URL (MCP 썸네일) / URL 패턴 기반 페이지 탐색
- **`src/lib/stores/index.ts`** - `showImageGallery`, `imageGalleryData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ImageGallerySidebar를 special panel로 통합 (mobile + desktop)
- **`src/lib/components/chat/Chat.svelte`** - showImageGallery subscribe/cleanup
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`** - `image_gallery` type 처리
- **`src/lib/utils/marked/extension.ts`** - `detailsStart` 정규식 수정 (`<details[\s>]`)

---

## 2. Tool Explorer Sidebar

MCP 도구 검색 결과를 오른쪽 사이드바에서 실시간으로 탐색하는 기능.

### Frontend
- **`src/lib/components/chat/ToolExplorerSidebar.svelte`** (new)
  - **All 탭** + 도구별 탭 (Confluence, Jira, MLM 등)
  - **전체 검색**: 모든 탭의 결과를 통합 검색 (탭 무시)
  - 검색 시 collapsible 자동 펼침 + 매칭 결과만 표시
  - 검색 안 할 때: 탭이 필터 역할 + 수동 접기/펼치기
  - 검색 중 소스 뱃지 표시 ([MLM] [Confluence])
  - 썸네일 클릭 → Image Gallery 연동 (같은 문서의 페이지 넘기기)
  - 원본 문서 링크, doc_type 뱃지, content snippet
  - **실시간 업데이트**: 스트리밍 중 MCP 결과 도착할 때마다 사이드바 자동 오픈 + merge (dedup)
  - 스트리밍 완료 후 "검색된 문서 보기" 버튼으로 사이드바 다시 열기
  - 이전 채팅 이동 시에도 결과가 있으면 사이드바 자동 오픈
  - 에러 결과 (metadata.error) 자동 필터링
  - chatId 기반 데이터 분리 (채팅 간 결과 혼재 방지)
- **`src/lib/stores/index.ts`** - `showToolExplorer`, `toolExplorerData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ToolExplorerSidebar 통합 (mobile + desktop)
- **`src/lib/components/chat/Chat.svelte`** - showToolExplorer/toolExplorerData subscribe/cleanup/chatId 기반 초기화
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`**
  - `tool_explorer` type: `use:action`으로 자동 오픈 + merge (chatId 추적)
  - `search_results_button` type: 스트림 완료 후 "검색된 문서 보기" 버튼

---

## 3. MCP Tool Selector

메시지 입력창 옆에 MCP 도구 선택 드롭다운. 사용자가 검색에 사용할 도구를 개별 선택 가능.

### Backend
- **`backend/open_webui/routers/mcp_tools.py`** (new)
  - `GET /api/v1/mcp_tools` - 마운트된 `mcp-config.json` 읽어서 도구 목록 반환
  - 환경변수: `MCP_CONFIG_PATH` (default: `/app/mcp-config.json`)
  - 도구 표시명: config의 `description` 필드 사용
- **`backend/open_webui/main.py`** - `mcp_tools` router 등록

### Frontend
- **`src/lib/components/chat/McpToolSelector.svelte`** (new)
  - Open WebUI `Dropdown` + `Switch` 컴포넌트 사용 (IntegrationsMenu 패턴)
  - All 토글 + 개별 도구 토글
  - 선택 상태 module-level 캐싱 (채팅 전환해도 유지)
  - 페이지 로드 시 전체 선택이 기본값
- **`src/lib/components/chat/MessageInput.svelte`** - McpToolSelector 추가
- **`src/lib/components/chat/Placeholder.svelte`** - 초기 화면에서도 selectedMcpTools 전달
- **`src/lib/components/chat/Chat.svelte`** - `mcp_tools` body에 포함하여 pipe에 전달

### Gateway
- **`src/models.py`** - `allowed_tools: Optional[List[str]]` 필드 추가
- **`src/backends/claude/client.py`** - `allowed_tools` 있으면 해당 도구만 활성화, MCP 서버도 선택된 것만 등록
- **Docker compose** - `mcp-config.json` 볼륨 마운트 (`:ro`)

### Pipe
- `body.mcp_tools` 읽어서 base SDK tools + 선택된 MCP patterns를 `allowed_tools`로 gateway에 전달

---

## 4. Chatdragon Completions Pipe

Gateway의 Claude Code SDK와 Open WebUI를 연결하는 파이프라인.

### File
- **`pipelines_dev/chatdragon_completions.py`**

### Key Custom Features
- **Tool Explorer 실시간 emission**: MCP tool_result마다 즉시 `<details type="tool_explorer">` 태그 emit
- **search_results_button**: 스트림 종료 시 전체 결과를 포함한 버튼 태그 emit
- **구조화된 결과 추출**: title, content, url(edm_link), thumbnail(thumbnail_url), doc_type 다양한 필드명 지원
- **Confluence URL 추출**: `_links.webui` + `space._links.self`에서 base URL, 또는 `viewpage.action?pageId=` fallback
- **에러 필터링**: metadata.error 또는 오류 content 결과 제외
- **Persisted-output 처리**: SDK가 큰 결과를 파일로 저장 후 Read로 읽는 패턴 감지 → file_path 매칭으로 원래 MCP 도구에 연결
- **Content parsing**: content block 리스트, Python repr, cat-n 줄 번호, double-escaped JSON, Extra data truncation
- **한글 유저 이름 URL 인코딩**: HTTP 헤더 ASCII 인코딩 에러 방지
- **MCP tool selection**: `body.mcp_tools`를 `allowed_tools`로 gateway에 전달
- **Image Gallery tag emission**: IMAGE_SERVER_BASE URL 감지 시 갤러리 태그 자동 생성

---

## 5. Gateway Changes (claude-code-gateway)

- **`src/models.py`** - `allowed_tools` 파라미터 추가
- **`src/backends/claude/client.py`** - 선택된 MCP 서버만 등록 (allowed_tools 패턴 매칭)
- **`src/streaming_utils.py`** - `SUBAGENT_STREAM_PROGRESS=false`가 non-subagent task event도 차단하던 버그 수정

---

## 6. Docker Compose Files

- **`docker-compose.pipelines.dev.yaml`** - Pipeline 서비스 dev 환경 (port 9098, PYTHONIOENCODING=utf-8, shared_images volume mount)
- **`docker-compose.rc.dev.yaml`** - Open WebUI RC dev 빌드용, mcp-config.json 볼륨 마운트

---

## 7. Scripts

- **`scripts/dev_fast_restart.sh`** - npm build + docker build + pipelines restart 자동화
- **`scripts/start_dev.sh`** - Frontend (Vite hot reload) + Backend (uvicorn --reload) + Pipelines restart

---

## 8. SSG D Index (부서 index)

로그인한 유저가 어느 부서에 속하는지를 `user.d_index`에 저장하는 기능.
접근 권한 체크(`SSG_DEPT_CODES`)와는 **별개의 후보 리스트**를 사용한다 —
권한 체크용 SSG 그룹 하나에는 여러 부서가 섞여 있을 수 있어서, index 판정에는
"SSG 코드 1개 = 부서 1개"인 리스트를 따로 받는다.

### 환경변수
- **`SSG_D_INDEX_CODES`** (JSON 배열, 순서가 의미를 가짐) - 부서 후보 리스트.
  유저가 속한 **첫 번째** 코드의 1-based 위치가 `d_index`가 된다.
  순서를 바꾸면 이미 저장된 index의 의미도 바뀌므로 주의.
- **`SSG_D_INDEX_REFRESH_ON_LOGIN`** (기본 `false`) - `false`면 index를 한 번만
  판정(신규 가입 시, 또는 컬럼 추가 후 첫 로그인)하고 이후 그대로 둔다. `true`면
  매 로그인마다 재판정한다 (로그인마다 후보 코드 수만큼 SSO 요청 발생).

### 저장 값
- `1..N` - 후보 리스트의 N번째 부서에 속함
- `0` - 후보 리스트 중 어디에도 속하지 않음
- `NULL` - 아직 판정 안 됨 (컬럼 추가 이전에 생성된 유저, 또는 SSO 요청이 전부 실패)

### Backend
- **`backend/open_webui/migrations/versions/d1a2b3c4e5f6_add_d_index_to_user_table.py`** (new)
  - `user.d_index` (Integer, nullable) 추가. server default 없이 nullable이라
    기존 행은 `NULL`로 남고, 다음 로그인 때 채워진다 (0으로 단정하지 않음)
- **`backend/open_webui/models/users.py`** - `User.d_index` 컬럼 + `UserModel.d_index` 필드
- **`backend/open_webui/config.py`** - `SSG_D_INDEX_CODES`, `SSG_D_INDEX_REFRESH_ON_LOGIN`
- **`backend/open_webui/utils/oauth.py`**
  - `query_sso_dept_membership(user_data, dept_codes)` - 기존 SSO 조회 루프를 헬퍼로 추출
    (권한 체크 블록도 이 헬퍼를 쓰도록 변경, 동작은 동일)
  - `resolve_d_index(user_data)` - `SSG_D_INDEX_CODES` 기준 index 판정
  - OAuth 콜백: 신규 유저는 생성 직후 저장, 기존 유저는 `d_index`가 `NULL`일 때
    (또는 refresh 옵션이 켜져 있을 때) 판정해서 저장. index 판정은 절대 로그인을 막지 않는다

### Gateway로 전달 (`X-OpenWebUI-User-D-Index`)

`X-OpenWebUI-User-Id` 등과 같은 방식으로 헤더에 실어 보낸다. **미판정(`NULL`)이면
헤더 자체를 붙이지 않는다** - 받는 쪽이 "아직 판정 안 됨"과 "어느 부서에도 안 속함(`0`)"을
구분할 수 있게 하기 위함 (sentinel 값을 만들지 않는다).

- **`backend/open_webui/env.py`** - `FORWARD_USER_INFO_HEADER_USER_D_INDEX`
  (기본 `X-OpenWebUI-User-D-Index`)
- **`backend/open_webui/utils/headers.py`** - `include_user_info_headers()`에 추가.
  이 헬퍼가 유일한 choke point이므로 openai/ollama/terminals/retrieval/tools 등
  기존 forwarding 경로 전부가 자동으로 이 헤더를 함께 보낸다
  (기존 `ENABLE_FORWARD_USER_INFO_HEADERS` 플래그에 그대로 종속 - 기본값 `False`)
- **`backend/open_webui/routers/openai.py`** - pipeline 모델용 `payload['user']`에
  `d_index` 추가. 이 dict는 화이트리스트라서 명시하지 않으면 pipe가 볼 수 없다.
  이 경로는 `ENABLE_FORWARD_USER_INFO_HEADERS`와 무관하게 항상 전달된다
- **`pipelines_dev/*.py` (9개 pipe 전부)** - `extra_headers`에
  `X-OpenWebUI-User-D-Index` 추가. `meta_headers`(core가 forward한 헤더)를 우선
  보고, 없으면 `__user__["d_index"]`로 fallback - 기존 `X-OpenWebUI-User-Name`
  블록과 동일한 패턴. `None`이면 헤더를 붙이지 않고, `0`은 그대로 보낸다

---

## 9. Sign Out All Users (전체 세션 무효화)

관리자 패널에서 **모든 사용자의 세션을 한 번에 종료**하는 기능. `d_index`는 SSO 로그인
콜백에서만 판정되므로, 이미 로그인해 있는 유저는 JWT가 만료될 때까지(기본 `4w`) 값이
채워지지 않는다. 전원 재로그인을 강제하기 위해 필요.

### 왜 새 메커니즘인가

`utils/auth.py`의 기존 revocation 두 가지(`jti` per-token, `revoked_at` per-user)는
**Redis 전용**이다 (`if request.app.state.redis:`). 이 배포에는 Redis가 없어서 둘 다
no-op이므로, Redis 없이도 동작하는 방식이 필요했다 — **cutoff 시각 하나를 DB에 저장**하고
그보다 이전에 발급된 토큰을 전부 거부한다 (토큰 목록을 관리하지 않으므로 O(1)).

### 동작

- `AUTH_SESSIONS_REVOKED_AT` (`PersistentConfig`, config path `auth.sessions_revoked_at`,
  기본 `0` = 무효화 이력 없음). env 변수가 아니라 런타임 상태이며 `config` 테이블에 저장돼
  재시작에도 유지된다. `AppConfig.__setattr__`가 DB 저장 + (Redis가 있으면) 미러링까지
  자동 처리하므로 멀티 워커/레플리카에도 전파된다
- `is_session_revoked(decoded, revoked_at)` - 토큰의 `iat <= cutoff`면 무효.
  `iat`가 없는 토큰은 **발급 시각을 알 수 없으므로 무효로 간주**(fail closed, 기존 Redis
  경로와 동일 규칙). cutoff가 파싱 불가면 **fail OPEN** — 잘못된 설정값 하나로 전원이
  영구 잠기는 상황을 만들지 않는다. `iat`는 float로 비교한다 (초 단위로 잘라내면 cutoff와
  같은 초에 갓 로그인한 유저를 불필요하게 한 번 더 튕긴다)
- **API 키는 영향 없음** — `get_current_user`의 API 키 분기는 JWT 검증 전에 return하며,
  API 키에는 `iat`가 없다

### 적용 지점 (HTTP만 막으면 소켓으로 살아남는다)

- `utils/auth.py: get_current_user` - 세션 토큰 전체. 기존 `jti` 체크보다 **앞에** 둔다
  (`jti`가 없는 토큰도 걸러야 하므로)
- `socket/main.py` - `decode_token` 4곳 전부 (connect + 3개 핸들러)
- `routers/terminals.py` - 터미널 websocket 인증
- `main.py: /api/config` - 무효 세션은 익명으로 처리 (로그인 전 config도 서비스하므로 401이 아님)

### 엔드포인트 / UI

- **`POST /api/v1/auths/admin/signout/all`** (`get_admin_user`) - cutoff을 현재 시각으로
  기록하고 `{'revoked_at': <ts>}` 반환. **호출한 관리자 본인도 로그아웃된다**
- **관리자 패널 → 설정 → 일반**, `JWT Expiration` 바로 아래에 버튼 + 확인 다이얼로그.
  성공 시 로컬 토큰을 지우고 `/auth`로 이동 (본인 토큰도 죽었으므로)
- 다른 유저는 다음 페이지 로드/새로고침 때 `+layout.svelte`의 기존 처리로 `/auth`로
  리다이렉트된다. SPA 화면을 열어둔 상태에서는 새로고침 전까지 요청이 401로 실패한다
- i18n 키 5개 (`en-US`, `ko-KR`)

### 파일

- `backend/open_webui/config.py`, `main.py`, `utils/auth.py`, `socket/main.py`,
  `routers/auths.py`, `routers/terminals.py`
- `src/lib/apis/auths/index.ts` (`signoutAllUsers`)
- `src/lib/components/admin/Settings/General.svelte`
- `src/lib/i18n/locales/{en-US,ko-KR}/translation.json`

### 관리자 사용자 목록에 표시

**관리자 패널 → 사용자** 테이블에 `D Index` 컬럼. Email 다음, Last Active 앞에 둔다
(타임스탬프 뒤로 밀어내지 않고 신원 컬럼 옆에 붙임).

세 상태를 **구분해서** 보여준다 — 롤아웃이 실제로 먹혔는지 관리자가 스캔할 수 있어야 한다:

| 표시 | 의미 |
|---|---|
| `1`, `2`, … | 후보 리스트의 N번째 부서 |
| muted `0` | 후보 중 어디에도 안 속함 (판정 완료) |
| muted `–` | 아직 미판정 = **이 유저는 재로그인이 필요하다** |

`0`과 `–`를 같은 모양으로 뭉개면 "전체 로그아웃이 먹혔는지"를 알 수 없다. 각각 tooltip로
의미를 붙였다. 컬럼은 `tabular-nums` + 좌정렬 — 값이 크기가 아니라 명목 index이므로
이웃 컬럼과 같은 축에 글리프를 세워 세로 스캔이 되게 한다.

정렬도 지원한다 (`setSortKey('d_index')` + `models/users.py`의 `order_by` 화이트리스트에
분기 추가). SQLite는 asc에서 NULL이 먼저 오므로 **오름차순 정렬이 곧 "재로그인 안 한
사람 모아보기"** 가 된다.

API 변경은 없다 — 목록 응답 모델 `UserGroupIdsModel`이 `UserModel`을 상속하므로
`d_index`가 이미 내려온다.

- `src/lib/components/admin/Users/UserList.svelte`, `backend/open_webui/models/users.py`,
  i18n 키 3개

---

## 10. TODO / Future Work

- **Confluence 인증 통합**: dscrowd.token_key 쿠키 자동 획득
  - Confluence tool 토글 시 로그인 팝업 → 쿠키 생성 → pipe에 전달
  - 백엔드 프록시 엔드포인트 필요 (`/api/v1/confluence/check-token`)
  - 현재: dscrowd.token_key가 pipe에 전달되지 않음 (유저가 프롬프트에 직접 입력 중)
