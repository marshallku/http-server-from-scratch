/*
 * C05 parser unit tests. c/src/http_parser.h 계약(workbook/c/05-parser.md)을
 * 구현한 c/src/http_parser.c 와 링크해서 실행한다.
 *
 *   python3 workbook/check.py c05
 *
 * 테스트 하나가 실패하면 어떤 입력에서 무엇을 기대했는지 출력한다.
 */
#include "http_parser.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;
static int checks = 0;

/* req 안의 slice가 입력 버퍼를 가리키므로, 버퍼는 테스트 끝에 한꺼번에 해제 */
static void *buffers[1024];
static size_t buffer_count = 0;

#define EXPECT(cond, ...)                                                      \
    do {                                                                       \
	checks++;                                                              \
	if (!(cond)) {                                                         \
	    failures++;                                                        \
	    printf("FAIL %s:%d: ", __func__, __LINE__);                        \
	    printf(__VA_ARGS__);                                               \
	    printf("\n");                                                      \
	}                                                                      \
    } while (0)

static int slice_eq(struct slice s, const char *expected)
{
    size_t n = strlen(expected);
    return s.len == n && memcmp(s.ptr, expected, n) == 0;
}

static enum parse_result parse(const char *buf, size_t len,
			       struct http_request *req, size_t *head_len,
			       int *status)
{
    *head_len = 0;
    *status = 0;
    /* 입력을 정확한 길이의 heap 버퍼로 복사해서, 끝을 넘어 읽으면 ASan이
     * 잡도록 한다. NUL 종료를 가정한 구현도 여기서 드러난다. */
    char *copy = malloc(len ? len : 1);
    if (copy == NULL) {
	perror("malloc");
	exit(2);
    }
    memcpy(copy, buf, len);
    if (buffer_count == sizeof(buffers) / sizeof(buffers[0])) {
	fprintf(stderr, "too many buffers\n");
	exit(2);
    }
    buffers[buffer_count++] = copy;
    return http_parse_head(copy, len, req, head_len, status);
}

static void expect_error(const char *name, const char *raw, size_t len,
			 int status)
{
    struct http_request req;
    size_t head_len;
    int got;
    enum parse_result r = parse(raw, len, &req, &head_len, &got);
    EXPECT(r == PARSE_ERROR && got == status,
	   "[%s] expected PARSE_ERROR %d, got result=%d status=%d", name,
	   status, (int)r, got);
}

static void test_simple_get(void)
{
    const char *raw = "GET /hello?x=1 HTTP/1.1\r\n"
		      "Host: example.com\r\n"
		      "User-Agent:   curl/8  \r\n"
		      "Empty:\r\n"
		      "\r\n";
    struct http_request req;
    size_t head_len;
    int status;
    enum parse_result r = parse(raw, strlen(raw), &req, &head_len, &status);

    EXPECT(r == PARSE_DONE, "result=%d status=%d", (int)r, status);
    if (r != PARSE_DONE)
	return;
    EXPECT(head_len == strlen(raw), "head_len=%zu want %zu", head_len,
	   strlen(raw));
    EXPECT(slice_eq(req.method, "GET"), "method");
    EXPECT(slice_eq(req.target, "/hello?x=1"), "target");
    EXPECT(slice_eq(req.path, "/hello"), "path must stop before '?'");
    EXPECT(req.minor_version == 1, "minor_version=%d", req.minor_version);
    EXPECT(req.header_count == 3, "header_count=%zu", req.header_count);
    if (req.header_count != 3)
	return;
    EXPECT(slice_eq(req.headers[0].name, "Host"), "header[0].name");
    EXPECT(slice_eq(req.headers[0].value, "example.com"), "header[0].value");
    EXPECT(slice_eq(req.headers[1].value, "curl/8"),
	   "OWS around value must be trimmed");
    EXPECT(slice_eq(req.headers[2].name, "Empty") &&
	       req.headers[2].value.len == 0,
	   "empty value is allowed");
}

