# C03 — 계속 서빙하기

> 목표: 서버가 클라이언트를 몇 명 받든 **fd 수가 늘지 않고**, 클라이언트가 어떻게 끊든 **죽지 않는다**.
> 선행: [C02](02-echo.md) · 다음: [C04](04-minimal-http.md) · 검사: `python3 workbook/check.py c03`

## 먼저 공부할 것

- `man 7 signal`의 SIGPIPE, `man 2 send`의 `MSG_NOSIGNAL`과 `EPIPE`
- `man 5 proc`의 `/proc/[pid]/fd`
- 검색어: `TCP FIN RST half close shutdown SIGPIPE MSG_NOSIGNAL`

답을 적고 시작하세요.

1. 이미 닫힌 연결에 `send`하면 커널은 프로세스에게 무엇을 하는가? 기본 동작은?
2. 클라이언트가 echo 응답을 다 읽기 전에 close하면 서버 쪽에서는 어떤 순서로 무엇이 일어나는가?
3. fd를 close하지 않고 계속 accept하면 언제 무엇이 실패하는가? (`ulimit -n`)

## 만들 것

- [ ] accept를 무한 루프로: 클라이언트 하나를 끝까지 echo하고, 닫고, 다음 accept
- [ ] **모든** 경로(정상 EOF, recv 오류, send 오류)에서 client fd가 정확히 한 번 닫힘 — 한 클라이언트 처리를 `handle_client(int fd)` 함수로 분리하면 쉬워집니다
- [ ] 한 클라이언트의 오류(ECONNRESET, EPIPE)는 서버 전체를 끝내지 않음. accept의 EINTR도 재시도
- [ ] SIGPIPE로 죽지 않기: `send(..., MSG_NOSIGNAL)` 또는 시작할 때 `signal(SIGPIPE, SIG_IGN)` — 둘 중 하나를 고르고 이유를 기록
- [ ] Makefile 고치기 (아래)

### Makefile 고치기

이제 서버가 끝나지 않으므로 `make check`/`make sanitize`가 바이너리를 실행하면 영원히 기다립니다. 바이너리를 직접 실행하는 줄을 검사기로 바꿉니다.

```make
STEP ?= c03

check-c: c
	clang-format --dry-run --Werror $(C_SOURCES) $(wildcard c/src/*.h)
	python3 workbook/check.py $(STEP)

sanitize:
	@mkdir -p build/sanitize
	$(CC) ... -o $(SAN_BINARY)          # 빌드 줄은 그대로
	python3 workbook/check.py $(STEP) -- ./$(SAN_BINARY)
```

이후 단계마다 `make check STEP=c08`처럼 씁니다. `help` 문구의 "no server yet"도 고쳐 둡니다. (Rust 쪽 `check-rust`의 `cargo run`은 R02에서 같은 식으로 바꿉니다.)

<details><summary>힌트 — MSG_NOSIGNAL vs SIG_IGN</summary>

`MSG_NOSIGNAL`은 그 호출에만 적용되고 Linux/BSD에 있습니다. `SIG_IGN`은 프로세스 전체에 적용되어 `write()`나 라이브러리 내부 쓰기까지 막지만, 자식 프로세스에 상속되는 등 전역 효과가 있습니다. 어느 쪽이든 그 뒤 send는 -1/`EPIPE`를 반환하므로 오류 처리는 여전히 필요합니다. 참고로 Rust 런타임은 main 전에 SIGPIPE를 무시하도록 설정합니다(R02에서 확인).
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c03     # 또는 make check STEP=c03
make sanitize STEP=c03                      # ASan/UBSan 빌드로 같은 검사
```

검사 내용: 연속 클라이언트 3명, 200번 접속·해제 후 fd 수 비교, 8KiB를 보내자마자 닫는 클라이언트 20명(SIGPIPE 유발) 후 생존 확인.

### 직접 관찰

```sh
./build/c/http-server & PID=$!
ls -l /proc/$PID/fd                       # 0,1,2 + listen socket
python3 -c '
import socket
for _ in range(50):
    s = socket.create_connection(("127.0.0.1", 8080)); s.sendall(b"hi"); s.shutdown(socket.SHUT_WR); s.recv(10); s.close()'
ls /proc/$PID/fd | wc -l                  # 늘었으면 누수
ss -tan 'sport = :8080' | awk '{print $1}' | sort | uniq -c   # 상태별 개수
kill $PID
```

`TIME-WAIT`이 쌓여 있는 건 정상입니다(커널이 관리, fd 아님). `CLOSE-WAIT`이 쌓이면 **상대는 끊었는데 내가 close를 안 한** 것 — 누수의 신호입니다.

### 일부러 깨뜨려 보기

1. `MSG_NOSIGNAL`을 빼고 `python3 workbook/check.py c03` → `exit=signal SIGPIPE` 확인. 셸에서 종료 코드는 `echo $?` → 141 (128 + 13).
2. 오류 경로 하나에서 close를 빼먹기 → fd 누수 검사 실패와 `CLOSE-WAIT` 누적 확인.
3. `ulimit -n 20`인 셸에서 누수 버전을 실행 → accept가 `EMFILE`로 실패하는 순간 관찰.

## 알았어야 하는 것

<details><summary>1. SIGPIPE는 왜 존재하나?</summary>

`yes | head -1`처럼 파이프 읽는 쪽이 끝나면 쓰는 쪽을 조용히 끝내기 위한 유닉스 관습입니다. 소켓도 같은 규칙을 따라서, 상대가 끊은 연결에 쓰면 기본 동작이 "프로세스 종료"입니다. 서버에서는 클라이언트 하나가 끊었다고 전체가 죽으면 안 되므로 시그널을 끄고 `EPIPE`를 연결 단위 오류로 처리합니다.
</details>

<details><summary>2. 보내자마자 닫는 클라이언트에서 실제로 일어나는 일</summary>

클라이언트가 8KiB를 보내고 close → FIN이 감(서버는 CLOSE_WAIT). 서버가 echo를 send → 클라이언트 커널은 이미 닫힌 소켓이므로 RST로 응답. 서버가 다음 데이터를 recv해서 또 send → 이번엔 RST를 받은 소켓이라 `EPIPE` + SIGPIPE. 첫 send는 성공했다는 점이 중요합니다. send 성공은 "상대가 받았다"가 아니라 "내 커널 버퍼에 들어갔다"입니다.
</details>

<details><summary>3. fd 누수는 왜 치명적인가?</summary>

프로세스 fd 한도(보통 soft 1024)에 닿으면 accept가 `EMFILE`로 실패하고 서버는 새 연결을 하나도 못 받습니다. 메모리 누수보다 훨씬 빨리, 훨씬 확실하게 서비스가 멈춥니다. 연결마다 fd가 정확히 하나 생기고 하나 사라져야 합니다.
</details>

<details><summary>4. CLOSE_WAIT과 TIME_WAIT의 차이</summary>

CLOSE_WAIT: 상대의 FIN을 받았고 **내 애플리케이션이 close하기를** 기다리는 상태. 쌓이면 내 버그입니다. TIME_WAIT: 먼저 close한 쪽이 60초 동안 머무는 상태. 커널이 알아서 치우고 fd를 차지하지 않습니다.
</details>

## 기록

SIGPIPE 실험의 종료 코드, 그리고 MSG_NOSIGNAL과 SIG_IGN 중 무엇을 골랐고 왜인지.
