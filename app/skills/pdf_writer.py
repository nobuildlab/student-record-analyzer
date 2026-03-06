"""Playwright HTML → PDF 변환 (subprocess 방식 — Streamlit asyncio 충돌 우회)"""

import subprocess
import sys
import tempfile
from pathlib import Path


def html_to_pdf(html_content: str, output_path: str) -> str:
    """HTML 문자열을 PDF로 변환한다.

    Streamlit + Playwright sync API 충돌(NotImplementedError) 우회를 위해
    별도 subprocess로 실행한다.

    Args:
        html_content: 완성된 HTML 문자열
        output_path: 출력 PDF 경로

    Returns:
        생성된 PDF 파일 경로
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # HTML을 임시 파일에 저장
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(html_content)
        html_file = f.name

    # 별도 프로세스에서 Playwright 실행
    script = f"""
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto('file:///{html_file.replace(chr(92), "/")}', wait_until='networkidle')
    page.pdf(
        path=r'{output_path}',
        format='A4',
        margin={{'top': '20mm', 'bottom': '20mm', 'left': '15mm', 'right': '15mm'}},
        print_background=True,
    )
    browser.close()
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )

    # 임시 HTML 파일 삭제
    try:
        Path(html_file).unlink()
    except Exception:
        pass

    if result.returncode != 0:
        raise RuntimeError(f"PDF 생성 실패:\n{result.stderr}")

    return output_path
