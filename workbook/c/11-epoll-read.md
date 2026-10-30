# C11 — epoll 이벤트 루프: 읽기 경로

> 목표: 스레드 하나로 여러 연결을 처리한다. "기다리기"를 epoll에 모으고, 각 연결의 진행 상황을 **명시적 상태**로 저장한다.
> 선행: [C10](10-threads.md) · 다음: [C12](12-epoll-write.md) · 검사: `python3 workbook/check.py c11`

## 먼저 공부할 것

- `man 7 epoll` 전체 — 특히 "Level-triggered and edge-triggered", "Questions and answers" 절
- `man 2 epoll_create1`, `man 2 epoll_ctl`, `man 2 epoll_wait`, `man 2 fcntl`(`O_NONBLOCK`)
- 검색어: `blocking nonblocking asynchronous IO readiness completion`, `select poll epoll level triggered edge triggered EAGAIN`, `reactor pattern connection state machine`

답을 적고 시작하세요.

1. nonblocking 소켓에서 읽을 데이터가 없을 때 `recv`는 무엇을 반환하나?
2. epoll이 "fd 5가 읽기 가능"이라고 알려줬다. 이것은 "recv가 데이터를 돌려준다"는 보장인가?
3. level-triggered에서 데이터 100바이트 중 50바이트만 읽고 다시 `epoll_wait`하면? edge-triggered라면?
4. nonblocking은 async와 같은 말인가?

## 만들 것

이 단계는 **읽기 경로**만 epoll로 옮깁니다. 응답 송신은 일단 기존 `send_all`처럼 보내도 됩니다 — 단, nonblocking 소켓에서 `EAGAIN`을 만나면 busy-wait하게 되므로, 응답이 작은 지금은 괜찮지만 1MiB echo에서 문제가 생깁니다. 그 문제가 C12의 주제입니다.

- [ ] listen 소켓과 accept한 소켓을 모두 `O_NONBLOCK`
- [ ] `epoll_create1(EPOLL_CLOEXEC)`, listen fd를 `EPOLLIN`(level-triggered)으로 등록
- [ ] 연결 상태 구조체: `struct conn { int fd; struct buf in, out; ... }` — C08에서 연결 루프의 **지역 변수**였던 것들이 여기로 옵니다
- [ ] fd → conn 찾기: `epoll_event.data.ptr`에 conn 포인터, 또는 fd 번호로 인덱싱하는 배열
- [ ] listen fd가 readable → `accept`를 `EAGAIN`이 날 때까지 반복, 새 fd 등록
- [ ] conn fd가 readable → `recv`해서 `in`에 붙이고 C08의 `handle_input`을 완성된 요청이 없을 때까지 반복
- [ ] 한 연결에서 한 번에 너무 많이 처리하지 않기 (예: recv 최대 4번) — level-triggered라 남은 데이터는 다음 바퀴에 다시 알려줍니다
- [ ] 닫을 때: `epoll_ctl(DEL)` → `close` → conn 해제. 같은 `epoll_wait` 결과 배열에 이미 닫은 fd의 이벤트가 남아 있을 수 있음에 주의
- [ ] `epoll_wait` timeout은 -1(무한). 할 일이 없으면 잠들어야 합니다

<details><summary>힌트 1 — 이벤트 루프 뼈대</summary>

```
loop:
    n = epoll_wait(epfd, events, 64, -1)       # EINTR이면 continue
    for each event:
        if fd == listen_fd: accept_all()
        else:
            c = lookup(fd); if c == NULL: continue   # 이번 배치에서 이미 닫힘
            if events & (EPOLLERR|EPOLLHUP) and not EPOLLIN: close_conn(c); continue
            on_readable(c)
```
</details>

<details><summary>힌트 2 — 왜 "닫은 fd의 이벤트"가 문제인가</summary>

이벤트 배열에 [A, B]가 있고 A를 처리하다 B를 닫았다면(예: 연결 수 제한), 다음 차례의 B 이벤트는 해제된 conn을 가리킵니다. 더 나쁜 경우, B를 닫은 뒤 새 accept가 **같은 fd 번호**를 재사용해서 엉뚱한 연결로 이벤트가 갑니다. fd 배열 방식이면 `conns[fd] = NULL`로, ptr 방식이면 "닫힘 표시 후 배치 끝에 해제"로 막습니다.
</details>

