# C05 — I/O와 분리한 요청 head 파서

> 목표: 파싱을 소켓에서 떼어내 **입력 bytes → 결과**인 함수로 만들고, 모든 경계를 유닛 테스트로 증명한다.
> 선행: [C04](04-minimal-http.md) · 다음: [C06](06-routing.md) · 검사: `python3 workbook/check.py c05`

## 먼저 공부할 것

- RFC 9112 §3 Request Line, §5 Field Syntax (§5.1–5.2), RFC 9110 §5.6.2 Tokens
- [계약](../00-contract.md)의 "요청 head" 표 — 이번 단계에서 구현할 규칙 전부
- 검색어: `C object lifetime pointer arithmetic undefined behavior`, `HTTP request smuggling parser differential`

답을 적고 시작하세요.

1. 파서를 recv 루프 안에 두지 않고 별도 함수로 빼면 무엇을 테스트할 수 있게 되는가?
2. 파서가 헤더 이름·값을 **복사하지 않고** 입력 버퍼를 가리키는 (포인터, 길이)로 돌려준다면, 그 결과는 언제까지 유효한가?
3. `GET / HTTP/1.1\r\nHost: a\r\n` 까지만 왔다. 이것은 오류인가?

## 만들 것

### API (이 모양 그대로 — 테스트가 이 API로 컴파일됩니다)

`c/src/http_parser.h`:

```c
#ifndef HTTP_PARSER_H
#define HTTP_PARSER_H

#include <stddef.h>

#define HTTP_MAX_HEAD_BYTES 8192
#define HTTP_MAX_HEADERS 100

struct slice {
	const char *ptr; /* 입력 버퍼 안을 가리킴. NUL 종료 아님 */
	size_t len;
};

struct http_header {
	struct slice name;
	struct slice value; /* 앞뒤 공백·탭 제거됨 */
};

struct http_request {
	struct slice method;
	struct slice target; /* "/a?b=1" */
	struct slice path;   /* "/a" — target에서 '?' 앞 */
	int minor_version;   /* HTTP/1.0 → 0, HTTP/1.1 → 1 */
	struct http_header headers[HTTP_MAX_HEADERS];
	size_t header_count;
};

enum parse_result { PARSE_NEED_MORE, PARSE_DONE, PARSE_ERROR };

/*
 * buf[0..len) 의 맨 앞에서 request head 하나를 파싱한다.
 * PARSE_DONE:      *head_len = 빈 줄(\r\n\r\n)까지 포함한 길이. 그 뒤 bytes는 건드리지 않음.
 * PARSE_NEED_MORE: 지금까지는 문제 없고 더 받아야 함.
 * PARSE_ERROR:     *error_status = 400 / 431 / 505.
 */
enum parse_result http_parse_head(const char *buf, size_t len,
				  struct http_request *req, size_t *head_len,
				  int *error_status);

#endif
```

`c/src/http_parser.c`에 구현합니다. Makefile은 `c/src/*.c`를 전부 빌드하므로 따로 고칠 것이 없습니다.

### 규칙

- [ ] 줄은 `\r\n`으로 끝남. `\n` 앞에 `\r`이 없거나 `\r` 뒤에 `\n`이 아닌 것이 오면 400
- [ ] request line: 공백 정확히 2개로 3부분. method는 token 문자, target은 `/`로 시작하고 공백·제어문자(0x00–0x20, 0x7f) 없음
- [ ] version: `HTTP/D.D` 형식이 아니면 400, 형식은 맞는데 1.0/1.1이 아니면 505
- [ ] 헤더: `name:value`, name은 token 1자 이상(콜론 앞 공백 → 400), value의 제어문자(탭 제외) → 400, value 앞뒤 OWS 제거
- [ ] 공백/탭으로 시작하는 헤더 줄(obs-fold) → 400
- [ ] 헤더 101개 → 431, head가 8192바이트를 넘으면 431 (아직 끝이 안 왔어도 8192바이트가 쌓였으면 431)
- [ ] 입력의 일부(prefix)만 주면 오류가 확정되기 전까지 `PARSE_NEED_MORE`
- [ ] 서버(main.c)가 C04의 `\r\n\r\n` 찾기 대신 이 파서를 쓰도록 교체. 400/505도 오류 응답으로 보냄

이번 단계는 head **문법**만 봅니다. Host 필수, Content-Length 같은 **의미** 검사는 C06/C07에서 파서 바깥 계층에 둡니다.

<details><summary>힌트 1 — 상태를 보존하는 증분 파서 vs 매번 처음부터</summary>

