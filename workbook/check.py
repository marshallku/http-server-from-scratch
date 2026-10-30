#!/usr/bin/env python3
"""학습지 단계별 검증기.

    python3 workbook/check.py c04                     # ./build/c/http-server 를 띄워 C04까지 검사
    python3 workbook/check.py c08 --only              # C08에서 새로 생긴 검사만
    python3 workbook/check.py c11 -- ./build/c/http-server
    python3 workbook/check.py r02 -- ./rust/target/debug/scratch-http
    python3 workbook/check.py --list

서버 바이너리를 직접 실행하고(래퍼 스크립트·cargo run 대신 바이너리 경로를 넘길 것),
127.0.0.1:PORT(기본 8080)에 raw socket으로 요청을 보낸 뒤 항상 프로세스를 정리한다.
표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_C_BINARY = "./build/c/http-server"
HELLO = b"Hello, World!\n"
MAX_HEAD = 8192
MAX_BODY = 1024 * 1024

USE_COLOR = sys.stdout.isatty()


def color(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


class CheckFailed(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def expect(cond: bool, message: str, hint: str = "") -> None:
    if not cond:
        raise CheckFailed(message, hint)


# --------------------------------------------------------------------------
# 서버 프로세스 관리
# --------------------------------------------------------------------------


@dataclass
class Server:
    cmd: list[str]
    port: int
    proc: subprocess.Popen | None = None
    log: tempfile._TemporaryFileWrapper | None = None

    def start(self) -> None:
        ensure_port_free(self.port)
        exe = shutil.which(self.cmd[0]) or self.cmd[0]
        if not os.path.exists(exe):
            raise CheckFailed(
                f"서버 실행 파일이 없음: {self.cmd[0]}",
                "먼저 빌드하세요 (make c / cargo build). 다른 경로면 `-- <경로>`로 넘깁니다.",
            )
        self.log = tempfile.NamedTemporaryFile(prefix="check-server-", suffix=".log", delete=False)
        env = dict(os.environ, PORT=str(self.port))
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def expect_alive(self, when: str) -> None:
        if not self.alive():
            code = self.proc.returncode if self.proc else None
            raise CheckFailed(f"{when} 서버 프로세스가 종료됨 (exit={describe_exit(code)})", self.log_tail())

    def wait_ready(self, timeout: float = 5.0) -> None:
        """connect가 성공할 때까지 기다린다. 연결은 곧바로 닫는다(빈 연결도 견뎌야 한다)."""
        sock = self.connect_retry(timeout)
        sock.close()

    def connect_retry(self, timeout: float = 5.0) -> socket.socket:
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive():
                hint = ""
                if "Address already in use" in self.log_tail(50):
                    hint = "bind가 EADDRINUSE로 실패했습니다. 이전 실행의 TIME_WAIT이면 SO_REUSEADDR로 해결됩니다.\n"
                raise CheckFailed(
                    f"listen 전에 서버 프로세스가 종료됨 (exit={describe_exit(self.proc.returncode)})",
                    hint + self.log_tail(),
                )
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
            except OSError:
                if time.monotonic() > deadline:
                    raise CheckFailed(
                        f"{timeout:.0f}초 안에 127.0.0.1:{self.port}에 연결하지 못함",
                        "bind/listen이 성공했는지, 포트를 PORT 환경변수 또는 8080으로 쓰는지 확인하세요.\n"
                        + self.log_tail(),
                    )
                time.sleep(0.05)

    def wait_exit(self, timeout: float) -> int | None:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if self.log is not None:
            self.log.close()
            os.unlink(self.log.name)
            self.log = None
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)
        else:
            # 서버가 스스로 끝났더라도 같은 그룹의 자식(스레드 아닌 fork 등)은 정리
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def log_tail(self, lines: int = 15) -> str:
        if self.log is None:
            return ""
        self.log.flush()
        try:
            text = Path(self.log.name).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        if not text:
            return "(서버 출력 없음)"
        return "서버 출력 마지막 부분:\n    " + "\n    ".join(text[-lines:])

    # /proc 관측 ------------------------------------------------------------

    def fd_count(self) -> int:
        return len(os.listdir(f"/proc/{self.pid}/fd"))

    def cpu_seconds(self) -> float:
        with open(f"/proc/{self.pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) / ticks

    def rss_kib(self) -> int:
        with open(f"/proc/{self.pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        return 0


def describe_exit(code: int | None) -> str:
    if code is None:
        return "running"
    if code < 0:
        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    return str(code)


def ensure_port_free(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        raise CheckFailed(
            f"포트 {port}를 이미 다른 프로세스가 쓰고 있음",
            f"`ss -ltnp 'sport = :{port}'`로 찾아서 종료하세요. 이전에 띄운 서버일 가능성이 큽니다.",
        )
    finally:
        probe.close()


# --------------------------------------------------------------------------
# 클라이언트 유틸
# --------------------------------------------------------------------------


def connect(server: Server, timeout: float = 3.0, rcvbuf: int | None = None) -> socket.socket:
    """rcvbuf: 수신 버퍼를 줄여 '안 읽는 클라이언트'를 흉내 낸다. window는 SYN에서 정해지므로 connect 전에 설정."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", server.port))
    except OSError as e:
        sock.close()
        server.expect_alive("connect 실패 시점에")
        raise CheckFailed(f"connect 실패: {e}")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def send_slowly(sock: socket.socket, data: bytes, step: int = 1, delay: float = 0.002) -> None:
    for i in range(0, len(data), step):
        sock.sendall(data[i : i + step])
        time.sleep(delay)


def recv_until_eof(sock: socket.socket, timeout: float = 3.0, limit: int = 64 * 1024 * 1024) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    total = 0
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise CheckFailed(f"{timeout}초 동안 서버가 연결을 닫지 않음 (EOF 없음)")
        except ConnectionResetError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    return b"".join(chunks)


def is_closed(sock: socket.socket, timeout: float) -> bool:
    """timeout 안에 EOF/RST가 오면 True, 아무것도 안 오면 False. 데이터가 오면 실패."""
    sock.settimeout(timeout)
    try:
        chunk = sock.recv(65536)
    except socket.timeout:
        return False
    except ConnectionResetError:
        return True
    if chunk:
        raise CheckFailed(f"예상치 못한 데이터를 받음: {chunk[:80]!r}")
    return True


def nothing_arrives(sock: socket.socket, timeout: float) -> bool:
    sock.settimeout(timeout)
    try:
        chunk = sock.recv(65536)
    except socket.timeout:
        return True
    except ConnectionResetError:
        return False
    return False


