.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

CC ?= cc
CARGO ?= cargo
BASE_CPPFLAGS := -D_POSIX_C_SOURCE=200809L
BASE_CFLAGS := -std=c17 -Wall -Wextra -Wpedantic -Werror
CPPFLAGS ?=
CFLAGS ?= -O0 -g3
LDFLAGS ?=
LDLIBS ?=
C_SOURCES := $(wildcard c/src/*.c)
C_OBJECTS := $(patsubst c/src/%.c,build/c/%.o,$(C_SOURCES))
C_BINARY := build/c/http-server
SAN_BINARY := build/sanitize/http-server
SAN_FLAGS := -fsanitize=address,undefined -fno-omit-frame-pointer
RUST_MANIFEST := rust/Cargo.toml

.PHONY: help doctor build c rust run-c run-rust check check-c check-rust fmt sanitize clean

help:
	@printf '%s\n' \
	  'doctor     Check required local tools' \
	  'build      Build C and Rust scaffolds' \
	  'run-c      Run C scaffold (no server yet)' \
	  'run-rust   Run Rust scaffold (no server yet)' \
	  'check      Build, format/lint checks, scaffold smoke runs' \
	  'fmt        Format C and Rust sources' \
	  'sanitize   Build/run C scaffold with ASan + UBSan' \
	  'clean      Remove generated build artifacts'

doctor:
	@set -eu; for tool in $(CC) $(CARGO) rustc clang-format; do command -v "$$tool"; done
	@$(CARGO) fmt --version
	@$(CARGO) clippy --version

build: c rust

c: $(C_BINARY)

$(C_BINARY): $(C_OBJECTS)
	$(CC) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/c/%.o: c/src/%.c Makefile
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(BASE_CPPFLAGS) $(CFLAGS) $(BASE_CFLAGS) -MMD -MP -c $< -o $@

-include $(C_OBJECTS:.o=.d)

rust:
	$(CARGO) build --locked --offline --manifest-path $(RUST_MANIFEST)

run-c: c
	./$(C_BINARY)

run-rust:
	$(CARGO) run --locked --offline --manifest-path $(RUST_MANIFEST)

check: check-c check-rust
	@printf '%s\n' 'Scaffold checks passed. HTTP/unit test suites are not implemented yet.'

check-c: c
	clang-format --dry-run --Werror $(C_SOURCES) $(wildcard c/src/*.h)
	./$(C_BINARY)

check-rust:
	$(CARGO) fmt --manifest-path $(RUST_MANIFEST) -- --check
	$(CARGO) clippy --locked --offline --manifest-path $(RUST_MANIFEST) --all-targets -- -D warnings
	$(CARGO) run --locked --offline --manifest-path $(RUST_MANIFEST)

fmt:
	clang-format -i $(C_SOURCES) $(wildcard c/src/*.h)
	$(CARGO) fmt --manifest-path $(RUST_MANIFEST)

sanitize:
	@mkdir -p build/sanitize
	$(CC) $(CPPFLAGS) $(BASE_CPPFLAGS) $(CFLAGS) $(BASE_CFLAGS) $(SAN_FLAGS) $(C_SOURCES) $(LDFLAGS) $(LDLIBS) -o $(SAN_BINARY)
	./$(SAN_BINARY)

clean:
	rm -rf build
	$(CARGO) clean --manifest-path $(RUST_MANIFEST)