진짜 증분 파서는 "어디까지 봤는지"를 상태로 들고 있다가 새 bytes부터 이어서 봅니다. 이 학습지의 API는 **매번 버퍼 처음부터 다시 파싱**하는 단순한 방식입니다. head가 8KiB로 제한되어 있어 비용이 작고, 상태가 없으니 버그도 적습니다. 다만 1바이트씩 8KiB가 오면 몇 바이트를 훑게 되는지 계산해 보세요(이것이 trade-off입니다).
</details>

<details><summary>힌트 2 — 줄 단위로 자르기</summary>

`pos`부터 `\r` 또는 `\n`을 찾는 함수 하나를 만들면 나머지가 쉬워집니다: 못 찾으면 NEED_MORE(또는 한도 초과면 431), `\n`이 먼저 나오면 400, `\r`이 버퍼 마지막 바이트면 NEED_MORE, `\r` 다음이 `\n`이 아니면 400. 이 함수를 검색 범위 `min(len, 8192)`로 부르면 431 판정도 자연스럽게 나옵니다.
</details>

<details><summary>힌트 3 — 테스트가 ASan에서 heap-buffer-overflow를 보고한다면</summary>

테스트는 입력을 **정확히 len 바이트짜리** malloc 버퍼에 복사해서 넘깁니다. `buf[len]`을 한 번이라도 읽으면(`strchr`, `strstr`, `sscanf`, `i <= len` 루프) 바로 잡힙니다. 버퍼 끝에 NUL이 있다고 가정하는 코드가 여기서 드러납니다.
</details>

## 확인하기

### 자동 검사

```sh
python3 workbook/check.py c05     # workbook/tests/test_parser.c + c/src/http_parser.c 를 ASan/UBSan으로 빌드해 실행
python3 workbook/check.py c04     # 파서로 바꾼 서버가 C04 동작을 그대로 유지하는지
```

`test_parser.c`를 읽어 보세요. 어떤 경계를 왜 시험하는지가 이 단계의 학습 내용입니다. 실패하면 `FAIL test_xxx:줄번호: 이유`가 나옵니다.

### 직접 관찰

```sh
printf 'GET / HTTP/2.0\r\nHost: a\r\n\r\n' | nc -N 127.0.0.1 8080     # 505 (nc는 openbsd-netcat)
printf 'GET / HTTP/1.1\nHost: a\n\n'        | nc -N 127.0.0.1 8080     # 400
```

### 일부러 깨뜨려 보기

1. 헤더 이름 비교에 `strncmp(name.ptr, "Host", 4)`를 쓰면 `Hostile: x`도 Host가 됩니다. slice 비교 함수 `slice_eq(s, "Host")`는 무엇을 먼저 확인해야 할까요?
2. 431 판정을 `len > 8192`로 바꾸면 어떤 테스트가 실패하나요? (실제로 실패합니다)

## 알았어야 하는 것

<details><summary>1. 파서를 I/O에서 분리한 이득</summary>

소켓 없이 "모든 바이트 위치에서 잘린 입력"을 결정적으로 테스트할 수 있습니다. 네트워크로는 recv 경계를 통제할 수 없어서 같은 테스트를 할 수 없습니다. 같은 파서를 blocking 서버, epoll 서버(C11)에서 그대로 쓰고, Rust에서도 같은 테스트 목록으로 포팅합니다(R01).
</details>

<details><summary>2. slice의 수명</summary>

slice는 입력 버퍼를 빌려 가리킬 뿐이므로 **그 버퍼가 그대로 있는 동안만** 유효합니다. 버퍼를 realloc하거나 memmove로 앞을 지우면(C08) 모든 slice가 무효입니다. 그래서 응답을 만들 때까지 필요한 정보를 다 쓰고 나서 버퍼를 줄여야 합니다. Rust에서는 이 규칙을 컴파일러가 lifetime으로 강제합니다(R01의 `Request<'a>`).
</details>

<details><summary>3. NEED_MORE와 ERROR의 구분이 왜 중요한가</summary>

잘린 입력을 오류로 처리하면 느린 네트워크의 정상 요청이 400을 받습니다. 반대로 오류를 NEED_MORE로 처리하면 잘못된 입력을 들고 영원히 기다립니다(그래서 한도가 필요). "지금까지 본 bytes만으로 오류가 확정되는가?"가 기준입니다. 예: `GET / HTTP/1.1\n` — `\n`을 본 순간 확정 오류.
</details>

<details><summary>4. 엄격한 거부가 왜 보안인가</summary>

`Host : a`(콜론 앞 공백)나 obs-fold, bare LF를 어떤 서버는 허용하고 앞단 프록시는 거부(또는 다르게 해석)하면, 두 파서가 같은 바이트를 다른 요청으로 봅니다. 이 차이가 request smuggling의 재료입니다. 모르는 형식을 관대하게 받아주지 않는 것이 안전합니다.
</details>

## 기록

`test_parser.c`에서 처음에 실패한 테스트 목록과 각각의 원인.
