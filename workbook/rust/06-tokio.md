# R06 — Tokio 서버: task, 한도, timeout

> 목표: R05에서 직접 만든 runtime을 Tokio로 바꾸고, C12–C13에서 손으로 만든 backpressure·deadline·연결 수 제한을 Tokio의 도구로 다시 표현한다.
> 선행: [R05](05-reactor.md), [C13](../c/13-deadlines.md) · 다음: [R07](07-tokio-shutdown.md) · 검사: `python3 workbook/check.py r06`

## 먼저 공부할 것

- [Tokio tutorial](https://tokio.rs/tokio/tutorial): Spawning → Shared state → I/O → Select
- `tokio::time::{timeout, timeout_at, Instant}`, `tokio::sync::Semaphore::acquire_owned`
- [`tokio::select!`](https://docs.rs/tokio/latest/tokio/macro.select.html)의 "Cancellation safety" 절과, 쓰는 메서드마다 문서의 "Cancel safety" 항목 (`AsyncReadExt::read`, `AsyncWriteExt::write_all`)
- 검색어: `Tokio select cancellation safety read_exact write_all`, `Tokio cooperative scheduling blocking spawn_blocking`, `Tokio Mutex std Mutex guard across await`

답을 적고 시작하세요.

1. `tokio::spawn`된 task와 OS 스레드의 차이는? 연결 1만 개 = task 1만 개는 괜찮은가?
2. `timeout(5s, stream.write_all(&buf))`이 시간 초과로 끝났다. 이미 보낸 bytes와 못 보낸 bytes는 어떻게 되었나? 그 연결을 계속 써도 되나?
3. async fn 안에서 `std::thread::sleep`이나 무거운 계산을 하면 무슨 일이 생기나?
4. C13의 "연결 최대 256개"를 Tokio로 어떻게 표현할까? accept 전에 제한하는 것과 후에 제한하는 것의 차이는?

## 의존성 추가

```sh
cargo add tokio@1 --features rt-multi-thread,macros,net,io-util,time,sync,signal --manifest-path rust/Cargo.toml
cargo fetch --manifest-path rust/Cargo.toml
git add rust/Cargo.toml rust/Cargo.lock
```

## 만들 것

`rust/src/main.rs` (→ `rust/target/debug/scratch-http`). R01의 `handle_input`을 그대로 씁니다.

- [ ] `#[tokio::main]`, `tokio::net::TcpListener::bind`
- [ ] accept loop: **accept 전에** `Semaphore(256)`의 permit을 `acquire_owned`로 얻고, permit을 task로 move (task가 끝나면 Drop으로 반납)
- [ ] 연결마다 `tokio::spawn(serve(stream))`. 연결 오류는 로그만
- [ ] `serve`: R05와 같은 루프 + deadline
  - 요청의 첫 바이트(또는 응답 후 idle 시작)부터 5초 `Instant`를 잡고, 읽기는 `timeout_at(deadline, stream.read(&mut buf))`
  - 쓰기는 계약대로 "진척 없이 5초": `write_all` 대신 `write`를 반복하며 **한 번의 write마다** `timeout(5s, stream.write(&out[sent..]))`. 시간 초과면 연결을 닫음
  - (비교 실험) `timeout(5s, stream.write_all(&out))`로 바꾸면 전체 송신 시간 제한이 됩니다. 느린 회선으로 큰 응답을 **정상적으로** 받는 클라이언트가 어떻게 되는지, C13의 write deadline과 무엇이 다른지 기록하세요
- [ ] 응답을 다 보낼 때까지 다음 요청을 읽지 않음 → 출력 한도·backpressure가 자연스럽게 생기는지 확인 (C12와 비교)

<details><summary>힌트 1 — `??`</summary>

`timeout(...).await`는 `Result<io::Result<T>, Elapsed>`입니다. `Elapsed`를 `io::Error`로 바꾸는 `From`이 Tokio에 있어서 `timeout(..).await??`로 두 겹을 한 번에 풀 수 있습니다. 시간 초과도 연결 오류로 처리되어 `serve`가 끝나고, stream이 Drop되며 닫힙니다.
</details>

<details><summary>힌트 2 — 왜 accept 전에 permit인가</summary>

accept 후에 permit을 기다리면 이미 fd를 하나 쥔 채 기다리는 것이므로 fd 수가 제한되지 않습니다. accept 전에 기다리면 초과 연결은 커널 backlog에 머물고 fd를 쓰지 않습니다. C13에서 "accept 후 즉시 close" vs "accept를 미룸"을 비교한 것과 같은 선택입니다.
</details>

## 확인하기

```sh
cargo build --manifest-path rust/Cargo.toml
python3 workbook/check.py r06          # C04~C13 전체
```

### 직접 관찰

```sh
./rust/target/debug/scratch-http & PID=$!
grep Threads /proc/$PID/status          # worker 스레드 수 = CPU 코어 수 (+ α)
ls /proc/$PID/fd | wc -l                # epoll fd, eventfd(waker) 등 Tokio가 연 fd
kill $PID
```

### 일부러 깨뜨려 보기

1. `serve` 안에 `std::thread::sleep(Duration::from_secs(2))`를 넣고 `TOKIO_WORKER_THREADS=1`로 실행 → C10 검사(느린 클라이언트 격리)가 실패합니다. `tokio::time::sleep`으로 바꾸면?
2. Semaphore를 빼면 C13의 최대 연결 검사가 실패하는지 확인.
3. `write_all` 대신 `select!`로 `write_all`과 다른 future를 경쟁시켜 `write_all`이 취소되게 만들고, 취소된 뒤 같은 연결에 다음 응답을 보내면 클라이언트는 무엇을 받나요?

## 알았어야 하는 것

<details><summary>1. task는 가볍다, 하지만 공짜는 아니다</summary>

task는 Future 하나(수백 바이트~수 KiB, R03에서 잰 크기)와 스케줄러 메타데이터입니다. OS 스레드 스택(8MiB 예약)과 비교하면 훨씬 작아서 1만 개도 무리가 없습니다. 하지만 각 task의 버퍼(`input`, `out`)는 여전히 메모리이고 상한이 필요합니다 — 그래서 Semaphore.
</details>

<details><summary>2. cancellation safety</summary>

Future를 drop하면 다음 await 지점에서 멈춘 상태 그대로 사라집니다. `read`는 "아직 아무것도 읽지 않았거나, 읽었으면 Ready를 반환했다" 둘 중 하나라 취소해도 데이터를 잃지 않습니다(cancel safe). `write_all`은 일부를 보낸 상태에서 취소될 수 있고 **얼마나 보냈는지 알려주지 않습니다** — 그 연결의 응답 경계가 깨졌으므로 닫아야 합니다. C12의 `out_off`를 직접 관리하던 것과 비교하세요.
</details>

<details><summary>3. cooperative scheduling</summary>

Tokio worker는 task가 `.await`에서 Pending을 반환해야 다른 task로 넘어갑니다. await 없이 오래 도는 코드(blocking syscall, 긴 계산)는 그 worker의 모든 task를 멈춥니다 — C10의 단일 스레드 blocking 서버 문제가 task 단위로 재현되는 것입니다. 해결: `spawn_blocking`(별도 스레드 풀), 계산을 쪼개 `yield_now`.
</details>

<details><summary>4. lock과 await</summary>

`std::sync::Mutex` guard를 쥔 채 `.await`하면, 그 task가 다른 worker로 옮겨지거나 오래 멈추는 동안 lock이 잡혀 있어 다른 task가 막힙니다(guard가 `!Send`라 spawn에서 컴파일 오류가 나는 경우도 많음). 짧은 임계 구역은 std Mutex로 await 없이, await를 건너야 하면 `tokio::sync::Mutex`나 메시지 전달로 설계합니다. R07의 `watch::Receiver::borrow()`에서 같은 문제를 만나게 됩니다.
</details>

## 기록

C12(손으로 만든 backpressure)와 R06(응답을 다 보낼 때까지 안 읽기)의 메모리 상한을 각각 계산식으로.
