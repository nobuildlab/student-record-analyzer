"""PDF → 텍스트 추출 에이전트

PDF에서 텍스트를 추출하고 정제하여 반환한다.
이름, 학교명 자동 감지 포함.
"""

import re
from skills.pdf_reader import extract_text_from_pdf


def extract_transcript(pdf_path: str) -> dict:
    """생기부 PDF에서 텍스트를 추출하고 정제한다.

    Returns:
        {
            "name": str | None,     # 자동 감지된 이름 (없으면 None)
            "school": str | None,   # 자동 감지된 학교명
            "raw_text": str,        # 정제된 전체 텍스트
        }
    """
    raw = extract_text_from_pdf(pdf_path)
    cleaned = _clean_text(raw)
    name = _detect_name(cleaned)
    school = _detect_school(cleaned)

    return {
        "name": name,
        "school": school,
        "raw_text": cleaned,
    }


def _clean_text(text: str) -> str:
    """불필요한 공백, 헤더/푸터, 반복 줄바꿈 제거."""
    # 연속 공백 → 단일 공백
    text = re.sub(r"[ \t]+", " ", text)
    # 3줄 이상 연속 빈 줄 → 2줄로
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 페이지 번호 패턴 제거 (예: "- 1 -", "1/15" 등)
    text = re.sub(r"\n\s*-?\s*\d+\s*-?\s*\n", "\n", text)
    text = re.sub(r"\n\s*\d+\s*/\s*\d+\s*\n", "\n", text)
    return text.strip()


def _detect_name(text: str) -> str | None:
    """생기부 텍스트에서 학생 이름을 자동 감지."""
    # 패턴 1: "성명: 홍길동" 또는 "이름: 홍길동"
    match = re.search(r"(?:성명|이름)\s*[:：]\s*([가-힣]{2,4})", text)
    if match:
        return match.group(1)
    # 패턴 2: "학생 홍길동" 패턴
    match = re.search(r"학생\s+([가-힣]{2,4})", text)
    if match:
        return match.group(1)
    return None


def _detect_school(text: str) -> str | None:
    """생기부 텍스트에서 학교명을 자동 감지."""
    # "○○고등학교" 또는 "○○고"
    match = re.search(r"([가-힣]+(?:고등학교|고))", text)
    if match:
        return match.group(1)
    return None
