# C09 — chunked request body

> 목표: 길이를 미리 모르는 body의 framing을 디코딩하고, 디코딩 과정의 모든 숫자·크기에 한도를 둔다.
> 선행: [C08](08-keep-alive.md) · 다음: [C10](10-threads.md) · 검사: `python3 workbook/check.py c09`

## 먼저 공부할 것

- RFC 9112 §7.1 Chunked Transfer Coding (§7.1.1 extensions, §7.1.2 trailer, §7.1.3 디코딩 의사 코드)
- 검색어: `HTTP chunked transfer coding chunk extensions trailers`

답을 적고 시작하세요.

1. `5\r\nhello\r\n0\r\n\r\n`의 각 부분은 무엇인가? body의 끝은 어디인가?
2. chunk-size가 16진수 20자리라면 무엇이 문제인가?
3. trailer에 `Content-Length: 0`이나 `Host: evil`이 오면 반영해야 하나?

## 만들 것

[계약](../00-contract.md)의 chunked 표.

- [ ] `Transfer-Encoding: chunked`(정확히 그 값, 대소문자 무시) → chunked 디코딩. 다른 값은 여전히 501
- [ ] chunk-size: 16진수 1–8자리 → 이상하면 400, 9자리 이상 413. `;` 뒤 extension 무시, size 줄 1024바이트 이하
- [ ] chunk data 뒤 `\r\n` 확인 (없으면 400)
- [ ] decoded 합계 1MiB 초과 → 413
- [ ] size 0 chunk 뒤 trailer 줄들을 빈 줄까지 읽고 버림
- [ ] `end`(chunked body가 끝난 위치)를 consumed로 돌려줘서 뒤에 붙은 요청을 처리
- [ ] 모든 prefix에서 NEED_MORE (C05처럼 매번 처음부터 다시 디코딩해도 됩니다)

<details><summary>힌트 — 디코더 시그니처</summary>

```c
/* in[pos..len)에서 chunked body를 디코딩해 body에 붙인다.
 * 완성되면 *end = body 다음 바이트 위치. */
enum { CH_NEED_MORE, CH_DONE, CH_ERROR }
decode_chunked(const char *in, size_t len, size_t pos, struct buf *body,
	       size_t *end, int *status);
```
루프 한 번 = chunk 하나: size 줄 → (0이면 trailer → 끝) → data → CRLF. 각 단계에서 "bytes가 모자라면 NEED_MORE"를 먼저 확인하고, 모자라지 않은데 형식이 틀리면 ERROR.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c09
make sanitize STEP=c09
```

### 직접 관찰

```sh
curl -s -H 'Transfer-Encoding: chunked' --data-binary @/tmp/rand.bin http://127.0.0.1:8080/echo | cmp - /tmp/rand.bin && echo same
printf 'POST /echo HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5;x=1\r\nhello\r\n0\r\nX-T: 1\r\n\r\n' | nc -N 127.0.0.1 8080
```

curl에 `Transfer-Encoding: chunked`를 주면 curl이 직접 chunk로 나눠 보냅니다. `--trace-ascii -`로 실제 chunk 경계를 보세요.

### 일부러 깨뜨려 보기

1. chunk-size를 `unsigned long`에 무한정 누적하면 `ffffffffffffffff1`이 몇이 되나요? 그 크기를 그대로 믿고 `body.len + size`를 계산하면?
2. trailer를 헤더 목록에 합쳐서 Host 검사에 반영하도록 바꾸면 어떤 공격이 가능한지 적어 보세요.

## 알았어야 하는 것

<details><summary>1. chunked가 존재하는 이유</summary>

생성하면서 보내는 body(스트리밍)는 시작할 때 길이를 모릅니다. chunked는 "다음 N바이트" 단위로 보내다가 0으로 끝을 알려서, 연결을 닫지 않고도 메시지 끝을 표시합니다. 요청에서는 드물지만 프록시·업로드 클라이언트가 씁니다.
</details>

<details><summary>2. 한도가 여러 겹 필요한 이유</summary>

chunk-size 자릿수(overflow), size 줄 길이(extension으로 무한 줄), decoded 합계(메모리), trailer 크기 — 각각이 따로 무한히 커질 수 있는 입력입니다. 하나라도 빠지면 그 축으로 메모리나 CPU를 고갈시킬 수 있습니다. "숫자 하나 + 길이 하나마다 한도"가 규칙입니다.
</details>

<details><summary>3. trailer를 버리는 이유</summary>

trailer는 body 뒤에 오므로, 라우팅·인증이 이미 끝난 뒤에 도착합니다. 이를 헤더에 합치면 "검사할 때 본 헤더"와 "나중에 쓰는 헤더"가 달라집니다. 프록시마다 trailer 처리 방식도 달라 smuggling 재료가 됩니다. 쓸 일이 없으면 문법만 검사하고 버립니다.
</details>

## 기록

> 여기서 `git tag c09-blocking`. 이 커밋이 B01의 "C blocking" 기준선입니다.
