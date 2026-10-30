# C10 — 느린 클라이언트 하나가 모두를 막는 문제 (thread-per-connection)

> 목표: 단일 스레드 blocking 서버의 한계를 **직접 재현**하고, 가장 단순한 해결(연결마다 스레드)과 그 비용을 측정한다.
> 선행: [C09](09-chunked.md) · 다음: [C11](11-epoll-read.md) · 검사: `python3 workbook/check.py c10`

## 먼저 공부할 것

- `man 3 pthread_create`, `man 3 pthread_detach`
- 검색어: `thread per connection thread pool context switch`, `blocking nonblocking asynchronous IO`

먼저 **아무것도 고치지 말고** 지금 서버로 실패를 봅니다.

```sh
python3 workbook/check.py c10 --only
```

`헤더를 반만 보내고 멈춘 클라이언트가 있어도 다른 GET은 1초 안에 응답`이 실패할 것입니다. 실패 메시지를 읽고 답하세요.

1. 서버는 지금 정확히 어느 syscall에서 멈춰 있는가? (`strace -p <PID>`로 확인)
2. 그동안 두 번째 클라이언트의 connect는 성공했나? 그 연결은 어디에 있나? (`ss -tn`의 Recv-Q, C01의 backlog)
3. 스레드 1만 개를 만들면 무엇이 문제가 되는가?

## 만들 것

- [ ] accept한 fd마다 `pthread_create`로 스레드를 만들어 연결 루프를 실행, `pthread_detach`
- [ ] fd를 스레드 인자로 넘기기 — `(void *)(intptr_t)fd` 또는 malloc한 구조체 (지역 변수의 주소를 넘기면 왜 안 될까?)
- [ ] 스레드 간 공유 상태가 없는지 점검 (전역 버퍼, `static` 변수, `strtok` 같은 비재진입 함수)
- [ ] Makefile의 `LDLIBS ?=`를 `LDLIBS ?= -pthread`로 (glibc 2.34+는 없어도 링크되지만, 이식성을 위해 명시)

<details><summary>힌트 — pthread_create 실패</summary>

스레드 생성이 실패하면(`EAGAIN`: 스레드/메모리 한도) 그 fd를 닫고 계속 accept합니다. 이 실패를 서버 종료로 처리하면 연결 폭주 한 번에 서버가 죽습니다. 그렇다고 이 버전에 연결 수 상한이 있는 것은 아닙니다 — 그건 C13에서.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c10
```

### 측정 — 스레드 비용

```sh
./build/c/http-server & PID=$!
python3 - <<'EOF' &
import socket, time
socks = [socket.create_connection(("127.0.0.1", 8080)) for _ in range(500)]
for s in socks: s.sendall(b"GET / HTTP/1.1\r\n")   # 반쪽 헤더로 붙잡아 둠
time.sleep(5)
EOF
sleep 2
grep -E 'Threads|VmRSS|VmSize' /proc/$PID/status
grep ctxt /proc/$PID/status
kill $PID
```

연결 500개 = 스레드 500개입니다. VmSize(가상 메모리)가 VmRSS보다 훨씬 큰 이유는 스레드마다 예약된 스택(기본 8MiB, `ulimit -s`)입니다.

> 여기서 `git tag c10-threads`. B01의 "C threads" 기준선입니다.

## 알았어야 하는 것

<details><summary>1. 단일 스레드 blocking 서버는 어디서 멈추나</summary>

반쪽 헤더를 보낸 클라이언트의 `recv`에서 멈춥니다. 그동안 accept를 부르지 않으므로 새 연결은 커널의 accept 큐(backlog)에 쌓인 채 기다립니다 — connect는 성공했지만 아무도 처리하지 않습니다. 느린 클라이언트 하나가 서버 전체를 점유하는 것, 이것이 이후 모든 동시성 단계가 푸는 문제입니다.
</details>

<details><summary>2. 지역 변수 주소를 스레드에 넘기면</summary>

`pthread_create(&t, NULL, fn, &fd)`에서 `fd`가 accept 루프의 지역 변수면, 새 스레드가 그 값을 읽기 전에 루프가 다음 accept로 `fd`를 덮어쓸 수 있습니다(data race). 값 자체를 넘기거나, 스레드마다 별도 메모리를 할당합니다. Rust에서는 `thread::spawn`의 `move` 클로저가 소유권을 넘겨서 이 실수를 컴파일 단계에서 막습니다(R02).
</details>

<details><summary>3. thread-per-connection의 비용</summary>

스레드마다 스택 가상 메모리, 커널 자료구조, 스케줄링 비용이 듭니다. 놀고 있는 연결도 스레드 하나를 잡아먹습니다. 수천 개까지는 의외로 잘 버티지만, 만 단위 idle 연결(C10K 문제)에서는 메모리와 context switch가 병목이 됩니다. 연결 수 상한이 없으면 공격자가 스레드를 무한히 만들게 할 수 있습니다. thread pool은 상한을 주지만, pool이 느린 클라이언트로 다 차면 결국 같은 문제가 됩니다.
</details>

<details><summary>4. "blocking I/O + 스레드"와 "nonblocking I/O + 이벤트 루프"의 차이</summary>

전자는 "기다림"을 커널 스케줄러에 맡깁니다(스레드가 잠듦). 후자는 한 스레드가 "지금 준비된 fd"만 골라 처리하고 기다림은 epoll에 모읍니다. 기다리는 주체가 스레드 → fd로 바뀝니다. 그 대가로 연결의 진행 상황(어디까지 읽었고 어디까지 보냈는지)을 스택이 아니라 **명시적 상태**로 저장해야 합니다. C11이 그 작업입니다.
</details>

## 기록

측정 결과(Threads, VmRSS, VmSize, ctxt switches)와 "단일 스레드가 멈춘 syscall"의 strace 한 줄.