static void test_http10(void)
{
    const char *raw = "HEAD / HTTP/1.0\r\n\r\n";
    struct http_request req;
    size_t head_len;
    int status;
    enum parse_result r = parse(raw, strlen(raw), &req, &head_len, &status);
    EXPECT(r == PARSE_DONE && req.minor_version == 0 && req.header_count == 0 &&
	       slice_eq(req.method, "HEAD"),
	   "HTTP/1.0 without headers: result=%d", (int)r);
}

static void test_every_prefix_needs_more(void)
{
    const char *raw = "POST /echo HTTP/1.1\r\n"
		      "Host: a\r\n"
		      "Content-Length: 5\r\n"
		      "\r\n";
    size_t n = strlen(raw);
    for (size_t i = 0; i < n; i++) {
	struct http_request req;
	size_t head_len;
	int status;
	enum parse_result r = parse(raw, i, &req, &head_len, &status);
	EXPECT(r == PARSE_NEED_MORE,
	       "prefix of %zu/%zu bytes must be NEED_MORE, got %d "
	       "(status=%d)",
	       i, n, (int)r, status);
    }
    struct http_request req;
    size_t head_len;
    int status;
    EXPECT(parse(raw, n, &req, &head_len, &status) == PARSE_DONE &&
	       head_len == n,
	   "full head must be DONE");
}

static void test_trailing_bytes_not_consumed(void)
{
    const char *raw = "POST /echo HTTP/1.1\r\nHost: a\r\nContent-Length: "
		      "5\r\n\r\nhelloGET / HTTP/1.1\r\n";
    size_t head = strlen("POST /echo HTTP/1.1\r\nHost: a\r\nContent-"
			 "Length: 5\r\n\r\n");
    struct http_request req;
    size_t head_len;
    int status;
    enum parse_result r = parse(raw, strlen(raw), &req, &head_len, &status);
    EXPECT(r == PARSE_DONE && head_len == head,
	   "head_len must stop at the blank line: got %zu want %zu", head_len,
	   head);
}

static void test_binary_safe(void)
{
    /* 헤더 뒤의 NUL은 body일 뿐, head 파싱에 영향을 주면 안 된다 */
    const char raw[] = "GET / HTTP/1.1\r\nHost: a\r\n\r\n\0\0\0";
    struct http_request req;
    size_t head_len;
    int status;
    enum parse_result r = parse(raw, sizeof(raw) - 1, &req, &head_len, &status);
    EXPECT(r == PARSE_DONE && head_len == sizeof(raw) - 4,
	   "NUL after head: result=%d head_len=%zu", (int)r, head_len);

    const char nul_in_target[] = "GET /a\0b HTTP/1.1\r\nHost: a\r\n\r\n";
    expect_error("NUL in target", nul_in_target, sizeof(nul_in_target) - 1,
		 400);
}

