# C06 — 라우팅과 상태 코드

> 목표: 파싱된 요청의 **의미**를 검사하고 method·경로별로 다른 응답을 만든다. 오류마다 맞는 상태 코드를 고른다.
> 선행: [C05](05-parser.md) · 다음: [C07](07-body.md) · 검사: `python3 workbook/check.py c06`

## 먼저 공부할 것

- RFC 9110 §9.3.1 GET, §9.3.2 HEAD, §15.5.6 405 (Allow 필수), §7.2 Host
- RFC 9112 §3.2 Request Target (origin-form), §3.2 마지막 부분의 Host 규칙
- 검색어: `RFC 9110 HEAD Content-Length 405 Allow`, `HTTP request target origin-form Host header validation`

답을 적고 시작하세요.

1. 404와 405의 차이는? 405 응답에 반드시 들어가야 하는 헤더는?
2. HEAD 응답의 `Content-Length`는 0이어야 하나, GET과 같아야 하나?
3. HTTP/1.1 요청에 Host가 없거나 두 개면? HTTP/1.0이면?

## 만들 것

[계약](../00-contract.md)의 "라우팅" 표와 Host 행. 아직 body는 다루지 않습니다(`POST /echo`는 C07).

- [ ] `GET /` → 200 Hello. `GET /?a=1`도 `/`로 라우팅 (`path` 사용)
- [ ] `HEAD /` → GET과 똑같은 헤더, body 생략
- [ ] `/`에 그 외 method → 405 + `Allow: GET, HEAD`
- [ ] `/echo`에 POST 외 method → 405 + `Allow: POST` (POST 자체는 C07 전까지 아무렇게나)
- [ ] 그 외 경로 → 404
- [ ] HTTP/1.1에서 Host 0개 / 2개 이상 / 빈 값 → 400. HTTP/1.0은 0개 허용
- [ ] 헤더 이름 비교는 대소문자 무시, method 비교는 대소문자 구분
- [ ] 응답 생성 함수 하나로 모으기: `write_response(out, status, extra_headers, body, body_len, send_body, close)` 같은 모양. 상태 코드 → 이유 문구 표도 여기

<details><summary>힌트 — 계층 나누기</summary>

```
recv 루프 (main.c)
  └─ http_parse_head()          문법: 400 / 431 / 505
       └─ 의미 검사               Host, (C07) Content-Length, Transfer-Encoding
            └─ 라우팅              (path, method) → 응답
```
각 계층이 한 종류의 오류만 결정하게 하면, C07·C09에서 규칙이 늘어도 어디에 넣을지 분명합니다. 이 함수들이 소켓을 직접 만지지 않고 **출력 버퍼에 응답 bytes를 붙이기만** 하면 C11에서 그대로 재사용됩니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c06     # C04 검사 + C06 검사
```

### 직접 관찰

```sh
curl -sI http://127.0.0.1:8080/                       # HEAD: Content-Length: 14, body 없음
curl -si -X DELETE http://127.0.0.1:8080/ | head -5   # 405 + Allow
curl -si http://127.0.0.1:8080/nope | head -1
curl -si -H 'Host:' http://127.0.0.1:8080/ | head -1  # curl이 Host를 빼고 보냄 → 400
printf 'GET / HTTP/1.0\r\n\r\n' | nc -N 127.0.0.1 8080
```

### 일부러 깨뜨려 보기

1. HEAD에 body를 붙여 보내고, `curl -I`와 keep-alive 클라이언트 입장에서 무슨 일이 생길지 생각해 보세요(C08에서 다음 응답의 시작으로 오해됩니다).
2. Host 중복을 허용하고, 앞단 프록시는 첫 번째 Host로·백엔드는 마지막 Host로 라우팅한다면?

## 알았어야 하는 것

<details><summary>1. HEAD의 Content-Length</summary>

HEAD 응답은 "GET이었다면 보냈을 헤더"를 보내므로 Content-Length는 GET과 같은 14입니다. body는 절대 보내지 않습니다. 클라이언트는 HEAD를 보냈다는 걸 기억하고 Content-Length를 무시합니다. 서버가 body를 보내면 지속 연결에서 그 bytes가 다음 응답으로 해석됩니다.
</details>

<details><summary>2. 405와 Allow</summary>

404는 "그런 자원이 없다", 405는 "자원은 있는데 그 method는 안 된다"입니다. 405는 `Allow`로 가능한 method 목록을 반드시 알려야 합니다(RFC 9110 §15.5.6).
</details>

<details><summary>3. 왜 Host가 중요한가</summary>

한 IP에 여러 도메인(virtual host)을 올릴 수 있게 HTTP/1.1이 Host를 필수로 만들었습니다. 0개나 2개 이상이면 어느 사이트로 갈지 모호하고, 서로 다른 계층이 다르게 고르면 캐시 오염·라우팅 우회로 이어집니다. 그래서 RFC가 400을 요구합니다.
</details>

<details><summary>4. 대소문자 규칙이 다른 이유</summary>

헤더 이름은 RFC에서 case-insensitive로 정의되어 있고, method는 case-sensitive token입니다. `get`은 GET의 다른 표기가 아니라 다른(알 수 없는) method이므로 `/`에서는 405입니다.
</details>

## 기록

계층 그림(파서 / 의미 검사 / 라우팅)과 각 계층이 내는 상태 코드.
