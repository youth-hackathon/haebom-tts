"""TTS 서버 검사와 벤치마크. 안내 서버도 폰도 필요 없다. 파이썬만 있으면 돈다.

    python check.py                                  # 127.0.0.1:8766
    python check.py --url https://tts.example.com    # 올려 둔 서버
    python check.py --out runs/mine --limit 5
    python check.py --engine baseline                # 서버의 베이스라인 엔진을 잰다 (항상 있다)
    python check.py --engine <이름> --out runs/<이름>   # 같은 서버의 새 엔진을 잰다

하는 일은 둘이다.
  1. 약속 검사: 앱(Speaker.kt)이 기대하는 대로 답하는지. 하나라도 틀리면 종료 코드 1.
  2. 벤치마크: sentences.txt의 문장마다 걸린 시간, 소리 길이, 크기. 소리 파일은 --out 폴더에 남는다(직접 들어 본다).

앱은 문장을 보내고 1.5초를 기다린다. 그 안에 소리가 다 오지 않으면 폰에 든 음성으로 읽는다.
그래서 가장 먼저 볼 숫자는 "처음 요청한 문장이 1.5초 안에 온 비율"이다. 폰에서는 여기에 통신 시간이 더해진다.
처음 요청 시간을 다시 재려면 서버를 다시 띄운다(만든 소리를 서버가 기억하고 있으면 두 번째부터는 빠르다).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import struct
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_WAIT_S = 1.5  # Speaker.kt WAIT_MS
APP_SPEED = 0.9   # Speaker.kt SPEED


# ---------------------------------------------------------------- 요청

def post(url: str, body: bytes, timeout: float = 30.0) -> tuple[int, str, bytes, float]:
    """(status, content type, body, seconds). A refused connection raises."""
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, r.headers.get("Content-Type", ""), data, time.perf_counter() - started
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read(), time.perf_counter() - started


ENGINE: str | None = None  # --engine: the engine that is measured (None: the server's default)


def say(base: str, text: str, speed: float | None = APP_SPEED, timeout: float = 30.0, engine: str | None = "") -> tuple[int, str, bytes, float]:
    payload: dict = {"text": text}
    if speed is not None:
        payload["speed"] = speed
    chosen = ENGINE if engine == "" else engine
    if chosen:
        payload["engine"] = chosen
    return post(base + "/tts", json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout)


# ---------------------------------------------------------------- 소리 읽기

def audio_info(data: bytes) -> dict:
    """{"format", "seconds" (None when it cannot be read without a decoder), "problem" (None when fine)}."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _wav_info(data)
    for magic, name in ((b"ID3", "mp3"), (b"OggS", "ogg"), (b"fLaC", "flac")):
        if data.startswith(magic):
            return {"format": name, "seconds": None, "problem": None}
    if data[4:8] == b"ftyp":
        return {"format": "mp4", "seconds": None, "problem": None}
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return {"format": "aac" if (data[1] & 0xF6) == 0xF0 else "mp3", "seconds": None, "problem": None}
    return {"format": "?", "seconds": None, "problem": "알 수 없는 형식입니다 (앞 4바이트 %r)" % data[:4]}


def _wav_info(data: bytes) -> dict:
    out = {"format": "wav", "seconds": None, "problem": None}
    riff = struct.unpack_from("<I", data, 4)[0]
    if riff != len(data) - 8:
        out["problem"] = f"RIFF 길이({riff})가 실제({len(data) - 8})와 다릅니다. 플레이어가 끝을 잘못 읽습니다"
    pos, byte_rate, size = 12, 0, None
    while pos + 8 <= len(data):
        chunk, n = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if chunk == b"fmt ":
            byte_rate = struct.unpack_from("<I", data, pos + 16)[0]
        elif chunk == b"data":
            left = len(data) - pos - 8
            if n != left:
                out["problem"] = out["problem"] or f"data 길이({n})가 실제({left})와 다릅니다"
            size = min(n, left)
            break
        pos += 8 + n + (n & 1)
    if size is None or not byte_rate:
        out["problem"] = out["problem"] or "fmt 또는 data 부분이 없습니다"
    else:
        out["seconds"] = size / byte_rate
    return out


# ---------------------------------------------------------------- 약속 검사

