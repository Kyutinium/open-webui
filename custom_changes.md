# Custom Changes Log

이 문서는 upstream open-webui에서 커스텀으로 추가/변경한 내역을 기록합니다.
업그레이드나 머지 시 이 변경사항들이 유지되어야 합니다.

---

## 1. Image Gallery Sidebar

이미지 서버(IMAGE_SERVER_BASE)의 이미지를 오른쪽 사이드바에서 넘겨볼 수 있는 기능.
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
  - 하단 썸네일 스트립
  - 두 가지 모드: Direct URL (MCP 썸네일) / Folder-based (이미지 서버 API → get_image_list)
  - 문서 썸네일 클릭 → 같은 문서의 모든 페이지를 넘겨볼 수 있음
- **`src/lib/stores/index.ts`** - `showImageGallery`, `imageGalleryData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ImageGallerySidebar를 special panel로 통합 (mobile + desktop)
- **`src/lib/components/chat/Chat.svelte`** - showImageGallery subscribe/cleanup
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`** - `image_gallery` type 처리
- **`src/lib/utils/marked/extension.ts`** - `detailsStart` 정규식 수정 (`<details>` → `<details[\s>]`)

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
  - 썸네일 클릭 → Image Gallery 연동 (같은 문서의 페이지 넘기기, get_image_list 사용)
  - 원본 문서 링크, doc_type 뱃지, content snippet
  - **실시간 업데이트**: 스트리밍 중 MCP 결과 도착할 때마다 사이드바 자동 오픈 + merge
  - 스트리밍 완료 후 "검색된 문서 보기" 버튼으로 사이드바 다시 열기 가능
  - 에러 결과 (metadata.error) 자동 필터링
  - 채팅 전환 시 데이터 초기화
- **`src/lib/stores/index.ts`** - `showToolExplorer`, `toolExplorerData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ToolExplorerSidebar 통합 (mobile + desktop), 닫을 때 controls 패널도 같이 닫힘
- **`src/lib/components/chat/Chat.svelte`** - showToolExplorer/toolExplorerData subscribe/cleanup/초기화
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`**
  - `tool_explorer` type 처리
  - Svelte `use:action`으로 자동 오픈 (side effect 안정성)
  - 중복 결과 dedup (query + result count 기반)

---

## 3. Chatdragon Completions Pipe

Gateway의 Claude Code SDK와 Open WebUI를 연결하는 파이프라인.

### File
- **`pipelines_dev/chatdragon_completions.py`**

### Key Custom Features
- **Image Gallery tag emission**: 응답에서 IMAGE_SERVER_BASE URL 감지 시 `<details type="image_gallery">` 태그 자동 생성
- **MCP 썸네일 수집**: tool_result에서 thumbnail/thumbnail_url 추출
- **Tool Explorer 실시간 emission**: MCP tool_result마다 즉시 `<details type="tool_explorer">` 태그 emit (스트림 종료 대기 없음)
- **구조화된 결과 추출**: title, content, url(edm_link), thumbnail(thumbnail_url), doc_type 다양한 필드명 지원
- **Confluence URL 추출**: `_links.webui` + `space._links.self`에서 base URL 추출, 또는 `viewpage.action?pageId=` fallback
- **에러 필터링**: metadata.error 또는 오류 content 결과 제외
- **Persisted-output 처리**: SDK가 큰 결과를 파일로 저장 후 Read로 읽는 패턴 감지 → file_path 매칭으로 원래 MCP 도구에 연결
- **Content parsing**: content block 리스트, Python repr (single quotes), cat-n 줄 번호 (탭/스페이스), double-escaped JSON, Extra data 잔여 데이터 truncation
- **한글 유저 이름 URL 인코딩**: HTTP 헤더에 한글 이름 넣을 때 UnicodeEncodeError 방지
- **검색어 표시 개선**: raw JSON args 대신 읽기 좋은 검색어 추출

---

## 4. Docker Compose Files

- **`docker-compose.pipelines.dev.yaml`** - Pipeline 서비스 dev 환경 (port 9098, PYTHONIOENCODING=utf-8, shared_images volume mount)
- **`docker-compose.rc.dev.yaml`** - Open WebUI RC dev 빌드용

---

## 5. Scripts

- **`scripts/dev_fast_restart.sh`** - npm build + docker build + pipelines restart 자동화
- **`scripts/start_dev.sh`** - Frontend (Vite hot reload) + Backend (uvicorn --reload) + Pipelines restart 한 번에 실행
