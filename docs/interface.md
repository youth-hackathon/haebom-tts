# 앱과 artemis 서버 사이의 규약

앱과 artemis 서버는 이 문서에 적힌 것만으로 연결됩니다. 앱은 서버 안이 어떻게 생겼는지 모르고, 서버는 앱이 화면에 무엇을 그리는지 모릅니다. 양쪽이 따로 고쳐도 이 문서가 그대로면 계속 맞물립니다.

여기 적은 것은 지금 앱(`PlannerClient.kt`, `GuideSession.kt`, `GuideCatalog.kt`)과 서버(`relay/server.py`)가 실제로 주고받는 내용입니다. 새로 정한 것은 없습니다.

## 기준은 plain artemis입니다

artemis는 원래 기기에서 화면을 직접 읽고(화면 계층 XML과 스크린샷), 모델이 고른 동작을 직접 실행한 뒤, 그 결과를 다음 판단에 넣습니다. 이 규약은 그 세 가지를 요청과 응답으로 옮긴 것입니다.

| plain artemis가 하는 일 | 이 규약에서 |
|---|---|
| 기기에서 화면 계층과 스크린샷을 얻는다 | 앱이 요청에 담아 보낸다 (`screen.xml`, `screen.screenshot_jpeg_b64`) |
| 모델이 고른 동작을 기기에서 실행한다 | 서버는 동작을 돌려주기만 한다. 앱이 그 동작을 사람에게 안내하고, 사람이 직접 한다 |
| 실행 결과를 다음 판단에 넣는다 | 앱이 다음 요청의 `event`, `event_detail`로 알린다 |

서버가 받는 관찰은 plain artemis가 기기에서 얻는 것과 같은 형식이고, 서버가 내는 동작은 artemis의 동작 그대로입니다. 그래서 서버는 앱을 몰라도 됩니다.

## 주소

| 주소 | 쓰는 쪽 | 하는 일 |
|---|---|---|
| `POST /v1/guide/next` | 앱, 벤치마크 | 관찰 하나를 받아 다음 동작 하나를 돌려준다 |
| `GET /health` | 앱, 벤치마크 | 서버가 떠 있는지, 어떤 모델을 쓰는지, 어떤 방식으로 답할 수 있는지(`modes`, `default_mode`) |
| `GET /v1/usage` | 벤치마크 | 모델별 호출 수, 토큰, 비용의 누계와 아직 안 끝난 뒷일의 수 |

요청과 응답은 모두 UTF-8 JSON입니다.

## 요청: `POST /v1/guide/next`

```json
{
  "session_id": "d2fa3798",
  "task": "시계 앱에서 오전 7시 30분 알람 추가해줘",
  "step": 3,
  "elapsed_s": 21,
  "event": "met",
  "event_detail": "the human tapped the highlighted element",
  "screen": {
    "package": "com.google.android.deskclock",
    "width": 1080,
    "height": 2400,
    "element_count": 47,
    "xml": "<?xml version='1.0' ...?><hierarchy rotation=\"0\"><node ... /></hierarchy>",
    "screenshot_jpeg_b64": "/9j/4AAQ..."
  }
}
```

| 필드 | 뜻 |
|---|---|
| `session_id` | 따라 하기 한 번을 가리키는 글자. 앱이 시작할 때 만든다. 서버는 이것으로 그 세션의 이력을 찾는다 |
| `task` | 사용자가 하려는 일을 적은 글. 서버는 `event`가 `start`인 요청의 것을 그 세션의 목표로 삼는다 |
| `mode` | (없어도 됨) 서버가 답하는 방식의 이름. `baseline`은 항상 있다. 없으면 서버의 기본 방식으로 답한다. 아래 "베이스라인과 새 방식" |
| `step` | 이번에 안내할 단계의 번호. 1부터 센다 |
| `elapsed_s` | 세션을 시작한 뒤 지난 시간(초) |
| `event` | 직전에 안내한 동작이 어떻게 끝났는지. 아래 표 |
| `event_detail` | 그 설명. 영어 한 문장이고 모델이 그대로 읽는다. 없으면 빈 글자 |
| `screen.package` | 앞에 떠 있는 앱의 패키지 이름 |
| `screen.width`, `screen.height` | 화면 크기(픽셀). XML의 좌표와 응답의 좌표가 이 크기 기준이다 |
| `screen.element_count` | 앱이 센 화면 요소의 수. 서버는 쓰지 않는다 |
| `screen.xml` | 화면 계층. 아래 "화면 XML" |
| `screen.screenshot_jpeg_b64` | 스크린샷. JPEG, 가로를 720픽셀로 줄인 것, 품질 70, base64 |
| `screen.screenshot_error` | 스크린샷을 찍지 못했을 때 그 이유. 이때 `screenshot_jpeg_b64`는 없다 |

