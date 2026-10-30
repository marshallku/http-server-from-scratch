# R03 — Future를 손으로 poll 하기

> 목표: `Future`는 "스스로 실행되는 무언가"가 아니라 **누군가 poll 해줘야 한 걸음 나아가는 상태 머신**이라는 것을 출력으로 확인한다.
> 선행: [R02](02-sync-server.md) · 다음: [R04](04-executor.md) · 검사: `python3 workbook/check.py r03`

## 먼저 공부할 것

- [`std::future::Future`](https://doc.rust-lang.org/std/future/trait.Future.html), [`std::task::Poll`](https://doc.rust-lang.org/std/task/enum.Poll.html), [`Context`](https://doc.rust-lang.org/std/task/struct.Context.html), [`Waker::noop`](https://doc.rust-lang.org/std/task/struct.Waker.html#method.noop) (Rust 1.85+)
- Async Book "Under the Hood" — Future trait, async/await 변환
- 검색어: `Rust async fn desugaring state machine Future poll lazy`

답을 적고 시작하세요.

1. `let f = some_async_fn();`만 하고 `.await`하지 않으면 함수 본문은 실행되나?
2. `poll`이 `Pending`을 반환할 때 Future가 지켜야 할 약속은 무엇인가? (`Future::poll` 문서의 "Return value" 절)
3. C11의 `struct conn`과 async fn이 컴파일러에 의해 바뀐 상태 머신은 무엇이 닮았나?

## 만들 것

`rust/examples/r03_countdown.rs` — 외부 crate 없이.

- [ ] `struct Countdown { remaining: u32 }`에 `Future` 구현: `remaining`이 0이면 `Ready("liftoff")`, 아니면 1 줄이고 `cx.waker().wake_by_ref()` 후 `Pending`
- [ ] `main`: `Countdown { remaining: 3 }`을 만들고 **바로** `created` 출력
- [ ] `Waker::noop()`으로 `Context`를 만들고, `Pin::new(&mut fut)`로 고정한 뒤 Ready가 나올 때까지 직접 poll 루프
- [ ] 출력 형식 (검사가 이 문자열을 봅니다):
  ```
  created
  poll 1: Pending
  poll 2: Pending
  poll 3: Pending
  poll 4: Ready("liftoff")
  ```

그 다음 실험 (검사 대상 아님, 기록 대상):

- [ ] `async fn add_slowly(a: u32, b: u32) -> u32 { let x = a; Countdown{remaining:1}.await; x + b }`를 같은 방법으로 poll → `Pending` 몇 번 후 `Ready`?
- [ ] `std::mem::size_of_val(&add_slowly(1, 2))`와 `size_of::<Countdown>()` 출력 — await를 건너 살아남는 지역 변수(`x`)가 Future의 크기에 들어갑니다

<details><summary>힌트 — Pin::new를 쓸 수 있는 이유</summary>

`Countdown`은 자기 자신을 가리키는 참조가 없어서 `Unpin`입니다. `Unpin` 타입은 `Pin::new(&mut x)`로 그냥 고정할 수 있습니다. async fn이 만든 Future는 일반적으로 `!Unpin`이라 `Box::pin(fut)`이나 `std::pin::pin!(fut)`이 필요합니다. 두 번째 실험에서 직접 겪어 보세요.
</details>

## 확인하기

```sh
python3 workbook/check.py r03      # 예제를 빌드·실행해 출력 형식, exit 0, created가 먼저인지 확인
cargo run --manifest-path rust/Cargo.toml --example r03_countdown
```

## 알았어야 하는 것

<details><summary>1. Future는 lazy</summary>

async fn을 호출하면 본문을 실행하지 않고 **초기 상태의 Future 값**만 만듭니다. 누군가 poll해야 첫 await 지점까지 실행됩니다. 그래서 `created`가 어떤 poll 로그보다 먼저 나옵니다. JavaScript의 Promise(만들자마자 실행)와 다른 점입니다.
</details>

<details><summary>2. Pending의 약속</summary>

`Pending`을 반환하는 Future는 **나중에 진행할 수 있게 되면 `cx.waker()`를 wake하도록 준비해 둬야** 합니다. 그렇지 않으면 아무도 다시 poll하지 않아 영원히 멈춥니다. 이 예제는 "바로 다시 깨워줘"(`wake_by_ref`)로 약속을 지키지만, noop waker라서 실제로는 아무 일도 안 일어나고 우리 루프가 무조건 다시 poll합니다 — 이것이 busy polling이고 R04가 고칠 문제입니다.
</details>

<details><summary>3. async fn = 컴파일러가 만든 struct conn</summary>

컴파일러는 async fn을 "await 지점마다 하나의 상태"를 가진 enum으로 바꾸고, await를 건너 살아남는 지역 변수를 그 enum에 저장합니다. C11에서 연결마다 "어디까지 읽었나"를 `struct conn`에 손으로 저장한 것과 같은 일을 자동으로 하는 것입니다. 그래서 Future의 크기는 await를 건너는 지역 변수들의 크기로 결정됩니다(두 번째 실험).
</details>

<details><summary>4. Pin은 무엇을 막나</summary>

async fn의 상태 머신은 자기 필드를 가리키는 참조를 가질 수 있습니다(`let buf = [0; 1024]; read(&mut buf).await`). 이 값이 메모리에서 이동하면 내부 참조가 엉뚱한 곳을 가리킵니다. `Pin<&mut T>`는 "poll이 시작된 뒤에는 이 값을 이동시키지 않겠다"는 약속입니다. 스택 고정이나 수명 연장이 아니라 **이동 금지**입니다.
</details>

## 기록

두 번째 실험의 `size_of_val` 결과와, await 사이에 `[u8; 1024]` 지역 변수를 하나 더 두었을 때 크기 변화.
