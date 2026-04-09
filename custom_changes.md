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

## 8. TODO / Future Work

- **Confluence 인증 통합**: dscrowd.token_key 쿠키 자동 획득
  - Confluence tool 토글 시 로그인 팝업 → 쿠키 생성 → pipe에 전달
  - 백엔드 프록시 엔드포인트 필요 (`/api/v1/confluence/check-token`)
  - 현재: dscrowd.token_key가 pipe에 전달되지 않음 (유저가 프롬프트에 직접 입력 중)
