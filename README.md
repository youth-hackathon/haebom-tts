# 해봄 TTS · STT 서버

한국어 문장을 WAV 음성으로 바꾸고, MP4나 음성 파일을 한국어 텍스트로 바꾸는 서버입니다. 기존 REST API에 스트리밍 TTS와 마이크용 Live STT를 함께 제공합니다. 해봄 앱이 안내 문장("‘설정’ 누르세요")을 소리 내어 읽는 데 씁니다. 해봄은 스마트폰이 낯선 사람 옆에서 다음에 누를 곳을 짚어 주는 앱이고, 사용자는 주로 고령자입니다. 그래서 또렷하고 알아듣기 쉬운 한국어 음성이 목표입니다.

2026 서울AI디지털배움터 청년AI해커톤, 팀 Toolus의 저장소 셋 가운데 하나입니다.

```
 해봄 앱 (Android) ──화면 상태──▶ artemis 서버 ──▶ 모델
        ▲           ◀──동작 하나──
        │
        └──문장──▶ TTS 서버 (이 저장소) ──▶ 소리
```

| 저장소 | 하는 일 |
|---|---|
| [haebom-app](https://github.com/youth-hackathon/haebom-app) | 화면 위에 다음에 누를 곳을 안내하는 Android 앱 |
| [haebom-artemis](https://github.com/youth-hackathon/haebom-artemis) | 화면 상태를 받아 다음 동작 하나를 고르는 서버 |
| haebom-tts (여기) | 한국어 TTS · STT 서버 |

서로 아는 것은 주고받는 형식뿐이고, 그 형식은 세 저장소가 같은 글([`docs/interface.md`](docs/interface.md))로 가지고 있습니다. 이 서버는 앱도 artemis 서버도 모릅니다. 기존 TTS 규약과 파일 STT API를 유지하며 실시간 음성 경로를 추가합니다.

## Gemini로 한국어 TTS · STT 사용하기

기존 `baseline` 외에 `gemini` TTS 엔진, 파일 STT, 스트리밍 TTS와 Live STT가 있습니다. 로컬 AI 모델을 설치하지 않고 Google Gemini API를 호출합니다. [파일 API 설정](docs/speech-api.md), [스트리밍 규약](docs/streaming-api.md), [Android 앱 변경 사항](docs/android-streaming.md)을 참고하세요.

```bash
[ -f .env ] || cp .env.example .env                 # .env의 GEMINI_API_KEY를 자신의 키로 채운다
# .env가 이미 있으면 복사하지 않고 기존 파일을 수정한다
docker compose up -d --build tts
curl http://127.0.0.1:8766/health
```

| 기능 | 요청 | 응답 |
|---|---|---|
| 한국어 TTS | `POST /tts` JSON `{"text":"안녕하세요","engine":"gemini"}` | `audio/wav` |
| 한국어 STT | `POST /stt` multipart `file`에 MP4·음성 파일 | `{"text":"인식한 문장","language":"ko-KR"}` |
| 스트리밍 TTS | `POST /tts/stream` JSON `{"text":"안녕하세요"}` | NDJSON: 24kHz mono PCM16 음성 조각·완료 이벤트 |
| Live STT | WebSocket `/stt/live`, 16kHz mono PCM16 바이너리 | 중간·최종 한국어 텍스트·완료 이벤트 |

TTS는 `gemini-3.8-flash-tts`, 파일 STT는 `gemini-3.5-transcribe`, Live STT는 `gemini-3.5-transcribe-live`를 기본으로 사용합니다. STT는 최대 20MiB 요청·120초 파일을 받아 FFmpeg로 음성만 추출합니다. API 키는 서버 환경 변수로만 사용하고 `.env`는 Git과 Docker 이미지에서 제외합니다.

**실제 Gemini TTS → MP4 → STT 왕복 호출을 확인했습니다.** 아래 한 문장 점검에서 첫 TTS는 4.223초로 앱의 1.5초 조건을 넘었습니다. 전체 문장의 품질·성능과 폰 재생은 별도 검증이 필요합니다.

## 기존 앱과의 약속

앱의 코드가 실제로 기대하는 것입니다. 전체는 [`docs/interface.md`](docs/interface.md)의 "앱과 TTS 서버"에 있습니다.

| | |
|---|---|
| 요청 | `POST /tts` `{"text": "문장", "speed": 0.9, "engine": "baseline"}`. `speed`와 `engine`은 없어도 됩니다 |
| 성공 | 정확히 200, `Content-Type: audio/…`, 안드로이드가 바로 재생할 수 있는 소리(wav, mp3, ogg, m4a). `X-TTS-Engine` 헤더에 읽은 엔진 |
| 실패 | 빈 문장, JSON이 아닌 요청, 모르는 엔진은 400. Gemini 키 미설정은 503, 소리를 만들다 실패하면 502 |
| 상태 | `GET /health` → `{"ok": true, "engines": [...], "default_engine": "...", "voice": "ko"}` |
| 시간 | 앱은 **1.5초**를 기다립니다. 그 안에 소리가 끝까지 다 오지 않으면 폰에 든 음성으로 읽고, 늦게 온 소리는 버립니다 |

기존 앱은 서버가 꺼져 있어도 폰 음성으로 안내를 계속합니다. 기존 `/tts` 경로에서는 "처음 요청한 문장이 1.5초 안에 다 온 비율"을 봅니다.

새 스트리밍 경로는 앱이 받은 PCM 조각부터 재생하고, 마이크 입력을 녹음 도중부터 전송하도록 수정해야 합니다. TTS 첫 음성 도착·재생 시작과 STT 첫 글자·발화 종료 후 확정 시간을 따로 봅니다. 서버 업데이트만으로 기존 앱이 스트리밍을 사용하게 되지는 않습니다.

## 띄우기

Docker가 있으면 됩니다([`docs/docker.md`](docs/docker.md)).

```bash
docker compose up -d --build          # http://127.0.0.1:8766
curl http://127.0.0.1:8766/health
```

에뮬레이터나 USB로 연결한 폰에서는 `adb reverse tcp:8766 tcp:8766`으로 닿습니다. 서버는 Python 3.12와 aiohttp를 사용하며 `python main.py` 진입점을 유지합니다. Docker 밖에서 실행하려면 `pip install -r requirements.txt`, FFmpeg·espeak-ng 설치와 환경 변수 설정이 필요합니다.

## 재기

`check.py`는 파이썬 표준 라이브러리만 씁니다. 앱도 폰도 필요 없습니다.

```bash
python -m unittest discover -s tests -v            # 외부 API 호출 없는 단위·HTTP·스트리밍 테스트
python check.py                                   # 기존 API 약속 검사 + 벤치마크
python check.py --engine baseline                 # 베이스라인 엔진을 잰다 (항상 있다)
python check.py --engine <이름> --out runs/<이름>   # 새 엔진을 잰다
python check.py --no-bench                        # 약속 검사만
python check.py --url https://tts.example.com     # 다른 곳에 떠 있는 서버
```

- **약속 검사** 12가지: 위의 약속을 하나씩 확인합니다. 실패가 하나라도 있으면 종료 코드 1입니다.
- **벤치마크**: `sentences.txt`의 문장(앱이 실제로 읽는 안내 문장과 완료 문장 30개)마다 처음 요청에 걸린 시간, 다시 요청했을 때의 시간, 소리 길이, 크기를 잽니다. 소리 파일을 `--out` 폴더에 남기므로 **직접 들어 봅니다**. 알아듣기 쉬운지는 숫자로 나오지 않습니다.

Windows에서 한글이 깨지면 `python -X utf8 check.py`로 실행합니다.

## 엔진: 베이스라인과 새 엔진

엔진은 브랜치로 나누지 않습니다. 서버 하나가 여러 엔진을 가지고, 요청이 이름으로 고릅니다.

- `baseline`은 **언제나 있습니다.** `espeak-ng`의 한국어 목소리입니다. 설치가 한 줄이고 수십 ms에 답하지만 소리는 기계음입니다. 약속과 검사 도구가 맞물려 도는지 확인하는 기준입니다.
- `gemini`는 Google API로 한국어 WAV를 만들며, 숫자 `speed`는 FFmpeg로 적용합니다.
- 새 엔진은 `main.py`의 `ENGINES`에 자기 이름으로 더합니다. 함수 하나(문장과 속도를 받아 소리 바이트를 돌려줌)이고, 설치는 `Dockerfile`에 넣습니다. `baseline`의 함수는 고치지 않습니다.
- 요청에 `engine`이 없으면 환경 변수 `TTS_ENGINE`의 엔진으로 읽습니다(기본 `baseline`).
- 앱은 설정에서 엔진을 고를 수 있습니다. 새 엔진이 말썽이면 서버를 건드리지 않고 `baseline`으로 돌립니다.

새 엔진을 고를 때 보는 것은 셋입니다: 들어서 알아듣기 쉬운가(숫자, 기호, 영어가 섞인 문장 포함), 처음 요청이 1.5초 안에 다 오는가, 쓸 수 있는 조건인가(라이선스, 키와 요금).

## 잰 결과

### baseline (espeak-ng), 2026-11-21

`python check.py --engine baseline --out results/baseline`, 같은 컴퓨터의 Docker 컨테이너. 표는 [`results/baseline/`](results/baseline/)에 있습니다.

| | |
|---|---|
| 약속 검사 | 12개 모두 통과 |
| 답한 문장 | 30 / 30 |
| 처음 요청 | 중앙값 20ms, 90% 35ms, 최대 39ms |
| 1.5초 안에 온 문장 | 30 / 30 |
| 다시 요청 | 중앙값 23ms |
| 읽는 빠르기 | 1초에 5.4자 (`speed` 0.9) |
| 들어 본 평가 | 기계음. 뜻은 알아들을 수 있지만 고령 사용자에게 쓰기에는 부족합니다 |

### Gemini 실호출 점검, 2026-11-21

실행 중인 Docker REST API에 한국어 한 문장을 보내고, 받은 WAV를 AAC 음성이 있는 MP4로 만든 뒤 `/stt`에 multipart로 업로드했습니다. 이는 **한 문장의 동작 점검**이며 30문장 벤치마크가 아닙니다.

| 항목 | 결과 |
|---|---|
| 입력 문장 | 안녕하세요. 해봄입니다. 설정 버튼을 눌러 주세요. |
| TTS 모델 | `gemini-3.8-flash-tts` |
| 첫 TTS 요청 | 4.223초; 24kHz WAV, 음성 길이 4.695초 |
| 같은 요청을 다시 보냄 | 0.002초; 동일한 WAV 바이트 |
| STT 모델 | `gemini-3.5-transcribe` |
| MP4 → 텍스트 | 2.108초 |
| 전사 결과 | 안녕하세요, 해봄입니다. 설정 버튼을 눌러주세요. |

첫 TTS가 1.5초를 넘었으므로 현재 앱 규약에서는 이 요청의 서버 음성이 사용되지 않고 폰 음성으로 넘어갑니다. 자주 쓰는 문장의 사전 생성·캐시나 앱 팀과의 시간 제한 조정이 필요합니다. API 키의 모델 접근 성공을 확인했으며, 키 값은 기록하지 않습니다.

### Python 스트리밍 실호출 점검, 2026-11-21

Docker의 `/tts/stream`에서 한 문장을 생성하고, 그 음성을 실시간 속도로 `/stt/live`에 두 번 전송했습니다. 두 발화는 같은 준비된 WebSocket을 재사용했습니다. 결과 원문은 [`results/streaming/result.json`](results/streaming/result.json)에 있습니다. **한 문장 동작 점검이며 성능 벤치마크나 실제 스피커·마이크 측정은 아닙니다.**

| 항목 | 결과 |
|---|---|
| TTS 첫 음성 조각 | 1.306초 |
| TTS 마지막 음성 조각 / 완료 확인 | 2.111초 / 2.167초 |
| 생성 음성 | 3.96초, 59개 조각 |
| Live 연결 준비 | 0.459초 |
| STT 첫 글자, 1·2차 | 1.204초 / 1.203초 |
| 입력 종료 후 최종 확정, 1·2차 | 0.382초 / 0.343초 |
| 두 전사 결과 | 안녕하세요, 해봄입니다. 설정 버튼을 눌러주세요. |

서버에 유효한 Gemini 키를 설정한 뒤 다음 명령으로 다시 확인할 수 있습니다. 이 검사는 **실제 Gemini 사용량을 발생시키며** 단위 테스트에 자동 포함되지 않습니다. 실행하는 쪽에 Python 의존성과 FFmpeg가 필요합니다.

```bash
python -m pip install -r requirements.txt
python check_streaming.py --url http://127.0.0.1:8766 --out runs/streaming
```

`runs/streaming/result.json`에 측정값, `streaming-tts.wav`에 생성 음성이 저장됩니다. 첫 조각 도착은 재생 시작과 다르며, 실제 기기 오디오 버퍼 지연은 앱에서 따로 측정합니다.

### 앱과 붙여 본 결과

같은 날 에뮬레이터(Pixel, Android 16, 한국어)에서 앱과 붙였습니다. "화면 밝기 설정 화면을 열어줘"를 따라 하는 동안 안내 문장 7개가 0.04~0.13초에 와서 끝까지 재생됐습니다. 서버를 끄면 앱이 1.5초 뒤 폰 음성으로 읽었고 안내는 그대로 이어졌습니다.

새 엔진을 더하면 이 아래에 같은 표를 덧붙입니다.

## 파일

| 파일 | 무엇 |
|---|---|
| `main.py` | aiohttp 서버와 기존 TTS·파일 STT. 완성된 TTS를 (엔진, 문장, 속도)별로 최대 128개 캐시 |
| `streaming.py` | TTS 스트림과 Live STT 세션. 서버에서 Gemini에 연결하고 취소·종료 정리 |
| `gemini.py` | Gemini REST 호출. 키는 환경 변수에서 읽습니다 |
| `media.py` | FFmpeg로 MP4 음성 추출·TTS 속도 조절 |
| `tests/` | 외부 호출 없는 HTTP·Gemini 응답 처리·실제 FFmpeg 테스트 |
| `docs/speech-api.md` | Gemini 설정과 기존 TTS·파일 STT 사용 예시 |
| `docs/streaming-api.md` | 스트리밍 메시지 규약·형식·제한·시간 측정 |
| `docs/android-streaming.md` | 앱의 PCM 녹음·재생, 타임아웃·취소 변경 지침 |
| `requirements.txt` | Python 서버 의존성 |
| `Dockerfile`, `docker-compose.yml` | 띄우는 방법. 포트 8766 |
| `check.py` | 기존 API 약속 검사와 벤치마크 |
| `check_streaming.py` | 선택 실행하는 실제 Gemini 스트리밍 왕복 점검·연결 재사용 측정 |
| `sentences.txt` | 벤치마크에 쓰는 문장 |
| `results/` | 잰 결과의 표 |
| `docs/interface.md` | 세 저장소가 같이 가지는 규약 |
| `docs/prompt.md` | 이 저장소를 만드는 데 쓴 프롬프트 |
| `docs/local.md` | 자기 컴퓨터에서 띄우고 재는 순서 |
| `docs/docker.md` | Docker 설치 |

## 알려진 한계

- `baseline`과 `gemini`가 있습니다. Gemini는 실제 한국어 왕복 점검에 성공했지만 첫 TTS 한 건은 4.223초로 앱 제한을 넘었습니다. 다양한 발음·소음 조건과 전체 문장 벤치마크는 아직 확인하지 않았습니다.
- `baseline`은 숫자와 기호를 한국어로 자연스럽게 읽지 못합니다("1 → 1.15").
- 기존 `/tts`의 캐시는 메모리에만 둡니다. 서버를 다시 띄우면 비워집니다. `/tts/stream`은 캐시하지 않습니다.
- 스트리밍 TTS는 `speed` 생략 또는 `1.0`만 받습니다. 다른 재생 속도는 앱에서 처리합니다.
- Live STT는 한 연결의 발화를 순서대로 처리합니다. 대기 90초·세션 10분 후에는 다시 연결해야 합니다.

## 라이선스

Apache-2.0 ([`LICENSE`](LICENSE)).
