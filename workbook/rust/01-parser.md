# R01 — 파서와 핸들러를 Rust로

> 목표: C05–C09의 파서·핸들러를 `&[u8]` 기반으로 옮기고, C에서 **규칙으로만 지키던** 수명·길이 조건을 타입이 강제하게 만든다.
> 선행: [C14](../c/14-shutdown.md) (최소 C09) · 다음: [R02](02-sync-server.md) · 검사: `python3 workbook/check.py r01`

## 먼저 공부할 것

- The Rust Book 10.3 (lifetime), 슬라이스 패턴(`[first, rest @ ..]`), `u8::is_ascii_*`, `<[u8]>::eq_ignore_ascii_case`, `trim_ascii`
- 검색어: `Rust byte slice incremental parser ownership zero copy`, `Rust lifetime struct holding references`

답을 적고 시작하세요.

1. C의 `struct slice { const char *ptr; size_t len; }`에 대응하는 Rust 타입은? C에서 slice가 무효가 되던 상황(C05 "알았어야 하는 것" 2)을 Rust 컴파일러는 어떻게 막나?
2. HTTP 입력을 `&str`(또는 `String`)로 바꿔서 파싱하면 안 되는 이유는?
3. C의 `PARSE_NEED_MORE / DONE / ERROR + error_status 출력 인자`를 Rust 타입으로 표현하면?

## 만들 것

```
rust/src/
├── lib.rs        pub mod parser; pub mod handler;
├── parser.rs     C05 계약
├── handler.rs    C06–C09 계약 (의미 검사, 라우팅, body/chunked)
└── main.rs       (아직 그대로)
```

`lib.rs`를 두면 `src/bin/*.rs`, `examples/*.rs`가 모두 `scratch_http::handler::...`로 같은 코드를 씁니다(R02, R05, R06).

권장 API (형태는 바꿔도 됩니다 — 검사는 `cargo test`만 봅니다):

```rust
// parser.rs
pub struct Request<'a> {
    pub method: &'a [u8],
    pub target: &'a [u8],
    pub path: &'a [u8],
    pub minor_version: u8,
    pub headers: Vec<(&'a [u8], &'a [u8])>,
}
pub enum Parse<'a> { NeedMore, Done { request: Request<'a>, head_len: usize } }
pub struct ParseError(pub u16);       // 응답할 status
pub fn parse_head(buf: &[u8]) -> Result<Parse<'_>, ParseError>;

// handler.rs
pub enum Handled { NeedMore, Response { consumed: usize, close: bool } }
pub fn handle_input(input: &[u8], out: &mut Vec<u8>, force_close: bool) -> Handled;
```

- [ ] `workbook/tests/test_parser.c`의 테스트를 `#[cfg(test)] mod tests`로 옮김 — 최소: 정상 GET, 모든 prefix NEED_MORE, 문법 오류 표, 8192 경계(N-1, N, N+1), 헤더 100/101개
- [ ] handler 테스트: pipelined 요청에서 `consumed`가 정확히 첫 요청 길이, chunked를 모든 prefix에서 NeedMore, framing 오류 표(CL 중복, TE+CL, overflow, TE gzip, Host 누락)
- [ ] `unsafe` 없음, `unwrap()`은 논리적으로 불가능한 경우에만 (`expect("이유")`)
- [ ] `cargo clippy --all-targets -- -D warnings` 통과

<details><summary>힌트 1 — 줄 찾기를 슬라이스로</summary>

```rust
let Some(i) = rest.iter().position(|&c| c == b'\r' || c == b'\n') else {
    return Ok(Parse::NeedMore);   // (+ 8192 한도 확인)
};
```
`let ... else`는 "못 찾으면 일찍 반환"을 깔끔하게 씁니다. C의 인덱스 산술(`pos`, `cr - pos`)이 `&rest[..i]`, `&rest[i + 2..]`로 바뀌고, 범위를 넘으면 UB 대신 panic이 납니다.
</details>

<details><summary>힌트 2 — 헤더 찾기</summary>

```rust
impl<'a> Request<'a> {
    pub fn headers_named<'s>(&'s self, name: &'s str) -> impl Iterator<Item = &'a [u8]> + 's { ... }
}
```
반환하는 값의 수명은 `'a`(입력 버퍼)이고, 이터레이터 자체는 `&self`를 빌립니다. 수명 둘을 구분하지 않으면 컴파일 오류가 납니다 — 그 오류 메시지를 읽는 것도 이 단계의 학습입니다.
</details>

<details><summary>힌트 3 — overflow</summary>

C07과 같은 알고리즘(자리마다 한도 확인)을 쓰거나 `checked_mul`/`checked_add`를 씁니다. debug 빌드에서 Rust의 정수 overflow는 panic, release에서는 wrap-around라는 것을 기억하세요 — "Rust라서 안전"이 아니라 **둘 다 틀린 동작**입니다.
</details>

## 확인하기

```sh
python3 workbook/check.py r01          # cargo test
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

`Makefile`의 `check-rust`에 있는 `cargo run`은 R02에서 서버가 생기면 멈추게 됩니다. 지금 `cargo test`로 바꿔 둡니다.

## 알았어야 하는 것

<details><summary>1. lifetime이 대신 해주는 일</summary>

`Request<'a>`는 "입력 버퍼 `'a`보다 오래 살 수 없다"는 뜻입니다. 서버 코드에서 `let req = parse_head(&input)?; input.drain(..n); use(req.path);`처럼 쓰면 `input`을 빌린 동안 수정(drain)할 수 없다는 컴파일 오류가 납니다. C에서는 이게 ASan의 use-after-free로 **실행해서 운 좋게 걸려야** 발견됐습니다.
</details>

<details><summary>2. 왜 &str이 아닌가</summary>

`&str`은 UTF-8이 보장된 bytes입니다. HTTP 입력은 임의의 octet이고(헤더 값에 0x80–0xFF 허용, body는 binary), 변환하면 유효하지 않은 입력에서 실패하거나(`from_utf8`) 데이터가 바뀝니다(`from_utf8_lossy`). 파싱은 bytes로 하고, 사람에게 보여줄 때만 문자열로 바꿉니다.
</details>

<details><summary>3. Result와 enum으로 바뀐 것</summary>

C의 출력 인자(`head_len`, `error_status`)는 "결과가 DONE일 때만 의미 있음" 같은 규칙을 주석으로만 표현했습니다. `Result<Parse, ParseError>`에서는 `Done`일 때만 `head_len`이 존재하고, 오류일 때만 status가 존재합니다. 잘못된 조합을 **만들 수조차 없습니다.**
</details>

## 기록

`docs/CHECKPOINTS.md`의 C → Rust 대응표 1행(malloc/free, pointer+length → Vec/slice, borrow)을 실제 코드 위치로 채웁니다.
