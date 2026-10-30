# C04 — 최소 HTTP

> 목표: "요청 하나가 어디서 끝나는가"를 애플리케이션이 판단한다. recv 한 번이 요청 하나라는 가정을 버린다.
> 선행: [C03](03-serve-forever.md) · 다음: [C05](05-parser.md) · 검사: `python3 workbook/check.py c04`

## 먼저 공부할 것

- [RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html) §2.1 Message Format, §2.2 Message Parsing — 짧습니다. "octets", "CRLF"라는 단어에 주목
- `curl -v http://example.com`으로 실제 요청/응답 모양 보기
- 검색어: `RFC 9112 message parsing CRLF octets`

답을 적고 시작하세요.

1. HTTP 요청의 head(request line + 헤더)는 어떤 바이트 열로 끝나는가?
2. 응답에 `Content-Length`가 왜 필요한가? 없으면 클라이언트는 body 끝을 어떻게 아는가?
3. 클라이언트가 헤더를 끝없이 보내면 서버 메모리는 어떻게 되는가?

## 만들 것

echo 서버를 HTTP 서버로 바꿉니다. 아직 요청 내용(method, 경로)은 보지 않습니다.

- [ ] 연결마다 **누적 버퍼**: recv한 bytes를 뒤에 붙이고, 붙일 때마다 `\r\n\r\n`이 있는지 검사 (`memmem`은 GNU 확장이니 직접 루프로 찾거나 `-D_GNU_SOURCE`)
- [ ] 찾으면 고정 응답을 보내고 close:
  ```
  HTTP/1.1 200 OK\r\n
  Content-Type: text/plain\r\n
  Content-Length: 14\r\n
  Connection: close\r\n
  \r\n
  Hello, World!\n
  ```
- [ ] 버퍼가 **8192바이트**가 됐는데 `\r\n\r\n`이 없으면 `431 Request Header Fields Too Large`(+ `Content-Length`, `Connection: close`) 후 close
- [ ] head가 다 오기 전에 EOF/오류가 나면 아무것도 보내지 않고 close
- [ ] 응답 송신은 C02의 `send_all` 재사용

버퍼는 고정 크기 `char buf[8192]`로 시작해도 됩니다. 8192 한도가 있으니까요. (C07에서 body가 생기면 동적 버퍼가 필요해집니다.)

<details><summary>힌트 1 — 찾을 때마다 처음부터 찾아야 하나?</summary>

새로 들어온 부분만 보면 되지만, `\r\n\r\n`이 **이전 recv와 이번 recv에 걸쳐** 있을 수 있습니다(`...\r\n\r` + `\n`). 이번 검색 시작 위치를 `max(0, 이전 길이 - 3)`로 잡으면 됩니다. 처음엔 매번 처음부터 찾아도 괜찮습니다 — 8KiB입니다.
</details>

<details><summary>힌트 2 — 431 경계</summary>

"head 전체가 8192바이트 이하면 허용"입니다. 마지막 `\r\n\r\n`을 포함해 정확히 8192바이트인 요청은 200, 8193바이트면 431. 그래서 판단은 "버퍼에 8192바이트가 있는데 그 안에 끝이 없다"입니다. C05의 유닛 테스트가 이 경계를 정확히 검사합니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c04
```

검사 내용: GET 응답 형식, 응답 후 닫기, **빈 줄이 오기 전에는 응답하지 않기**, 1바이트씩 쪼갠 요청, 9KiB 헤더 → 431, 조기 종료 클라이언트, fd 누수.

### 직접 관찰

```sh
curl -v --http1.1 http://127.0.0.1:8080/
curl -sS -o /dev/null -w '%{http_code}\n' -H "X-Big: $(head -c 9000 /dev/zero | tr '\0' a)" http://127.0.0.1:8080/   # 431

# 요청을 두 조각으로: 첫 조각 후 서버는 아무 말도 하지 않아야 함
python3 - <<'EOF'
import socket, time
s = socket.create_connection(("127.0.0.1", 8080))
s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n"); time.sleep(1)
s.settimeout(0.5)
try: print("early:", s.recv(100))
except socket.timeout: print("server waits (good)")
s.sendall(b"\r\n"); s.settimeout(2); print(s.recv(200))
EOF
```

strace로 위 스크립트를 보면 recvfrom 2번 → sendto 1번 순서가 보여야 합니다.

### 일부러 깨뜨려 보기

1. 첫 recv 직후 바로 응답하도록 바꾸고 `check.py c04` → 어떤 검사가 실패하나요? curl로는 왜 여전히 잘 되나요?
2. 431 한도를 없애고 `head -c 100000000 /dev/zero | python3 -c '...'`로 거대 헤더 → 고정 버퍼면 어디서 넘치나요? ASan으로.

## 알았어야 하는 것

<details><summary>1. 왜 curl은 잘 되는데 검사는 실패할 수 있나?</summary>

curl은 요청 전체를 한 번의 write로 보내고, loopback에서는 거의 항상 한 번의 recv로 도착합니다. "recv 한 번 = 요청 하나" 가정은 **로컬에서는 우연히 맞고** 실제 네트워크(작은 MSS, 느린 클라이언트, 프록시)에서 깨집니다. 테스트가 일부러 쪼개 보내는 이유입니다.
</details>

<details><summary>2. 응답에 Content-Length와 Connection: close가 왜 필요한가?</summary>

HTTP/1.1은 기본이 지속 연결이라, 클라이언트는 body가 어디서 끝나는지 알아야 다음 응답을 읽을 수 있습니다. Content-Length가 그 경계입니다. 지금은 응답 후 바로 닫으니 EOF로도 끝을 알 수 있지만, `Connection: close`로 "이 연결은 끝난다"고 명시하는 것이 정직합니다. C08에서 keep-alive를 켜면 Content-Length 없이는 아예 동작하지 않습니다.
</details>

<details><summary>3. 431 한도의 의미</summary>

입력 크기에 상한이 없으면 클라이언트 하나가 서버 메모리를 원하는 만큼 쓰게 할 수 있습니다. 네트워크 입력을 받는 모든 버퍼에는 상한이 있어야 하고, 상한에 닿았을 때의 동작(431 + close)을 정해야 합니다. 이 원칙이 C07(body 1MiB), C12(출력 64KiB), C13(시간, 연결 수)으로 이어집니다.
</details>

## 기록

"일부러 깨뜨려 보기 1"에서 실패한 검사 이름과 이유.
