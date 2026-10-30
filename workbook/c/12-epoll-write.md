# C12 — epoll 쓰기 경로와 backpressure

> 목표: 보낼 데이터를 **연결 상태에 보관**하고 소켓이 받아줄 때만 보낸다. 상대가 안 읽으면 그 연결의 입력도 멈춰서 메모리를 제한한다.
> 선행: [C11](11-epoll-read.md) · 다음: [C13](13-deadlines.md) · 검사: `python3 workbook/check.py c12`

## 먼저 공부할 것

- `man 7 epoll`의 EPOLLOUT, `man 2 epoll_ctl`의 `EPOLL_CTL_MOD`
- 검색어: `partial write EPOLLOUT output buffer`, `backpressure bounded queue`

먼저 C11 서버로 실패를 봅니다.

```sh
python3 workbook/check.py c12 --only
```

답을 적고 시작하세요.

1. nonblocking 소켓에 1MiB를 send했는데 300KiB만 들어갔다. 나머지 700KiB를 지금 루프에서 계속 재시도하면 다른 연결은 어떻게 되나?
2. 클라이언트가 응답을 읽지 않으면서 요청만 계속 보낸다. 서버가 계속 읽어서 응답을 쌓아 두면?
3. "읽기를 멈춘다"는 것은 클라이언트 입장에서 어떻게 보이나? (TCP flow control)

## 만들 것

- [ ] `struct conn`에 송신 offset: `out`(대기 중인 응답 bytes) + `out_off`(어디까지 보냈나)
- [ ] `flush(c)`: `send`를 `EAGAIN`까지 반복, 보낸 만큼 `out_off` 전진. 다 보내면 `out` 비우기
- [ ] **관심 이벤트를 상태에서 계산**하는 함수 하나:
  - 대기 출력이 있으면 `EPOLLOUT` 구독
  - 대기 출력이 한도(64KiB) 미만이고 닫을 예정이 아니면 `EPOLLIN` 구독
  - 바뀔 때만 `EPOLL_CTL_MOD`
- [ ] writable 이벤트 → `flush` → 다 보냈으면 (읽기를 멈췄던 동안 버퍼에 남아 있던) 다음 요청을 처리
- [ ] `Connection: close` 응답은 **다 보낸 뒤에** close
- [ ] 처리 루프도 출력이 한도를 넘으면 멈춤 (pipelining된 요청 수천 개를 한 번에 응답으로 만들지 않도록)

<details><summary>힌트 1 — 상태로부터 interest 계산하기</summary>

```c
static int update_interest(struct conn *c)
{
	uint32_t ev = 0;
	if (!c->closing && pending(c) < OUT_LIMIT) ev |= EPOLLIN;
	if (pending(c) > 0)                       ev |= EPOLLOUT;
	if (ev == c->registered) return 0;
	/* EPOLL_CTL_MOD, c->registered = ev */
}
```
이벤트 핸들러가 끝날 때마다 이걸 부르면, "EPOLLOUT을 언제 켜고 끄나"를 핸들러마다 따로 생각할 필요가 없습니다.
</details>

<details><summary>힌트 2 — 왜 응답을 다 만든 뒤 즉시 send를 시도하나</summary>

대부분의 응답은 작아서 바로 다 보내집니다. 그때마다 EPOLLOUT을 켜고 다음 바퀴를 기다리면 syscall만 늘어납니다. "일단 보내 보고, 남으면 EPOLLOUT"이 일반적인 패턴입니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c12
```

검사 내용: (1) 1MiB 응답을 안 읽는 클라이언트가 있는 동안 다른 GET이 1초 안에 응답하고, 나중에 읽으면 1MiB가 정확히 도착, (2) 응답을 안 읽고 64KiB 요청을 계속 밀어넣는 클라이언트가 있어도 서버 RSS 증가 32MiB 미만 + 다른 요청 정상 + idle CPU 정상.

> 검사 (2)는 한도 없는 구현이면 서버 메모리가 초당 GB 단위로 늘어납니다. 검사기는 32MiB를 넘는 순간 멈춥니다.

### 직접 관찰

```sh
./build/c/http-server & PID=$!
python3 - <<'EOF' &
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
s.connect(("127.0.0.1", 8080))
body = b"x" * (1 << 20)
s.sendall(b"POST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
time.sleep(5)   # 응답을 안 읽음
EOF
sleep 1; ss -tnp 'sport = :8080'     # 서버 쪽 Send-Q가 쌓여 있음
grep VmRSS /proc/$PID/status
```

### 일부러 깨뜨려 보기

1. 출력 한도 확인을 빼고 검사 (2)를 실행 → RSS 실패 메시지 확인.
2. `flush`에서 `out_off`를 쓰지 않고 매번 `out.data`부터 보내면? 검사 (1)의 "나중에 읽으면 1MiB 정확히 도착"이 어떻게 실패하나요?

## 알았어야 하는 것

<details><summary>1. backpressure</summary>

생산자(클라이언트의 요청)가 소비자(클라이언트의 응답 읽기)보다 빠를 때, 중간 버퍼를 무한히 키우는 대신 **생산자를 늦추는 것**입니다. 서버가 그 연결에서 읽기를 멈추면 커널 수신 버퍼가 차고, TCP 수신 window가 0이 되어 클라이언트의 send가 막힙니다. 결국 메모리 사용량이 "출력 한도 + 커널 버퍼"로 제한됩니다. blocking + 스레드 서버에서는 send가 막혀서 이게 저절로 일어났습니다(C10이 이 검사를 통과하는 이유).
</details>

<details><summary>2. 쓰기 경로가 읽기보다 어려운 이유</summary>

읽기는 "들어온 만큼 처리"라 상태가 단순하지만, 쓰기는 "아직 못 보낸 bytes"라는 **약속된 미래 작업**을 들고 있어야 합니다. 그 사이 연결이 끊기거나, 다음 요청이 오거나, 타임아웃이 날 수 있습니다. Rust async에서 `write_all`을 `select!`로 취소하면 이 "못 보낸 bytes"가 어디로 가는지가 cancellation safety 문제입니다(R06).
</details>

<details><summary>3. 공정성</summary>

한 연결이 계속 데이터를 보내도 C11의 recv 횟수 제한과 출력 한도 덕분에 한 바퀴에 처리하는 양이 제한되고, 다른 연결의 이벤트도 같은 epoll_wait 결과에서 처리됩니다. 한 연결의 작업량을 제한하지 않으면 그 연결이 이벤트 루프를 독점합니다.
</details>

## 기록

`ss -tnp`에서 본 Send-Q/Recv-Q 값과, 내 interest 계산 규칙 2줄.