static void test_syntax_errors(void)
{
    struct {
	const char *name;
	const char *raw;
	int status;
    } cases[] = {
	{"empty request line", "\r\n\r\n", 400},
	{"missing target", "GET HTTP/1.1\r\n\r\n", 400},
	{"double space", "GET  / HTTP/1.1\r\n\r\n", 400},
	{"lowercase version", "GET / http/1.1\r\n\r\n", 400},
	{"garbage version", "GET / HTTP/1\r\n\r\n", 400},
	{"unsupported version", "GET / HTTP/2.0\r\n\r\n", 505},
	{"unsupported minor", "GET / HTTP/1.2\r\n\r\n", 505},
	{"absolute-form target", "GET http://a/ HTTP/1.1\r\n\r\n", 400},
	{"bad method char", "G(T / HTTP/1.1\r\n\r\n", 400},
	{"bare LF", "GET / HTTP/1.1\nHost: a\n\n", 400},
	{"bare LF in header", "GET / HTTP/1.1\r\nHost: a\n\r\n", 400},
	{"space before colon", "GET / HTTP/1.1\r\nHost : a\r\n\r\n", 400},
	{"no colon", "GET / HTTP/1.1\r\nHost\r\n\r\n", 400},
	{"empty name", "GET / HTTP/1.1\r\n: a\r\n\r\n", 400},
	{"obs-fold", "GET / HTTP/1.1\r\nX: a\r\n b\r\n\r\n", 400},
	{"control char in value", "GET / HTTP/1.1\r\nX: a\x01\r\n\r\n", 400},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
	expect_error(cases[i].name, cases[i].raw, strlen(cases[i].raw),
		     cases[i].status);
}

/* head 전체(마지막 \r\n\r\n 포함) 길이가 정확히 total 바이트인 요청 */
static char *head_of_size(size_t total)
{
    const char *prefix = "GET / HTTP/1.1\r\nX: ";
    const char *suffix = "\r\n\r\n";
    size_t fill = total - strlen(prefix) - strlen(suffix);
    char *buf = malloc(total);
    if (buf == NULL)
	exit(2);
    memcpy(buf, prefix, strlen(prefix));
    memset(buf + strlen(prefix), 'a', fill);
    memcpy(buf + total - strlen(suffix), suffix, strlen(suffix));
    return buf;
}

static void test_head_size_limit(void)
{
    struct http_request req;
    size_t head_len;
    int status;

    char *ok = head_of_size(HTTP_MAX_HEAD_BYTES);
    enum parse_result r =
	parse(ok, HTTP_MAX_HEAD_BYTES, &req, &head_len, &status);
    EXPECT(r == PARSE_DONE,
	   "head of exactly %d bytes must be accepted, "
	   "got result=%d status=%d",
	   HTTP_MAX_HEAD_BYTES, (int)r, status);

    char *big = head_of_size(HTTP_MAX_HEAD_BYTES + 1);
    r = parse(big, HTTP_MAX_HEAD_BYTES + 1, &req, &head_len, &status);
    EXPECT(r == PARSE_ERROR && status == 431,
	   "head of %d bytes must be 431, got result=%d status=%d",
	   HTTP_MAX_HEAD_BYTES + 1, (int)r, status);

    /* 아직 끝이 안 왔더라도 한도만큼 쌓였으면 더 기다리지 말고 431 */
    r = parse(big, HTTP_MAX_HEAD_BYTES, &req, &head_len, &status);
    EXPECT(r == PARSE_ERROR && status == 431,
	   "%d bytes without end of head must already be 431, got "
	   "result=%d",
	   HTTP_MAX_HEAD_BYTES, (int)r);
    r = parse(big, HTTP_MAX_HEAD_BYTES - 1, &req, &head_len, &status);
    EXPECT(r == PARSE_NEED_MORE, "%d bytes is still under the limit",
	   HTTP_MAX_HEAD_BYTES - 1);
    free(ok);
    free(big);
}

static void test_header_count_limit(void)
{
    char buf[4096];
    for (int count = HTTP_MAX_HEADERS; count <= HTTP_MAX_HEADERS + 1; count++) {
	size_t n = (size_t)snprintf(buf, sizeof(buf), "GET / HTTP/1.1\r\n");
	for (int i = 0; i < count; i++)
	    n += (size_t)snprintf(buf + n, sizeof(buf) - n, "H%d: v\r\n", i);
	n += (size_t)snprintf(buf + n, sizeof(buf) - n, "\r\n");

	struct http_request req;
	size_t head_len;
	int status;
	enum parse_result r = parse(buf, n, &req, &head_len, &status);
	if (count == HTTP_MAX_HEADERS)
	    EXPECT(r == PARSE_DONE && req.header_count == HTTP_MAX_HEADERS,
		   "%d headers must be accepted", count);
	else
	    EXPECT(r == PARSE_ERROR && status == 431,
		   "%d headers must be 431, got result=%d status=%d", count,
		   (int)r, status);
    }
}

int main(void)
{
    test_simple_get();
    test_http10();
    test_every_prefix_needs_more();
    test_trailing_bytes_not_consumed();
    test_binary_safe();
    test_syntax_errors();
    test_head_size_limit();
    test_header_count_limit();

    for (size_t i = 0; i < buffer_count; i++)
	free(buffers[i]);
    printf("%d/%d checks passed\n", checks - failures, checks);
    return failures == 0 ? 0 : 1;
}
