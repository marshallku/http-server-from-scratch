# C07 — Content-Length body와 POST /echo

> 목표: head 뒤의 body가 **정확히 몇 바이트인지** 결정하는 규칙(framing)을 구현하고, 모호한 framing은 거부한다.
> 선행: [C06](06-routing.md) · 다음: [C08](08-keep-alive.md) · 검사: `python3 workbook/check.py c07`

## 먼저 공부할 것

- RFC 9112 §6.1–6.3 Message Body Length — 이 프로젝트에서 가장 중요한 절입니다. §6.3의 우선순위 목록을 직접 정리하세요
- RFC 9110 §10.1.1 Expect
- PortSwigger "HTTP request smuggling" 개요 (CL.TE, TE.CL)
- 검색어: `RFC 9112 message body length Transfer-Encoding Content-Length`, `C integer overflow checked arithmetic`

답을 적고 시작하세요.

1. 요청에 Content-Length와 Transfer-Encoding이 둘 다 있으면 RFC는 무엇을 하라고 하나? 이 프로젝트는?
2. `Content-Length: 10`인데 5바이트 보내고 연결이 끊겼다. 서버는 무엇을 해야 하나?
3. `Content-Length: 18446744073709551626`을 `uint64_t`로 계산하면 얼마가 되나?

## 만들 것

[계약](../00-contract.md)의 "요청 body" 표 전부.

- [ ] 입력 버퍼를 **동적 버퍼**로 (head 8KiB + body 최대 1MiB). C00 3.1의 `struct buf`
- [ ] Content-Length 파싱: 숫자만 1자 이상, **자릿수를 더하기 전에** 1MiB 초과 확인 → 413. 그 외 형식 오류 400
- [ ] Content-Length 2개 이상 → 400 (값이 같아도)
- [ ] Transfer-Encoding이 있으면: CL과 함께면 400, 2개 이상이면 400, 그 외는 **501** (chunked는 C09에서)
- [ ] Expect 헤더 → 417
- [ ] head를 파싱한 뒤 `head_len + content_length` 바이트가 모일 때까지 NEED_MORE
- [ ] `POST /echo` → 200, body 그대로. GET/HEAD에 body가 와도 읽고 버림
- [ ] body가 다 오기 전에 EOF → 응답 없이 close
- [ ] 오류 응답은 모두 `Connection: close` 후 close

<details><summary>힌트 1 — overflow 없는 10진수 파싱</summary>

```c
uint64_t n = 0;
for (each char c) {
	if (c < '0' || c > '9') return 400;
	n = n * 10 + (c - '0');
	if (n > MAX_BODY) return 413;
}
```
한도(1MiB)가 `UINT64_MAX / 10`보다 훨씬 작으므로 매 자리마다 한도를 넘는지 보면 곱셈 전에 이미 멈춥니다. 한도가 크다면 `n > (MAX - digit) / 10`을 곱하기 전에 확인해야 합니다. `strtol`/`atoi`는 왜 쓰지 않을까요? (`+5`, 앞 공백, overflow 시 동작을 man에서 확인)
</details>

<details><summary>힌트 2 — 의미 검사 순서</summary>

1) Host  2) CL 중복 / TE+CL / TE 중복 → 400  3) Expect → 417  4) TE 값 → 501  5) CL 값 → 400/413  6) body 기다리기.
검사 순서에 따라 "TE: gzip + CL: abc"가 400이 될지 501이 될지 갈립니다. 계약은 framing 충돌(400)을 먼저 봅니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c07
make sanitize STEP=c07          # 버퍼 재할당 버그는 ASan에서 드러납니다
```

### 직접 관찰

```sh
head -c 100000 /dev/urandom > /tmp/rand.bin
curl -s --data-binary @/tmp/rand.bin -H 'Content-Type: application/octet-stream' http://127.0.0.1:8080/echo | cmp - /tmp/rand.bin && echo same
curl -si -H 'Expect: 100-continue' --data-binary @/tmp/rand.bin http://127.0.0.1:8080/echo | head -1   # 417
printf 'POST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nabc' | nc -N 127.0.0.1 8080
```

curl은 1KiB가 넘는 body를 보낼 때 기본으로 `Expect: 100-continue`를 붙입니다(버전에 따라 다름). 첫 명령이 417을 받으면 `-H 'Expect:'`를 붙여 끄세요. 이 서버가 100-continue를 지원하지 않는 대가입니다.

### 일부러 깨뜨려 보기

1. CL 중복을 허용하고 첫 값을 쓰도록 바꾼 뒤, `Content-Length: 0` + `Content-Length: 30` + body 30바이트(그 안에 `GET /secret ...`)를 보내면? 마지막 값을 쓰는 프록시 뒤에 있다고 상상해 보세요.
2. body 버퍼를 realloc한 뒤에 예전 `struct http_request`의 slice를 쓰면? ASan의 `heap-use-after-free`를 직접 보세요.

## 알았어야 하는 것

<details><summary>1. framing이 모호하면 왜 거부해야 하나 (request smuggling)</summary>

프록시와 서버가 같은 바이트 열에서 body 길이를 다르게 계산하면, 한쪽이 body로 본 bytes를 다른 쪽은 **다음 요청**으로 봅니다. 공격자는 이를 이용해 다른 사용자의 요청 앞에 자기 요청을 끼워 넣습니다. RFC 9112 §6.3은 TE+CL 요청을 "smuggling 시도일 수 있으니 처리하더라도 연결을 닫으라"고 하고, 이 프로젝트는 더 엄격하게 400으로 거부합니다.
</details>

<details><summary>2. 잘린 body</summary>

Content-Length가 약속한 만큼 오지 않았으므로 요청은 **불완전**합니다. 처리하면 클라이언트가 보내지 않은 요청을 실행하는 셈입니다. 응답할 상대도 이미 떠났으니 조용히 닫습니다.
</details>

<details><summary>3. overflow</summary>

18446744073709551626 = 2⁶⁴ + 10. `uint64_t`에서 wrap-around되어 10이 됩니다. 서버는 10바이트 body로 보고, 나머지는 다음 요청으로 해석합니다 — framing 공격이 됩니다. signed 정수였다면 UB라서 컴파일러가 무엇을 해도 됩니다. 검사는 이 값을 정확히 보냅니다.
</details>

<details><summary>4. Expect: 100-continue를 왜 417로 처리하나</summary>

클라이언트는 `100 Continue`를 받을 때까지 body를 보내지 않고 기다립니다(curl은 1초 타임아웃 후 그냥 보냄). 서버가 이를 무시하면 서버는 body를, 클라이언트는 100을 기다리는 교착이 생길 수 있습니다. 구현하지 않을 거라면 명시적으로 거부하는 것이 계약을 지키는 방법입니다.
</details>

## 기록

§6.3 우선순위를 내 말로 요약한 목록, 그리고 내 서버가 그중 어디까지 구현했는지.
