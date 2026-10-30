# C14 — graceful shutdown

> 목표: 종료 신호를 이벤트 루프의 이벤트로 받아서, 새 연결은 막고 진행 중인 요청은 끝내고, 정해진 시간 안에 반드시 종료한다.
> 선행: [C13](13-deadlines.md) · 다음: [R01](../rust/01-parser.md) · 검사: `python3 workbook/check.py c14`

## 먼저 공부할 것

- `man 2 signalfd`, `man 2 sigprocmask`, `man 7 signal`의 "Async-signal-safe functions"
- 검색어: `signal handler async signal safe self-pipe trick signalfd`, `graceful shutdown drain`

답을 적고 시작하세요.

1. 시그널 핸들러 안에서 `printf`나 `free`를 부르면 왜 위험한가?
2. 시그널을 "epoll이 기다리는 fd 이벤트"로 바꾸는 방법은 무엇이 있나?
3. 종료할 때 진행 중인 요청을 기다리지 않고 바로 끝내면 클라이언트는 무엇을 보게 되나? 반대로 무한정 기다리면?

## 만들 것

[계약](../00-contract.md)의 "종료" 행.

- [ ] `SIGTERM`, `SIGINT`를 `sigprocmask`로 block → `signalfd`로 받아서 epoll에 등록
- [ ] 신호를 받으면:
  - listen fd를 epoll에서 빼고 close → 새 connect는 `ECONNREFUSED`
  - 놀고 있는 연결(입력 버퍼 비어 있음 + 보낼 것 없음) 즉시 close
  - 진행 중인 연결은 계속 처리하되, 이후 응답에는 `Connection: close`를 붙이고 응답 후 close
  - 종료 deadline(예: 3초)을 잡고, 지나면 남은 연결을 모두 닫음
- [ ] 연결이 0개가 되거나 deadline이 지나면 루프 종료, 모든 fd 정리, **exit 0**

<details><summary>힌트 — 응답에 Connection: close를 어떻게 넣나</summary>

C08의 `handle_input`이 `close_after`를 결정합니다. "종료 중" 플래그를 입력으로 하나 추가하세요(전역 변수든 인자든). 계층을 지켰다면 수정은 한 줄입니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c14     # C04~C13 전체 + 종료. 약 40초
make sanitize STEP=c14                      # 종료 시 LeakSanitizer가 누수를 보고합니다 (정리 경로 검증)
```

검사 내용: (1) 반쯤 보낸 POST가 있는 상태에서 SIGTERM → 1초 안에 새 연결 거부, idle keep-alive 연결 닫힘, 나머지 body를 보내면 200 + `Connection: close`, exit 0. (2) 끝나지 않는 요청이 있어도 SIGTERM 후 5초 안에 exit 0.

### 직접 관찰

```sh
./build/c/http-server & PID=$!
strace -p $PID -e trace=epoll_wait,read,close,exit_group &
sleep 0.5; kill -TERM $PID; wait $PID; echo "exit=$?"
```

signalfd에서 `read`로 `struct signalfd_siginfo`(128바이트)를 읽는 줄이 보입니다.

> 여기서 `git tag c14-epoll`. C 트랙 완료입니다. `docs/CHECKPOINTS.md`의 M1–M4를 체크하세요.

## 알았어야 하는 것

<details><summary>1. async-signal-safety</summary>

시그널은 프로그램의 **아무 지점**에서나 끼어듭니다. 예를 들어 `malloc`이 내부 lock을 잡은 채로 끼어들었는데 핸들러가 또 `malloc`(printf 내부 포함)을 부르면 교착하거나 힙이 깨집니다. 핸들러에서는 `write`, `_exit` 같은 async-signal-safe 함수와 `volatile sig_atomic_t` 플래그 설정만 합니다. signalfd는 시그널을 비동기 끼어들기가 아니라 **동기적으로 읽는 데이터**로 바꿔서 이 문제를 없앱니다. 이식성이 필요하면 self-pipe trick(핸들러가 파이프에 1바이트 write)을 씁니다.
</details>

<details><summary>2. 종료 순서가 중요한 이유</summary>

listen을 먼저 닫아야 종료 도중 새 연결이 들어와 다시 기다리는 일이 없고, 로드밸런서는 connect 실패를 보고 다른 인스턴스로 보냅니다. 진행 중인 요청을 끝내 주면 클라이언트는 잘린 응답 대신 완전한 응답을 받습니다. deadline이 있어야 느린 클라이언트 하나 때문에 배포가 멈추지 않습니다. "drain + deadline + 강제 종료"는 Tokio에서도 같은 구조입니다(R07).
</details>

<details><summary>3. 왜 sigprocmask로 먼저 block하나</summary>

block하지 않으면 시그널은 기본 동작(SIGTERM = 종료)으로 처리되어 signalfd에 도착하지 않습니다. block된 시그널은 pending 상태로 남고, signalfd가 그것을 읽게 해줍니다. 스레드가 여러 개면 모든 스레드에서 block되어야 합니다(새 스레드는 마스크를 상속).
</details>

## 기록

C 트랙 회고: C01부터 C14까지 가장 오래 막혔던 단계와 그 원인 3가지. Rust에서 무엇이 달라질지 예상.