### `event`

| 값 | 뜻 | `event_detail`의 예 |
|---|---|---|
| `start` | 세션의 첫 요청. 서버는 그 세션의 이력을 비운다 | (없음) |
| `met` | 사용자가 안내한 동작을 했다. 앱이 접근성 이벤트로 확인했다 | `the human tapped the highlighted element` |
| `already-satisfied` | 안내하려고 보니 이미 되어 있었다 | `type_guide: the condition already held when the step was armed` |
| `declared_done` | 사용자가 "했어요"를 눌렀다. 앱이 확인한 것은 아니다 | `the human pressed '했어요' (says this step is done)` |
| `declared_stuck` | 사용자가 막혔다고 눌렀다 | |
| `unaimable` | 그 동작을 이 화면에서 안내할 수 없었다. 이유가 따라온다 | `target-lost: the highlighted element is gone` |
| `wait_done` | `wait_for_delay`로 기다린 시간이 지났다 | (없음) |
| `user_message` | 사용자가 글로 말했다. `event_detail`이 그 글이다 | |
| `retry` | 사용자가 "다시 찾기"를 눌렀다 | (없음) |
| `off_plan` | 앱이 정해진 흐름을 따라가다가 화면이 어느 단계에도 맞지 않았다 | 이유 |

`unaimable`의 이유는 `이유: 설명` 꼴입니다. 지금 앱이 쓰는 이유는 `no-target`, `no-text`, `not-editable`, `no-direction`, `no-key`, `no-app`, `app-not-found`, `ambiguous`, `not-guided`, `target-lost`, `occluded`, `unsupported-action`입니다.

### 화면 XML

artemis가 기기에 설치하는 접근성 도우미가 내보내는 것과 같은 형식입니다. `<hierarchy>` 아래에 화면 요소가 `<node>`로 겹쳐 들어갑니다. 서버는 이것을 artemis의 읽기 과정(`parse_hierarchy_xml_to_elements` → `filter_ui_hierarchy` → `fuse_ocr_with_xml` → `format_minimal_list_with_elements`)에 그대로 넣습니다.

- 항상 있는 속성: `index`, `text`, `resource-id`, `class`, `package`, `content-desc`, `checkable`, `checked`, `clickable`, `enabled`, `focusable`, `focused`, `scrollable`, `long-clickable`, `password`, `selected`, `visible-to-user`, `bounds`, `drawing-order`
- 있을 때만 붙는 속성: `hint`, `editable`, `heading`, `screen-reader-focusable`, `state-description`, `error`, `pane-title`, `tooltip`
- 창의 맨 위 요소에 붙는 속성: `window-id`, `window-type`, `window-layer`, `window-active`, `window-focused`
- `bounds`는 `[왼쪽,위][오른쪽,아래]`이고 단위는 화면 픽셀입니다.
- 앱 자신의 창(링과 말풍선)은 넣지 않습니다. 화면에 보이지 않는 요소도 넣지 않습니다.

## 응답

```json
{
  "memo": "Milestones: open Clock → Alarm tab → add 7:30 AM",
  "explain": "알람 탭으로 가면 새 알람을 추가할 수 있어요.",
  "action": {
    "name": "click",
    "target": 14,
    "target_point_px": [135, 2190],
    "target_text": "알람",
    "target_bounds_px": [0, 2127, 270, 2253]
  },
  "meta": {"rounds": 1, "latency_s": 3.4, "model_calls": 1, "input_tokens": 5120, "output_tokens": 96, "cost": 0.00041, "cost_currency": "unit", "source": "model", "mode": "baseline"}
}
```

