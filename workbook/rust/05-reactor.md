# R05 — reactor: epoll을 waker에 연결하기

> 목표: R04의 executor에 **epoll 기반 reactor**를 붙여, 스레드 하나로 HTTP 연결 여러 개를 async/await로 처리한다. C11의 이벤트 루프와 같은 일을 하지만 연결 상태는 컴파일러가 만든 Future가 들고 있다.
> 선행: [R04](04-executor.md), [C11](../c/11-epoll-read.md) · 다음: [R06](06-tokio.md) · 검사: `python3 workbook/check.py r05`

## 먼저 공부할 것

- `man 7 epoll`을 다시 — 이번엔 **edge-triggered** 절과 "Possible pitfalls"
- [`libc` crate](https://docs.rs/libc)의 `epoll_create1`, `epoll_ctl`, `epoll_wait`, `epoll_event`
- `std::net::TcpStream::set_nonblocking`, `std::os::fd::AsRawFd`, `thread_local!`, `RefCell`
- 검색어: `reactor executor separation mio`, `edge triggered epoll async runtime register once`

답을 적고 시작하세요.

1. reactor와 executor는 각각 무엇을 기다리나? 누가 누구를 깨우나?
2. `read`가 `WouldBlock`을 반환한 뒤 waker를 등록하기 전에 데이터가 도착하면, ET 모드에서 이벤트를 잃나?
3. fd를 epoll에서 지우기 전에 close하면 무엇이 잘못될 수 있나?

## 의존성 추가

`Makefile`의 Cargo 명령은 `--locked --offline`입니다. 의존성은 한 번 온라인으로 받고 lockfile을 커밋합니다.

```sh
cargo add libc@0.2 --manifest-path rust/Cargo.toml
cargo fetch --manifest-path rust/Cargo.toml
git add rust/Cargo.toml rust/Cargo.lock
```

## 만들 것

`rust/examples/r05_server.rs` (→ `rust/target/debug/examples/r05_server`). 단일 스레드.

- [ ] **Reactor** (`thread_local!` + `RefCell`): epoll fd, `HashMap<RawFd, {read: Option<Waker>, write: Option<Waker>}>`
  - `register(fd)`: `EPOLLIN | EPOLLOUT | EPOLLRDHUP | EPOLLET`로 **한 번** 등록
  - `deregister(fd)`: `EPOLL_CTL_DEL` + map에서 제거
  - `wait()`: `epoll_wait(-1)` 후 이벤트의 방향에 맞는 waker를 `take()`해서 wake. ERR/HUP이면 둘 다
- [ ] **Readiness Future**: 첫 poll에서 reactor에 waker를 맡기고 Pending, 깨워진 뒤 poll에서 Ready
- [ ] **`Async<T: AsRawFd>`** 래퍼: 생성 시 register, Drop 시 deregister(그 뒤 내부 `T`가 Drop되며 close)
  - `accept()`, `read(&mut [u8])`, `write_all(&[u8])`: 실제 syscall을 먼저 시도 → `WouldBlock`이면 `readiness(...).await` 후 재시도
- [ ] **Executor**: R04 구조 + `spawn`(실행 중 task가 새 task를 추가) + queue가 비면 Condvar 대신 `reactor.wait()`로 잠듦
  - Waker에는 task id만(`Send + Sync`), future는 executor의 `HashMap<usize, Pin<Box<dyn Future>>>`에
  - poll하는 동안 future를 map에서 **꺼내 두기** — poll 안에서 spawn이 executor를 빌리면 `RefCell` 이중 대여 panic
- [ ] **서버**: accept loop task가 연결마다 `spawn(serve(stream))`. `serve`는 R02의 연결 루프에 `.await`만 붙인 모양

<details><summary>힌트 1 — 왜 edge-triggered + 한 번 등록인가</summary>

LT로 등록해 두면, 소켓이 쓰기 가능하지만 task는 읽기를 기다리는 동안 epoll_wait가 계속 EPOLLOUT으로 깨어납니다(C11의 busy loop). 관심 방향이 바뀔 때마다 `EPOLL_CTL_MOD`를 하면 syscall이 늘어납니다. ET로 양방향을 한 번 등록하고, "WouldBlock을 본 뒤에만 기다린다"는 규칙을 지키면 둘 다 피합니다. 이 규칙이 ET의 drain 요구사항을 자연스럽게 만족시킵니다: 기다리기 전에 항상 WouldBlock까지 시도했으니까요. mio와 Tokio가 이 방식입니다.
</details>

<details><summary>힌트 2 — 단일 스레드에서 lost wakeup이 안 생기는 이유</summary>

WouldBlock 이후 데이터가 도착하면 커널은 그 edge를 epoll ready list에 올려 둡니다. 우리는 waker를 저장한 **뒤에야** `epoll_wait`를 부르므로(같은 스레드, 순서 보장) 그 이벤트를 받아 저장된 waker를 깨웁니다. 멀티스레드 reactor에서는 등록과 이벤트 처리가 동시에 일어날 수 있어서 R04 힌트 2 같은 동기화가 필요합니다.
</details>

<details><summary>힌트 3 — unsafe 범위</summary>

`unsafe`는 libc 호출 세 줄에만. 각 블록 위에 "왜 안전한가"(포인터가 유효한 배열을 가리킴, 길이가 배열 길이와 같음 등)를 `// SAFETY:` 주석으로 적으세요. 이 습관이 C에서 손으로 지키던 규칙을 명시적으로 만드는 방법입니다.
</details>

## 확인하기

```sh
cargo build --manifest-path rust/Cargo.toml --examples
python3 workbook/check.py r05          # C04~C11 검사: 느린 클라이언트 격리 + idle CPU 포함
```

스레드 하나(`grep Threads /proc/<PID>/status`)로 C10의 동시성 검사를 통과해야 합니다.

### 일부러 깨뜨려 보기

1. `EPOLLET`을 빼고(LT) 실행 → idle CPU 검사가 실패하나요? 왜?
2. `Readiness`가 waker를 저장하지 않고 Pending만 반환 → 첫 요청에서 영원히 멈춤. Future의 약속(R03 "알았어야 하는 것" 2)을 어긴 결과입니다.
3. `Async`의 Drop에서 deregister를 빼면? 같은 fd 번호가 재사용될 때 map에 남은 옛 waker가 엉뚱한 task를 깨웁니다.

## 알았어야 하는 것

<details><summary>1. readiness → wake → poll 경로</summary>

1) task가 `read` → WouldBlock → Readiness Future가 reactor에 waker를 맡기고 Pending. 2) executor는 할 일이 없어 `epoll_wait`에서 잠듦. 3) 데이터 도착 → epoll_wait 반환 → reactor가 그 fd의 read waker를 wake → task id가 ready queue에. 4) executor가 task를 poll → Readiness가 Ready → `read` 재시도 성공. `docs/INDEX.md`의 최종 도달점 "readiness → wake → poll 경로를 설명한다"가 이것입니다.
</details>

<details><summary>2. C11과 비교</summary>

C11: 연결 상태(`struct conn`) + 이벤트마다 상태를 보고 분기하는 핸들러를 손으로. R05: 연결 상태 = `serve` async fn의 Future(컴파일러가 생성), 분기 = `.await` 지점. reactor와 executor는 재사용 가능한 런타임으로 분리됐고, 서버 코드는 R02의 blocking 코드와 거의 같은 모양입니다. 이것이 async/await가 해결하는 문제입니다.
</details>

<details><summary>3. 이 runtime에 없는 것 (Tokio가 해주는 것)</summary>

타이머(C13 deadline), 멀티스레드 work-stealing, task 취소와 JoinHandle, 공정성(한 task가 계속 Ready면 다른 task가 굶음 — Tokio는 coop budget), blocking 작업 격리. R06에서 이것들을 Tokio로 얻습니다.
</details>

## 기록

readiness → wake → poll 경로를 내 코드의 함수 이름으로 다시 쓴 4줄.
