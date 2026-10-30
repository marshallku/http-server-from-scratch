# C00 — 준비와 사전 지식

> 목표: 도구를 갖추고, C에서 버퍼·오류·자원을 다룰 때 이 학습지 내내 쓸 사고방식을 먼저 맞춥니다.
> 다음: [C01](01-listen-accept.md)

## 1. 도구

```sh
make doctor                      # cc, cargo, clang-format, rustfmt, clippy
sudo pacman -S strace openbsd-netcat   # 이 머신에는 아직 없음 (Arch 기준)
command -v curl ss gdb python3   # 이미 있음
```

| 도구 | 언제 | 무엇을 보는가 |
|---|---|---|
| `strace -f -e trace=network,read,write,close` | C01~ | 내 코드가 실제로 부른 syscall과 반환값 |
| `ss -tanp` | C01~ | 소켓 상태(LISTEN, ESTAB, TIME_WAIT, CLOSE_WAIT)와 큐 길이 |
| `ls -l /proc/<PID>/fd` | C03~ | 열린 fd 목록 (누수 확인) |
| `nc 127.0.0.1 8080` | C02~ | 손으로 bytes 보내기 |
| `curl -v --http1.1` | C04~ | 정상 HTTP 클라이언트의 동작 |
| `make sanitize` (ASan/UBSan) | C05~ | 버퍼 범위 초과, use-after-free, 정수 UB |
| `gdb` | 막힐 때 | 크래시 지점, 변수 값 |

## 2. Makefile이 서버와 만날 때 생기는 문제 (C03에서 고칠 것)

지금 `make check`, `make sanitize`는 빌드한 바이너리를 **실행하고 끝나기를 기다립니다**. C03부터 서버는 영원히 돌기 때문에 이 명령들이 멈춥니다.
C03에 도달하면 [C03 문서](03-serve-forever.md#makefile-고치기)대로 `check-c`/`sanitize`가 바이너리를 직접 실행하는 대신 `check.py`를 부르게 바꿉니다.

## 3. 사전 지식 — 답할 수 있으면 넘어가세요

### 3.1 버퍼는 (포인터, 길이, 용량) 세 값이다

네트워크에서 온 데이터는 **문자열이 아니라 bytes**입니다. 중간에 `\0`이 있을 수 있고 끝에 `\0`이 없습니다.
그래서 `strlen`, `strstr`, `printf("%s")`, `strcpy`를 받은 데이터에 쓰면 안 됩니다. 항상 길이를 따로 들고 다닙니다.

```c
struct buf {
	char *data;  /* malloc한 메모리 */
	size_t len;  /* 실제로 채워진 바이트 수 */
	size_t cap;  /* 할당된 크기 */
};
```

- 추가할 때: `len + n > cap`이면 `realloc`. realloc은 **주소를 바꿀 수 있어서** 예전 `data`를 가리키던 포인터는 전부 무효가 됩니다.
- 앞에서 n바이트 소비할 때: `memmove(data, data + n, len - n); len -= n;` (또는 시작 offset을 따로 둠)

<details><summary>질문: <code>recv</code>가 -1을 반환했는데 결과를 <code>size_t</code>로 바로 바꾸면?</summary>

`(size_t)-1`은 `SIZE_MAX`(64비트에서 약 1.8×10¹⁹)가 됩니다. 그 값만큼 `len`을 늘리거나 `fwrite`하면 메모리 범위를 넘습니다. 그래서 `ssize_t`로 받고, -1/0/양수를 먼저 구분한 **다음에** `size_t`로 바꿉니다.
</details>

### 3.2 syscall 오류는 반환값 + errno

대부분의 syscall은 실패하면 -1을 반환하고 `errno`에 이유를 넣습니다. `errno`는 **다음 libc 호출이 덮어쓸 수 있으므로** 실패 직후에 확인하거나 `perror`합니다.

| errno | 의미 | 대응 |
|---|---|---|
| `EINTR` | 기다리는 중에 시그널이 와서 중단됨 | 같은 호출을 다시 |
| `EAGAIN`/`EWOULDBLOCK` | nonblocking fd에 지금은 할 게 없음 | 나중에(epoll이 알려줄 때) 다시 (C11) |
| `EPIPE`, `ECONNRESET` | 상대가 연결을 끊음 | 이 연결만 정리 |
| `EADDRINUSE` | 포트를 누가 쓰고 있음 | C01에서 다룸 |
| `EMFILE` | 프로세스 fd 한도 | C13에서 다룸 |

### 3.3 자원을 얻었으면 모든 경로에서 정리한다

fd, malloc한 메모리는 **정상 경로와 모든 오류 경로**에서 정확히 한 번 해제해야 합니다. 원래 `main.c`에 "아 이거 이제 좀 쓰기 귀찮음"이라고 적었던 그 문제입니다.
C에서 흔히 쓰는 해결책은 함수 끝에 정리 구역을 하나 두고 `goto`로 모으는 것입니다.

```c
int fd = -1;
char *p = NULL;
int status = 1;

fd = socket(...);
if (fd == -1) { perror("socket"); goto out; }
p = malloc(...);
if (p == NULL) goto out;
/* ... */
status = 0;
out:
	free(p);              /* free(NULL)은 아무것도 안 함 */
	if (fd != -1) close(fd);
	return status;
```

> Rust에서는 이 정리 구역이 `Drop`으로 옮겨갑니다. R02에서 "C의 어느 정리 코드가 Rust의 어디로 갔는가"를 대응표로 적게 됩니다.

### 3.4 정수 overflow

`Content-Length: 99999999999999999999999`을 `n = n * 10 + digit`으로 그냥 계산하면 `uint64_t`가 넘칩니다. unsigned는 조용히 wrap-around되고(작은 숫자가 됨), signed는 **undefined behavior**입니다.
규칙: **더하기 전에** 결과가 한도를 넘는지 확인합니다. (C07)

### 3.5 network byte order

포트·주소를 `struct sockaddr_in`에 넣을 때는 big-endian(network order)이어야 합니다. x86은 little-endian이라 `htons(8080)`으로 바꿉니다.
<details><summary>질문: <code>htons</code>를 빼먹고 <code>sin_port = 8080</code>이라고 쓰면 어느 포트에서 listen 할까?</summary>

8080 = `0x1F90`. little-endian 메모리에는 `90 1F`로 저장되고, 커널은 이를 big-endian `0x901F` = 36895로 읽습니다. `ss -ltn`으로 직접 확인해 보세요.
</details>

## 4. 읽을거리

- Beej's Guide to Network Programming — 5장(System Calls or Bust)까지. 원래 main.c 주석에 링크했던 그 문서입니다.
- `man 2 socket`, `man 2 recv`, `man 2 send`, `man 7 tcp` — **RETURN VALUE와 ERRORS 절**을 습관처럼 먼저 봅니다.
- `docs/STUDY.md`의 "C와 컴퓨터 시스템" 표 — 막힐 때 검색어로 씁니다.
