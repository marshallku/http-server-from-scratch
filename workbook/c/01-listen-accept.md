# C01 — 연결 하나 받기

> 목표: `socket → setsockopt → bind → listen → accept → close`의 각 단계가 커널에 무엇을 만드는지 설명할 수 있다.
> 선행: [C00](00-prep.md) · 다음: [C02](02-echo.md) · 검사: `python3 workbook/check.py c01`

## 먼저 공부할 것

- `man 2 socket`, `man 2 bind`, `man 2 listen`, `man 2 accept`, `man 7 socket`의 `SO_REUSEADDR`
- Beej's Guide 5.1–5.6
- 검색어: `socket bind listen accept SO_REUSEADDR network byte order backlog`, `TCP TIME_WAIT active close`

답을 적고 시작하세요.

1. listen 소켓과 accept가 돌려준 소켓은 무엇이 다른가? 각각 무엇을 나타내는가?
2. `listen(fd, backlog)`의 backlog는 무엇의 길이인가? accept를 아직 안 했는데 클라이언트의 connect는 성공할까?
3. 연결을 **먼저 close한 쪽**에 남는 TCP 상태는? 얼마나 남는가?

## 만들 것

`c/src/main.c`를 다시 작성합니다 (지금 비어 있습니다).

- [ ] `socket(AF_INET, SOCK_STREAM, 0)`
- [ ] `setsockopt(SO_REUSEADDR, 1)` — **처음에는 빼고** 만든 뒤, 아래 실험으로 왜 필요한지 확인하고 추가합니다.
- [ ] `127.0.0.1:8080`에 `bind`. (선택) `PORT` 환경변수가 있으면 그 포트 사용 — `getenv`, `strtol`
- [ ] `listen`
- [ ] `accept`로 클라이언트 **하나**를 받고, `accepted fd=N` 같은 로그 출력
- [ ] 클라이언트 fd를 바로 `close`, listen fd도 `close`, exit 0
- [ ] 각 syscall 실패 시 `perror` 후 이미 얻은 fd를 정리하고 0이 아닌 코드로 종료 (C00 3.3의 정리 패턴)

이 단계에서는 데이터를 읽거나 쓰지 않습니다.

<details><summary>힌트 1 — 구조체 채우기</summary>

`struct sockaddr_in addr = {0};` 후 `sin_family = AF_INET`, `sin_port = htons(port)`, `sin_addr.s_addr = htonl(INADDR_LOOPBACK)`. `bind`에는 `(struct sockaddr *)&addr`와 `sizeof(addr)`를 넘깁니다. 왜 캐스팅이 필요한지는 `man 7 ip`의 주소 구조체 설명을 보세요.
</details>

<details><summary>힌트 2 — accept의 두 번째·세 번째 인자</summary>

클라이언트 주소가 필요 없으면 `NULL, NULL`. 필요하면 `struct sockaddr_in peer; socklen_t len = sizeof(peer);`를 넘기고, `len`은 입력이자 출력입니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c01
```

검사 내용: (1) 연결을 받고 닫은 뒤 exit 0, (2) **곧바로 다시 실행해도** bind 성공.

### 직접 관찰

```sh
# 터미널 A
strace -e trace=socket,setsockopt,bind,listen,accept,close ./build/c/http-server
# 터미널 B
python3 -c 'import socket; s=socket.create_connection(("127.0.0.1",8080)); print(s.recv(1))'
```

- strace 출력에서 accept가 반환한 fd 번호가 listen fd와 다른 것을 확인합니다. 0, 1, 2는 왜 비어 있지 않나요?
- 서버 실행 중에 `ss -ltn 'sport = :8080'` → `LISTEN` 줄의 Recv-Q/Send-Q는 무엇인가요? (`man 8 ss`)

### 일부러 깨뜨려 보기

1. **SO_REUSEADDR 없이** 실행 → 클라이언트 접속 → 서버 종료 → **바로** 다시 실행. `bind: Address already in use`가 나는지 보고, 그 순간 `ss -tan 'sport = :8080'`에서 `TIME-WAIT`을 찾습니다. 약 60초 뒤엔 다시 됩니다.
2. 같은 순서인데 **클라이언트가 먼저 끊으면**(서버가 accept 후 `sleep(1)` 하고 close) 서버 재실행이 되나요? TIME_WAIT은 누구 쪽에 남았나요?
3. 서버를 두 개 동시에 띄우면? (SO_REUSEADDR가 있어도 LISTEN 중인 포트는 bind 불가)
4. `htons`를 빼고 `ss -ltn`으로 실제 포트 확인.

## 알았어야 하는 것

<details><summary>1. fd는 무엇인가?</summary>

프로세스별 테이블의 인덱스(작은 정수)입니다. 커널의 열린 파일 객체(open file description)를 가리키고, 소켓도 파일처럼 같은 테이블에 들어갑니다. 새 fd는 비어 있는 가장 작은 번호를 받으므로, 0/1/2(stdin/stdout/stderr) 다음인 3부터 나옵니다.
</details>

<details><summary>2. listen fd와 accept fd의 차이</summary>

listen fd는 "이 주소로 오는 연결 요청을 받는 창구"이고 데이터를 주고받지 않습니다. accept는 커널이 3-way handshake를 끝내 놓은 연결을 큐(backlog)에서 하나 꺼내 **새 fd**로 줍니다. 연결 하나당 accept fd 하나입니다. 그래서 connect는 accept 전에도 성공합니다 — handshake는 커널이 합니다.
</details>

<details><summary>3. 왜 재시작 시 EADDRINUSE가 나고 SO_REUSEADDR로 해결되는가?</summary>

TCP에서 먼저 close한 쪽(active close)은 TIME_WAIT 상태로 2×MSL(Linux는 60초) 동안 남아 늦게 도착한 패킷이 새 연결로 오인되는 것을 막습니다. C01 서버는 accept 직후 먼저 close하므로 서버의 8080 포트에 TIME_WAIT이 남고, 기본 규칙상 그 포트에 다시 bind할 수 없습니다. SO_REUSEADDR는 "TIME_WAIT 연결이 있어도 LISTEN 소켓을 bind하겠다"는 뜻입니다. 이미 LISTEN 중인 포트를 빼앗지는 못합니다.
</details>

<details><summary>4. backlog는 얼마가 적당한가? (원래 main.c의 질문)</summary>

backlog는 handshake가 끝나고 accept를 기다리는 연결 큐의 상한이고, 커널이 `net.core.somaxconn`(최근 Linux 기본 4096)으로 잘라냅니다. accept가 충분히 빠르면 작아도 되고, 순간적인 연결 폭주를 흡수하려면 크게 둡니다. 성능에 따라 런타임에 바꾸는 값이 아니라 "accept 루프가 잠깐 밀릴 때의 완충"입니다. 큐가 넘치면 Linux는 기본적으로 클라이언트의 ACK를 버려 재전송을 유도합니다(`tcp_abort_on_overflow=0`).
</details>

<details><summary>5. sockaddr 캐스팅이 필요한 이유</summary>

bind/connect/accept는 IPv4, IPv6, Unix 소켓 등 여러 주소 체계를 받아야 해서 공통 헤더 `struct sockaddr *`와 길이를 받습니다. 실제 구조체(`sockaddr_in`)의 첫 필드 `sin_family`를 보고 커널이 해석 방법을 고릅니다. C에 다형성이 없을 때의 관용구입니다.
</details>

## 기록

`docs/CHECKPOINTS.md`에: TIME_WAIT 실험에서 본 `ss` 출력 한 줄, 그리고 "SO_REUSEADDR가 하는 일"을 3문장으로.
