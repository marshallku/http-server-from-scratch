# R04 — Waker로 깨우는 작은 executor

> 목표: "준비된 task만 다시 poll하고, 할 일이 없으면 잠든다"를 `std::task::Wake`로 구현한다. busy polling과 lost wakeup을 직접 본다.
> 선행: [R03](03-future.md) · 다음: [R05](05-reactor.md) · 검사: `python3 workbook/check.py r04`

## 먼저 공부할 것

- [`std::task::Wake`](https://doc.rust-lang.org/std/task/trait.Wake.html) (문서 예제의 `block_on`을 먼저 읽고 이해합니다), `Waker::will_wake`
- `std::sync::{Mutex, Condvar}` 또는 `std::sync::mpsc`, `std::thread::park`
- 검색어: `std task Wake Waker Context executor`, `Rust lost wakeup register waker recheck`

답을 적고 시작하세요.

1. executor의 ready queue에 task를 넣는 것은 누구인가? executor 자신? Future? Waker?
2. 타이머 Future가 "아직 안 끝남"을 확인한 **직후**, waker를 저장하기 **직전**에 타이머 스레드가 끝나서 wake하려 했다. 무슨 일이 생기나?
3. ready queue가 비었을 때 executor는 무엇을 해야 하나?

## 만들 것

`rust/examples/r04_executor.rs` — 외부 crate 없이.

- [ ] **Timer Future**: `Timer::after(Duration)`이 스레드를 하나 띄워 `sleep` 후 공유 상태에 `done = true`를 쓰고 저장된 waker를 wake. `poll`은 `done`이면 Ready, 아니면 waker를 저장하고 Pending
- [ ] **Task**: `Mutex<Option<Pin<Box<dyn Future<Output = ()> + Send>>>>` + ready queue에 대한 참조. `impl Wake for Task`의 `wake`는 자기 자신(`Arc<Task>`)을 queue에 넣고 executor를 깨움
- [ ] **Executor** `block_on_all(futures)`: 모든 task를 queue에 넣고, queue에서 꺼내 poll, Ready면 완료 처리. 남은 task가 있는데 queue가 비면 **잠듦**(Condvar wait / channel recv / park)
- [ ] 전역 `AtomicUsize`로 poll 횟수를 셈
- [ ] `main`: 300ms, 100ms, 200ms 타이머를 기다리는 async 블록 3개를 동시에 실행. 각각 끝나면 `timer 100ms done` 형식 출력, 마지막에 `total polls: N`

검사 기준: 출력 순서 100→200→300, 전체 0.3초 이상 0.55초 미만(동시에 기다림), `total polls` ≤ 9, CPU 사용 0.1초 미만.

<details><summary>힌트 1 — Waker는 왜 Send + Sync여야 하나</summary>

`Waker::from(Arc<W>)`는 `W: Wake + Send + Sync`를 요구합니다. 타이머 스레드처럼 **다른 스레드가** wake를 부르기 때문입니다. 그래서 Task 안의 future를 `Mutex`로 감쌌습니다. 단일 스레드 executor에서 `!Send` future를 다루려면 Task에 future 대신 **task id만** 넣고 future는 executor가 들고 있게 합니다(R05에서 이 방식).
</details>

<details><summary>힌트 2 — lost wakeup을 피하는 순서</summary>

`poll` 안에서 "done 확인"과 "waker 저장"을 **같은 lock 안에서** 합니다. 타이머 스레드도 같은 lock 안에서 "done = true"와 "waker 꺼내기"를 합니다. 그러면 두 순서(확인→저장→완료→wake, 또는 완료→확인=Ready) 중 하나만 일어납니다. lock 밖에서 확인하고 저장하면 그 사이에 끝난 타이머의 wake가 사라집니다.
</details>

## 확인하기

```sh
python3 workbook/check.py r04
cargo run --manifest-path rust/Cargo.toml --example r04_executor
```

### 일부러 깨뜨려 보기 (검사가 실패하는 것을 확인)

1. Pending이면 waker를 기다리지 않고 task를 바로 queue 뒤에 다시 넣기(busy polling) → `total polls`가 수만~수백만, CPU 검사 실패.
2. 힌트 2의 lock 범위를 나눠서 확인과 저장 사이에 `thread::sleep(1ms)`를 넣고, 타이머를 0ms로 → 가끔 영원히 멈춥니다. 왜 "가끔"인가요?
3. 타이머를 순서대로 `block_on` 하나씩 → 출력 순서가 300→100→200, 시간 0.6초.

## 알았어야 하는 것

<details><summary>1. executor와 waker의 역할 분담</summary>

executor: "준비된 task를 poll한다"만 압니다. 무엇을 기다리는지 모릅니다. Future: 무엇을 기다리는지 알고, 기다릴 대상에게 waker를 맡깁니다. Waker: "이 task를 다시 queue에 넣어 줘"라는 콜백 핸들. 그래서 executor는 타이머든 소켓이든 상관없이 같은 코드로 동작합니다.
</details>

<details><summary>2. 이 executor가 아직 못 하는 것</summary>

타이머마다 OS 스레드를 하나씩 만들어 sleep합니다 — 연결 1만 개면 스레드 1만 개(C10 문제 그대로). I/O도 마찬가지로 스레드가 blocking read를 해야 합니다. "하나의 스레드가 많은 fd/타이머를 대신 기다렸다가 해당 waker를 깨우는 것"이 reactor이고, 다음 단계입니다.
</details>

<details><summary>3. C11 epoll 루프와 무엇이 같은가</summary>

C11: epoll_wait로 잠듦 → 준비된 fd의 conn 상태를 꺼내 진행. R04: Condvar로 잠듦 → 깨워진 task의 Future를 꺼내 poll. "준비된 것만 진행하고 나머지 시간엔 잠든다"는 구조가 같습니다. 다른 점은 C11은 fd가 곧 상태의 키였고, 여기서는 waker가 키입니다.
</details>

## 기록

"일부러 깨뜨려 보기 1"의 total polls와 CPU 시간, 정상 버전과 비교.
