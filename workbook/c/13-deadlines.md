# C13 — 시간 제한과 연결 수 제한

> 목표: 메모리뿐 아니라 **시간과 연결 수**도 제한된 자원으로 다룬다. 느린 클라이언트가 연결을 영원히 붙잡지 못하게 한다.
> 선행: [C12](12-epoll-write.md) · 다음: [C14](14-shutdown.md) · 검사: `python3 workbook/check.py c13`

## 먼저 공부할 것

- `man 2 epoll_wait`의 timeout, `man 2 clock_gettime`(`CLOCK_MONOTONIC`)
- `man 2 accept`의 `EMFILE`/`ENFILE`
- 검색어: `slowloris attack`, `admission control deadline`, `timer wheel min heap`

답을 적고 시작하세요.

1. slowloris 공격은 어떤 자원을 고갈시키나? C12까지의 서버는 왜 이걸 못 막나?
2. epoll 루프에서 "5초 뒤에 이 연결을 닫기"는 어떻게 구현할 수 있나? (스레드·sleep 없이)
3. 왜 `CLOCK_REALTIME`이 아니라 `CLOCK_MONOTONIC`인가?

## 만들 것

[계약](../00-contract.md)의 "동시성과 자원" 표 중 C13 행.

- [ ] 연결마다 `deadline`(ms) 하나:
  - 요청의 첫 바이트를 받은 순간 = 지금 + 5초 (request deadline — 요청이 완성되지 않은 채 지나면 닫기)
  - 응답을 다 보내고 버퍼가 빈 순간 = 지금 + 5초 (idle deadline)
  - 보낼 게 남아 있는 동안 send가 진척될 때마다 = 지금 + 5초 (write deadline)
- [ ] `epoll_wait` timeout = 가장 가까운 deadline까지 남은 ms (없으면 -1). 깨어날 때마다 지난 deadline의 연결을 닫음
- [ ] 동시 연결 최대 256개: 넘치면 accept 후 바로 close (또는 accept를 잠시 멈춤 — 둘의 차이를 기록)
- [ ] accept가 `EMFILE`이면 서버가 죽지 않도록 (그리고 busy loop에 빠지지 않도록 — 힌트 2)

<details><summary>힌트 1 — 가장 가까운 deadline 찾기</summary>

연결 256개면 매 바퀴 전부 훑어도 충분히 빠릅니다(먼저 단순하게). 연결이 많아지면 min-heap이나 timer wheel이 필요합니다. 또는 deadline이 항상 "지금 + 5초"로만 갱신되므로, 갱신된 연결을 리스트 끝으로 옮기면 리스트가 deadline 순으로 정렬된 채 유지됩니다 — 고정 timeout일 때만 쓸 수 있는 요령입니다.
</details>

<details><summary>힌트 2 — EMFILE의 함정</summary>

fd가 바닥나면 accept는 `EMFILE`로 실패하지만, 연결은 **여전히 accept 큐에 있으므로** listen fd는 계속 readable입니다. level-triggered에서 그대로 두면 epoll_wait가 즉시 돌아와 busy loop가 됩니다. 흔한 해결책: 예비 fd 하나를 열어 두었다가 EMFILE 때 닫고 → accept → 즉시 close → 예비 fd 다시 열기. 또는 잠시 listen fd를 epoll에서 빼기. 최대 연결 수를 fd 한도보다 충분히 낮게 잡으면 대부분 피할 수 있습니다.
</details>

## 확인하기

### 자동 검사

```sh
make c && python3 workbook/check.py c13     # 약 30초 (5초 타이머 검사 3개)
```

### 직접 관찰

```sh
./build/c/http-server & PID=$!
python3 - <<'EOF'
import socket, time
s = socket.create_connection(("127.0.0.1", 8080)); t = time.time()
for b in b"GET / HTTP/1.1\r\nHost: a\r\n\r\n":
    try: s.send(bytes([b])); time.sleep(0.5)
    except OSError as e: print("closed after", round(time.time() - t, 1), "s:", e); break
else:
    print("response:", s.recv(100))
EOF
strace -p $PID -e trace=epoll_wait 2>&1 | head -5     # timeout 인자가 남은 ms로 줄어드는지
kill $PID
```

### 일부러 깨뜨려 보기

1. deadline을 "마지막으로 데이터를 받은 시각 + 5초"로 구현하면 slowloris 검사가 통과하나요? 1바이트/4초 클라이언트는?
2. 연결 수 제한을 빼고 `ulimit -n 64`에서 검사의 "300개 연결"을 돌리면? 힌트 2의 busy loop를 `top`으로 확인해 보세요.

## 알았어야 하는 것

<details><summary>1. "마지막 활동 + N초"로는 왜 부족한가</summary>

slowloris는 연결이 끊기지 않을 만큼만 아주 가끔 1바이트씩 보냅니다. 활동 기준 타임아웃은 매번 갱신되어 영원히 끊기지 않습니다. 그래서 "요청 시작부터 완료까지의 총 시간"(request deadline)이 필요합니다. 쓰기는 반대로 "진척이 있으면 연장"이 맞습니다 — 큰 응답을 느린 회선으로 정상적으로 받는 클라이언트를 끊지 않기 위해서입니다.
</details>

<details><summary>2. 이벤트 루프의 타이머</summary>

이벤트 루프는 잠들 때 "언제 깨워야 하는지"를 epoll_wait timeout으로 커널에 알려줍니다. I/O 이벤트와 타이머를 같은 대기 하나로 합치는 것입니다. 대안은 `timerfd`(타이머를 fd로 만들어 epoll에 등록). Tokio의 `time::sleep`도 결국 runtime의 timer wheel이 다음 deadline을 계산해 epoll_wait timeout에 넘깁니다(R06).
</details>

<details><summary>3. MONOTONIC</summary>

REALTIME은 NTP 조정이나 사용자가 시계를 바꾸면 뒤로 가거나 건너뜁니다. 경과 시간 측정에는 절대 뒤로 가지 않는 MONOTONIC을 씁니다.
</details>

<details><summary>4. admission control</summary>

연결 수, 메모리, 시간 — 모든 자원에 상한이 있어야 서버의 최악 자원 사용량을 계산할 수 있습니다(256 연결 × (8KiB head + 1MiB body + 64KiB 출력)). 상한을 넘는 요청은 "받아서 느리게 처리"하는 것보다 "빨리 거절"하는 것이 전체 서비스를 지킵니다.
</details>

## 기록

최악의 경우 메모리 계산식(연결 수 × 연결당 최대 버퍼)과 실제 숫자.
