# HTTP server from scratch — C → Rust

C로 HTTP 서버와 OS의 접점을 구현하고, Rust로 포팅하면서 async 실행 모델을 학습합니다.

학습은 [`workbook/`](workbook/README.md)의 단계별 학습지를 따라갑니다. 단계마다 공부할 것, 만들 것, `python3 workbook/check.py <단계>` 검증, 자가 점검 질문이 있습니다.

## 시작

Linux, GNU Make, C17 컴파일러, Rust 2024 edition 지원 툴체인을 필요로 합니다.
`rustfmt`, `clippy`, `clang-format`은 코드 검사에 사용합니다.

```sh
make help
make doctor
make build
make run-c
make run-rust
make check
make sanitize
```

## 구조

```text
c/src/       C 구현 (현재 main 골격)
rust/src/    Rust 구현 (현재 main 골격)
workbook/    단계별 학습지 + 검증기(check.py) + 파서 유닛 테스트
docs/        개인 학습 문서로 가는 symlink
Makefile     공통 개발 명령
```
