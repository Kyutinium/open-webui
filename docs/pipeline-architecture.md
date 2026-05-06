# ChatDRAGON Pipeline Architecture

발표용 다이어그램 토대 문서입니다. GitHub 에서 Mermaid 가 렌더링되며, 슬라이드에 캡쳐해 사용해도 됩니다.

전체 흐름 한 줄 요약:

> **User → Open WebUI → ChatDRAGON Pipe → Oh-My-Gateway (Agent + memory.md) ↔ External MCP Servers / → LiteLLM Proxy → vLLM/SGLang LLM**

MCP 서버는 게이트웨이에 *붙는* 외부 프로세스이지, 게이트웨이 내부 모듈이 아닙니다.

---

## 1. High-Level Overview

### Mermaid (GitHub 라이브 렌더)

```mermaid
flowchart LR
    User([👤 User])

    subgraph WEBUI["🖥️ Open WebUI"]
        UI[Chat UI<br/>SvelteKit]
        Pipe["ChatDRAGON Pipe<br/>(pipelines_dev/<br/>chatdragon_*.py)"]
    end

    subgraph GATEWAY["🚪 Oh-My-Gateway (FastAPI)"]
        RespAPI["/v1/responses<br/>OpenAI-compatible"]
        Session[Session Manager<br/>previous_response_id]
        Workspace["Workspace<br/>(USER_WORKSPACES_DIR)<br/>📝 memory.md"]
        Agent["Agent Backend<br/>Claude SDK / OpenCode / Codex"]
    end

    subgraph MCPEXT["🔌 External MCP Servers (attached, not inside)"]
        MCP1["MCP Server A<br/>fs / bash"]
        MCP2["MCP Server B<br/>web / search"]
        MCP3["MCP Server C<br/>custom skills"]
    end

    subgraph LITELLM["🔀 LiteLLM Serving"]
        Proxy["LiteLLM Proxy<br/>:3999"]
        Strip["strip_thinking<br/>(THINK_OUTPUT_MODE)"]
    end

    subgraph LLM["🧠 LLM Backends (vLLM / SGLang)"]
        M1[GLM-5-FP8<br/>:8088]
        M2[GLM-5.1-FP8<br/>:8089]
        M3[Qwen3.5-122B<br/>:10036]
        M4[Gemma-4-31B<br/>:8090]
        M5[SAMUEL-v2<br/>:8020]
    end

    User -- "HTTP/SSE" --> UI
    UI -- "OpenAI chat<br/>/completions" --> Pipe
    Pipe -- "POST /v1/responses<br/>(stream=true)" --> RespAPI
    RespAPI --> Session
    Session --> Workspace
    Session --> Agent
    Agent <-. "MCP protocol<br/>(stdio / http)" .-> MCP1
    Agent <-. "MCP protocol" .-> MCP2
    Agent <-. "MCP protocol" .-> MCP3
    Agent -- "OpenAI API<br/>via litellm" --> Proxy
    Proxy --> Strip
    Strip --> M1
    Strip --> M2
    Strip --> M3
    Strip --> M4
    Strip --> M5

    classDef ui fill:#E3F2FD,stroke:#1976D2,color:#0D47A1
    classDef gw fill:#FFF3E0,stroke:#F57C00,color:#E65100
    classDef mcp fill:#FFEBEE,stroke:#C62828,color:#B71C1C
    classDef proxy fill:#F3E5F5,stroke:#7B1FA2,color:#4A148C
    classDef llm fill:#E8F5E9,stroke:#388E3C,color:#1B5E20
    class UI,Pipe ui
    class RespAPI,Session,Workspace,Agent gw
    class MCP1,MCP2,MCP3 mcp
    class Proxy,Strip proxy
    class M1,M2,M3,M4,M5 llm
```

### SVG (슬라이드/발표용 정적 이미지)

![High-Level Pipeline Overview](images/pipeline-highlevel.svg)

