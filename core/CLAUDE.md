# CLAUDE.md — core/

> NAIA 2.0의 핵심 시스템 계층. AppContext, 파이프라인, API 브릿지.

---

## 디렉터리 구조 및 의존성

```
NAIA_web_headless.py (지원 진입점)
    ↓
core/web_session_app.py
    ↓
core/web_session_context.py (headless runtime context)
    ↓
core/headless_generation_service.py
core/headless_result_service.py
core/prompt_generation_service.py
```

**core/가 의존하는 것**: `interfaces/`, `utils/`, `data/`, `app/web/remote`
**core/를 의존하는 것**: `NAIA_web_headless.py`, `app/backend/`, Electron shell launch flow

**상세 레퍼런스**:
- [Character Prompt / Character Reference 계약](.claude/NAI_CHARACTER_CLAUDE.md) — 캐릭터 프롬프트·레퍼런스를 다른 모듈에서 쓰는 법과 **우회 3종**

---

## 주요 파일

| 파일 | 역할 |
|------|------|
| **api_service.py** | API 호출 (NAI/WEBUI/COMFYUI), Auto-Outpainting |
| **sequence_parser.py** | 시퀀스 프롬프트 파싱 (`:begin`, `:seq`, `:end`) |
| **generation_queue_manager.py** | 생성 큐 (우선순위, 일시정지) |
| **comfyui_workflow_manager.py** | ComfyUI 워크플로우 관리 |
| **comfyui_service.py** | ComfyUI 순수 HTTP 통신 (WebSocket 제거됨) |
| **wildcard_processor.py** | 와일드카드 치환 (랜덤/순차/종속), 오버라이드 지원 |
| **prompt_processor.py** | 프롬프트 파이프라인 |
| **wildcard_manager.py** | 와일드카드 파일 로딩, 가중치 파싱, 라인 제거 |
| **prompt_context.py** | PromptContext 데이터 클래스 |
| **mode_ware_manager.py** | 모드 인식 모듈 일괄 저장/로드 |
| **generation_request.py** | GenerationRequest 데이터 클래스 |
| **filter_data_manager.py** | 텍스트/JSON 태그 사전 로드, noise whitelist |
| **tag_filter_helpers.py** | 공유 태그 필터링 (10라운드), 색상 예외 |

---

## AppContext

중앙 허브. 공유 서비스, 이벤트 버스, 파이프라인 훅, API 모드 관리.

### 이벤트 버스

```python
app_context.subscribe("event_name", callback)      # 구독
app_context.unsubscribe("event_name", callback)    # 구독 해제 (수명주기 짧은 창에서 필수)
app_context.publish("event_name", {"key": "value"}) # 발행
```

`unsubscribe`는 동일 콜백이 중복 등록된 경우 모두 제거합니다.

### 주요 이벤트

| 이벤트 | 데이터 | 용도 |
|--------|--------|------|
| `api_mode_changed` | `{"old_mode": str, "new_mode": str}` | 모드 전환 |
| `prompt_generated` | `PromptContext` | 프롬프트 생성 완료 |
| `comfyui_workflow_changed` | `{"has_custom": bool, "model_compat": str\|None, "locked_loader_class": str\|None, "locked_model_display": str\|None}` | ComfyUI 사용자 커스텀 워크플로우 로드/해제 시 UI 잠금 동기화 (175) |

### 파이프라인 훅 등록

```python
app_context.register_pipeline_hook(hook_info, module_instance)
# hook_info: {'target_pipeline': 'PromptProcessor', 'hook_point': str, 'priority': int}

app_context.get_pipeline_hooks('PromptProcessor', 'post_processing')
# → [module_instance, ...] (우선순위순)
```

### API 모드 변경

```python
app_context.set_api_mode("WEBUI")  # "NAI", "WEBUI", "COMFYUI"
app_context.get_api_mode()  # → str
```

### API Payload 저장 (디버깅용)

```python
app_context.store_api_payload(payload, api_type="Unknown")
```

---

## 프롬프트 파이프라인 (`prompt_processor.py`)