class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def line(self, state: str, name: str, detail: str = "") -> None:
        if state == "실패":
            self.failed += 1
        elif state == "경고":
            self.warned += 1
        print(f"  [{state}] {name}" + (f"  - {detail}" if detail else ""), flush=True)

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.line("통과" if ok else "실패", name, detail)
        return ok


def contract(base: str, rep: Report) -> None:
    print("약속 검사", flush=True)
    try:
        with urllib.request.urlopen(base + "/health", timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
        rep.check(r.status == 200 and isinstance(body, dict), "GET /health가 200과 JSON으로 답한다", json.dumps(body, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        rep.check(False, "GET /health가 200과 JSON으로 답한다", repr(exc))

    sentence = "'디스플레이'를 눌러 주세요"
    code, ctype, data, took = say(base, sentence)
    ok = rep.check(code == 200 and ctype.split(";")[0].strip().startswith("audio/") and len(data) > 0,
                   "POST /tts가 200과 소리로 답한다", f"{code}, Content-Type {ctype or '없음'}, {len(data)}바이트, {took * 1000:.0f}ms")
    if ok:
        info = audio_info(data)
        length = "길이를 읽지 못함(디코더 없이 읽을 수 없는 형식)" if info["seconds"] is None else f"{info['seconds']:.2f}초"
        rep.check(info["problem"] is None and (info["seconds"] is None or info["seconds"] >= 0.2),
                  "소리가 재생할 수 있는 형식이다", info["problem"] or f"{info['format']}, {length}")

    code, _, data, _ = say(base, sentence, engine="baseline")
    rep.check(code == 200 and len(data) > 0, "engine을 baseline으로 보내면 답한다 (베이스라인은 항상 있다)", str(code))
    code, _, _, _ = say(base, sentence, engine="no-such-engine")
    rep.check(400 <= code < 500, "없는 engine에는 4xx로 답한다", str(code))
    code, _, _, _ = say(base, "   ")
    rep.check(400 <= code < 500, "빈 문장에는 4xx로 답한다", str(code))
    code, _, _, _ = post(base + "/tts", b"this is not json")
    rep.check(400 <= code < 500, "JSON이 아닌 요청에는 4xx로 답한다", str(code))
    code, ctype, data, _ = say(base, sentence, speed=None)
    rep.check(code == 200 and len(data) > 0, "speed 없이 보내도 답한다", str(code))
    code, _, data, took = say(base, "글꼴 크기 막대가 조금 커졌어요. + 버튼을 한 번 더 눌러 오른쪽 끝까지 올려 주세요. "
                              "그다음 뒤로 가기를 눌러 설정 메인 목록으로 돌아가면 바뀐 글자 크기를 확인할 수 있어요.")
    rep.check(code == 200 and len(data) > 0, "긴 문장(100자쯤)에 답한다", f"{code}, {took * 1000:.0f}ms")
    code, _, data, _ = say(base, "Wi-Fi를 켜고 ‘확인’을 누르세요 (1 → 1.15, 80%).")
    rep.check(code == 200 and len(data) > 0, "영어, 숫자, 기호가 섞인 문장에 답한다", str(code))

    with ThreadPoolExecutor(4) as pool:
        codes = [r[0] for r in pool.map(lambda i: say(base, f"동시에 보내는 문장 {i}번입니다."), range(4))]
    rep.check(all(c == 200 for c in codes), "네 문장을 동시에 보내도 모두 답한다", str(codes))

    slow, fast = say(base, "속도를 바꾸어 읽는 문장입니다.", 0.7), say(base, "속도를 바꾸어 읽는 문장입니다.", 1.3)
    a, b = audio_info(slow[2]).get("seconds"), audio_info(fast[2]).get("seconds")
    if slow[0] != 200 or fast[0] != 200:
        rep.line("실패", "speed 0.7과 1.3에 답한다", f"{slow[0]}, {fast[0]}")
    elif a is None or b is None:
        rep.line("경고", "speed가 반영되는지 확인하지 못했다", "소리 길이를 읽을 수 없는 형식")
    elif a > b * 1.1:
        rep.line("통과", "speed가 반영된다", f"0.7에서 {a:.2f}초, 1.3에서 {b:.2f}초")
    else:
        rep.line("경고", "speed가 반영되지 않는다", f"0.7에서 {a:.2f}초, 1.3에서 {b:.2f}초")


# ---------------------------------------------------------------- 벤치마크

def read_sentences(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def bench(base: str, sentences: list[str], out: Path, speed: float) -> dict:
    print(f"\n벤치마크: 문장 {len(sentences)}개, speed {speed}", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, text in enumerate(sentences, 1):
        code, ctype, data, first = say(base, text, speed)
        row = {"n": i, "text": text, "chars": len(text), "status": code, "first_ms": round(first * 1000), "again_ms": None,
               "bytes": len(data), "format": "", "seconds": None, "file": ""}
        if code == 200:
            info = audio_info(data)
            ext = info["format"] if info["format"] != "?" else "audio"
            row.update(format=info["format"], seconds=None if info["seconds"] is None else round(info["seconds"], 2), file=f"{i:02d}.{ext}")
            (out / row["file"]).write_bytes(data)
            row["again_ms"] = round(say(base, text, speed)[3] * 1000)
        rows.append(row)
        length = "-" if row["seconds"] is None else f"{row['seconds']:.2f}초"
        late = "  (1.5초 넘김)" if first > APP_WAIT_S else ""
        print(f"  {i:2d}. {row['first_ms']:5d}ms  다시 {row['again_ms'] if row['again_ms'] is not None else '-':>4}ms  {length:>7}  "
              f"{row['bytes'] // 1024:4d}KB  {text[:38]}{late}", flush=True)

    ok = [r for r in rows if r["status"] == 200]
    summary: dict = {"url": base, "speed": speed, "sentences": len(rows), "answered": len(ok)}
    if ok:
        first = [r["first_ms"] for r in ok]
        again = [r["again_ms"] for r in ok]
        summary.update(first_ms_median=round(statistics.median(first)), first_ms_p90=round(pct(first, 0.9)), first_ms_max=max(first),
                       within_app_wait=sum(1 for v in first if v <= APP_WAIT_S * 1000),
                       again_ms_median=round(statistics.median(again)), kb_median=round(statistics.median(r["bytes"] for r in ok) / 1024))
        timed = [r for r in ok if r["seconds"]]
        if timed:
            summary.update(make_per_audio_second=round(statistics.median(r["first_ms"] / 1000 / r["seconds"] for r in timed), 3),
                           chars_per_second=round(statistics.median(r["chars"] / r["seconds"] for r in timed), 1))
    (out / "result.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "result.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n요약", flush=True)
    print(f"  답한 문장                    {summary['answered']} / {summary['sentences']}")
    if ok:
        print(f"  처음 요청: 중앙값 / 90% / 최대  {summary['first_ms_median']}ms / {summary['first_ms_p90']}ms / {summary['first_ms_max']}ms")
        print(f"  1.5초 안에 온 문장            {summary['within_app_wait']} / {len(ok)}   (넘으면 앱이 폰 음성으로 읽습니다)")
        print(f"  다시 요청: 중앙값             {summary['again_ms_median']}ms")
        print(f"  소리 크기: 중앙값             {summary['kb_median']}KB")
        if "chars_per_second" in summary:
            print(f"  소리 1초를 만드는 데 걸린 시간  {summary['make_per_audio_second']}초")
            print(f"  읽는 빠르기                  1초에 {summary['chars_per_second']}자")
    print(f"  소리 파일과 표                {out}")
    return summary


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="TTS 서버 검사와 벤치마크")
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--sentences", type=Path, default=HERE / "sentences.txt")
    ap.add_argument("--out", type=Path, default=HERE / "runs" / "tts-check")
    ap.add_argument("--speed", type=float, default=APP_SPEED)
    ap.add_argument("--engine", default="", help="재는 엔진(baseline 또는 서버가 가진 다른 이름). 없으면 서버의 기본 엔진")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 이만큼의 문장만 잰다")
    ap.add_argument("--no-bench", action="store_true", help="약속 검사만 한다")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    global ENGINE
    ENGINE = args.engine or None
    print(f"재는 엔진: {args.engine or '서버의 기본'}")

    rep = Report()
    try:
        contract(base, rep)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        print(f"  [실패] {base}에 닿지 못했습니다: {exc}")
        return 1
    if not args.no_bench:
        sentences = read_sentences(args.sentences)
        bench(base, sentences[:args.limit] if args.limit else sentences, args.out, args.speed)
    print(f"\n약속 검사: 실패 {rep.failed}개, 경고 {rep.warned}개")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