<details><summary>힌트 3 — 왜 EPOLLOUT을 항상 구독하면 안 되나</summary>

소켓 송신 버퍼에 공간이 있으면 거의 항상 "쓰기 가능"입니다. level-triggered로 EPOLLOUT을 구독해 두면 epoll_wait가 매번 즉시 돌아와서 CPU 100%가 됩니다. 검사의 "idle CPU"가 이걸 잡습니다. 보낼 게 있을 때만 구독하는 건 C12에서 합니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c11     # C04~C10 전부 + idle CPU + 연결 churn
make sanitize STEP=c11
```

C10의 "느린 클라이언트" 검사를 **스레드 없이** 통과해야 합니다.

### 직접 관찰

```sh
strace -e trace=epoll_wait,accept,accept4,recvfrom,sendto,close ./build/c/http-server
# 다른 터미널에서 curl 몇 번 + 반쪽 헤더 연결 하나
```

- `epoll_wait(...) = 1`과 그 뒤 recvfrom이 `EAGAIN`으로 끝나는 패턴을 찾습니다.
- 아무 요청이 없을 때 strace 출력이 멈춰 있어야 합니다(잠들어 있음).
- `cat /proc/$PID/status | grep Threads` → 1.

### 일부러 깨뜨려 보기

1. conn fd를 `EPOLLIN | EPOLLOUT`으로 등록 → `python3 workbook/check.py c11 --only`의 idle CPU 검사 실패 확인, `top`으로 100% 확인.
2. listen 소켓을 blocking으로 두면? 이벤트 1개에 accept를 2번 부르면 두 번째에서 무슨 일이?
3. accept를 루프 없이 이벤트당 1번만 하면 level-triggered에서는 괜찮은데, edge-triggered에서는 왜 연결이 방치되나요?

## 알았어야 하는 것

<details><summary>1. readiness는 완료가 아니다</summary>

epoll은 "지금 recv하면 blocking 없이 뭔가 일어난다"(readiness)를 알려줄 뿐, 몇 바이트를 줄지·요청이 완성됐는지는 모릅니다. readable인데 recv가 `EAGAIN`인 경우(spurious wakeup)도 허용됩니다. 그래서 항상 실제 syscall 결과로 판단합니다. 이와 달리 io_uring/IOCP는 "이 읽기가 끝났다"(completion)를 알려주는 모델입니다.
</details>

<details><summary>2. LT vs ET</summary>

level-triggered: 조건이 **참인 동안** 매번 알림. 덜 읽어도 다음 epoll_wait가 다시 알려주므로 공정성을 위해 일부만 처리해도 됩니다. edge-triggered: 조건이 **바뀔 때** 한 번 알림. `EAGAIN`이 날 때까지 다 읽지 않으면 다음 알림이 오지 않아 연결이 멈춥니다. ET는 알림 횟수가 적지만 drain 규칙과 starvation 방지를 직접 관리해야 합니다. R05에서 ET를 써 봅니다.
</details>

<details><summary>3. nonblocking ≠ async</summary>

nonblocking은 "기다려야 하면 기다리지 말고 EAGAIN을 돌려달라"는 fd의 속성입니다. async는 "기다리는 동안 다른 일을 하다가, 준비되면 이어서 한다"는 프로그램 구조입니다. epoll 이벤트 루프는 nonblocking fd + readiness 알림 + 연결별 상태 저장으로 async 구조를 만든 것입니다. Rust의 async/await는 이 "연결별 상태 저장"을 컴파일러가 대신 해줍니다(R03).
</details>

<details><summary>4. 스택에 있던 상태가 구조체로 간 이유</summary>

blocking 버전에서 "어디까지 읽었나"는 연결 루프의 지역 변수와 **어느 줄에서 멈춰 있는지(프로그램 카운터)** 로 표현됐습니다. 이벤트 루프에서는 한 스레드가 여러 연결을 번갈아 처리하므로, 그 정보를 연결마다 구조체에 저장하고 이벤트가 올 때 꺼내야 합니다. 이것이 상태 머신입니다.
</details>

## 기록

strace에서 본 "epoll_wait → recvfrom … → recvfrom = -1 EAGAIN" 한 묶음과, 내 `struct conn`의 필드 목록 + 각 필드가 C08 버전에서 무엇이었는지.
