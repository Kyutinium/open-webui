# Custom Changes Log

이 문서는 upstream open-webui에서 커스텀으로 추가/변경한 내역을 기록합니다.
업그레이드나 머지 시 이 변경사항들이 유지되어야 합니다.

---

## 1. Image Gallery Sidebar

이미지 서버(IMAGE_SERVER_BASE)의 이미지를 오른쪽 사이드바에서 넘겨볼 수 있는 기능.

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
  - 두 가지 모드: Direct URL (MCP 썸네일) / Folder-based (이미지 서버 API)
- **`src/lib/stores/index.ts`** - `showImageGallery`, `imageGalleryData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ImageGallerySidebar를 special panel로 통합
- **`src/lib/components/chat/Chat.svelte`** - showImageGallery subscribe/cleanup
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`** - `image_gallery` type 처리
- **`src/lib/utils/marked/extension.ts`** - `detailsStart` 정규식 수정 (`<details>` -> `<details[\s>]`)

---

## 2. Tool Explorer Sidebar

MCP 도구 검색 결과를 오른쪽 사이드바에서 도구별/검색별로 탐색하는 기능.

### Frontend
- **`src/lib/components/chat/ToolExplorerSidebar.svelte`** (new)
  - 도구별 탭 (Confluence, Jira 등)
  - 검색어별 아코디언 펼침/접힘
  - 검색/필터 기능
  - 썸네일 프리뷰 (클릭 시 Image Gallery 연동)
  - 원본 문서 링크, doc_type 뱃지
  - 결과가 도착하면 자동으로 사이드바 열림
- **`src/lib/stores/index.ts`** - `showToolExplorer`, `toolExplorerData` 스토어 추가
- **`src/lib/components/chat/ChatControls.svelte`** - ToolExplorerSidebar 통합 (mobile + desktop)
- **`src/lib/components/chat/Chat.svelte`** - showToolExplorer subscribe/cleanup
- **`src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`** - `tool_explorer` type 처리 (auto-open + 버튼)

---

## 3. Chatdragon Completions Pipe

Gateway의 Claude Code SDK와 Open WebUI를 연결하는 파이프라인.

### File
- **`pipelines_dev/chatdragon_completions.py`**

### Key Custom Features
- **Image Gallery tag emission**: 응답에서 IMAGE_SERVER_BASE URL 감지 시 `<details type="image_gallery">` 태그 자동 생성
- **MCP 썸네일 수집**: tool_result에서 thumbnail/thumbnail_url 추출, inline image list로 갤러리 태그 emit
- **Tool Explorer tag emission**: MCP tool_result에서 구조화된 데이터 수집 (title, content, url, thumbnail, doc_type), `<details type="tool_explorer">` 태그로 emit
- **Persisted-output 처리**: SDK가 큰 결과를 파일로 저장 후 Read로 읽는 패턴 감지 → file_path 매칭으로 원래 MCP 도구에 연결
- **Content parsing**: content block 리스트, Python repr (single quotes), cat-n 줄 번호, double-escaped JSON, Extra data 잔여 데이터 등 다양한 형태 파싱
- **한글 유저 이름 URL 인코딩**: HTTP 헤더에 한글 이름 넣을 때 UnicodeEncodeError 방지

---

## 4. Docker Compose Files

- **`docker-compose.pipelines.dev.yaml`** - Pipeline 서비스 dev 환경 (port 9098, PYTHONIOENCODING=utf-8)
- **`docker-compose.rc.dev.yaml`** - Open WebUI RC dev 빌드용

---

## 5. Scripts

- **`scripts/dev_fast_restart.sh`** - npm build + docker build + pipelines restart 자동화
