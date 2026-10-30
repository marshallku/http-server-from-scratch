# C08 — keep-alive와 pipelining

> 목표: **메시지의 끝 ≠ 연결의 끝**. 한 연결에서 여러 요청을 처리하고, recv 한 번에 섞여 들어온 다음 요청의 bytes를 잃지 않는다.
> 선행: [C07](07-body.md) · 다음: [C09](09-chunked.md) · 검사: `python3 workbook/check.py c08`

## 먼저 공부할 것

- RFC 9112 §9.3 Persistence, §9.3.2 Pipelining, §9.6 Tear-down
- 검색어: `HTTP persistent connections pipelining head of line blocking`

답을 적고 시작하세요.

1. HTTP/1.1에서 연결을 닫는 경우는 언제인가? (클라이언트 요청, 서버 결정, 버전)
2. 클라이언트가 응답을 기다리지 않고 요청 3개를 한 번에 보냈다. 서버는 응답을 어떤 순서로 보내야 하나? 왜 HTTP/1.1에서는 그 순서를 바꿀 수 없나?
3. 400 오류 뒤에 붙어 온 bytes를 다음 요청으로 처리해도 될까?

## 만들 것

- [ ] 연결 처리 루프를 "요청 하나 → 응답 → close"에서 "요청 처리 → 응답 → **소비한 만큼만 버퍼에서 제거** → 남은 bytes로 다시 파싱 → 부족할 때만 recv"로 바꿈
- [ ] `Connection` 헤더 값을 쉼표로 나눈 토큰 중 `close`(대소문자 무시)가 있으면 → 응답에 `Connection: close` + 닫기
- [ ] HTTP/1.0 → 항상 닫기 (응답에 `Connection: close`)
- [ ] 오류 응답 후에는 남은 bytes를 버리고 닫기
- [ ] 클라이언트가 요청 사이에 끊으면(recv 0, 버퍼 비어 있음) 조용히 정리

이 루프를 다음 모양의 **순수 함수**로 정리해 두면 C11(epoll)에서 그대로 재사용됩니다.

```c
enum handle_result { H_NEED_MORE, H_RESPONSE };
/* in[0..len)의 맨 앞 요청 하나를 처리. 완성됐으면 응답을 out에 붙이고
 * *consumed(처리한 바이트 수), *close_after를 채운다. 소켓을 모른다. */
enum handle_result handle_input(const char *in, size_t len, struct buf *out,
				size_t *consumed, int *close_after);
```

<details><summary>힌트 1 — 연결 루프</summary>

```
loop:
    r = handle_input(in, &out, &consumed, &close)
    if r == RESPONSE:
        send_all(out); out.len = 0
        if close: break
        buf_consume(in, consumed)   # memmove로 앞을 지움
        continue                    # recv하지 말고 남은 bytes로 먼저 재시도!
    n = recv(...)
    if n <= 0: break
    append(in, n)
```
`continue`가 핵심입니다. 요청 3개가 한 번에 왔을 때 recv부터 하면 두 번째 요청을 처리하지 못한 채 영원히 기다립니다.
</details>

<details><summary>힌트 2 — consume 후 slice</summary>

`buf_consume`이 memmove로 데이터를 옮기므로, 그 전에 파싱 결과(slice)를 이용하는 일은 모두 끝나야 합니다. handle_input이 응답을 out에 다 써서 돌아오면 안전합니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c08
```

### 직접 관찰

```sh
curl -sv http://127.0.0.1:8080/ http://127.0.0.1:8080/nope 2>&1 | grep -iE 'reusing|re-using|left intact|HTTP/1.1 '
# "Reusing existing ... connection" (curl 버전에 따라 "Re-using") → 같은 연결로 두 번째 요청

printf 'GET / HTTP/1.1\r\nHost: a\r\n\r\nPOST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: 3\r\n\r\nabcGET /x HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n' \
  | nc -N 127.0.0.1 8080
```

strace에서 recvfrom 1번 → sendto 3번이 보이면 pipelining을 제대로 처리한 것입니다.

### 일부러 깨뜨려 보기

1. 힌트 1의 `continue`를 빼고(응답 후 항상 recv) 위 printf 파이프라인을 보내 보세요. 응답이 몇 개 오나요?
2. `buf_consume` 대신 `in.len = 0`(버퍼 통째로 비우기)로 바꾸면 어떤 검사가 실패하나요?

## 알았어야 하는 것

<details><summary>1. 메시지 끝과 연결 끝</summary>

HTTP/1.0 시절엔 응답 하나 후 연결을 닫아서 EOF가 곧 메시지 끝이었습니다. 지속 연결에서는 메시지 끝을 Content-Length(또는 chunked)로만 알 수 있고, 연결은 계속 살아 있습니다. 그래서 C07의 framing이 정확하지 않으면 keep-alive에서 바로 다음 요청이 망가집니다.
</details>

<details><summary>2. pipelining과 head-of-line blocking</summary>

HTTP/1.1 응답에는 "몇 번째 요청의 응답인지" 표시가 없어서 **요청 순서대로** 응답해야 합니다. 첫 요청이 느리면 뒤 요청은 준비돼 있어도 기다립니다(head-of-line blocking). HTTP/2는 stream id로 이 문제를 풉니다. 실제 브라우저는 이 문제 때문에 pipelining을 거의 쓰지 않지만, 서버는 받을 수 있어야 합니다.
</details>

<details><summary>3. 오류 뒤의 bytes</summary>

framing 오류(예: CL 중복)가 났다면 "이 요청이 어디서 끝나는지"를 모르므로 뒤 bytes의 시작점도 모릅니다. 그걸 다음 요청으로 해석하면 smuggling입니다. 그래서 오류 후에는 닫습니다. 404/405는 framing이 정상이므로 연결을 유지해도 됩니다.
</details>

<details><summary>4. 연결별 메모리 vs 요청별 메모리</summary>

입력 버퍼는 연결이 살아 있는 동안 유지되고(다음 요청의 앞부분이 들어 있을 수 있음), 파싱 결과·decoded body는 요청 하나가 끝나면 버립니다. 이 구분이 C11의 `struct conn`과 R06의 task 지역 변수로 이어집니다.
</details>

## 기록

"일부러 깨뜨려 보기 1"의 결과와, 내 연결 루프의 의사 코드.

> 다음 단계로 가기 전에: C09까지 끝나면 `git tag c09-blocking` (B01 비교용 기준선).
