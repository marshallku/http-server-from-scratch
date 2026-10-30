# 프로토콜 계약

C와 Rust의 모든 구현이 지킬 동작입니다. `check.py`는 이 표를 검사합니다.
RFC 9112/9110을 전부 구현하지 않습니다. 대신 **지원하지 않는 것은 묵살하지 않고 명시적으로 거부**합니다. 서로 다른 파서가 메시지 경계를 다르게 해석하면 request smuggling이 되기 때문입니다.

각 행에 "도입" 단계가 있습니다. 그 단계 전에는 지키지 않아도 됩니다.

## 연결

| 항목 | 값 | 도입 |
|---|---|---|
| 주소 | `127.0.0.1`, 포트는 `PORT` 환경변수 또는 8080 | C01 |
| 응답 framing | 모든 응답에 `Content-Length`. chunked 응답은 보내지 않음 | C04 |
| 오류 응답 (400/413/417/431/501/505) | `Connection: close`를 붙이고, 보낸 뒤 연결을 닫음. 뒤에 붙은 bytes는 처리하지 않음 | C04 |
| 지속 연결 | HTTP/1.1은 기본 keep-alive. `Connection` 헤더 토큰에 `close`가 있으면 응답에 `Connection: close`를 붙이고 닫음. HTTP/1.0은 항상 닫음 | C08 |
| pipelining | 한 연결의 요청을 도착 순서대로 처리하고 같은 순서로 응답 | C08 |
| 404/405 | framing은 정상이므로 연결을 유지(keep-alive 단계 이후) | C08 |

## 요청 head

| 항목 | 규칙 | 오류 | 도입 |
|---|---|---|---|
| head 크기 | request line + 헤더 + 마지막 빈 줄까지 **8192바이트 이하**. 8192바이트가 쌓였는데 끝이 없으면 더 기다리지 않음 | 431 | C04 |
| 헤더 개수 | 100개 이하 | 431 | C05 |
| 줄바꿈 | `\r\n`만. bare `\n` 또는 `\n` 없는 `\r` | 400 | C05 |
| request line | `METHOD SP TARGET SP VERSION`. 공백은 정확히 한 칸 | 400 | C05 |
| method | token 문자(`A-Za-z0-9!#$%&'*+-.^_\`\|~`). **대소문자 구분** (`get`은 GET이 아님) | 400 | C05 |
| target | origin-form만: `/`로 시작, 제어문자·공백 없음. `?` 앞이 경로 | 400 | C05 |
| version | `HTTP/1.0`, `HTTP/1.1`. 형식은 맞지만 다른 버전 (`HTTP/2.0`) → 505, 형식이 틀리면 400 | 400/505 | C05 |
| 헤더 줄 | `name:value`. name은 token, 콜론 앞 공백 금지, value 앞뒤 공백·탭(OWS) 제거, 제어문자 금지, 빈 value 허용 | 400 | C05 |
| obs-fold | 공백/탭으로 시작하는 줄(이전 헤더의 연속) | 400 | C05 |
| 헤더 이름 비교 | 대소문자 무시 (`hOsT` = `Host`) | — | C06 |
| Host | HTTP/1.1: 정확히 1개, 빈 값 금지. HTTP/1.0: 없어도 됨(2개 이상은 400) | 400 | C06 |

## 라우팅

| 요청 | 응답 | 도입 |
|---|---|---|
| `GET /` | 200, `Content-Type: text/plain`, body `Hello, World!\n` (14바이트) | C04 (C04에서는 모든 요청에 이 응답) |
| `HEAD /` | GET과 같은 헤더(`Content-Length: 14`), body 없음 | C06 |
| `/`에 다른 method | 405, `Allow: GET, HEAD` | C06 |
| `POST /echo` | 200, `Content-Type: application/octet-stream`, body = 요청 body 그대로 | C07 |
| `/echo`에 다른 method | 405, `Allow: POST` | C06 |
| 그 외 경로 | 404 | C06 |