> 위 Mermaid 소스를 mermaid-cli 로 렌더링한 SVG 입니다. 슬라이드에 그대로 쓰거나 PNG 로 변환해 사용하세요. 파일: [`docs/images/pipeline-highlevel.svg`](images/pipeline-highlevel.svg)

---

## 2. End-to-End Request Sequence

한 번의 사용자 메시지가 토큰 스트림으로 돌아오기까지의 과정.

```mermaid
sequenceDiagram
    autonumber
    participant U as 👤 User
    participant W as Open WebUI
    participant P as ChatDRAGON Pipe
    participant G as Oh-My-Gateway<br/>(/v1/responses)
    participant A as Agent<br/>(Claude/OpenCode)
    participant M as External MCP<br/>Servers
    participant L as LiteLLM Proxy
    participant V as vLLM / SGLang

    U->>W: 메시지 입력
    W->>P: chat completion request
    Note over P: 메시지 정규화,<br/>session 키 결정,<br/>SSE 핸들링 셋업
    P->>G: POST /v1/responses<br/>{model, input, previous_response_id, stream}
    G->>G: Session 조회/생성<br/>workspace 격리, memory.md 로드
    G->>A: 에이전트 호출<br/>(system_prompt + history)

    loop 추론 루프 (multi-turn tool use)
        A->>L: OpenAI chat.completions<br/>(stream)
        L->>V: hosted_vllm / openai 백엔드 호출
        V-->>L: tokens (+ reasoning_content)
        L-->>A: strip_thinking 적용 후 토큰
        A->>M: tool_call (MCP protocol over stdio/http)
        M-->>A: tool_result
        A->>G: read/write memory.md<br/>(workspace fs)
    end

    A-->>G: 최종 답변 + usage
    G-->>P: SSE: response.output_text.delta...<br/>response.completed
    P-->>W: OpenAI delta stream 변환
    W-->>U: 토큰 단위 렌더링
```

---

## 3. Component Responsibilities

| 레이어 | 레포 | 역할 | 주요 파일 |
|---|---|---|---|
| Frontend / Chat UI | `kyutinium/open-webui` | 사용자 입출력, 채팅 UX, 모델 셀렉터 | `src/`, `backend/` |
| Pipe (어댑터) | `kyutinium/open-webui` | OpenAI Chat ↔ Responses API 변환, SSE 핸들링, 세션 키 매핑 | `pipelines_dev/chatdragon_*.py` |
| Gateway | `jiny0ung-shin/oh-my-gateway` | `/v1/responses` 표준화, 세션/워크스페이스/MCP 클라이언트/에이전트 백엔드 라우팅 | `src/main.py`, `src/session_manager.py`, `src/workspace_manager.py`, `src/mcp_config.py`, `src/backends/` |
| Agent Backend | `kyutinium/opencode` (+ Claude Agent SDK) | 도구 사용 루프, 코딩 에이전트 실행 | OpenCode CLI / Claude SDK |
| Memory | gateway workspace | 대화/세션 단위 영속 메모리 (게이트웨이가 마운트하는 fs) | `working_dir/<session>/memory.md` |
| **MCP Servers (외부)** | 각 MCP 구현체 (별도 프로세스) | 게이트웨이/에이전트가 클라이언트로서 연결하는 외부 도구 서버 | stdio 서브프로세스 또는 HTTP endpoint |
| Model Proxy | `kyutinium/litellm_serving` | OpenAI/Anthropic 호환 라우팅, reasoning 후처리 | `litellm_config.yaml`, `strip_thinking.py` |
| LLM Serving | (vLLM / SGLang 인스턴스) | 실제 토큰 생성 | 포트별 모델 서버 |

---

## 4. Inside Oh-My-Gateway

게이트웨이 내부 구조와 외부에 붙는 MCP 서버의 관계입니다. **MCP 서버 자체는 게이트웨이 외부 프로세스**이고, 게이트웨이 안에는 그것을 가리키는 *config* 와 *MCP client* 만 있습니다.

