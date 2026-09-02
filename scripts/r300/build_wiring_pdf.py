#!/usr/bin/env python3
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


def browser() -> Path:
    for candidate in BROWSERS:
        if candidate.is_file():
            return candidate
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise SystemExit("Edge/Chrome/Chromium is required to build the wiring PDF")


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
