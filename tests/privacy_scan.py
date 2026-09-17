#!/usr/bin/env python3
"""Fail when repository files contain private deployment data or likely secrets."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIXES = {
    ".7z",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".tar",
    ".webp",
    ".xlsx",
    ".zip",
}

FORBIDDEN_FILES = [
    re.compile(r"(^|/)\.env(?:\.|$)"),
    re.compile(r"(^|/)(?![^/]*\.example$)[^/]*\.conf$"),
    re.compile(r"(^|/)[^/]*(?:\.bak(?:-|$)|\.pre-|state\.json$)"),
]

CONTENT_RULES = {
    "private IPv4 address": re.compile(
        r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9.])"
    ),
    "known deployment identity": re.compile(
        r"Invader3256|NASFN|DESKTOP-2IAPQK8|"
        r"/home/Invader3256|/vol3/1000|vpn\.btdnode\.top|nas\.btdnode\.top",
        re.IGNORECASE,
    ),
    "hard-coded personal server": re.compile(
        r"DEFAULT_SERVER_NAME\s*=\s*[\"']铁底湾[\"']|--server-name(?:=|\s+)铁底湾"
    ),
    "literal Windows user directory": re.compile(
        r"[A-Za-z]:\\Users\\(?!<|%|\$env:)[^\\\s\"']+", re.IGNORECASE
    ),
    "email address": re.compile(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
    ),
}

ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)"
    r"\s*=\s*(?P<value>[^#\r\n]+)"
)
SENSITIVE_NAME = re.compile(
    r"(?:^|_)(?:PASS(?:WORD)?|TOKEN|SECRET|API_KEY|AUTH)(?:_|$)"
)

ALLOWED_SECRET_VALUES = {
    "",
    '""',
    "''",
    '"change-me"',
    "'change-me'",
    '"not-a-secret"',
    "'not-a-secret'",
    '"example"',
    "'example'",
}


def text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    failures: list[str] = []
    for path in text_files():
        if path.resolve() == Path(__file__).resolve():
            continue
        relative = path.relative_to(ROOT).as_posix()
        for pattern in FORBIDDEN_FILES:
            if pattern.search(relative):
                failures.append(f"{relative}: forbidden file type")

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for label, pattern in CONTENT_RULES.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{relative}:{line}: {label}")

        for match in ASSIGNMENT.finditer(text):
            if not SENSITIVE_NAME.search(match.group("name")):
                continue
            value = match.group("value").strip()
            if value in ALLOWED_SECRET_VALUES or value.startswith(("${", "$env:")):
                continue
            line = text.count("\n", 0, match.start()) + 1
            failures.append(
                f"{relative}:{line}: possible literal secret in {match.group('name')}"
            )

    if failures:
        print("Privacy scan failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Privacy scan passed: {len(text_files())} text files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
