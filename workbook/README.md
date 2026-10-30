# HTTP 서버 from scratch — 단계별 학습지

C로 소켓부터 epoll 이벤트 루프까지 직접 만들고, 같은 서버를 Rust로 옮기며 Future → executor → reactor → Tokio 순서로 async를 분해하는 학습지입니다.
`docs/ROADMAP.md`의 M1–M8을 **한 번에 하나만 새로 배우는 크기**로 쪼갰습니다.

각 단계는 같은 구조입니다.

| 섹션 | 하는 일 |
|---|---|
| **먼저 공부할 것** | 코드를 쓰기 전에 읽을 것과 답할 질문. 질문에 답하지 못하면 코드를 쓰지 않습니다. |
| **만들 것** | 이번 단계의 명세. 이것만 만들고 다음 단계 기능은 미리 넣지 않습니다. |
| **힌트** | 막혔을 때만 펼칩니다. 설계 방향과 함정만 있고 정답 코드는 없습니다. |
| **확인하기** | `check.py` 자동 검사 + 직접 관찰할 명령 + 일부러 깨뜨려 볼 실험. |
| **알았어야 하는 것** | 자가 점검 질문. 답은 접혀 있습니다. 소리 내어 설명할 수 있어야 완료입니다. |

> 모든 단계의 검사는 실제 참조 구현으로 통과/실패를 확인했습니다. 단, 참조 구현은 이 저장소에 포함하지 않습니다(직접 만드는 게 목적이므로).

## 진행 순서

### 0. 준비

| 단계 | 주제 | 새로 배우는 것 | 검사 |
|---|---|---|---|
| [C00](c/00-prep.md) | 준비와 사전 지식 | 도구 설치, C 포인터·길이·용량, errno, 정리(cleanup) 패턴 | — |
| [계약](00-contract.md) | 이 서버의 프로토콜 계약 | 지원 범위와 오류 정책. C와 Rust가 똑같이 지킨다 | — |

### 1부. C — blocking 서버 (M1–M3)

| 단계 | 주제 | 새로 배우는 것 | 검사 |
|---|---|---|---|
| [C01](c/01-listen-accept.md) | 연결 하나 받기 | socket/bind/listen/accept, fd, byte order, TIME_WAIT | `c01` |
| [C02](c/02-echo.md) | TCP echo | 바이트 스트림, recv 0 = EOF, short write, EINTR | `c02` |
| [C03](c/03-serve-forever.md) | 계속 서빙 | accept 루프, fd 누수, SIGPIPE/EPIPE | `c03` |
| [C04](c/04-minimal-http.md) | 최소 HTTP | 메시지 경계 `\r\n\r\n`, 누적 버퍼, 헤더 한도 431 | `c04` |
| [C05](c/05-parser.md) | 파서 분리 | I/O 없는 파서, slice(ptr+len), NEED_MORE, ASan 유닛 테스트 | `c05` |
| [C06](c/06-routing.md) | 라우팅과 상태 코드 | HEAD, 404/405+Allow, 400/505, Host 규칙 | `c06` |
| [C07](c/07-body.md) | Content-Length body | body framing, overflow 없는 숫자 파싱, TE/CL 충돌, 413/417/501 | `c07` |
| [C08](c/08-keep-alive.md) | keep-alive, pipelining | 연결별 잔여 버퍼, 메시지 끝 ≠ 연결 끝 | `c08` |
| [C09](c/09-chunked.md) | chunked body | chunked 디코딩, 한도, trailer | `c09` |

### 2부. C — 동시성 (M4)

| 단계 | 주제 | 새로 배우는 것 | 검사 |
|---|---|---|---|
| [C10](c/10-threads.md) | thread-per-connection | head-of-line blocking 관찰, pthread, 스레드 비용 | `c10` |
| [C11](c/11-epoll-read.md) | epoll 읽기 경로 | nonblocking, readiness, LT, EAGAIN, 연결 상태 머신 | `c11` |
| [C12](c/12-epoll-write.md) | epoll 쓰기 경로 | EPOLLOUT, 부분 송신 offset, 출력 한도와 backpressure | `c12` |
| [C13](c/13-deadlines.md) | 시간·연결 수 제한 | slowloris, idle/write deadline, epoll_wait timeout, admission | `c13` |
| [C14](c/14-shutdown.md) | graceful shutdown | signalfd, listen 중단 → drain → deadline | `c14` |

### 3부. Rust (M5–M7)