| 필드 | 뜻 |
|---|---|
| `explain` | 이 단계를 왜 하는지 사용자에게 알려 주는 한 문장. 한국어, 60자 이내. 앱은 이것을 채팅 기록, 질문 카드, 마지막 완료 카드에 쓴다. 링 옆 말풍선의 문장("‘설정’ 누르세요")은 앱이 `action`으로 직접 만든다 |
| `action` | 다음에 할 동작 하나. 아래 표 |
| `memo` | 모델이 다음 판단 때 다시 보려고 적어 둔 것. 앱은 쓰지 않는다 |
| `meta` | 이 응답을 만드는 데 든 것. 앱은 쓰지 않고, 벤치마크가 읽는다. 서버가 자유롭게 채운다 |

### `action`

artemis의 동작 이름과 인자 그대로입니다. 서버는 여기서 고른 동작을 실행하지 않습니다.

| `name` | 인자 | 앱이 하는 일 | 끝났다고 보는 때 |
|---|---|---|---|
| `click` | 대상 | 그 자리의 누를 수 있는 요소에 링을 그린다 | 그 요소가 눌렸다는 이벤트 |
| `long_press` | 대상 | 같은 방식, 길게 누르기 | 길게 눌렸다는 이벤트 |
| `input_text` | 대상, `text`, `clear_exist`(기본 true) | 그 자리의 입력란에 링을 그리고 넣을 글자를 보여 준다 | 입력란의 글자가 `text`와 같아짐. `text`가 줄바꿈으로 끝나면 키보드의 완료 키까지 |
| `swipe` | `direction`(`up`, `down`, `left`, `right`), 대상(없어도 됨) | 밀 수 있는 영역과 방향을 보여 준다 | 그 영역이 그 방향으로 밀림 |
| `press_key` | `key`(`BACK`, `HOME`, `APP_SWITCH`, `ENTER`) | 화면 아래 내비게이션 버튼을 가리킨다 | 그 버튼이 눌리거나 화면이 넘어감 |
| `manage_app` | `action`(`launch`만 안내한다), `app_name` | 홈 화면에서 그 앱의 아이콘을 가리킨다 | 그 앱이 앞에 뜸 |
| `wait_for_delay` | `time_in_ms` | 기다린다(0.3초에서 10초 사이로 맞춘다) | 시간이 지나면 `wait_done`을 보낸다 |
| `ask_user` | `question` | 질문을 보여 주고 답을 받는다 | 답이 `user_message`로 간다 |
| `report_task_status` | `status`(`completed`, `failed`), `explanation` | 세션을 끝낸다 | |

대상은 이렇게 옵니다.

- `target`: 모델이 고른 것. 서버가 읽은 요소 목록의 번호(정수)이거나, 화면을 0에서 1000으로 본 좌표 `[x, y]`입니다.
- `target_point_px`: 서버가 `target`을 화면 픽셀 `[x, y]`로 푼 것. 번호였으면 그 요소의 한가운데입니다. **앱은 이것만 봅니다.**
- `target_text`, `target_bounds_px`: `target`이 번호였을 때 그 요소의 글자와 범위(픽셀, `[왼쪽, 위, 오른쪽, 아래]`). 앱은 화면에서 찾은 요소에 글자가 없을 때 `target_text`로 말풍선의 문장을 만듭니다(16자 이내일 때).
- `target_description`: 모델이 대상을 말로 적은 것. `target_text`가 없을 때 제목에 씁니다.

앱은 `target_point_px`에 있는 실제 요소를 접근성 트리에서 찾아 링을 그립니다. 찾지 못하거나 하나로 정해지지 않으면 추측하지 않고 다음 요청에 `unaimable`을 보냅니다. 표에 없는 `name`도 `unaimable`(`unsupported-action`)이 됩니다. `unaimable`이 네 번 이어지면 앱은 서버에 더 묻지 않고 사용자에게 무엇을 하려는지 묻습니다. 그러니 `unaimable` 뒤에는 같은 동작을 다시 내지 않습니다.