```
1. pre_processing 훅       ← 모듈 개입
2. 해상도 자동 맞춤         (내부)
3. post_processing 훅      ← 모듈 개입
4. 와일드카드 확장           (내부)
5. after_wildcard 훅       ← 모듈 개입
6. final_hookpoint 훅      ← 모듈 개입
7. 최종 포맷팅              (내부: 인물 정렬, 인물 태그만 중복 제거, 괄호 이스케이프, 주석 포맷)
```

**와일드카드 라인 선택 모드** (`WildcardProcessor`):

| 모드 | 구문 | 선택 방식 | 가중치 |
|------|------|-----------|--------|
| 랜덤 | `__name__`, `<name>` | `random.choices(weights=)` | 적용 |
| 순차 | `__*name__` | `entries[counter % total][1]` | 무시 |
| 종속 | `__$master:slave__` | `entries[slave_index][1]` | 무시 |

**⚠️ 전개 결과 형식 차이** (`wildcard_processor.py`):

| 구문 | 반환 형식 | 예시 |
|------|-----------|------|
| `$wildcard` | 개별 태그 리스트 | `['tag1', 'tag2', 'tag3']` |
| `__wildcard__` | 콤마 합쳐진 단일 문자열 | `['tag1, tag2, tag3']` |

`__wildcard__`는 `_expand_recursive`에서 `''.join(result_parts)` 반환 (복합 패턴 `prefix__wc__suffix` 지원 목적). 개별 태그를 순회하는 downstream 훅에서는 콤마 split 필요.

---

## API 서비스 (`api_service.py`)

### 다중 백엔드 분기

`call_generation_api()`: `api_mode`에 따라 `_call_nai_api`, `_call_webui_api`, `_call_comfyui_api` 분기. 입력 정리 (주석 제거, 개행 제거), 재시도 로직 포함.

### HTTP 스레드 정리

**모든 API 호출 후 반드시 `_cleanup_http_threads()` 호출**. urllib3 풀, requests 세션, Qt 스레드 풀, GC 정리.

### Auto-Outpainting (`_single_pass_outpainting`)

`type == 'auto_outpainting'` 인터셉트 → OutpaintWindow 데이터 있으면 직접 사용, 없으면 기본 캔버스 자동 생성 (가로→1:1, 세로→3:2) → 마스크 생성 → `type='inpaint'`로 재호출.

### 캐릭터 위치 좌표

`character_positions` 파라미터를 `v4_prompt.caption.char_captions[].centers`로 전달.

좌표 매핑: A-E→x:0.1-0.9, 1-5→y:0.1-0.9. Fallback: 기본값 `{"x": 0.5, "y": 0.5}`.

---

## ComfyUIWorkflowManager (`comfyui_workflow_manager.py`)

ComfyUI API 워크플로우의 파싱/검증/파라미터 치환. 기본 워크플로우(base/anima) 내장 + 사용자 import 워크플로우 지원.

### 모델 호환성 3-state (175)

사용자가 import 한 워크플로우의 **체크포인트 로더가 표준인지** 판정하여 `node_map["model_compat"]` 에 기록:

| 값 | 판정 기준 | apply_params 시 모델 치환 |
|----|-----------|--------------------------|
| `native_checkpoint` | terminal 로더가 `CheckpointLoaderSimple` | ✅ `ckpt_name` 교체 |
| `native_unet` | terminal 로더가 `UNETLoader` + CLIPLoader 존재 | ✅ `unet_name` 교체 |
| `locked_unknown` | 표준 로더 아님 (커스텀 — INT8/GGUF/NF4 등) | 🔒 skip, 워크플로우 원본값 유지 |

### Sampler-centric 역추적

KSampler/SamplerCustom 의 `model` 입력을 따라 **terminal 로더 노드를 식별**. 중간에 놓인 패치 노드(SageAttention, TorchCompile, RescaleCFG, ModelPatch 등)는 자동 pass-through.

Helper: `_trace_model_source_to_terminal(nodes_by_id, links_data, start_id, is_ui_format)` — UI/API 두 포맷 모두 처리. 순환 / max_depth(64) 초과 / dangling link 시 `None` → 호출부가 LOCKED 귀결.

