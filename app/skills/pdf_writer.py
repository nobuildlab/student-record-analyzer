"""WeasyPrint HTML → PDF 변환"""

from pathlib import Path
from weasyprint import HTML


def html_to_pdf(html_content: str, output_path: str) -> str:
    """HTML 문자열을 PDF로 변환한다.

    Args:
        html_content: 완성된 HTML 문자열
        output_path: 출력 PDF 경로

    Returns:
        생성된 PDF 파일 경로
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_content).write_pdf(output_path)
    return output_path