artemis의 `ask_explorer`(목록에 없는 요소를 스크린샷에서 찾기)는 서버 안에서 끝납니다. 응답으로는 나가지 않습니다.

## 오류와 시간 제한

- 서버는 답을 만들지 못하면 200이 아닌 코드와 `{"error": "..."}`를 돌려줍니다. 모델 호출이 실패하면 502, 요청을 읽지 못하면 400입니다.
- 앱은 200이 아니면 "안내를 준비하지 못했어요"를 보여 주고, 사용자가 "다시 시도"를 누르면 같은 `event`로 다시 보냅니다.
- 앱은 연결을 3초, 응답을 120초까지 기다립니다.
- `GET /health`는 1.5초 안에 200으로 답해야 합니다. 앱은 코드만 보고 첫 화면에 "연결됨"을 띄웁니다. 본문은 읽지 않습니다.

## 세션

- 서버는 `session_id`마다 지금까지의 단계(관찰, 고른 동작, 그 결과)를 기억하고 다음 판단에 씁니다. 앱은 이력을 다시 보내지 않습니다.
- `event`가 `start`인 요청이 오면 그 세션의 이력을 비웁니다.
- 서버를 다시 띄우면 이력이 사라집니다. 따라 하던 세션은 처음부터 다시 시작해야 합니다.
- 앱은 앞 요청의 답을 기다리는 중에도 같은 세션으로 새 요청을 보낼 수 있습니다(그 사이 화면이 바뀐 경우). 앞 요청의 답은 버립니다. 서버는 겹쳐 온 요청을 각각 답해야 하고, 하나가 오래 걸린다고 다른 요청을 막으면 안 됩니다.

## 베이스라인과 새 방식

서버는 답하는 방식을 여럿 가질 수 있고, 요청이 그중 하나를 이름으로 고릅니다. 브랜치를 바꾸거나 서버를 따로 띄우지 않습니다.

- **`baseline`은 언제나 있습니다.** plain artemis 그대로입니다(artemis의 모델, 프롬프트, 이력을 쌓는 방법). 새 방식을 아무리 더해도 `"mode": "baseline"`인 요청에는 이것으로 답합니다.
- 새 방식(예: 저장해 둔 계획을 쓰는 `cache`)은 자기 이름으로 그 옆에 더합니다. 베이스라인의 코드를 고쳐서 만들지 않습니다. 새 방식이 답을 못 내는 경우에는 베이스라인의 판단을 불러 쓸 수 있습니다.
- 베이스라인으로 답할 때는 새 방식이 저장해 둔 것을 읽지도 쓰지도 않습니다. 그래야 베이스라인의 숫자가 언제 재도 같은 뜻입니다.
- 요청에 `mode`가 없으면 서버의 기본 방식으로 답합니다. 기본 방식은 서버를 띄울 때 환경 변수 `GUIDE_MODE`로 정하고, 정하지 않으면 `baseline`입니다.
- 서버가 모르는 이름이면 400과 가진 방식의 목록을 돌려줍니다.
- 응답의 `meta.mode`에 실제로 어느 방식으로 답했는지 적습니다.
- `GET /health`는 `modes`(가진 방식의 이름들)와 `default_mode`를 같이 돌려줍니다.

앱은 설정에서 방식을 고를 수 있습니다(기본은 "서버가 정한 대로"이고 이때는 `mode`를 보내지 않습니다). 새 방식이 말썽이면 그 자리에서 `baseline`으로 돌립니다. 벤치마크는 `--mode`로 같은 서버의 두 방식을 따로 재서 나란히 봅니다.

## 서버가 지키면 앱이 그대로 도는 것

앱 코드(`PlannerClient.kt`, `GuideSession.kt`, `GuideCatalog.kt`)가 서버에 기대는 것은 아래가 전부입니다.

