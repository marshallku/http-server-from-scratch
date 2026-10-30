# R02 — Rust 동기 서버 (std::net + thread)

> 목표: C10의 thread-per-connection 서버를 옮기며, C에서 손으로 하던 자원 정리가 Rust의 어디로 갔는지 대응표를 완성한다.
> 선행: [R01](01-parser.md) · 다음: [R03](03-future.md) · 검사: `python3 workbook/check.py r02`

## 먼저 공부할 것

- `std::net::TcpListener`, `TcpStream`, `std::io::{Read, Write}`의 `read`/`write`/`write_all` 문서 — 특히 `write_all`이 `Interrupted`를 어떻게 처리하는지
- `std::io::ErrorKind`
- 검색어: `Rust OwnedFd AsFd Drop RAII`, `Rust ignores SIGPIPE`, `Rust TcpListener bind SO_REUSEADDR`

답을 적고 시작하세요.

1. C03에서 fd 누수를 막으려고 모든 경로에 `close`를 넣었다. Rust에서 `TcpStream`이 닫히는 시점은?
2. C02의 `send_all`과 EINTR 재시도 루프에 해당하는 Rust 표준 라이브러리 함수는?
3. C03의 SIGPIPE 문제와 C01의 SO_REUSEADDR 문제는 Rust에서 어떻게 되나? (추측 후 소스/문서로 확인)

## 만들 것

`rust/src/bin/sync_server.rs` (→ `rust/target/debug/sync_server`). `src/main.rs`는 R06에서 Tokio 서버가 됩니다.

- [ ] `TcpListener::bind((Ipv4Addr::LOCALHOST, port))`, `PORT` 환경변수
- [ ] `incoming()` 루프, 연결마다 `thread::spawn(move || serve(stream))`
- [ ] `serve`: C08 연결 루프와 같은 구조. `handle_input` → `write_all` → `input.drain(..consumed)` → 부족하면 `read`
- [ ] `serve`는 `io::Result<()>`를 반환하고 `?`로 오류 전파. 연결 하나의 오류는 로그만 남기고 서버는 계속
- [ ] accept 오류도 서버를 끝내지 않음
- [ ] Makefile `check-rust`의 `cargo run`을 `cargo test` + `python3 workbook/check.py r02`로 교체

<details><summary>힌트 — 오류는 어디서 처리하나</summary>

`serve` 안에서는 `?`로 위로 올리고, `thread::spawn`의 클로저에서 `if let Err(e) = serve(stream) { eprintln!(...) }`. C에서 오류마다 `goto out`하던 정리 코드는 필요 없습니다 — `stream`, `input`, `out`이 스코프를 벗어나며 Drop됩니다.
</details>

## 확인하기

```sh
cargo build --manifest-path rust/Cargo.toml
python3 workbook/check.py r02           # C04~C10 검사를 Rust 바이너리에
```

C 서버와 **같은 검사**를 통과한다는 것이 "동작 동등성"의 증거입니다.

### 직접 관찰 — C와 syscall 비교

```sh
strace -f -e trace=network,close ./rust/target/debug/sync_server 2>&1 | head -30
```

- `setsockopt(3, SOL_SOCKET, SO_REUSEADDR, [1], 4)`가 보이나요? std가 해줍니다.
- `accept4(..., SOCK_CLOEXEC)`: std는 모든 fd에 CLOEXEC를 붙입니다. C에서는? (`man 2 accept4`)
- `sendto(..., MSG_NOSIGNAL)`: std의 send는 Linux에서 MSG_NOSIGNAL을 씁니다. 그와 별개로 Rust 런타임은 main 전에 SIGPIPE를 무시하도록 설정합니다.
- 연결 종료 시 `close`가 정확히 한 번 — 누가 불렀나요?

### 일부러 깨뜨려 보기

1. `serve`의 오류를 `.unwrap()`으로 바꾸고 RST로 끊는 클라이언트를 보내면? 스레드 하나의 panic이 프로세스를 죽이나요?
2. `stream`을 `std::mem::forget`하면 C03의 fd 누수를 재현할 수 있습니다. 검사의 fd 누수 항목으로 확인.

## 알았어야 하는 것

<details><summary>1. RAII 대응</summary>

| C | Rust |
|---|---|
| `close(fd)` 모든 경로 | `TcpStream`의 `Drop` (내부적으로 `OwnedFd`) |
| `free(buf.data)` | `Vec`의 `Drop` |
| `send_all` + EINTR 루프 | `write_all` (Interrupted 재시도 포함) |
| `recv == 0` | `read` → `Ok(0)` |
| `errno == ECONNRESET` | `ErrorKind::ConnectionReset` |
| `(void *)(intptr_t)fd` + 지역 변수 race | `move` 클로저가 소유권 이전 |
</details>

<details><summary>2. Rust가 보장하지 않는 것</summary>

메모리 안전(범위 초과, use-after-free, data race)은 보장되지만 **프로토콜 정확성과 자원 한도**는 아닙니다. 8192 한도, 1MiB 한도, CL 중복 거부, 연결 수 제한은 여전히 직접 구현해야 합니다. `Vec`은 무한히 커질 수 있고, 스레드는 무한히 만들 수 있습니다(C10과 같은 문제).
</details>

<details><summary>3. 스레드의 panic</summary>

기본적으로 panic은 그 스레드만 unwind하고 끝냅니다(`panic = "abort"` 설정이 아니라면). `JoinHandle::join`이 `Err`를 받습니다. detach된 스레드라면 메시지만 남고 서버는 계속 돕니다. 그래도 네트워크 입력으로 panic을 일으킬 수 있으면 DoS 재료이므로 `unwrap`을 입력 경로에 두지 않습니다.
</details>

## 기록

위 RAII 대응표를 `docs/CHECKPOINTS.md`의 "C → Rust 대응표"에 실제 코드 위치와 함께 채웁니다.