## 요청 body

| 항목 | 규칙 | 오류 | 도입 |
|---|---|---|---|
| Content-Length 값 | 숫자만, 1자 이상 (`+5`, `-1`, `1 2`, 빈 값 모두 거부) | 400 | C07 |
| Content-Length 중복 | 값이 같아도 거부 (이 프로젝트의 엄격한 정책) | 400 | C07 |
| body 크기 | 1 MiB(1048576) 이하. overflow 나는 큰 숫자도 413 | 413 | C07 |
| body가 덜 온 채 EOF | 200으로 처리하지 않음. 연결을 닫음 | — | C07 |
| GET/HEAD에 body | 읽어서 버림 (다음 요청과 섞이지 않도록) | — | C07 |
| `Transfer-Encoding` + `Content-Length` | 함께 오면 거부 | 400 | C07 |
| `Transfer-Encoding` 헤더 2개 이상 | 거부 | 400 | C07 |
| `Transfer-Encoding` 값 | C09 전: 모두 501. C09 후: 정확히 `chunked`(대소문자 무시)만 지원, 나머지(`gzip`, `gzip, chunked`) 501 | 501 | C07 |
| `Expect` 헤더 | 100-continue를 구현하지 않으므로 모두 거부 | 417 | C07 |

## chunked (C09)

| 항목 | 규칙 | 오류 |
|---|---|---|
| chunk-size | 16진수(대소문자) 1–8자리. 숫자 없음/16진수 아님 → 400, 9자리 이상 → 413(또는 400) | 400/413 |
| chunk extension | `;` 뒤는 무시. chunk-size 줄 전체 1024바이트 이하 | 400 |
| chunk data 뒤 | 반드시 `\r\n` | 400 |
| 합계 | decoded body 1 MiB 이하 | 413 |
| trailer | `0\r\n` 뒤 헤더 줄들을 빈 줄까지 읽고 버림 (라우팅/검증에 쓰지 않음). 8192바이트 이하 | 400/431 |

## 동시성과 자원 (C10~)

| 항목 | 규칙 | 도입 |
|---|---|---|
| 격리 | 느린 클라이언트(반쪽 헤더, 느린 body, 놀고 있는 keep-alive)가 있어도 다른 요청은 1초 안에 응답 | C10 |
| idle CPU | 할 일이 없으면 CPU를 쓰지 않음 (1초에 0.1초 미만) | C11 |
| 출력 한도 | 연결당 대기 출력이 한도(권장 64 KiB)를 넘으면 그 연결에서 읽기 중단 → 안 읽는 클라이언트가 서버 메모리를 무한히 늘리지 못함 | C12 |
| request deadline | 요청 첫 바이트부터 5초 안에 요청이 완성되지 않으면 연결을 닫음 (408을 보내도 되고 그냥 닫아도 됨) | C13 |
| idle deadline | 응답 후 다음 요청 없이 5초 → 닫음 | C13 |
| write deadline | 보낼 데이터가 있는데 5초 동안 전혀 진척이 없으면 → 닫음 | C13 |
| 최대 연결 | 동시 256개. 넘치면 accept 후 바로 닫거나 accept를 미룸(backlog) | C13 |
| 종료 | SIGTERM → listen socket을 닫아 새 연결 거부 → 놀고 있는 연결 즉시 닫기 → 진행 중 요청은 완료(응답에 `Connection: close`) → 최대 5초 안에 exit 0 | C14 |

## 상태 코드 이유 문구

`200 OK`, `400 Bad Request`, `404 Not Found`, `405 Method Not Allowed`, `408 Request Timeout`, `413 Content Too Large`, `417 Expectation Failed`, `431 Request Header Fields Too Large`, `501 Not Implemented`, `505 HTTP Version Not Supported`.
검사는 이유 문구를 보지 않지만 맞춰 두면 curl 출력이 읽기 쉽습니다.