1. `POST /v1/guide/next`가 위의 요청을 받아 200과 JSON으로 답한다. 실패하면 200이 아닌 코드로 답한다.
2. 응답의 `action`에 `name`이 있고, 그 값이 위 표의 아홉 가지 가운데 하나다.
3. 대상이 있는 동작(`click`, `long_press`)에는 화면 안의 `target_point_px`가 있다. `input_text`에는 `text`, `swipe`에는 `direction`, `press_key`에는 `key`, `manage_app`에는 `app_name`, `report_task_status`에는 `status`가 있다.
4. 응답에 `explain`이 글자로 들어 있다.
5. `GET /health`가 1.5초 안에 200으로 답한다.
6. 같은 세션의 요청이 겹쳐 와도 각각 답한다.
7. 응답이 120초를 넘기지 않는다.
8. `mode`가 `baseline`인 요청에는 언제나 베이스라인으로 답한다. 모르는 `mode`에는 400으로 답한다.

`bench/run.py`는 기록된 상태를 보낼 때마다 응답이 1번부터 4번까지를 지키는지 같이 검사하고, 시작할 때 5번을 잽니다. 어긴 것은 "규약 위반"으로 따로 셉니다.

## 바꿀 때

- 필드를 **더하는 것**은 알리기만 하면 됩니다. 양쪽 모두 모르는 필드는 무시합니다.
- 있는 필드의 **이름, 뜻, 값의 종류를 바꾸는 것**은 양쪽이 같이 정합니다. `event`의 값과 `action`의 `name`, 인자도 여기에 들어갑니다.
- `meta` 안은 서버가 마음대로 바꿔도 됩니다. 다만 벤치마크가 읽는 칸(아래 "응답의 `meta`")은 남겨 둡니다.

## 벤치마크만 쓰는 것

앱은 아래를 쓰지 않습니다. 서버를 앱 없이 재기 위한 것입니다(`bench/run.py`).

생각은 이렇습니다. 실제 실행 때의 에뮬레이터 상태(화면 XML, 스크린샷, 직전 동작의 결과)를 단계마다 기록해 둡니다. 벤치마크는 가상 에뮬레이터가 되어 그 상태를 차례로 서버에 내밉니다. 서버가 낸 답은 실행하지 않고, 기록에서 실제로 한 동작을 "해낸 것"으로 알려 준 뒤 다음 상태로 넘어갑니다. **이력을 쌓는 일(요약, 줄이기, 배워 두기)은 서버가 자기 방식으로 합니다.** 재려는 것이 그 방식이기 때문입니다.

### 기록

서버를 `GUIDE_RECORD_DIR=<폴더>`로 띄우면 세션마다 파일 하나(`<session_id>.jsonl`)를 씁니다. 이 파일이 벤치마크의 워크로드입니다. 줄의 종류는 셋입니다.

```json
{"image": "3f2a9c01d4e5b677", "jpeg_b64": "/9j/4AAQ..."}
{"t": "2026-11-21T08:40:12",
 "request":  { ...앱이 보낸 요청 그대로. 스크린샷 자리에는 {"image": id}... },
 "status": 200,
 "response": { ...그때 돌려준 답... },
 "latency_s": 6.4,
 "prompt":   {"system": "...시스템 글 전체...",
              "content": [{"type": "text", "text": "# EXECUTION HISTORY ..."}, {"type": "image", "image": "3f2a9c01d4e5b677"}, ...]},
 "rounds":   [{"role": "planner", "model": "letsur:gemini-3.8-flash", "effort": "medium", "output": {...}, "meta": {...}}],
 "explorer": [],
 "session":  {"task": "...", "user_notes": [], "turns": [{"step": 1, "elapsed_s": 0, "package": "...", "action": {...}, "resolved_action": {...},
                                                           "memo": "...", "explorer": [], "outcome": "met (...)", "summary": "...", "image": "..."}]}}
{"t": "2026-11-21T08:40:15", "summary_step": 1, "summary": "In Step 1, I tapped ...", "latency_s": 2.1}
```

