# 한국어 TTS / STT API

이 서버는 Gemini API를 호출해 텍스트를 WAV 음성으로 만들고, 업로드한 영상·음성의 말을 한국어 텍스트로 돌려줍니다. MP4는 서버에서 음성 트랙을 추출한 뒤 STT에 전달합니다.

이 문서의 `/tts`·`/stt`는 전체 파일을 한 번 주고받는 기존 규약입니다. 새 `/tts/stream`·`/stt/live`의 조각 전송, 이벤트, 시간 측정은 [`streaming-api.md`](streaming-api.md), Android 앱 변경은 [`android-streaming.md`](android-streaming.md)에 있습니다.

## 실행

저장소 폴더에서 실행합니다.

```bash
[ -f .env ] || cp .env.example .env
chmod 600 .env
```

`.env`의 `GEMINI_API_KEY`를 본인의 키로 바꾼 뒤 실행합니다. 키는 앱이나 요청 본문에 넣지 않고 서버 환경 변수로만 전달합니다. `.env`는 Git과 Docker 빌드에서 제외되며 이미지에 들어가지 않습니다.

```bash
docker compose up -d --build tts
curl http://127.0.0.1:8766/health
```

`.env.example`은 기본 TTS 엔진을 `gemini`로 설정합니다. `.env`나 환경 변수 설정이 없으면 기본값은 `baseline`입니다. 환경 변수를 수정한 뒤에는 `docker compose up -d tts`로 컨테이너에 반영합니다.

| 환경 변수 | 기본값 / 설명 |
|---|---|
| `GEMINI_API_KEY` | Gemini 호출에 필요한 서버 키 |
| `TTS_ENGINE` | `baseline`; `.env.example`에서는 `gemini` |
| `GEMINI_TTS_MODEL` | `gemini-3.8-flash-tts` |
| `GEMINI_STT_MODEL` | `gemini-3.5-transcribe`; 파일 STT |
| `GEMINI_LIVE_STT_MODEL` | `gemini-3.5-transcribe-live`; Live STT |
| `GEMINI_VOICE` | `Kore` |
| `GEMINI_TIMEOUT_SECONDS` | `60`; 1~300초 |

`GET /health`는 서버 상태와 엔진 목록, `streaming.tts`·`streaming.stt`의 경로와 PCM 형식 정보를 반환합니다. `gemini_configured: true`는 키가 설정되었다는 뜻이며, 키의 유효성·사용 권한·할당량을 검사한 결과는 아닙니다.

## TTS: 텍스트 → WAV

```bash
curl --fail-with-body http://127.0.0.1:8766/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"설정 버튼을 눌러 주세요.","speed":0.9,"engine":"gemini"}' \
  --output /tmp/haebom-speech.wav
```

| 필드 | 설명 |
|---|---|
| `text` | 필수 문자열. 앞뒤 공백을 제거하고 300자에서 자릅니다. 빈 문장은 거부합니다 |
| `speed` | 선택. 기본 `1.0`, 범위 `0.5`~`2.0`. 작을수록 느리게 읽습니다 |
| `engine` | 선택. `gemini` 또는 `baseline`; 생략하면 서버 기본 엔진 |

성공 응답은 **200**, `Content-Type: audio/wav`, WAV 바이트입니다. `X-TTS-Engine` 헤더로 실제 엔진을 확인할 수 있습니다. `baseline`은 키 없이 동작하는 espeak-ng 한국어 음성입니다. Gemini 호출이 실패하면 오류를 반환하며, `baseline`으로 자동 전환하지 않습니다.

## STT: MP4 / 음성 → 텍스트

`multipart/form-data`의 `file` 필드에 파일 하나를 넣습니다.

```bash
curl --fail-with-body http://127.0.0.1:8766/stt \
  -F 'file=@sample.mp4'
```

파일 자체를 요청 본문으로 보내는 방식도 지원합니다.

```bash
curl --fail-with-body http://127.0.0.1:8766/stt \
  -H 'Content-Type: video/mp4' \
  --data-binary @sample.mp4
```

성공 응답 예시는 다음과 같습니다.

```json
{"text":"설정 버튼을 눌러 주세요.","language":"ko-KR"}
```

- MP4, WAV, MP3, M4A, AAC, OGG, FLAC, WebM 등을 지원합니다. 영상에는 음성 트랙이 있어야 합니다.
- 요청 본문 전체가 **20MiB 이하**, 미디어 길이는 **120초 이하**여야 합니다. Multipart의 구분자·헤더도 요청 크기에 포함됩니다. 길이를 초과하면 자르지 않고 거부합니다.
- 서버는 첫 번째 음성 트랙을 16kHz mono PCM16 WAV로 변환해 Gemini에 전송합니다. 로컬 임시 파일은 처리 후 삭제됩니다.
- 파일 업로드는 `Content-Length`가 필요하며 chunked 업로드는 지원하지 않습니다. 위 `curl` 예시는 파일 길이를 자동으로 보냅니다.
- 요청 본문 압축(`Content-Encoding: gzip` 등)은 지원하지 않습니다. 미디어 파일 자체가 MP3/AAC로 압축된 것은 허용됩니다.

## 오류

오류 본문은 `{"error":"설명"}` 형태이며, 성공 음성이나 텍스트와 구분해서 처리합니다.

| 상태 코드 | 뜻 |
|---|---|
| `400` | 잘못된 JSON·필드·엔진·미디어, 음성 트랙 없음, 120초 초과 |
| `408` | 요청 본문을 15초 안에 받지 못함 |
| `411` | 요청의 `Content-Length` 없음 |
| `413` | 요청 크기 초과: STT 20MiB, TTS JSON 64KiB |
| `415` | 지원하지 않는 업로드 `Content-Type` 또는 요청 본문 `Content-Encoding` |
| `503` | 서버의 Gemini 키 미설정 |
| `502` | Gemini 호출·응답 처리 실패. 잘못된 키, 권한·할당량 문제, 시간 초과 등 |

## 검증과 응답 시간

```bash
python -m unittest discover -s tests -v
python check.py --engine baseline --no-bench
python check.py --engine gemini --out runs/gemini
```

Unit test는 모의 Gemini 응답과 로컬 미디어 변환을 검사합니다. 이것만 통과해서는 실제 Gemini 연결 성공이나 한국어 음성 품질을 확인한 것이 아닙니다. 마지막 명령은 실제 API를 호출하므로 유효한 키와 할당량이 필요하며 비용이 발생할 수 있습니다. STT도 실제 음성 파일로 별도 확인합니다.

기존 앱은 TTS 음성을 **1.5초 안에 전부** 받지 못하면 폰 음성으로 읽습니다. Gemini 첫 호출의 응답 시간은 보장하지 않으며 실제 환경에서 측정해야 합니다. 기존 `/tts`는 `(엔진, 문장, 속도)`별 최대 128개 결과를 메모리에 캐시하고, 서버를 다시 시작하면 캐시가 비워집니다. 스트리밍 `/tts/stream`은 이 캐시를 사용하지 않습니다.

2026-11-21 실호출 점검에서는 한국어 한 문장의 TTS가 4.223초, 같은 문장 캐시가 0.002초, 생성한 음성이 든 MP4의 STT가 2.108초였습니다. TTS·STT 호출 성공은 확인했지만, 첫 TTS는 기존 앱의 1.5초 제한을 넘었습니다. 한 문장 결과이므로 일반적인 성능 수치로 보지 않습니다.

공식 API 문서: [Gemini TTS](https://aistudio.google.com/docs/speech-generation), [Gemini STT](https://aistudio.google.com/docs/transcribe).