@dataclass
class Response:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes
    raw_head: bytes

    def header(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        return values[0] if values else None

    def wants_close(self) -> bool:
        value = self.header("connection") or ""
        return "close" in [t.strip().lower() for t in value.split(",")]


class Reader:
    """한 소켓에서 HTTP 응답을 Content-Length 기준으로 하나씩 읽는다."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        self.sock.settimeout(remaining)
        try:
            chunk = self.sock.recv(65536)
        except ConnectionResetError:
            chunk = b""
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read(self, head_only: bool = False, timeout: float = 3.0) -> Response:
        deadline = time.monotonic() + timeout
        try:
            while b"\r\n\r\n" not in self.buf:
                if len(self.buf) > 64 * 1024:
                    raise CheckFailed("응답 헤더가 64KiB를 넘음 — \\r\\n\\r\\n으로 헤더를 끝냈나요?")
                if not self._fill(deadline):
                    if not self.buf:
                        raise CheckFailed("응답 없이 연결이 닫힘")
                    raise CheckFailed(f"응답 헤더가 끝나기 전에 연결이 닫힘: {self.buf[:120]!r}")
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin-1").split(" ", 2)
            expect(
                len(parts) >= 2 and parts[0] == "HTTP/1.1" and parts[1].isdigit(),
                f"status line 형식이 아님: {lines[0][:80]!r}",
                "예: `HTTP/1.1 200 OK\\r\\n`",
            )
            headers = []
            for line in lines[1:]:
                expect(b":" in line, f"헤더 줄에 ':'가 없음: {line[:80]!r}")
                k, v = line.split(b":", 1)
                headers.append((k.decode("latin-1"), v.decode("latin-1").strip()))
            resp = Response(int(parts[1]), parts[2] if len(parts) > 2 else "", headers, b"", head)
            length = resp.header("content-length")
            expect(
                length is not None and length.isdigit(),
                f"응답에 올바른 Content-Length가 없음 (status {resp.status})",
                "이 학습지의 계약: 모든 응답에 Content-Length를 붙인다 (00-contract.md).",
            )
            if head_only:
                return resp
            n = int(length)
            while len(self.buf) < n:
                if not self._fill(deadline):
                    raise CheckFailed(f"body {n}바이트 중 {len(self.buf)}바이트만 받고 연결이 닫힘")
            resp.body, self.buf = self.buf[:n], self.buf[n:]
            return resp
        except socket.timeout:
            raise CheckFailed(f"{timeout}초 안에 응답이 완성되지 않음 (받은 것: {self.buf[:120]!r})")


def request(
    method: str = "GET",
    target: str = "/",
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    version: str = "HTTP/1.1",
    host: bool = True,
    close: bool = False,
) -> bytes:
    lines = [f"{method} {target} {version}"]
    if host:
        lines.append("Host: localhost")
    for k, v in headers or []:
        lines.append(f"{k}: {v}")
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def exchange(server: Server, raw: bytes, head_only: bool = False, timeout: float = 3.0) -> Response:
    sock = connect(server)
    try:
        sock.sendall(raw)
        return Reader(sock).read(head_only=head_only, timeout=timeout)
    finally:
        sock.close()


def expect_status(resp: Response, status: int, what: str, hint: str = "") -> None:
    expect(
        resp.status == status,
        f"{what}: {status}을 기대했지만 {resp.status}",
        hint,
    )


def expect_error_and_close(server: Server, raw: bytes, status: int | tuple[int, ...], what: str, hint: str = "") -> None:
    statuses = status if isinstance(status, tuple) else (status,)
    sock = connect(server)
    try:
        sock.sendall(raw)
        reader = Reader(sock)
        resp = reader.read()
        expect(
            resp.status in statuses,
            f"{what}: {'/'.join(map(str, statuses))}을 기대했지만 {resp.status}",
            hint,
        )
        expect(resp.wants_close(), f"{what}: 오류 응답에 `Connection: close`가 없음", "framing 오류 뒤에는 연결을 닫는다.")
        sock.settimeout(2.0)
        try:
            rest = reader.buf + sock.recv(65536)
        except socket.timeout:
            raise CheckFailed(f"{what}: 오류 응답 후 2초 안에 연결을 닫지 않음")
        except ConnectionResetError:
            rest = b""
        expect(rest == b"", f"{what}: 오류 응답 뒤에 추가 데이터가 옴: {rest[:80]!r}")
    finally:
        sock.close()


def wait_until(cond: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def stable_fd_count(server: Server, baseline: int, slack: int, timeout: float = 2.0) -> int:
    wait_until(lambda: server.fd_count() <= baseline + slack, timeout)
    return server.fd_count()


def rst_close(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


# --------------------------------------------------------------------------
# 검사 정의
# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    fn: Callable[[Server], None]


@dataclass
class Step:
    key: str
    title: str
    checks: list[Check] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    mode: str = "server"  # server | oneshot | parser | command
    command: Callable[[argparse.Namespace], int] | None = None
    default_cmd: str = DEFAULT_C_BINARY


STEPS: dict[str, Step] = {}


def step(key: str, title: str, includes: list[str] | None = None, mode: str = "server") -> Step:
    s = Step(key, title, includes=includes or [], mode=mode)
    STEPS[key] = s
    return s


def check(s: Step, name: str):
    def deco(fn: Callable[[Server], None]):
        s.checks.append(Check(name, fn))
        return fn

    return deco


# ---- C01 ------------------------------------------------------------------

C01 = step("c01", "socket → bind → listen → accept 한 번", mode="oneshot")


@check(C01, "클라이언트 하나를 받아서 연결을 닫고, exit 0으로 종료")
def c01_accept_once(server: Server) -> None:
    sock = server.connect_retry()
    try:
        expect(is_closed(sock, 3.0), "accept 후 3초 안에 연결을 닫지 않음", "accept가 돌려준 client fd를 close 하세요.")
    finally:
        sock.close()
    code = server.wait_exit(3.0)
    expect(code is not None, "클라이언트를 처리한 뒤 3초 안에 종료하지 않음", "C01은 한 번 accept하고 끝나는 프로그램입니다.")
    expect(code == 0, f"종료 코드가 0이 아님: {describe_exit(code)}", server.log_tail())


@check(C01, "곧바로 다시 실행해도 bind 성공 (TIME_WAIT과 SO_REUSEADDR)")
def c01_rebind(server: Server) -> None:
    sock = server.connect_retry()
    is_closed(sock, 3.0)
    sock.close()
    server.wait_exit(3.0)
    again = Server(server.cmd, server.port)
    again.start()
    try:
        sock = again.connect_retry()
        sock.close()
        again.wait_exit(3.0)
        expect(
            "Address already in use" not in again.log_tail(50),
            "두 번째 실행에서 bind가 EADDRINUSE로 실패",
            "서버가 먼저 close한 연결은 서버 쪽 포트에 TIME_WAIT을 남깁니다. bind 전에 SO_REUSEADDR을 켜세요.\n"
            + again.log_tail(),
        )
    finally:
        again.stop()


# ---- C02 ------------------------------------------------------------------

C02 = step("c02", "TCP echo — EOF까지 받고, 받은 만큼 전부 돌려보내기", mode="oneshot")


def echo_roundtrip(sock: socket.socket, payload: bytes, chunk: int = 7) -> bytes:
    errors: list[BaseException] = []

    def sender() -> None:
        try:
            for i in range(0, len(payload), chunk):
                sock.sendall(payload[i : i + chunk])
            sock.shutdown(socket.SHUT_WR)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=sender, daemon=True)
    t.start()
    try:
        got = recv_until_eof(sock, timeout=5.0)
    finally:
        t.join(timeout=5.0)
    if errors:
        raise CheckFailed(f"클라이언트 송신 중 오류: {errors[0]}")
    return got


@check(C02, "분할 전송한 1MiB binary(NUL 포함)를 그대로 돌려받고, 반쪽 닫기(EOF) 후 연결 종료")
def c02_echo(server: Server) -> None:
    sock = server.connect_retry()
    payload = random.Random(2).randbytes(1024 * 1024)
    try:
        got = echo_roundtrip(sock, payload, chunk=4093)
    finally:
        sock.close()
    expect(
        len(got) == len(payload),
        f"{len(payload)}바이트를 보냈는데 {len(got)}바이트를 돌려받음",
        "send가 요청보다 적게 보낼 수 있습니다(short write). 남은 만큼 반복해서 보내세요. "
        "문자열 함수(strlen, printf %s)는 NUL에서 멈춥니다.",
    )
    expect(got == payload, "돌려받은 내용이 보낸 내용과 다름")


# ---- C03 ------------------------------------------------------------------

C03 = step("c03", "계속 서빙하기 — accept 루프, fd 정리, SIGPIPE")


@check(C03, "연속된 클라이언트 3개 모두 echo")
def c03_sequential(server: Server) -> None:
    for i in range(3):
        sock = connect(server)
        try:
            got = echo_roundtrip(sock, f"client {i}\n".encode() * 100)
        finally:
            sock.close()
        expect(got == f"client {i}\n".encode() * 100, f"{i + 1}번째 클라이언트 echo가 틀림")
    server.expect_alive("3번 연결 후")


@check(C03, "200번 접속·해제 후에도 열린 fd 수가 늘지 않음")
def c03_fd_leak(server: Server) -> None:
    baseline = server.fd_count()
    for _ in range(200):
        sock = connect(server)
        sock.sendall(b"x")
        sock.shutdown(socket.SHUT_WR)
        recv_until_eof(sock)
        sock.close()
    after = stable_fd_count(server, baseline, 1)
    expect(
        after <= baseline + 1,
        f"fd 수가 {baseline} → {after}로 늘어남",
        "모든 경로(정상/오류)에서 client fd를 close 하나요? `ls -l /proc/<PID>/fd`로 확인하세요.",
    )


@check(C03, "보내자마자 닫아버리는 클라이언트 뒤에도 서버가 살아 있음 (SIGPIPE)")
def c03_sigpipe(server: Server) -> None:
    # 클라이언트가 닫은 뒤 서버가 echo를 쓰면 RST가 돌아오고, 그 다음 send는 EPIPE.
    # MSG_NOSIGNAL(또는 SIGPIPE 무시) 없이 쓰면 프로세스가 SIGPIPE로 죽는다.
    for _ in range(20):
        sock = connect(server)
        sock.sendall(b"a" * 8192)
        sock.close()
        time.sleep(0.02)
        if not server.alive():
            break
    time.sleep(0.2)
    if not server.alive():
        raise CheckFailed(
            f"닫힌 연결에 쓰다가 서버가 죽음 (exit={describe_exit(server.proc.returncode)})",
            "send(..., MSG_NOSIGNAL) 또는 signal(SIGPIPE, SIG_IGN) 후 EPIPE를 오류로 처리하세요.",
        )
    sock = connect(server)
    try:
        expect(echo_roundtrip(sock, b"still alive") == b"still alive", "끊긴 클라이언트 이후 echo가 동작하지 않음")
    finally:
        sock.close()


# ---- C04 ------------------------------------------------------------------

C04 = step("c04", "최소 HTTP — 헤더 끝(\\r\\n\\r\\n)까지 모으고 고정 응답")


@check(C04, "GET / → 200, Content-Length: 14, body 'Hello, World!\\n'")
def c04_hello(server: Server) -> None:
    resp = exchange(server, request(close=True))
    expect_status(resp, 200, "GET /")
    expect(resp.body == HELLO, f"body가 다름: {resp.body!r}")


@check(C04, "Connection: close 요청이면 응답 후 서버가 연결을 닫음")
def c04_close(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(request(close=True))
        reader = Reader(sock)
        reader.read()
        expect(reader.buf == b"" and is_closed(sock, 2.0), "응답 후 2초 안에 연결을 닫지 않음")
    finally:
        sock.close()


@check(C04, "헤더가 다 오기 전에는 응답하지 않음 (recv 한 번 = 요청 하나가 아님)")
def c04_wait_for_head(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n")
        expect(
            nothing_arrives(sock, 0.4),
            "빈 줄(\\r\\n\\r\\n)이 오기 전에 응답하거나 연결을 닫음",
            "recv 한 번에 요청이 다 온다고 가정하지 말고, 버퍼에 누적하며 \\r\\n\\r\\n을 찾으세요.",
        )
        sock.sendall(b"\r\n")
        expect_status(Reader(sock).read(), 200, "나머지 빈 줄을 보낸 뒤")
    finally:
        sock.close()


@check(C04, "1바이트씩 쪼개 보낸 요청에도 200")
def c04_byte_by_byte(server: Server) -> None:
    sock = connect(server)
    try:
        send_slowly(sock, request(close=True), step=1, delay=0.003)
        expect_status(Reader(sock).read(), 200, "1바이트씩 보낸 GET /")
    finally:
        sock.close()


@check(C04, "8192바이트를 넘는 헤더 → 431 + Connection: close")
def c04_too_large(server: Server) -> None:
    raw = request(headers=[("X-Fill", "a" * MAX_HEAD)])
    expect_error_and_close(
        server,
        raw,
        431,
        "9KiB 헤더",
        "버퍼가 8192바이트가 됐는데 \\r\\n\\r\\n이 없으면 더 기다리지 말고 431을 보내세요.",
    )


@check(C04, "아무것도 안 보내고 끊거나, 반만 보내고 끊는 클라이언트를 견딤")
def c04_early_close(server: Server) -> None:
    for raw in (b"", b"GET / HTT", b"GET / HTTP/1.1\r\nHost: a\r\n"):
        sock = connect(server)
        if raw:
            sock.sendall(raw)
        sock.close()
    sock = connect(server)
    rst_close(sock)
    time.sleep(0.2)
    server.expect_alive("조기 종료 클라이언트 처리 후")
    expect_status(exchange(server, request(close=True)), 200, "조기 종료 이후의 GET /")


@check(C04, "100번 요청 후 fd 수가 늘지 않음")
def c04_fd_leak(server: Server) -> None:
    baseline = server.fd_count()
    for _ in range(100):
        exchange(server, request(close=True))
    after = stable_fd_count(server, baseline, 1)
    expect(after <= baseline + 1, f"fd 수가 {baseline} → {after}로 늘어남", "응답 후 client fd를 close 하나요?")


# ---- C05 (parser unit tests) ----------------------------------------------

C05 = step("c05", "I/O와 분리한 요청 head 파서 + 유닛 테스트", mode="parser")


def run_parser_tests(args: argparse.Namespace) -> int:
    c_dir = Path(os.environ.get("CHECK_C_DIR", ROOT / "c/src"))
    src = c_dir / "http_parser.c"
    hdr = c_dir / "http_parser.h"
    for p in (src, hdr):
        if not p.exists():
            print(color("31", f"✘ {p} 가 없음"), "— workbook/c/05-parser.md의 API를 먼저 만드세요.")
            return 1
    out = Path(tempfile.mkdtemp(prefix="check-c05-")) / "test_parser"
    cc = os.environ.get("CC", "cc")
    cmd = [
        cc,
        "-std=c17",
        "-D_POSIX_C_SOURCE=200809L",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-Werror",
        "-g",
        "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer",
        "-I",
        str(c_dir),
        str(ROOT / "workbook/tests/test_parser.c"),
        str(src),
        "-o",
        str(out),
    ]
    print(color("2", "$ " + " ".join(cmd)))
    if subprocess.call(cmd) != 0:
        print(color("31", "✘ 컴파일 실패"))
        return 1
    env = dict(os.environ, UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1")
    code = subprocess.call([str(out)], env=env, timeout=60)
    shutil.rmtree(out.parent, ignore_errors=True)
    if code == 0:
        print(color("32", "✔ parser 유닛 테스트 통과 (ASan/UBSan 포함)"))
    else:
        print(color("31", f"✘ parser 유닛 테스트 실패 (exit {code})"))
    return 0 if code == 0 else 1


C05.command = run_parser_tests


# ---- C06 ------------------------------------------------------------------

C06 = step("c06", "라우팅과 상태 코드 — GET/HEAD/404/405/400/505", includes=["c04"])


@check(C06, "HEAD / → 200, Content-Length: 14, body 없음")
def c06_head(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(request("HEAD", close=True))
        reader = Reader(sock)
        resp = reader.read(head_only=True)
        expect_status(resp, 200, "HEAD /")
        expect(resp.header("content-length") == "14", f"HEAD도 GET과 같은 Content-Length여야 함: {resp.header('content-length')}")
        rest = reader.buf + recv_until_eof(sock, timeout=2.0)
        expect(rest == b"", f"HEAD 응답에 body가 붙음: {rest[:40]!r}", "HEAD는 헤더만 보내고 body는 보내지 않습니다.")
    finally:
        sock.close()


@check(C06, "query string은 경로에서 제외 (GET /?a=1 → 200)")
def c06_query(server: Server) -> None:
    expect_status(exchange(server, request(target="/?a=1&b=2", close=True)), 200, "GET /?a=1&b=2")


@check(C06, "없는 경로 → 404, 알려진 경로의 다른 method → 405 + Allow")
def c06_routes(server: Server) -> None:
    expect_status(exchange(server, request(target="/nope", close=True)), 404, "GET /nope")
    resp = exchange(server, request("DELETE", close=True))
    expect_status(resp, 405, "DELETE /")
    allow = {m.strip() for m in (resp.header("allow") or "").split(",")}
    expect(allow == {"GET", "HEAD"}, f"DELETE / 의 Allow가 'GET, HEAD'가 아님: {resp.header('allow')!r}")
    resp = exchange(server, request("GET", "/echo", close=True))
    expect_status(resp, 405, "GET /echo")
    expect((resp.header("allow") or "").strip() == "POST", f"GET /echo의 Allow가 'POST'가 아님: {resp.header('allow')!r}")
    expect_status(exchange(server, request("get", close=True)), 405, "소문자 method 'get' (method는 대소문자 구분)")


@check(C06, "문법 오류 → 400, 지원하지 않는 버전 → 505 (모두 연결 종료)")
def c06_errors(server: Server) -> None:
    expect_error_and_close(server, b"GET /\r\n\r\n", 400, "버전 없는 request line")
    expect_error_and_close(server, b"GET / HTTP/1.1\nHost: a\n\n", 400, "bare LF 줄바꿈", "줄 끝은 반드시 \\r\\n.")
    expect_error_and_close(server, b"GET / HTTP/1.1\r\nHost : a\r\n\r\n", 400, "콜론 앞 공백")
    expect_error_and_close(server, b"GET http://x/ HTTP/1.1\r\nHost: x\r\n\r\n", 400, "absolute-form target(이 서버는 미지원)")
    expect_error_and_close(server, b"GET / HTTP/2.0\r\nHost: a\r\n\r\n", 505, "HTTP/2.0")


@check(C06, "Host 규칙: 1.1에서 누락/중복/빈 값 → 400, 1.0은 Host 없이도 200")
def c06_host(server: Server) -> None:
    expect_error_and_close(server, request(host=False), 400, "Host 누락 (HTTP/1.1)")
    expect_error_and_close(server, request(headers=[("Host", "b")]), 400, "Host 중복")
    expect_error_and_close(server, b"GET / HTTP/1.1\r\nHost:\r\n\r\n", 400, "빈 Host")
    expect_status(exchange(server, request(version="HTTP/1.0", host=False)), 200, "HTTP/1.0 without Host")
    expect_status(exchange(server, b"GET / HTTP/1.1\r\nhOsT: a\r\nConnection: close\r\n\r\n"), 200, "헤더 이름은 대소문자 무시 (hOsT)")


# ---- C07 ------------------------------------------------------------------

C07 = step("c07", "Content-Length body와 POST /echo, framing 검증", includes=["c04", "c06"])


def post_echo(body: bytes, extra: list[tuple[str, str]] | None = None, close: bool = True) -> bytes:
    return request("POST", "/echo", [("Content-Length", str(len(body)))] + (extra or []), body, close=close)


@check(C07, "POST /echo — 작은 body, 빈 body, NUL 포함 100KiB binary")
def c07_echo(server: Server) -> None:
    for body in (b"hello", b"", random.Random(7).randbytes(100 * 1024)):
        resp = exchange(server, post_echo(body))
        expect_status(resp, 200, f"POST /echo ({len(body)}바이트)")
        expect(resp.body == body, f"{len(body)}바이트 body echo가 다름 (받은 길이 {len(resp.body)})", "body는 문자열이 아니라 bytes입니다.")


@check(C07, "body가 여러 번에 나뉘어 와도 Content-Length만큼 기다림")
def c07_split_body(server: Server) -> None:
    sock = connect(server)
    try:
        raw = post_echo(b"0123456789")
        head, body = raw.split(b"\r\n\r\n", 1)
        sock.sendall(head + b"\r\n\r\n" + body[:4])
        expect(nothing_arrives(sock, 0.4), "body가 다 오기 전에 응답함")
        send_slowly(sock, body[4:], step=1, delay=0.01)
        resp = Reader(sock).read()
        expect(resp.status == 200 and resp.body == b"0123456789", f"나눠 보낸 body echo 실패: {resp.status} {resp.body!r}")
    finally:
        sock.close()


@check(C07, "Content-Length 검증: 숫자가 아님/중복 → 400, 1MiB 초과/overflow → 413")
def c07_bad_length(server: Server) -> None:
    for value in ("abc", "-1", "1 2", "+5", ""):
        expect_error_and_close(server, request("POST", "/echo", [("Content-Length", value)]), 400, f"Content-Length: {value!r}")
    expect_error_and_close(
        server,
        request("POST", "/echo", [("Content-Length", "3"), ("Content-Length", "3")], b"abc"),
        400,
        "같은 값이라도 Content-Length 중복",
        "이 프로젝트의 엄격한 정책: 중복은 무조건 400 (request smuggling 방지).",
    )
    expect_error_and_close(server, request("POST", "/echo", [("Content-Length", str(MAX_BODY + 1))]), 413, "1MiB + 1 body")
    expect_error_and_close(
        server,
        request("POST", "/echo", [("Content-Length", "184467440737095516160")]),
        413,
        "2^64보다 큰 Content-Length",
        "자릿수를 더하기 전에 한도를 넘는지 확인해야 overflow가 나지 않습니다.",
    )


@check(C07, "Transfer-Encoding+Content-Length → 400, 모르는 coding → 501, Expect → 417")
def c07_te_expect(server: Server) -> None:
    expect_error_and_close(
        server,
        request("POST", "/echo", [("Content-Length", "5"), ("Transfer-Encoding", "chunked")], b"hello"),
        400,
        "TE + CL 동시",
    )
    expect_error_and_close(server, request("POST", "/echo", [("Transfer-Encoding", "gzip")]), 501, "Transfer-Encoding: gzip")
    expect_error_and_close(
        server,
        request("POST", "/echo", [("Content-Length", "5"), ("Expect", "100-continue")]),
        417,
        "Expect: 100-continue",
    )


@check(C07, "body 중간에 EOF → 200으로 처리하지 않고 연결 종료")
def c07_truncated(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(request("POST", "/echo", [("Content-Length", "10")], b"12345"))
        sock.shutdown(socket.SHUT_WR)
        got = recv_until_eof(sock, timeout=3.0)
        expect(b" 200 " not in got.split(b"\r\n", 1)[0], "잘린 body를 완성된 요청으로 처리함", "recv가 0을 반환하면 아직 덜 온 요청은 버립니다.")
    finally:
        sock.close()
    server.expect_alive("잘린 body 이후")


@check(C07, "GET에 붙은 body도 읽어서 소비 (200)")
def c07_get_with_body(server: Server) -> None:
    resp = exchange(server, request("GET", "/", [("Content-Length", "5")], b"hello", close=True))
    expect(resp.status == 200 and resp.body == HELLO, f"body가 붙은 GET /: {resp.status} {resp.body!r}")


# ---- C08 ------------------------------------------------------------------

C08 = step("c08", "keep-alive와 pipelining — 연결별 잔여 버퍼", includes=["c04", "c06", "c07"])


@check(C08, "한 연결에서 요청 3개를 차례로 보내면 3번 응답하고 연결 유지")
def c08_sequential(server: Server) -> None:
    sock = connect(server)
    try:
        reader = Reader(sock)
        for i in range(3):
            sock.sendall(request())
            resp = reader.read()
            expect_status(resp, 200, f"keep-alive {i + 1}번째 요청")
            expect(not resp.wants_close(), "Connection: close를 요청하지 않았는데 close 응답")
        expect(nothing_arrives(sock, 0.3), "keep-alive 연결을 서버가 먼저 닫음", "HTTP/1.1은 기본이 지속 연결입니다.")
    finally:
        sock.close()


@check(C08, "한 번의 send에 요청 3개(pipelining) → 순서대로 3개 응답")
def c08_pipelined(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(request() + post_echo(b"abc", close=False) + request(target="/nope"))
        reader = Reader(sock)
        got = [reader.read() for _ in range(3)]
        expect(
            [r.status for r in got] == [200, 200, 404] and got[1].body == b"abc",
            f"pipelining 응답 순서/내용이 다름: {[(r.status, r.body[:10]) for r in got]}",
            "recv 한 번에 요청이 여러 개 들어올 수 있습니다. 처리한 만큼만 버퍼에서 지우고 남은 bytes로 다시 파싱하세요.",
        )
    finally:
        sock.close()


@check(C08, "1바이트씩 쪼갠 요청 2개 → 응답 2개")
def c08_split_pipeline(server: Server) -> None:
    sock = connect(server)
    try:
        send_slowly(sock, post_echo(b"xy", close=False) + request(), step=1, delay=0.002)
        reader = Reader(sock)
        a, b = reader.read(), reader.read()
        expect(a.body == b"xy" and b.status == 200, f"쪼갠 pipelining 실패: {a.status} {a.body!r}, {b.status}")
    finally:
        sock.close()


@check(C08, "Connection: close / HTTP/1.0 → 응답에 Connection: close, 그리고 닫힘")
def c08_close(server: Server) -> None:
    for raw, what in ((request(close=True), "Connection: close"), (request(version="HTTP/1.0", host=False), "HTTP/1.0")):
        sock = connect(server)
        try:
            sock.sendall(raw)
            reader = Reader(sock)
            resp = reader.read()
            expect(resp.wants_close(), f"{what} 요청의 응답에 Connection: close가 없음")
            expect(reader.buf == b"" and is_closed(sock, 2.0), f"{what} 응답 후 연결을 닫지 않음")
        finally:
            sock.close()


@check(C08, "오류(400) 뒤에 붙은 요청은 처리하지 않음")
def c08_no_reinterpret(server: Server) -> None:
    raw = request("POST", "/echo", [("Content-Length", "1"), ("Content-Length", "1")], b"x") + request()
    expect_error_and_close(server, raw, 400, "중복 CL 뒤의 pipelined 요청", "framing을 믿을 수 없으면 뒤 bytes를 다음 요청으로 해석하면 안 됩니다.")


@check(C08, "keep-alive 연결 50개 × 요청 3개 후 fd 수가 늘지 않음")
def c08_fd_leak(server: Server) -> None:
    baseline = server.fd_count()
    for _ in range(50):
        sock = connect(server)
        reader = Reader(sock)
        for _ in range(3):
            sock.sendall(request())
            reader.read()
        sock.close()
    after = stable_fd_count(server, baseline, 1)
    expect(after <= baseline + 1, f"fd 수가 {baseline} → {after}로 늘어남", "클라이언트가 keep-alive 중에 닫으면(recv 0) 정리하나요?")


# ---- C09 ------------------------------------------------------------------

C09 = step("c09", "chunked request body 디코딩", includes=["c04", "c06", "c07", "c08"])


def post_chunked(chunks: bytes, close: bool = True) -> bytes:
    return request("POST", "/echo", [("Transfer-Encoding", "chunked")], chunks, close=close)


@check(C09, "chunked body 디코딩 (대소문자 hex, extension, trailer 포함)")
def c09_basic(server: Server) -> None:
    cases = [
        (b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n", b"hello world"),
        (b"A\r\n0123456789\r\na\r\nabcdefghij\r\n0\r\n\r\n", b"0123456789abcdefghij"),
        (b"3;name=value\r\nabc\r\n0\r\nX-Trailer: 1\r\n\r\n", b"abc"),
        (b"0\r\n\r\n", b""),
    ]
    for chunks, want in cases:
        resp = exchange(server, post_chunked(chunks))
        expect(resp.status == 200 and resp.body == want, f"chunked {chunks[:30]!r}: {resp.status} {resp.body!r} (기대 {want!r})")


@check(C09, "chunked body 뒤에 붙은 요청도 처리, 1바이트씩 와도 동작")
def c09_pipeline(server: Server) -> None:
    sock = connect(server)
    try:
        send_slowly(sock, post_chunked(b"2\r\nhi\r\n0\r\n\r\n", close=False) + request(), step=1, delay=0.002)
        reader = Reader(sock)
        a, b = reader.read(), reader.read()
        expect(a.body == b"hi" and b.status == 200, f"chunked 다음 요청 처리 실패: {a.status} {a.body!r} / {b.status}")
    finally:
        sock.close()


@check(C09, "잘못된 chunk → 400, 1MiB 초과 → 413, 'gzip, chunked' → 501")
def c09_errors(server: Server) -> None:
    expect_error_and_close(server, post_chunked(b"zz\r\nhello\r\n0\r\n\r\n"), 400, "hex가 아닌 chunk size")
    expect_error_and_close(server, post_chunked(b"5\r\nhelloXX0\r\n\r\n"), 400, "chunk data 뒤에 CRLF 없음")
    expect_error_and_close(server, post_chunked(b"\r\nhello\r\n0\r\n\r\n"), 400, "빈 chunk size")
    expect_error_and_close(server, post_chunked(b"ffffffffffffffffffff\r\n"), (400, 413), "overflow 나는 chunk size")
    big = b"".join(b"%x\r\n" % 65536 + b"a" * 65536 + b"\r\n" for _ in range(17))
    expect_error_and_close(server, post_chunked(big + b"0\r\n\r\n"), 413, "합계 1MiB 초과 chunked body")
    expect_error_and_close(
        server,
        request("POST", "/echo", [("Transfer-Encoding", "gzip, chunked")]),
        501,
        "Transfer-Encoding: gzip, chunked",
    )


@check(C09, "chunk 중간 EOF → 200 아님")
def c09_truncated(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(post_chunked(b"a\r\n01234"))
        sock.shutdown(socket.SHUT_WR)
        got = recv_until_eof(sock)
        expect(b" 200 " not in got.split(b"\r\n", 1)[0], "잘린 chunked body를 완성된 요청으로 처리함")
    finally:
        sock.close()


# ---- C10 ------------------------------------------------------------------

C10 = step("c10", "느린 클라이언트가 다른 클라이언트를 막지 않기 (thread-per-connection)", includes=["c04", "c06", "c07", "c08", "c09"])


def timed_get(server: Server, timeout: float) -> float:
    start = time.monotonic()
    resp = exchange(server, request(close=True), timeout=timeout)
    expect_status(resp, 200, "다른 클라이언트의 GET /")
    return time.monotonic() - start


@check(C10, "헤더를 반만 보내고 멈춘 클라이언트가 있어도 다른 GET은 1초 안에 응답")
def c10_slow_header(server: Server) -> None:
    slow = connect(server)
    try:
        slow.sendall(b"GET / HTTP/1.1\r\nHost: a\r\n")
        time.sleep(0.1)
        try:
            elapsed = timed_get(server, timeout=1.0)
        except CheckFailed as e:
            raise CheckFailed(
                f"느린 클라이언트 때문에 다른 요청이 막힘 ({e})",
                "단일 스레드 blocking 서버는 recv에서 멈춘 동안 다음 accept를 못 합니다. "
                "이게 이 단계에서 해결할 문제입니다.",
            )
        expect(elapsed < 1.0, f"다른 요청이 {elapsed:.2f}초 걸림")
    finally:
        slow.close()


@check(C10, "body를 천천히 보내는 클라이언트와 keep-alive로 놀고 있는 연결이 있어도 응답")
def c10_slow_body_idle(server: Server) -> None:
    slow = connect(server)
    idle = connect(server)
    try:
        slow.sendall(request("POST", "/echo", [("Content-Length", "100")], b"abc"))
        idle.sendall(request())
        Reader(idle).read()
        time.sleep(0.1)
        timed_get(server, timeout=1.0)
    finally:
        slow.close()
        idle.close()


@check(C10, "동시에 연결 100개가 요청해도 모두 5초 안에 200")
def c10_concurrent(server: Server) -> None:
    socks = [connect(server, timeout=5.0) for _ in range(100)]
    try:
        for s in socks:
            s.sendall(request(close=True))
        for i, s in enumerate(socks):
            expect_status(Reader(s).read(timeout=5.0), 200, f"동시 연결 {i}")
    finally:
        for s in socks:
            s.close()


# ---- C11 ------------------------------------------------------------------

C11 = step("c11", "epoll 이벤트 루프 — nonblocking 읽기 경로와 연결 상태", includes=["c04", "c06", "c07", "c08", "c09", "c10"])


def idle_cpu(server: Server, seconds: float = 1.0) -> float:
    before = server.cpu_seconds()
    time.sleep(seconds)
    return server.cpu_seconds() - before


@check(C11, "할 일이 없을 때 CPU를 쓰지 않음 (busy loop 없음)")
def c11_idle_cpu(server: Server) -> None:
    conns = [connect(server) for _ in range(5)]
    try:
        for s in conns:
            s.sendall(request())
            Reader(s).read()
        half = connect(server)
        half.sendall(b"GET / HTTP/1.1\r\n")
        conns.append(half)
        used = idle_cpu(server, 1.0)
        expect(
            used < 0.1,
            f"놀고 있는 1초 동안 CPU {used:.2f}초 사용",
            "보낼 데이터가 없는데 EPOLLOUT을 구독하고 있으면 LT 모드에서 매번 깨어납니다. "
            "epoll_wait timeout이 0인지도 확인하세요.",
        )
    finally:
        for s in conns:
            s.close()


@check(C11, "클라이언트 300번 접속·해제 후 fd 수가 늘지 않음 (epoll에서 지운 뒤 close)")
def c11_churn(server: Server) -> None:
    baseline = server.fd_count()
    for i in range(300):
        sock = connect(server)
        if i % 3 == 0:
            sock.sendall(request(close=True))
            Reader(sock).read()
        elif i % 3 == 1:
            sock.sendall(b"GET / HT")
        sock.close()
    after = stable_fd_count(server, baseline, 1)
    expect(after <= baseline + 1, f"fd 수가 {baseline} → {after}로 늘어남")


# ---- C12 ------------------------------------------------------------------

C12 = step("c12", "쓰기 경로 — EPOLLOUT, 출력 버퍼 한도, 안 읽는 클라이언트", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11"])


@check(C12, "1MiB 응답을 안 읽는 클라이언트가 있어도 다른 요청은 1초 안에 응답, 나중에 읽으면 전부 도착")
def c12_slow_reader(server: Server) -> None:
    body = random.Random(12).randbytes(MAX_BODY)
    slow = connect(server, rcvbuf=4096)
    try:
        sender = threading.Thread(target=lambda: slow.sendall(post_echo(body, close=False)), daemon=True)
        sender.start()
        time.sleep(0.5)
        timed_get(server, timeout=1.0)
        timed_get(server, timeout=1.0)
        resp = Reader(slow).read(timeout=10.0)
        expect(resp.body == body, "늦게 읽은 1MiB 응답 내용이 다름 (부분 송신 offset을 확인하세요)")
    finally:
        slow.close()


@check(C12, "응답을 안 읽으며 요청만 계속 보내는 클라이언트 → 서버 메모리가 무한히 늘지 않음")
def c12_backpressure(server: Server) -> None:
    rss_before = server.rss_kib()
    flood = connect(server, rcvbuf=4096)
    payload = post_echo(b"z" * 65536, close=False) * 16
    sent = 0
    offset = 0
    stop = time.monotonic() + 3.0
    flood.setblocking(False)
    try:
        while time.monotonic() < stop:
            try:
                n = flood.send(payload[offset:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            sent += n
            offset = (offset + n) % len(payload)
            # 한도 없는 구현이면 몇 초 만에 GB 단위로 늘어나므로 넘는 즉시 멈춘다
            if server.rss_kib() - rss_before > 32 * 1024:
                break
        grown = server.rss_kib() - rss_before
        expect(
            grown < 32 * 1024,
            f"클라이언트가 {sent // 1024}KiB를 밀어넣는 동안 서버 RSS가 {grown // 1024}MiB 늘어남",
            "출력 버퍼가 한도(예: 64KiB)를 넘으면 그 연결에서 읽기를 멈추세요 (EPOLLIN 해제).",
        )
        timed_get(server, timeout=1.0)
        used = idle_cpu(server, 1.0)
        expect(used < 0.1, f"막힌 연결이 있는 동안 1초에 CPU {used:.2f}초 사용 (busy loop)")
    finally:
        rst_close(flood)


# ---- C13 ------------------------------------------------------------------

C13 = step("c13", "시간 제한과 연결 수 제한", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11", "c12"])


def wait_fd_back(server: Server, baseline: int, timeout: float) -> bool:
    return wait_until(lambda: server.fd_count() <= baseline, timeout, 0.1)


@check(C13, "slowloris: 요청을 아주 느리게 보내면 첫 바이트 후 5초쯤 연결을 끊음")
def c13_slowloris(server: Server) -> None:
    sock = connect(server)
    start = time.monotonic()
    closed_at = None
    try:
        for b in request():
            try:
                sock.sendall(bytes([b]))
            except OSError:
                closed_at = time.monotonic()
                break
            sock.settimeout(0.5)
            try:
                if not sock.recv(65536):
                    closed_at = time.monotonic()
                    break
            except socket.timeout:
                pass
            except ConnectionResetError:
                closed_at = time.monotonic()
                break
            if time.monotonic() - start > 8:
                break
    finally:
        sock.close()
    expect(closed_at is not None, "8초 동안 1바이트씩 보내는 클라이언트를 끊지 않음", "요청 첫 바이트부터 5초 deadline을 두세요.")
    elapsed = closed_at - start
    expect(4.0 <= elapsed <= 6.5, f"{elapsed:.1f}초 만에 끊음 (계약: 5초)")


@check(C13, "keep-alive로 놀고 있는 연결은 5초쯤 뒤 닫음")
def c13_idle(server: Server) -> None:
    sock = connect(server)
    try:
        sock.sendall(request())
        Reader(sock).read()
        start = time.monotonic()
        expect(is_closed(sock, 7.0), "7초가 지나도 idle 연결을 닫지 않음")
        elapsed = time.monotonic() - start
        expect(4.0 <= elapsed <= 6.5, f"{elapsed:.1f}초 만에 닫음 (계약: 5초)")
    finally:
        sock.close()


@check(C13, "응답을 5초 넘게 전혀 안 읽는 클라이언트는 끊음 (write deadline)")
def c13_write_stall(server: Server) -> None:
    baseline = server.fd_count()
    sock = connect(server, rcvbuf=4096)
    try:
        sock.sendall(post_echo(b"w" * MAX_BODY, close=False))
        time.sleep(0.5)
        expect(server.fd_count() > baseline, "연결이 너무 일찍 정리됨")
        expect(wait_fd_back(server, baseline, 7.0), "7초가 지나도 안 읽는 연결의 fd가 정리되지 않음")
    finally:
        sock.close()


@check(C13, "동시 연결은 최대 256개 — 300개를 열어도 fd가 한도 안, 정리 후 정상 응답")
def c13_max_conns(server: Server) -> None:
    baseline = server.fd_count()
    socks = []
    try:
        for _ in range(300):
            try:
                socks.append(socket.create_connection(("127.0.0.1", server.port), timeout=2.0))
            except OSError:
                break
        time.sleep(0.5)
        count = server.fd_count()
        expect(count <= baseline + 256, f"연결 300개 시도 중 서버 fd가 {count}개 (baseline {baseline} + 한도 256 초과)")
        server.expect_alive("연결 폭주 중")
    finally:
        for s in socks:
            s.close()
    wait_fd_back(server, baseline, 3.0)
    expect_status(exchange(server, request(close=True)), 200, "연결 폭주가 끝난 뒤 GET /")


# ---- C14 ------------------------------------------------------------------

C14 = step("c14", "graceful shutdown — SIGTERM", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11", "c12", "c13"])


def refused(server: Server) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", server.port), timeout=0.5)
    except OSError:
        return True
    s.close()
    return False


@check(C14, "SIGTERM → 새 연결 거부, 진행 중 요청은 완료해서 응답, exit 0")
def c14_drain(server: Server) -> None:
    sock = connect(server)
    idle = connect(server)
    try:
        idle.sendall(request())
        Reader(idle).read()
        sock.sendall(request("POST", "/echo", [("Content-Length", "10")], b"01234"))
        time.sleep(0.2)
        os.kill(server.pid, signal.SIGTERM)
        expect(wait_until(lambda: refused(server), 1.0), "SIGTERM 후 1초가 지나도 새 연결을 받음", "먼저 listen socket을 닫으세요.")
        expect(is_closed(idle, 1.5), "SIGTERM 후 놀고 있던 keep-alive 연결을 닫지 않음")
        sock.sendall(b"56789")
        resp = Reader(sock).read()
        expect(resp.status == 200 and resp.body == b"0123456789", f"진행 중이던 요청이 완료되지 않음: {resp.status}")
        expect(resp.wants_close(), "종료 중 응답에는 Connection: close를 붙여야 함")
    finally:
        sock.close()
        idle.close()
    code = server.wait_exit(3.0)
    expect(code == 0, f"SIGTERM 후 exit 0으로 끝나지 않음: {describe_exit(code)}", server.log_tail())


@check(C14, "끝나지 않는 요청이 있어도 SIGTERM 후 5초 안에 exit 0")
def c14_deadline(server: Server) -> None:
    server.stop()
    fresh = Server(server.cmd, server.port)
    fresh.start()
    try:
        fresh.wait_ready()
        stuck = connect(fresh)
        stuck.sendall(b"POST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: 100\r\n\r\nabc")
        time.sleep(0.2)
        start = time.monotonic()
        os.kill(fresh.pid, signal.SIGTERM)
        code = fresh.wait_exit(6.0)
        elapsed = time.monotonic() - start
        stuck.close()
        expect(code is not None and elapsed <= 5.5, f"SIGTERM 후 {elapsed:.1f}초가 지나도 종료하지 않음", "drain에도 deadline이 필요합니다.")
        expect(code == 0, f"exit 코드 {describe_exit(code)}", fresh.log_tail())
    finally:
        fresh.stop()


# ---- Rust 단계 --------------------------------------------------------------

R01 = step("r01", "Rust 파서 포팅 — cargo test", mode="command")
R02 = step("r02", "Rust 동기 서버 (std::net + thread)", includes=["c04", "c06", "c07", "c08", "c09", "c10"])
R03 = step("r03", "Future를 직접 poll 해보기", mode="command")
R04 = step("r04", "Waker로 깨우는 작은 executor", mode="command")
R05 = step("r05", "epoll reactor + 직접 만든 runtime 위의 HTTP 서버", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11"])
R06 = step("r06", "Tokio 서버 — task, 한도, timeout", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11", "c12", "c13"])
R07 = step("r07", "Tokio graceful shutdown", includes=["c04", "c06", "c07", "c08", "c09", "c10", "c11", "c12", "c13", "c14"])
R02.default_cmd = "./rust/target/debug/sync_server"
R05.default_cmd = "./rust/target/debug/examples/r05_server"
R06.default_cmd = R07.default_cmd = "./rust/target/debug/scratch-http"


RUST_DIR = Path(os.environ.get("CHECK_RUST_DIR", ROOT / "rust"))


def cargo(*args: str) -> list[str]:
    return ["cargo", *args, "--manifest-path", str(RUST_DIR / "Cargo.toml")]


def run_r01(args: argparse.Namespace) -> int:
    cmd = cargo("test")
    print(color("2", "$ " + " ".join(cmd)))
    code = subprocess.call(cmd, timeout=300)
    print(color("32", "✔ cargo test 통과") if code == 0 else color("31", "✘ cargo test 실패"))
    return 0 if code == 0 else 1


def run_example(name: str, timeout: float) -> tuple[int, str, float, float]:
    build = cargo("build", "--example", name)
    if subprocess.call(build, timeout=300) != 0:
        return 1, "(빌드 실패)", 0.0, 0.0
    exe = RUST_DIR / "target/debug/examples" / name
    start = time.monotonic()
    usage_before = os.times()
    proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=timeout)
    usage_after = os.times()
    cpu = (usage_after.children_user - usage_before.children_user) + (
        usage_after.children_system - usage_before.children_system
    )
    return proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, cpu


def report(results: list[tuple[str, bool, str]]) -> int:
    failed = 0
    for name, ok, detail in results:
        if ok:
            print(color("32", "✔"), name)
        else:
            failed += 1
            print(color("31", "✘"), name)
            if detail:
                print("   ", detail.replace("\n", "\n    "))
    return 1 if failed else 0


def run_r03(args: argparse.Namespace) -> int:
    code, out, _, _ = run_example("r03_countdown", 30)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    polls = [l for l in lines if l.startswith("poll")]
    return report(
        [
            ("r03_countdown 이 exit 0으로 끝남", code == 0, out[-500:]),
            (
                "poll 로그 4줄: Pending 3번 후 Ready(\"liftoff\")",
                polls == ["poll 1: Pending", "poll 2: Pending", "poll 3: Pending", 'poll 4: Ready("liftoff")'],
                "출력:\n" + "\n".join(lines[:12]),
            ),
            (
                "Future를 만들기만 하고 poll 하지 않으면 아무것도 실행되지 않음 (lazy)",
                "created" in lines and lines.index("created") < (lines.index(polls[0]) if polls else 0),
                "Future를 만든 직후 'created'를 출력하고, 그 전에 다른 출력이 없어야 합니다.",
            ),
        ]
    )


def run_r04(args: argparse.Namespace) -> int:
    code, out, elapsed, cpu = run_example("r04_executor", 30)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    polls = next((l for l in lines if l.startswith("total polls:")), "")
    n = int(polls.split(":")[1]) if polls and polls.split(":")[1].strip().isdigit() else -1
    return report(
        [
            ("r04_executor 가 exit 0으로 끝남", code == 0, out[-500:]),
            ("타이머 3개(100/200/300ms)가 순서대로 끝남", [l for l in lines if l.startswith("timer")] == ["timer 100ms done", "timer 200ms done", "timer 300ms done"], "\n".join(lines[:10])),
            ("전체 실행 시간 0.3초 이상 0.55초 미만 (동시에 기다림)", 0.3 <= elapsed < 0.55, f"{elapsed:.2f}초"),
            ("busy polling 없음: total polls ≤ 9", 0 < n <= 9, f"'{polls}' — 깨어난 task만 다시 poll 해야 합니다."),
            ("CPU 사용 0.1초 미만 (idle에서 park)", cpu < 0.1, f"CPU {cpu:.2f}초"),
        ]
    )


R01.command = run_r01
R03.command = run_r03
R04.command = run_r04


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------


def collect_checks(s: Step, only: bool) -> list[tuple[str, Check]]:
    # Rust 서버 단계(r02 등)는 자기 검사 없이 C 검사를 재사용하므로 --only가 의미 없다
    keys = [s.key] if only and s.checks else [*s.includes, s.key]
    out = []
    for key in keys:
        for c in STEPS[key].checks:
            out.append((key, c))
    return out


def run_server_step(s: Step, args: argparse.Namespace) -> int:
    cmd = args.cmd or [s.default_cmd]
    checks = collect_checks(s, args.only)
    if not checks:
        print(f"{s.key}: 서버 검사가 없습니다.")
        return 1
    print(color("1", f"[{s.key}] {s.title}") + color("2", f"   ({' '.join(cmd)}, port {args.port})"))
    failed = 0
    server = None
    # Ctrl-C 등으로 중간에 끝나도 서버(별도 세션)가 8080을 붙잡고 남지 않도록 항상 정리
    try:
        for key, c in checks:
            # oneshot 단계는 검사마다 새로 띄운다. 서버형 단계는 한 번 띄워서 재사용하되 죽으면 다시 띄운다.
            if server is None or s.mode == "oneshot" or not server.alive() or STEPS[key].mode == "oneshot":
                if server is not None:
                    server.stop()
                server = Server(cmd, args.port)
                try:
                    server.start()
                    if s.mode != "oneshot":
                        server.wait_ready()
                except CheckFailed as e:
                    print(color("31", "✘ 서버 시작 실패:"), e)
                    if e.hint:
                        print("   ", e.hint.replace("\n", "\n    "))
                    server.stop()
                    return 1
            label = f"{key.upper()} {c.name}"
            start = time.monotonic()
            try:
                c.fn(server)
                print(color("32", "✔"), label, color("2", f"({time.monotonic() - start:.1f}s)"))
            except CheckFailed as e:
                failed += 1
                print(color("31", "✘"), label)
                print("   ", str(e))
                if e.hint:
                    print("   ", color("33", e.hint.replace("\n", "\n    ")))
            except (OSError, socket.timeout) as e:
                failed += 1
                print(color("31", "✘"), label)
                print("   ", f"소켓 오류: {e!r}")
                if not server.alive():
                    print("   ", server.log_tail())
    finally:
        if server is not None:
            server.stop()
    total = len(checks)
    summary = f"{total - failed}/{total} 통과"
    print(color("32" if failed == 0 else "31", summary))
    return 0 if failed == 0 else 1


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", nargs="?", help="c01..c14, r01..r07")
    parser.add_argument("--only", action="store_true", help="이전 단계 회귀 검사 없이 이 단계 검사만 실행")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--list", action="store_true", help="단계 목록")
    argv = sys.argv[1:]
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1 :]
    args = parser.parse_args(argv)
    args.cmd = cmd

    if args.list or not args.step:
        for s in STEPS.values():
            extra = f" ({len(s.checks)} checks)" if s.checks else ""
            print(f"  {s.key}  {s.title}{extra}")
        return 0
    s = STEPS.get(args.step.lower())
    if s is None:
        print(f"알 수 없는 단계: {args.step}. --list 로 확인하세요.")
        return 2
    if s.command is not None:
        return s.command(args)
    return run_server_step(s, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