| 칸 | 뜻 |
|---|---|
| `request` | 그 순간의 에뮬레이터 상태. 앱이 보낸 요청 그대로입니다 |
| `response` | 그때 서버가 앱에 돌려준 답. 다음 줄의 `request.event`가 `met`이면 사용자가 해낸 것이고, 이것이 정답입니다 |
| `prompt` | 그 단계의 첫 판단 호출에 실제로 모델에 들어간 입력. 시스템 글과 내용 블록(글, 스크린샷)입니다 |
| `rounds` | 그 단계에서 모델을 부른 기록. 판단 호출과 위치 찾기 호출마다 모델, 답, 걸린 시간과 토큰 |
| `explorer` | 그 단계에서 위치 찾기와 대상 오류가 프롬프트 끝에 덧붙인 줄들 |
| `session` | 그 프롬프트를 만들 때 서버가 가지고 있던 세션 기억 |
| `{"image": id, "jpeg_b64": ...}` | 스크린샷. 여러 프롬프트에 같은 스크린샷이 들어가므로 파일에는 한 번만 넣고 id로 가리킵니다 |
| `{"summary_step": n, "summary": ...}` | n단계의 요약이 도착한 때와 그 글 |

벤치마크가 서버에 내미는 것은 `request`뿐이고, 채점에는 `response`를 씁니다. `prompt`, `session`, `rounds`는 그때 실제로 무슨 일이 있었는지 보는 자료이고, 아래 두 가지에 씁니다.

- `event`가 `start`인 요청이 오면 그 세션의 파일을 새로 씁니다.
- 요청에 `replay`가 있으면 기록하지 않습니다.
- 화면에 보인 것이 글자와 그림으로 그대로 들어가므로, 남의 폰에서 기록한 것은 공개 저장소에 올리기 전에 내용을 확인합니다.

### `replay.previous`

벤치마크는 세션마다 기록된 요청을 순서대로 다시 보냅니다. `session_id`만 새것으로 바꾸고 `replay`를 붙입니다.

```json
"replay": {"previous": {"step": 2, "elapsed_s": 12, "package": "com.android.settings", "width": 1080, "height": 2400,
                         "reply": {"memo": "", "explain": "...", "action": {"name": "click", "target_point_px": [287, 811], "target_text": "연결된 기기"}},
                         "explorer": []}}
```

- `previous`는 기록에서 직전 단계에 실제로 한 동작입니다. 서버는 그 세션의 직전 단계 기억을 **자기가 낸 답 대신 이것으로** 바꿉니다. 그 동작은 해낸 것이고, 이번 요청의 `event`와 화면이 그 결과입니다.
- 서버가 직전 단계에 답하지 못했으면(오류) 기억에 그 단계가 없으므로 `previous`를 그 자리에 넣습니다. 이때는 `previous.screenshot_jpeg_b64`에 그 단계의 스크린샷이 같이 옵니다.
- 첫 요청(`event`가 `start`)에는 `previous`가 없습니다.
- 나머지는 평소와 똑같이 합니다. 이력을 쌓고, 요약을 만들고, 답을 냅니다.

세션끼리는 서로 무관해서 벤치마크는 여러 세션을 동시에 돌립니다. 한 세션 안에서는 앞 단계의 답을 받고, 그 세션의 뒷일(요약)이 끝나기를 기다린 뒤 다음 단계를 보냅니다. 사람이 단계 사이에 몇 초를 쓰는 것을 흉내 낸 것입니다.

`replay.return_prompt`가 true이면 서버는 이번 답에 쓴 프롬프트를 응답의 `meta.prompt`에 실어 줍니다(글은 그대로, 스크린샷은 id로). 벤치마크의 `--check-context`가 이것을 기록의 `prompt`와 대조해, 재생에서 쌓인 이력이 실제 실행 때와 같은지 봅니다.

### `replay.prompt` (선택)

벤치마크의 `--mode prompt`가 씁니다. 서버의 이력 쌓기를 건너뛰고, 같은 입력에 모델이나 프로바이더만 바꿔 비교할 때를 위한 것입니다.

```json
"replay": {"prompt": {"system": "...", "content": [{"type": "text", "text": "..."},
                                                     {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/..."}}]},
           "session": { ...기록된 세션 기억... }}
```

서버는 `replay.prompt`가 있으면 판단 모델에 그 프롬프트를 그대로 넣습니다. 세션 기억을 읽지도 고치지도 않고 요약도 만들지 않습니다. 화면 XML은 평소처럼 읽어 대상 번호를 좌표로 푸는 데 씁니다. 단계끼리 기대지 않으므로 벤치마크는 모든 단계를 한꺼번에 보냅니다.

