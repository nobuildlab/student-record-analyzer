"""Jinja2 HTML 렌더링

ASCII box-drawing 테이블 → 마크다운 테이블 자동 변환 포함.
"""

import re
from pathlib import Path
import markdown
from jinja2 import Environment, FileSystemLoader
from config import TEMPLATE_DIR


def render_report_html(
    parts: dict[int, str],
    student_name: str,
    major: str,
) -> str:
    """PART 1~6 텍스트를 HTML 리포트로 렌더링한다.

    Args:
        parts: {1: "PART 1 텍스트", 2: "PART 2 텍스트", ...}
        student_name: 학생 이름
        major: 희망 전공

    Returns:
        완성된 HTML 문자열
    """
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("report.html")

    # 각 PART: 전처리 → 마크다운 → HTML
    parts_html = {}
    for part_num, text in parts.items():
        preprocessed = _preprocess_markdown(text)
        parts_html[part_num] = markdown.markdown(
            preprocessed,
            extensions=["tables", "fenced_code", "nl2br"],
        )

    # CSS 로드
    css_path = TEMPLATE_DIR / "styles.css"
    css_content = css_path.read_text(encoding="utf-8") if css_path.exists() else ""

    return template.render(
        student_name=student_name,
        major=major,
        parts=parts_html,
        css=css_content,
    )


# ═══════════════════════════════════════════════════════
#  전처리: ASCII 테이블 변환 + 장식선 제거
# ═══════════════════════════════════════════════════════

def _preprocess_markdown(text: str) -> str:
    """마크다운 변환 전 전처리: ASCII 테이블 → 마크다운 테이블, 장식선 제거."""
    text = _convert_ascii_tables(text)
    text = _clean_decorative_lines(text)
    return text


# ── ASCII 테이블 → 마크다운 테이블 ──

def _convert_ascii_tables(text: str) -> str:
    """Box-drawing 문자(│┌┐└┘├┤┬┴┼─)로 된 테이블을 마크다운 파이프 테이블로 변환."""
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        if _is_box_border(lines[i]) or _is_box_data_row(lines[i]):
            # 연속된 테이블 줄 수집
            table_block = []
            while i < len(lines) and (
                _is_box_border(lines[i]) or _is_box_data_row(lines[i])
            ):
                table_block.append(lines[i])
                i += 1

            md_table = _box_block_to_markdown(table_block)
            if md_table:
                result.append("")     # 테이블 앞 빈 줄 (마크다운 파싱 보장)
                result.append(md_table)
                result.append("")     # 테이블 뒤 빈 줄
            else:
                # 변환 실패 → 원본 유지
                result.extend(table_block)
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _is_box_border(line: str) -> bool:
    """테이블 테두리 줄인지 확인 (┌─┬─┐, ├─┼─┤, └─┴─┘, ╔═╗ 등)."""
    s = line.strip()
    if len(s) < 3:
        return False
    border_chars = set("┌┐└┘├┤┬┴┼─╔╗╚╝╠╣╦╩╬═ ")
    return all(c in border_chars for c in s)


def _is_box_data_row(line: str) -> bool:
    """테이블 데이터 행인지 확인 (│ ... │ 또는 ║ ... ║)."""
    s = line.strip()
    return (
        (s.startswith("│") and s.endswith("│"))
        or (s.startswith("║") and s.endswith("║"))
    )


def _box_block_to_markdown(block: list[str]) -> str:
    """Box-drawing 테이블 블록 → 마크다운 파이프 테이블 문자열."""
    # 데이터 행만 추출
    data_rows = []
    for line in block:
        if not _is_box_data_row(line):
            continue
        cells = re.split(r"[│║]", line)
        # 앞뒤 빈 문자열 제거 (분리자 양 끝)
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        cells = [c.strip() for c in cells]
        # 빈 행 스킵 (│ 하나만 있는 줄 등)
        if not cells:
            continue
        data_rows.append(cells)

    if not data_rows:
        return ""

    # 연속행 병합: 첫 셀이 비어있으면 이전 행 내용에 합침 (다중행 셀 처리)
    merged: list[list[str]] = []
    for row in data_rows:
        if not row:
            continue
        if merged and not row[0] and any(c for c in row):
            for j in range(len(merged[-1])):
                if j < len(row) and row[j]:
                    merged[-1][j] += " " + row[j]
        else:
            merged.append(list(row))

    if not merged:
        return ""

    # 열 수 통일
    max_cols = max(len(r) for r in merged)
    for row in merged:
        while len(row) < max_cols:
            row.append("")

    # 단일 열 → 테이블이 아니라 장식 프레임 (║ 텍스트 ║)
    if max_cols <= 1:
        return "\n".join(row[0] for row in merged if row[0])

    # 셀 내부 파이프 문자 이스케이프 (마크다운 테이블 깨짐 방지)
    for row in merged:
        for j in range(len(row)):
            row[j] = row[j].replace("|", "&#124;")

    # 마크다운 테이블 구성: 첫 행 = 헤더
    header = merged[0]
    body = merged[1:]

    md = []
    md.append("| " + " | ".join(header) + " |")
    md.append("| " + " | ".join(["---"] * max_cols) + " |")
    for row in body:
        md.append("| " + " | ".join(row[:max_cols]) + " |")

    return "\n".join(md)


# ── 장식선 제거 ──

def _clean_decorative_lines(text: str) -> str:
    """장식용 구분선 제거/정리 (═══, ───, ╔═╗ 프레임 등)."""
    lines = text.split("\n")
    result = []
    for line in lines:
        s = line.strip()

        # 순수 장식 라인 (═══, ───, ━━━ 등) — 4자 이상
        if s and re.fullmatch(r"[═─━┄┈\-=_]{4,}", s):
            result.append("")  # 빈 줄로 대체

        # ╔═╗ / ╚═╝ 프레임 라인
        elif s and re.fullmatch(r"[╔╗╚╝═ ]+", s):
            result.append("")

        # ║ 텍스트 ║ → 텍스트만 추출
        elif s.startswith("║") and s.endswith("║"):
            inner = s[1:-1].strip()
            result.append(inner if inner else "")

        else:
            result.append(line)

    return "\n".join(result)
