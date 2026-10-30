# C02 — TCP echo

> 목표: TCP는 **메시지가 아니라 바이트 스트림**이라는 것을, recv/send 반환값을 정확히 처리하는 코드로 증명한다.
> 선행: [C01](01-listen-accept.md) · 다음: [C03](03-serve-forever.md) · 검사: `python3 workbook/check.py c02`

## 먼저 공부할 것

- `man 2 recv`, `man 2 send`의 RETURN VALUE·ERRORS 절. 특히 "return value 0"과 `EINTR`
- 검색어: `TCP byte stream partial read short write recv zero`, `TCP half close shutdown SHUT_WR`

답을 적고 시작하세요.

1. 클라이언트가 `send("hello")` 한 번, `send("world")` 한 번 했다. 서버의 `recv`는 몇 번 호출되고 각각 무엇을 받을까?
2. `recv`가 0을 반환하는 것과 -1을 반환하는 것은 각각 무슨 뜻인가?
3. `send(fd, buf, 1000, 0)`이 400을 반환했다. 나머지 600바이트는 어떻게 되었나?

## 만들 것

C01 코드를 이어서 고칩니다. 여전히 **클라이언트 하나**만 받고 끝납니다.

- [ ] accept한 연결에서 `recv`를 반복: 받은 bytes를 그대로 돌려보냄
- [ ] `recv == 0` → 상대가 보내기를 끝냄(EOF). 루프 종료 후 close
- [ ] `recv == -1 && errno == EINTR` → 다시 호출. 그 외 -1 → 오류 로그 후 종료
- [ ] `send_all(fd, buf, len)` 함수: `send`가 일부만 보내도 **남은 부분을 offset을 옮겨가며** 다 보낼 때까지 반복. EINTR도 재시도
- [ ] 받은 데이터를 문자열 함수로 다루지 않음 (NUL이 섞여 있을 수 있음)

<details><summary>힌트 1 — send_all의 모양</summary>

```c
static int send_all(int fd, const char *p, size_t n)
{
	size_t off = 0;
	while (off < n) {
		/* send(fd, p + off, n - off, 0) 의 반환값을 세 갈래로 처리 */
	}
	return 0;
}
```
반환값이 양수면 `off += (size_t)w`. 왜 `(size_t)` 캐스팅을 **양수 확인 후에** 하는지 C00 3.1을 떠올리세요.
</details>

<details><summary>힌트 2 — 1MiB를 echo하는데 멈춘다면</summary>

서버가 1KiB 받고 1KiB 돌려보내는 구조면 문제없습니다. 서버가 **전부 받은 뒤에** 돌려보내려 하면, 클라이언트도 다 보낸 뒤에 읽기 시작하는 경우 양쪽 버퍼가 차서 교착합니다. 받은 만큼 바로 돌려보내세요.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c02
```

검사는 NUL이 섞인 1MiB 랜덤 데이터를 4093바이트씩 쪼개 보내고, `shutdown(SHUT_WR)`로 EOF를 알린 뒤 돌려받은 내용이 정확히 같은지 봅니다.

### 직접 관찰

```sh
strace -e trace=accept,recvfrom,sendto,close ./build/c/http-server &
python3 - <<'EOF'
import socket, time
s = socket.create_connection(("127.0.0.1", 8080))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
for part in (b"hel", b"lo ", b"world"):
    s.sendall(part); time.sleep(0.1)
s.shutdown(socket.SHUT_WR)          # FIN: "나는 다 보냈다". 읽기는 계속 가능
print(s.recv(100))
EOF
```

- `sleep` 없이 보내면 recvfrom 호출 수가 어떻게 바뀌나요? 같은 프로그램인데 매번 같나요?
- 마지막 `recvfrom(...) = 0`이 FIN을 받은 순간입니다.
- `nc 127.0.0.1 8080`으로 손으로 쳐 보고, Ctrl-D(EOF)와 Ctrl-C(종료)의 차이를 strace로 보세요.

### 일부러 깨뜨려 보기

1. `send_all` 대신 `send` 한 번만 쓰고 반환값을 무시 → 검사가 통과할 수도 있습니다. 왜 loopback에서는 short write가 잘 안 일어날까요? (큰 send buffer) 그래도 계약상 처리해야 하는 이유는?
2. 받은 데이터를 `printf("%s", buffer)`로 출력 → NUL에서 잘리고, 끝에 `\0`이 없으면 버퍼 밖을 읽습니다. `make sanitize`로 실행해 ASan이 잡는지 보세요.
3. `recv` 반환값을 확인하지 않고 바로 `size_t`로 → 클라이언트를 RST로 끊으면(`SO_LINGER` 0) 무슨 일이?

## 알았어야 하는 것

<details><summary>1. send 두 번과 recv의 관계</summary>

대응하지 않습니다. TCP는 경계 없는 바이트 순서만 보장합니다. 커널은 여러 send를 합쳐 한 segment로 보낼 수 있고(Nagle), 한 send를 여러 segment로 나눌 수 있고, 수신 쪽은 도착해 쌓인 만큼 recv에 줍니다. "hello"와 "world"를 한 번에 "helloworld"로 받을 수도, "hel"/"loworld"로 받을 수도 있습니다. 그래서 **메시지 경계는 애플리케이션 프로토콜이 정해야** 합니다 — HTTP에서는 `\r\n\r\n`과 Content-Length(C04, C07).
</details>

<details><summary>2. recv 0, -1, EINTR</summary>

0: 상대가 FIN을 보냈고 그 전 데이터는 다 읽었음(EOF). 이후 recv는 계속 0. -1: 오류 — `ECONNRESET`(RST를 받음), `EINTR`(시그널로 중단, 재시도) 등. 0바이트 버퍼로 호출하지 않는 한 "0 = EOF"는 확실합니다.
</details>

<details><summary>3. short write</summary>

`send`는 커널 송신 버퍼에 복사한 바이트 수를 반환합니다. 버퍼가 꽉 차면 blocking 소켓은 공간이 생길 때까지 기다리지만, 시그널이 끼거나 nonblocking이면 일부만 복사하고 돌아옵니다. 나머지는 **아무 데도 안 갔으니** 호출자가 다시 보내야 합니다. 송신 offset을 관리하는 이 패턴은 C12(epoll 쓰기 경로)에서 연결 상태로 그대로 옮겨갑니다.
</details>

<details><summary>4. shutdown(SHUT_WR)과 close의 차이</summary>

`shutdown(SHUT_WR)`은 FIN만 보내고 fd와 읽기 방향은 남깁니다(half-close). echo 클라이언트가 "다 보냈으니 남은 응답을 마저 줘"를 표현하는 방법입니다. `close`는 fd를 해제하고, 그 뒤 도착하는 데이터에는 RST로 응답합니다. 아직 읽지 않은 데이터가 수신 버퍼에 있는데 close하면 FIN 대신 곧바로 RST가 나갑니다.
</details>

## 기록

strace로 본 "send 3번 → recvfrom N번"의 실제 N, 그리고 왜 매번 다를 수 있는지.
