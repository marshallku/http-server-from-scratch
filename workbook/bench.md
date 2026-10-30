# B01 — 네 구현 비교

> 목표: C blocking / C threads / C epoll / Rust Tokio(+ Rust sync)를 **같은 조건**에서 측정하고, 차이를 syscall·스케줄링·메모리로 설명한다. 통과 기준은 특정 RPS가 아니라 재현 가능한 설명입니다.
> 선행: C14, R07 · 참고: `docs/ROADMAP.md` M8, `docs/DEVELOPMENT.md` "비교 실험 규칙"

## 먼저 공부할 것

- `wrk` README(옵션 `-t -c -d --latency`), `perf stat` (`sudo pacman -S perf`)
- 검색어: `coordinated omission latency percentiles load generator saturation`

답을 적고 시작하세요.

1. 같은 머신에서 부하 생성기와 서버를 돌리면 무엇이 측정을 왜곡하나?
2. 평균 latency 대신 p99를 보는 이유는? wrk의 latency에 coordinated omission이 끼는 이유는?
3. 측정 전에 어떤 빌드 옵션을 맞춰야 하나?

## 준비 — 기준선 빌드

```sh
make clean && make c CFLAGS='-O2 -g'                                   # C epoll (현재 HEAD)
for t in c09-blocking c10-threads; do
  git worktree add /tmp/bench-$t $t
  make -C /tmp/bench-$t c CFLAGS='-O2 -g'
done
cargo build --release --manifest-path rust/Cargo.toml --bins --examples
```

## 측정

```sh
run() {   # $1 = 이름, 나머지 = 서버 명령
  local name=$1; shift
  "$@" 2>/dev/null & local pid=$!; sleep 0.5      # 연결 오류 로그는 버림
  wrk -t2 -c50 -d5s http://127.0.0.1:8080/ >/dev/null                         # warm-up
  for i in 1 2 3; do
    wrk -t2 -c50 -d30s --latency http://127.0.0.1:8080/ | grep -E '^Requests/sec|^ +50%|^ +99%|Socket errors'
  done
  grep -E 'VmRSS|Threads' /proc/$pid/status
  kill $pid; wait $pid 2>/dev/null
}
run c-blocking /tmp/bench-c09-blocking/build/c/http-server
run c-threads  /tmp/bench-c10-threads/build/c/http-server
run c-epoll    ./build/c/http-server
run rust-sync  ./rust/target/release/sync_server
run rust-tokio ./rust/target/release/scratch-http
```

다음 조건을 바꿔 가며 반복합니다 (한 번에 하나씩):

| 시나리오 | 바꿀 것 | 무엇이 드러나나 |
|---|---|---|
| 짧은 요청, keep-alive | 기본 (wrk는 keep-alive) | syscall당 비용, 스케줄링 |
| 동시 연결 수 | `-c10`, `-c100`, `-c1000` (`ulimit -n` 확인) | 스레드 수 증가 비용, 단일 스레드 blocking의 한계 |
| idle 연결 | 별도로 반쪽 헤더 연결 200개를 붙잡아 둔 채 측정 | thread-per-connection의 메모리 |
| 큰 body | `wrk -s post.lua`로 64KiB POST /echo (아래 스크립트) | 복사·버퍼 관리 |

```lua
-- post.lua
wrk.method = "POST"
wrk.path = "/echo"
wrk.body = string.rep("x", 65536)
wrk.headers["Content-Type"] = "application/octet-stream"
```

## 설명 근거 수집

```sh
perf stat -e task-clock,context-switches,cpu-migrations -p <PID> -- sleep 10    # 부하 중
strace -c -f -p <PID>       # 10초 후 Ctrl-C: syscall 종류별 횟수와 시간
```

- 요청 1개당 syscall 수: blocking(recvfrom/sendto/accept/close) vs epoll(+ epoll_wait/epoll_ctl) vs Tokio
- context switch 수: threads vs epoll
- wrk 스레드가 CPU 100%면 **부하 생성기가 병목**입니다. 서버 비교가 아닙니다.

## 결과 정리 양식

| 구현 | 빌드 | RPS (3회) | p50 | p99 | 오류 | RSS | 스레드 | syscall/요청 | ctx switch/s |
|---|---|---|---|---|---|---|---|---|---|

그리고 다음 질문에 각각 측정값을 근거로 3–5문장:

1. 단일 스레드 blocking은 어느 조건에서 무너지나? 왜 `-c50` 짧은 요청에서는 의외로 괜찮을 수 있나?
2. threads와 epoll의 차이가 가장 크게 나는 시나리오는? 그 원인은 메모리인가 context switch인가?
3. C epoll과 Rust Tokio의 차이는 어디서 오나? (멀티스레드 여부, 버퍼 복사, 런타임 오버헤드)
4. 이 측정이 **말해주지 않는 것**은?

## 정리 후

```sh
git worktree remove /tmp/bench-c09-blocking
git worktree remove /tmp/bench-c10-threads
```

`docs/CHECKPOINTS.md` M8을 결과표와 함께 체크합니다.