| 단계 | 주제 | 새로 배우는 것 | 검사 |
|---|---|---|---|
| [R01](rust/01-parser.md) | 파서 포팅 | `&[u8]` 파서, lifetime, `cargo test` | `r01` |
| [R02](rust/02-sync-server.md) | 동기 서버 | `std::net`, RAII/Drop, `io::ErrorKind` | `r02` |
| [R03](rust/03-future.md) | Future 직접 poll | `Future`/`Poll`/`Context`, lazy, async fn = 상태 머신 | `r03` |
| [R04](rust/04-executor.md) | 작은 executor | `Wake`/`Waker`, ready queue, park, lost wakeup | `r04` |
| [R05](rust/05-reactor.md) | reactor | epoll ↔ waker 연결, edge-triggered, 직접 만든 runtime 위 HTTP | `r05` |
| [R06](rust/06-tokio.md) | Tokio 서버 | spawn, Semaphore, timeout, cancellation safety | `r06` |
| [R07](rust/07-tokio-shutdown.md) | Tokio 종료 | watch 채널, JoinSet, drain과 abort | `r07` |

### 4부. 비교 (M8)

| 단계 | 주제 | 새로 배우는 것 |
|---|---|---|
| [B01](bench.md) | 네 구현 비교 | 측정 조건 고정, latency 분포, syscall/context switch로 설명 |

## 한 단계를 진행하는 방법

1. **먼저 공부할 것**의 질문에 답을 적습니다. 모르면 자료를 읽습니다. (AI에게 물어도 되지만 *코드 말고 개념*만 묻기)
2. **만들 것**을 구현합니다. 막히면 15분은 혼자 버팁니다 → 힌트를 하나씩 펼칩니다.
3. `python3 workbook/check.py <단계>`로 검사합니다. 실패 메시지의 노란 힌트부터 읽습니다.
4. **확인하기**의 관찰 명령(strace, ss, /proc)을 직접 돌려 보고 결과를 눈으로 확인합니다.
5. **알았어야 하는 것**을 답을 보지 않고 설명해 봅니다. 막히면 그게 다음에 읽을 거리입니다.
6. `docs/CHECKPOINTS.md`의 실험 기록 양식으로 3–5줄 남기고 커밋합니다.

> **AI 페어와 함께할 때의 규칙** — 검사가 실패하면 AI에게 코드를 고쳐 달라고 하기 전에 실패 메시지를 *자기 말로* 설명해 봅니다. 그다음 "이 실패의 원인 후보를 개념으로만 알려줘"라고 묻습니다. 코드를 받으면 배운 게 아니라 붙여 넣은 것입니다.

## check.py 사용법

```sh
python3 workbook/check.py --list                 # 단계 목록
python3 workbook/check.py c04                    # ./build/c/http-server로 C04까지(이전 HTTP 단계 포함) 검사
python3 workbook/check.py c08 --only             # C08에서 새로 추가된 검사만
python3 workbook/check.py c11 -- ./build/sanitize/http-server   # 다른 바이너리 (ASan 빌드 등)
python3 workbook/check.py r02                    # Rust 단계는 기본 경로가 rust/target/... 로 바뀜
```

- 서버를 직접 실행하고, `PORT` 환경변수(기본 8080)로 포트를 알려 주고, 끝나면 반드시 죽입니다. 여러분의 서버는 `PORT`를 읽어도 되고 8080 고정이어도 됩니다.
- `cargo run` 같은 래퍼가 아니라 **바이너리 경로**를 넘겨야 `/proc/<PID>/fd` 관측이 정확합니다.
- 검사 전에 8080을 다른 프로세스가 쓰고 있으면 바로 실패합니다: `ss -ltnp 'sport = :8080'`.
- HTTP 단계(C04~)는 누적 검사입니다. C08 검사는 C04·C06·C07 검사를 다시 돌립니다. 새 기능이 예전 기능을 깨뜨리지 않았는지 보는 회귀 테스트입니다.

## 기준선 보존 (B01 비교에 필요)

같은 `c/src/main.c`를 계속 고쳐 나가므로, 동시성 모델이 바뀌기 직전 상태에 tag를 남깁니다.

```sh
git tag c09-blocking     # C09 완료 직후 (단일 스레드 blocking)
git tag c10-threads      # C10 완료 직후 (thread-per-connection)
git tag c14-epoll        # C14 완료
```

Rust는 `src/bin/sync_server.rs`(R02), `examples/r05_server.rs`(R05), `src/main.rs`(R06–R07)로 파일을 나눠서 셋 다 남깁니다.

## 이 학습지의 범위 밖

TLS, HTTP/2·3, 정적 파일 서빙, 프록시, io_uring, 완전한 RFC 9112 준수. 지원 범위는 [계약](00-contract.md)에 적은 것까지입니다.
