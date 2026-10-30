# R07 — Tokio graceful shutdown

> 목표: C14의 "listen 중단 → 알림 → drain → deadline → 강제 종료"를 task 단위로 구현하고, 각 task가 언제 어떻게 끝나는지 책임을 분명히 한다.
> 선행: [R06](06-tokio.md), [C14](../c/14-shutdown.md) · 다음: [B01](../bench.md) · 검사: `python3 workbook/check.py r07`

## 먼저 공부할 것

- [Tokio: Graceful Shutdown](https://tokio.rs/tokio/topics/shutdown)
- `tokio::signal::unix::signal`, `tokio::sync::watch`(`wait_for`, `borrow`), `tokio::task::JoinSet`(`join_next`, `shutdown`)
- 검색어: `Tokio JoinHandle drop abort JoinSet graceful shutdown`

답을 적고 시작하세요.

1. `JoinHandle`을 drop하면 task가 멈추나? `abort()`는?
2. 종료 신호를 연결 task들에게 어떻게 전달하나? 채널 종류별(mpsc, broadcast, watch) 장단점은?
3. 연결 task가 `read`를 기다리던 중에 종료 신호가 오면, 그 read를 취소해도 안전한가? `write_all` 중이면?

## 만들 것

- [ ] `signal(SignalKind::terminate())`를 accept loop의 `select!`에 추가. 신호가 오면 loop를 빠져나와 listener를 drop (→ 새 연결 거부)
- [ ] `watch::channel(false)`: 신호 후 `true` 전송. 모든 연결 task가 receiver를 가짐
- [ ] `serve`에서 **요청을 기다리며 놀고 있을 때만**(입력 버퍼 비어 있음) `select!`로 read와 `shutdown.wait_for(|s| *s)`를 경쟁 → 신호면 바로 종료
- [ ] 요청 처리 중이면 끝까지 처리하되 `handle_input(.., force_close = 종료 중)` → `Connection: close`
- [ ] 연결 task를 `JoinSet`으로 추적. 평소에는 accept loop의 `select!`에서 `join_next()`로 끝난 task를 회수(안 하면 JoinSet이 계속 커짐)
- [ ] 종료 시 `timeout(3s, 모든 task join)` → 시간 초과면 `JoinSet::shutdown()`(abort + 대기) → `Ok(())` 반환 = exit 0

<details><summary>힌트 1 — borrow() guard</summary>

```rust
match handle_input(&input, &mut out, *shutdown.borrow()) {   // 컴파일 오류
```
`borrow()`가 돌려주는 `Ref` guard는 match 식 전체가 끝날 때까지 살아 있어서, match 안의 `shutdown.wait_for(...)`(가변 대여)와 충돌합니다. guard를 쥔 채 await하면 sender가 막힐 수도 있습니다. `let stopping = *shutdown.borrow();`로 값만 복사하고 guard를 즉시 버리세요. R06 "알았어야 하는 것" 4의 실제 사례입니다.
</details>

<details><summary>힌트 2 — select! 분기 조건</summary>

```rust
let n = tokio::select! {
    r = timeout_at(deadline, stream.read(&mut tmp)) => r??,
    _ = shutdown.wait_for(|stop| *stop), if idle => return Ok(()),
};
```
`, if idle`은 분기를 조건부로 켭니다. 요청이 반쯤 온 상태(idle 아님)에서는 종료 신호에 반응하지 않고 계속 읽습니다. `read`는 cancel safe라서 신호 분기가 이겨도 bytes를 잃지 않습니다.
</details>

## 확인하기

```sh
cargo build --manifest-path rust/Cargo.toml
python3 workbook/check.py r07          # C04~C14 전체
```

### 직접 관찰

```sh
./rust/target/debug/scratch-http & PID=$!
python3 -c 'import socket,time; s=socket.create_connection(("127.0.0.1",8080)); s.sendall(b"POST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: 100\r\n\r\nabc"); time.sleep(10)' &
sleep 0.5; kill -TERM $PID; time wait $PID; echo "exit=$?"     # 약 3초(grace) 후 exit 0
```

> 여기서 Rust 트랙 완료. `docs/CHECKPOINTS.md`의 M5–M7과 C → Rust 대응표를 채우세요.

## 알았어야 하는 것

<details><summary>1. JoinHandle drop vs abort</summary>

`JoinHandle` drop은 task를 **분리(detach)** 할 뿐 멈추지 않습니다. `abort()`는 task에 취소를 요청하고, task는 다음 await 지점에서 Future가 drop되며 끝납니다(await 없이 도는 코드는 abort로도 못 멈춤). `JoinSet`은 drop 시 모든 task를 abort하고, `shutdown()`은 abort 후 끝날 때까지 기다립니다.
</details>

<details><summary>2. 왜 watch인가</summary>

종료 신호는 "현재 상태가 종료 중인가"라는 **하나의 값**입니다. watch는 최신 값만 유지하고 새 receiver도 현재 값을 바로 볼 수 있어서, 신호 후에 생긴 task도 놓치지 않습니다. broadcast는 메시지 큐라 늦게 구독하면 놓치고, mpsc는 수신자가 하나입니다.
</details>

<details><summary>3. 종료 중 취소의 안전성</summary>

idle 상태의 `read` 취소는 안전합니다(cancel safe, 아직 아무 요청도 시작되지 않음). 요청 처리 중이나 `write_all` 중에 취소하면 클라이언트는 잘린 응답을 받으므로, grace 기간 동안은 끝까지 진행시키고 deadline이 지난 뒤에만 abort합니다. C14의 정책과 같습니다.
</details>

## 기록

C14와 R07의 종료 순서를 나란히 적은 표(신호 수신 / listen 중단 / idle 연결 / 진행 중 연결 / deadline / exit).
