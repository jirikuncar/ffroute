.PHONY: help
help:
	@echo "Available targets:"
	@echo ""
	@echo "Development:"
	@echo "  install            - Install dev dependencies with uv"
	@echo "  build              - Build the Rust extension (dev mode)"
	@echo "  build-release      - Build the Rust extension (release mode)"
	@echo "  pre-commit-install - Install prek-managed pre-commit hooks"
	@echo "  pre-commit         - Run prek on all files"
	@echo ""
	@echo "Quality:"
	@echo "  lint               - Run ruff check + clippy"
	@echo "  format             - Run ruff format + cargo fmt"
	@echo "  check              - Run ruff check + ruff format check + clippy + cargo fmt check"
	@echo ""
	@echo "Testing:"
	@echo "  test               - Run pytest"
	@echo ""
	@echo "Release:"
	@echo "  sdist              - Build the source distribution"
	@echo "  wheel              - Build a wheel for the current platform"

.PHONY: install
install:
	uv sync --all-groups

.PHONY: build
build:
	uv run --no-sync maturin develop

.PHONY: build-release
build-release:
	uv run --no-sync maturin develop --release

.PHONY: pre-commit-install
pre-commit-install:
	uv run prek install

.PHONY: pre-commit
pre-commit:
	uv run prek run --all-files

.PHONY: lint
lint:
	uv run ruff check --fix .
	cargo clippy --all-targets -- -D warnings

.PHONY: format
format:
	uv run ruff format .
	cargo fmt --all

.PHONY: check
check:
	uv run ruff check .
	uv run ruff format --check .
	cargo fmt --all -- --check
	cargo clippy --all-targets -- -D warnings

.PHONY: test
test:
	uv run pytest

.PHONY: sdist
sdist:
	uv run maturin sdist --out dist

.PHONY: wheel
wheel:
	uv run maturin build --release --out dist