```mermaid
flowchart TB
    In["POST /v1/responses<br/>{model, input,<br/>previous_response_id,<br/>user, metadata}"]

    subgraph GW["Oh-My-Gateway (FastAPI) — 게이트웨이 내부"]
        subgraph Routing["요청 라우팅"]
            Auth[API Key / Admin 인증]
            Rate[Rate Limiter]
            Reg[Backend Registry<br/>claude / opencode / codex]
        end

        subgraph Sess["Session & Workspace"]
            SM[Session Manager<br/>previous_response_id]
            WM["Workspace Manager<br/>USER_WORKSPACES_DIR/&lt;user&gt;/"]
            Mem["📝 memory.md<br/>(에이전트가 read/write)"]
            Files["기타 작업 파일<br/>(skills, plugins)"]
        end

        subgraph Backend["Agent Execution"]
            SP[System Prompt<br/>preset 주입]
            Loop[Agent Tool Loop]
            MCPC["MCP Config (MCP_CONFIG)<br/>+ MCP Client"]
        end
    end

    subgraph Ext["외부 (게이트웨이 밖)"]
        Tools1["MCP Server A<br/>fs / bash"]
        Tools2["MCP Server B<br/>web / search"]
        Tools3["MCP Server C<br/>custom skills"]
        LLM[(LiteLLM Proxy)]
    end

    SSE["SSE Stream<br/>response.* events"]

    In --> Auth --> Rate --> Reg
    Reg --> SM --> WM
    WM --> Mem
    WM --> Files
    Reg --> SP --> Loop
    Loop -- "tools/use" --> MCPC
    MCPC <-. "MCP protocol<br/>(stdio / http)" .-> Tools1
    MCPC <-. "MCP protocol" .-> Tools2
    MCPC <-. "MCP protocol" .-> Tools3
    Loop <-- "read/write" --> Mem
    Loop -- "LLM call" --> LLM
    LLM --> Loop
    Loop --> SSE

    classDef sess fill:#FFF3E0,stroke:#F57C00
    classDef mem fill:#FFEBEE,stroke:#C62828,color:#B71C1C
    classDef cfg fill:#E8EAF6,stroke:#3F51B5
    classDef ext fill:#FFEBEE,stroke:#C62828,color:#B71C1C,stroke-dasharray: 4 2
    class SM,WM,Files sess
    class Mem mem
    class MCPC cfg
    class Tools1,Tools2,Tools3 ext
```

핵심 포인트:

- **세션 연속성**: `previous_response_id` 로 동일 세션의 multi-turn 을 잇는다. 다른 백엔드 혼용은 거부.
- **워크스페이스 격리**: 기본은 임시 세션 디렉토리, `USER_WORKSPACES_DIR` 설정 시 사용자별 디렉토리.
- **memory.md**: 워크스페이스 안의 평범한 파일이지만, 시스템 프롬프트/스킬이 "장기 기억" 으로 사용하도록 가이드한다. 에이전트가 매 턴 자연스럽게 read/write.
- **MCP 는 외부 프로세스**: 게이트웨이는 `MCP_CONFIG` 만 들고 있고, 실제 도구는 stdio 서브프로세스 또는 별도 HTTP endpoint 로 떠있는 외부 MCP 서버. OpenCode 매니지드 모드에선 `opencode serve` 의 MCP 설정도 게이트웨이가 자동 생성해 넘긴다.

---

## 5. LiteLLM Serving Detail

```mermaid
flowchart LR
    Caller["Oh-My-Gateway<br/>(agent runtime)"]
    subgraph LiteLLM["LiteLLM Proxy :3999"]
        Router["Router<br/>(model_list)"]
        Strip["strip_thinking hook<br/>THINK_OUTPUT_MODE<br/>= none / think_tag /<br/>text / default"]
        Merge["merge_reasoning_content_in_choices<br/>(/v1/chat/completions)"]
    end

    subgraph Backends["Self-hosted LLM Backends"]
        V1["vLLM: glm-5-fp8 :8088"]
        V2["vLLM: glm-5.1-fp8 :8089"]
        V3["openai: Qwen3.5-122B-A10B :10036"]
        V4["vLLM: SAMUEL-v2 :8020"]
        V5["vLLM: gemma-4-31b-it :8090"]
    end

    Caller -- "OpenAI chat.completions<br/>or Anthropic messages" --> Router
    Router --> Strip
    Strip --> Merge
    Merge --> V1
    Merge --> V2
    Merge --> V3
    Merge --> V4
    Merge --> V5
```