### 응답의 `meta`

벤치마크가 읽는 칸입니다. 그 요청 하나에 든 것만 셉니다.

| 칸 | 뜻 |
|---|---|
| `model_calls` | 그 답을 내려고 모델을 부른 횟수(판단과 위치 찾기) |
| `input_tokens`, `cached_input_tokens`, `output_tokens` | 그 호출들의 토큰 |
| `cost`, `cost_currency` | 모델을 불러 주는 쪽(Letsur)이 응답에 실어 주는 추정치와 그 단위 |
| `latency_s` | 모델을 기다린 시간 |
| `source` | 답이 어디서 나왔는지. 모델이면 `model`. 저장해 둔 답으로 대답했으면 다른 값을 적습니다 |
| `mode` | 어느 방식으로 답했는지(`baseline` 등) |

### `GET /v1/usage`

```json
{"models": {"letsur:gemini-3.8-flash": {"calls": 6, "input_tokens": 21146, "cached_input_tokens": 6776, "output_tokens": 816,
                                         "cost": 0.00227, "cost_currency": "unit", "latency_s": 18.4, "gateway_cache_hits": 0}},
 "pending": 0}
```

- `models`는 서버가 뜬 뒤의 누계입니다. 응답을 보낸 뒤에 도는 뒷일(단계 요약)의 사용량도 들어갑니다. 벤치마크는 측정 앞뒤로 읽어 뒷일까지 합친 사용량을 냅니다.
- `pending`은 아직 끝나지 않은 뒷일의 수입니다. `?session=<session_id>`를 붙이면 그 세션의 것만 셉니다.

## 앱과 TTS 서버

artemis 서버와는 따로입니다. 앱에서 TTS 서버를 부르는 코드는 `Speaker.kt`인데, 예전 앱(`main`)에는 없고 `server-arch` 브랜치에만 있습니다. 아래는 그 코드가 기대하는 것입니다.

| 주소 | 하는 일 |
|---|---|
| `POST /tts` | `{"text": "문장", "speed": 0.9}`를 받아 소리를 돌려준다. `"engine"`으로 읽는 엔진을 고를 수 있다 |
| `GET /health` | 서버가 떠 있는지. 앱은 쓰지 않고 검사 도구가 쓴다 |

- 앱은 코드가 정확히 200일 때만 소리로 봅니다. 받은 것을 확장자 없는 파일로 저장해 재생하므로, 형식은 내용만으로 알아볼 수 있어야 합니다(wav, mp3, ogg, m4a).
- 앱은 문장을 보내고 1.5초를 기다립니다. 그 안에 소리가 끝까지 다 오지 않으면 폰에 든 음성으로 읽고, 늦게 온 소리는 버립니다. 연결은 1초, 응답은 5초까지 기다립니다.
- 같은 문장을 4초 안에 다시 보내지 않습니다. 새 문장이 오면 앞 문장의 재생을 멈춥니다.
- TTS 서버가 꺼져 있어도 안내는 계속됩니다. 소리만 폰 음성으로 바뀝니다.

TTS 서버도 엔진을 여럿 가질 수 있고 요청이 이름으로 고릅니다.

- **`baseline` 엔진은 언제나 있습니다.** 새 엔진은 자기 이름으로 그 옆에 더합니다.
- 요청에 `engine`이 없으면 서버의 기본 엔진으로 읽습니다. 기본 엔진은 환경 변수 `TTS_ENGINE`으로 정하고, 정하지 않으면 `baseline`입니다.
- 서버가 모르는 이름이면 400과 가진 엔진의 목록을 돌려줍니다.
- 응답의 `X-TTS-Engine` 헤더에 실제로 읽은 엔진의 이름을 적습니다. `GET /health`는 `engines`와 `default_engine`을 돌려줍니다.
- 앱은 설정에서 엔진을 고를 수 있습니다(기본은 "서버가 정한 대로"이고 이때는 `engine`을 보내지 않습니다).

`tts/check.py`가 이 약속을 검사합니다.
