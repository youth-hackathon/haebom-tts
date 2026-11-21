# 공통 프롬프트: Docker Desktop 설치

이 문서를 그대로 코딩 에이전트에게 줍니다. 팀원 모두가 자기 컴퓨터에서 한 번 실행합니다.

---

## 할 일

이 컴퓨터에서 `docker`와 `docker compose`가 동작하게 합니다. 서버를 각자 컴퓨터에서 띄워 시험하는 데 씁니다.

## 지킬 것

- 이미 동작하면 아무것도 설치하지 않습니다. 확인만 하고 끝냅니다.
- 관리자 권한, 재시작, 로그아웃이 필요한 단계는 실행하기 전에 무엇을 왜 하는지 말하고 사람의 확인을 받습니다.
- 사람이 직접 해야 하는 일(약관 동의 창, 재시작, 권한 허용)은 무엇을 누르면 되는지 알려 주고 기다립니다. 대신 해 보려고 다른 방법을 찾지 않습니다.
- 다른 프로그램을 지우거나 그 설정을 바꾸지 않습니다.
- 설치 방법이 아래와 다르면 공식 문서(`https://docs.docker.com/desktop/`)를 따릅니다.

## 1. 지금 상태 보기

아래 셋을 실행해 봅니다.

- `docker version`: Client와 Server가 둘 다 나오면 엔진이 떠 있는 것입니다.
- `docker compose version`: v2 이상이면 됩니다.
- `docker run --rm hello-world`: "Hello from Docker!"가 나오면 됩니다.

셋 다 되면 4번으로 갑니다. `docker` 명령은 있는데 Server가 안 나오면 Docker Desktop이 꺼져 있는 것이니 켜고 다시 확인합니다.

## 2. 설치

운영체제를 확인하고 해당하는 것만 합니다.

### Windows 10, 11

1. WSL 2가 필요합니다. `wsl --status`로 봅니다. 없으면 `wsl --install`을 실행합니다(관리자 권한, 끝난 뒤 재시작이 필요할 수 있습니다). 있으면 `wsl --update`를 합니다.
2. 가상화가 켜져 있어야 합니다. 작업 관리자의 성능 탭에서 CPU의 "가상화"가 "사용"인지 봅니다. 꺼져 있으면 BIOS에서 켜야 하니 사람에게 알립니다.
3. `winget install -e --id Docker.DockerDesktop`으로 설치합니다. winget이 없으면 공식 문서의 설치 파일을 받게 합니다.
4. 설치가 끝나면 로그아웃했다가 다시 들어오거나 재시작해야 할 수 있습니다.
5. Docker Desktop을 실행합니다. 처음 실행하면 약관 동의 창이 뜨고, 사람이 동의해야 합니다.

### macOS

1. `uname -m`으로 칩을 봅니다. `arm64`면 Apple silicon용, `x86_64`면 Intel용입니다.
2. Homebrew가 있으면 `brew install --cask docker-desktop`으로 설치합니다. 그 이름이 없다고 나오면 `brew search docker`로 이름을 확인합니다. Homebrew가 없으면 공식 문서에서 칩에 맞는 설치 파일을 받게 합니다.
3. Docker 앱을 실행합니다. 처음 실행하면 권한 허용과 약관 동의를 묻고, 사람이 해야 합니다.

### Linux

Docker Desktop이 아니어도 됩니다. Docker Engine과 compose 플러그인을 배포판의 공식 방법으로 설치합니다. `sudo` 없이 `docker`를 쓰려면 사용자를 `docker` 그룹에 넣고 다시 로그인합니다.

## 3. 엔진이 뜰 때까지 기다리기

Docker Desktop을 켠 뒤 엔진이 뜨는 데 1분쯤 걸립니다. `docker info`가 오류 없이 나올 때까지 몇 초 간격으로 다시 해 봅니다. 2분이 지나도 안 되면 화면에 뜬 메시지를 사람에게 물어봅니다.

## 4. 확인

- `docker version`에 Client와 Server가 둘 다 나온다.
- `docker compose version`이 v2 이상이다.
- `docker run --rm hello-world`가 "Hello from Docker!"를 출력한다.
- 디스크에 10GB 넘게 남아 있다. 서버 이미지 하나가 2GB를 조금 넘습니다.
- macOS에서는 Docker Desktop 설정의 Resources에서 메모리가 4GB 이상인지 봅니다. Windows는 WSL이 알아서 잡습니다.

## 5. 알아 둘 것

- Windows의 Git Bash에서 컨테이너 안의 경로를 인자로 줄 때(`docker exec -w /app ...`) 경로가 Windows 경로로 바뀌어 실패합니다. 명령 앞에 `MSYS_NO_PATHCONV=1`을 붙입니다.
- Windows에서 파이썬 출력의 한글이 깨지면 `python -X utf8`로 실행합니다.
- Docker Desktop이 켜져 있어야 `docker` 명령이 동작합니다. 컴퓨터를 다시 켠 뒤에는 Docker Desktop부터 켭니다.

## 6. 끝나면 알려 줄 것

- 세 명령의 결과(버전과 hello-world가 됐는지)
- 새로 설치한 것, 그리고 사람이 직접 한 일(재시작, 약관 동의)
- 안 된 것이 있으면 어디서 무슨 메시지가 나왔는지