| 모드 | thinking 처리 | 용도 |
|---|---|---|
| `none` (기본) | thinking 콘텐츠 제거 | 운영 — 깔끔한 응답만 |
| `think_tag` | `<think>...</think>` 로 감싸 출력 | UI 에서 접고 펼 때 |
| `text` | 평문으로 출력 | 디버깅/로깅 |
| `default` | LiteLLM 원본 동작 | 호환성 검증 |

---

## 6. Why Three Layers? (발표 포인트)

1. **Open WebUI 만으론 부족한 이유**
   - WebUI 는 OpenAI Chat Completions 클라이언트일 뿐, 에이전트/세션/도구 사용을 모른다.
   - Pipe 로 추상화 레이어를 끼워 Responses API 와 SSE 이벤트를 정상 파이프라인화.

2. **Oh-My-Gateway 가 하는 일**
   - 멀티 백엔드(Claude SDK / OpenCode / Codex)를 하나의 `/v1/responses` 로 통일.
   - 사용자별 워크스페이스 + `memory.md` 로 "기억 가진 에이전트" 구현.
   - **외부 MCP 서버를 client 로 연결**해 코딩/검색/파일 등 도구 실행을 위임. (MCP 서버는 떼었다 붙였다 가능한 외부 컴포넌트)
   - 어드민/사용량/세션 관측 → 운영 가능한 형태.

3. **LiteLLM 이 분리된 이유**
   - 자체 호스팅한 vLLM/SGLang 모델을 OpenAI/Anthropic 표준 API 로 노출.
   - reasoning(`<think>`) 후처리 정책을 한 군데서 통제.
   - 모델 추가/교체가 게이트웨이/웹UI 에 영향 없이 가능.

---

## 7. Slide-Ready One-Liner

```mermaid
flowchart LR
    A[Open WebUI] --> B[ChatDRAGON Pipe]
    B --> C["Oh-My-Gateway<br/>📝 memory.md · 🤖 Agent"]
    C <-. MCP .-> X["🔌 External<br/>MCP Servers"]
    C --> D[LiteLLM Proxy]
    D --> E[(vLLM / SGLang LLMs)]

    style A fill:#E3F2FD,stroke:#1976D2,stroke-width:2px
    style B fill:#BBDEFB,stroke:#1976D2,stroke-width:2px
    style C fill:#FFE0B2,stroke:#F57C00,stroke-width:2px
    style X fill:#FFCDD2,stroke:#C62828,stroke-width:2px
    style D fill:#E1BEE7,stroke:#7B1FA2,stroke-width:2px
    style E fill:#C8E6C9,stroke:#388E3C,stroke-width:2px
```

---

## Repo Map

| 컴포넌트 | 레포 | 진입점 |
|---|---|---|
| Open WebUI (UI + Pipe 호스팅) | `Kyutinium/open-webui` | `pipelines_dev/chatdragon_responses.py` |
| ChatDRAGON 정의/도큐 | `Kyutinium/ChatDRAGON` | — |
| Gateway | `JinY0ung-Shin/oh-my-gateway` | `src/main.py` (`/v1/responses`), `src/mcp_config.py` |
| OpenCode 백엔드 | `Kyutinium/opencode` | OpenCode CLI |
| LiteLLM | `Kyutinium/litellm_serving` | `litellm_config.yaml` |
| MCP Servers | (외부 — 각 도구별 구현) | stdio/http endpoints, gateway `MCP_CONFIG` 에 등록 |
