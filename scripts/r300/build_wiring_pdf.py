#!/usr/bin/env python3
# [보조 도구] 한국어 배선 가이드(HTML)를 PDF로 변환한다.
# - 역할: 설치된 Edge/Chrome/Chromium을 headless로 실행해 docs/interface/R300_배선_가이드.html 을 PDF로 인쇄한다.
# - 읽기 순서: 핵심 읽기 순서(1~13번) 밖의 보조 도구다. 첫 읽기에서는 건너뛰어도 된다.
# - 다음에 볼 곳: docs/interface/R300_배선_가이드.html (원본), 배선이 지키는 안전 로직은 firmware/avr/r300_safety.c
"""Build the Korean R300 wiring guide with an installed Chromium browser."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "interface" / "R300_배선_가이드.html"
OUTPUT = ROOT / "docs" / "interface" / "R300_배선_가이드.pdf"
BROWSERS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


# PDF를 만들 브라우저를 찾는다: 먼저 Windows 설치 경로(BROWSERS), 없으면 PATH 검색.
# 하나도 없으면 SystemExit로 안내 메시지를 내고 끝낸다.
def browser() -> Path:
    for candidate in BROWSERS:
        if candidate.is_file():
            return candidate
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise SystemExit("Edge/Chrome/Chromium is required to build the wiring PDF")


# headless 브라우저로 PDF를 만든 뒤 결과를 검사한다.
# 검사: '%PDF-' 로 시작, 50,000바이트 이상, 페이지 수 6장(페이지 수를 셀 수 있을 때만).
# 가이드 분량이 바뀌면 여기의 6장 기대값도 같이 고쳐야 한다.
def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"missing source: {SOURCE}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r300-wiring-pdf-") as profile:
        command = [
            str(browser()),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={OUTPUT}",
            SOURCE.as_uri(),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
        if result.returncode:
            raise SystemExit(result.stderr.strip() or f"browser exited {result.returncode}")

    pdf = OUTPUT.read_bytes()
    if not pdf.startswith(b"%PDF-") or len(pdf) < 50_000:
        raise SystemExit(f"invalid or unexpectedly small PDF: {len(pdf)} bytes")
    page_count = len(re.findall(rb"/Type\s*/Page\b", pdf))
    if page_count and page_count != 6:
        raise SystemExit(f"expected 6 PDF pages, got {page_count}")
    print(f"[wiring-pdf] PASS | file={OUTPUT.relative_to(ROOT)} pages={page_count or 'unknown'} bytes={len(pdf)}")


if __name__ == "__main__":
    main()