Helper: `_extract_locked_model_display(terminal_node, is_ui_format)` — `.safetensors`/`.ckpt`/`.gguf`/`.pt`/`.bin`/`.onnx` 확장자 문자열을 inputs/widgets_values 에서 추출 (UI 표시용, 자동 치환 힌트 아님).

### user_workflow_node_map 스키마

```python
{
    "model_compat": "native_checkpoint" | "native_unet" | "locked_unknown",
    "workflow_type": "checkpoint" | "unet" | "locked",  # apply_params 분기용 (하위 호환)

    # native_checkpoint
    "checkpoint_loader": "<node_id>",

    # native_unet
    "unet_loader": "<node_id>",
    "clip_loader": "<node_id>",
    "vae_loader": "<node_id>",          # optional

    # locked_unknown
    "locked_loader_node_id": "<node_id>",
    "locked_loader_class":   "<class_type>",
    "locked_model_display":  "<filename.safetensors>",

    # 공통
    "sampler":         "<ksampler_node_id>",
    "positive_prompt": "<clip_text_encode_id>",
    "negative_prompt": "<clip_text_encode_id>",
    "latent_image":    "<empty_latent_id>",
    "rescale_cfg":     "<node_id>",     # optional
    "model_sampler":   "<node_id>",     # ModelSamplingDiscrete, optional
    "ays_scheduler":   "<node_id>",     # optional
}
```

### comfyui_workflow_changed 이벤트

`load_workflow_from_metadata` 성공 / `clear_user_workflow` 시 AppContext 경유로 발행.

### LOCKED 파이프라인 계약

`apply_params_to_workflow` 에서 `model_compat == "locked_unknown"` 이면 **모델 치환을 명시적으로 skip**. 프롬프트/시드/해상도/샘플러/RescaleCFG 는 정상 치환. 중간 패치 노드의 설정(sage_attention, torch.compile mode 등)은 건드리지 않고 그대로 유지 — ComfyUI 서버에 해당 커스텀 노드 팩이 설치돼 있으면 그대로 실행됨.

---

## WildcardManager (`wildcard_manager.py`)

### 가중치 구문

`{정수}:텍스트` (`{정수}::` NAI 가중치는 제외). 정규식: `^(\d+):(?!:)(.*)`

```python
"100: 0.68::artist:ciloranko ::"  → (100, "0.68::artist:ciloranko ::")
"500:high_weight"                → (500, "high_weight")
"plain text"                     → (100, "plain text")  # 기본 가중치
"100::nai_hundred"               → (100, "100::nai_hundred")  # NAI 구문, 기본값
```

**자료구조**: `wildcard_dict_tree[key] = list[tuple[int, str]]`

**소비자 영향**:
- `WildcardProcessor`: 랜덤 모드 `random.choices(weights=)`, 순차/종속 `entries[index][1]`

### 라인 제거 (`remove_line`)

`remove_line(key, value)` → txt 파일에서 value에 해당하는 라인 제거. 가중치 파싱 후 text 정확 매치 → fallback contains 검색. 마지막 라인 보호.

### 오버라이드 (`wildcard_override`)

`app_context.wildcard_override: dict` — `{actual_key: value}` 형태. `WildcardProcessor._get_wildcard_line()`의 일반 무작위 분기에서 오버라이드 값 우선 반환. 순차/종속은 영향 없음. `_app_context_ref`(weakref)로 접근.

### 스코프 추적 (`scoped_wildcard`)

`app_context.scoped_wildcard: str` — 스코프에 등록된 와일드카드 키 (최대 1개).

---

## 주요 함정/주의사항

### HTTP "Dummy" 스레드 누적 방지

모든 API 호출 후 `_cleanup_http_threads()` 호출 필수. `requests.Session` 어댑터 풀 정리 포함.

### 이벤트 미전달 디버깅

1. `initialize_with_context()`에서 구독했는지 확인
2. 이벤트 이름 대소문자 정확히 일치
3. 콜백 시그니처: 인자 1개 (`data: dict`)
