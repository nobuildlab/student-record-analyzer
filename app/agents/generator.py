"""PART 생성 에이전트

프롬프트 파일 로드 → 변수 치환 → LLM 호출 → 결과 반환.
PART 1의 근거블록을 PART 2~6에 컨텍스트로 주입.
"""

import re
from pathlib import Path
from config import PROMPT_DIR, MODEL
from skills.llm_caller import call_llm

# PART 1에서 추출한 근거블록 캐시
_evidence_block: str | None = None


def generate_part(
    part_num: int,
    name: str,
    major: str,
    transcript: str,
    factsheet_text: str | None = None,
    feedback: str | None = None,
    factsheet: dict | None = None,
) -> str:
    """특정 PART를 생성한다.

    Args:
        part_num: 파트 번호 (1~6)
        name: 학생 이름
        major: 희망 전공
        transcript: 생기부 전체 텍스트
        factsheet_text: 팩트 시트 텍스트 (선행 추출된 구조화 데이터)
        feedback: 재생성 시 검수 피드백 (첫 생성은 None)

    Returns:
        생성된 PART 텍스트
    """
    global _evidence_block

    prompt_path = PROMPT_DIR / f"part{part_num}.txt"
    raw_prompt = prompt_path.read_text(encoding="utf-8")

    system_prompt, user_prompt = _split_prompt(raw_prompt)

    # 변수 치환
    user_prompt = user_prompt.replace("{학생이름}", name)
    user_prompt = user_prompt.replace("{희망전공}", major)

    # 팩트 시트가 있으면 생기부 텍스트 앞에 삽입
    if factsheet_text:
        # 팩트시트에서 성적 핵심 데이터를 강조 블록으로 추출
        grade_constraint = _build_grade_constraint(factsheet)
        reading_constraint = _build_reading_constraint(factsheet)
        combined_text = (
            grade_constraint
            + "\n\n"
            + reading_constraint
            + "\n\n"
            + factsheet_text
            + "\n\n--- 이하 생기부 원문 (팩트 시트와 모순되는 내용 생성 금지) ---\n\n"
            + transcript
        )
        user_prompt = user_prompt.replace("{생기부 텍스트}", combined_text)
    else:
        user_prompt = user_prompt.replace("{생기부 텍스트}", transcript)

    # PART 2~6: PART 1 근거블록 주입 (비용 최적화)
    if part_num >= 2 and _evidence_block:
        user_prompt += (
            "\n\n---\n"
            "아래는 PART 1에서 추출한 근거 블록입니다. 이 근거를 참고하세요:\n\n"
            f"{_evidence_block}"
        )

    # 이름/전공 포함 지시 (PART 5·6은 면제, reviewer 이름 체크와 동기화)
    if part_num not in (5, 6):
        user_prompt += (
            "\n\n---\n"
            f"※ 반드시 분석 대상 학생 이름 '{name}'을 1회 이상 포함하세요.\n"
            f"※ 희망 전공 '{major}'도 명시하세요."
        )

    # 재생성 시 피드백 주입
    if feedback:
        user_prompt += (
            "\n\n---\n"
            "⚠️ 이전 생성 결과에서 다음 문제가 발견되었습니다. 반드시 수정해주세요:\n\n"
            f"{feedback}"
        )

    result = call_llm(system_prompt, user_prompt, model=MODEL)

    # PART 1 결과에서 근거블록 추출 → 캐시
    if part_num == 1:
        _evidence_block = _extract_evidence_block(result)

    # 후처리: 독서 학년 + 성적 방향 자동 교정 (LLM 확률적 오류 방지)
    if factsheet:
        result = _fix_reading_years(result, factsheet)
        result = _fix_grade_directions(result, factsheet)

    return result


def _split_prompt(raw: str) -> tuple[str, str]:
    """프롬프트 파일을 [시스템 프롬프트]와 [유저 프롬프트]로 분리."""
    # 구분자 패턴
    sys_marker = "[시스템 프롬프트]"
    usr_marker = "[유저 프롬프트]"

    sys_idx = raw.find(sys_marker)
    usr_idx = raw.find(usr_marker)

    if sys_idx == -1 or usr_idx == -1:
        # 구분자가 없으면 전체를 유저 프롬프트로
        return ("당신은 대입 면접 전문 코치입니다.", raw)

    system_prompt = raw[sys_idx + len(sys_marker):usr_idx].strip()
    user_prompt = raw[usr_idx + len(usr_marker):].strip()

    return system_prompt, user_prompt


def _extract_evidence_block(part1_result: str) -> str:
    """PART 1 결과에서 '0단계: 근거 블록' 섹션을 추출."""
    # "0단계" 또는 "근거 블록" 이후 ~ 다음 "###" 또는 "---" 이전
    patterns = [
        r"(?:###?\s*)?0단계.*?근거.*?\n([\s\S]*?)(?=\n###|\n---|\n## )",
        r"근거 블록 추출[\s\S]*?\n([\s\S]*?)(?=\n###|\n---|\n## )",
    ]

    for pattern in patterns:
        match = re.search(pattern, part1_result)
        if match:
            return match.group(1).strip()

    # 패턴 매칭 실패 시 → 첫 2000자 반환 (안전장치)
    return part1_result[:2000]


def _build_grade_constraint(factsheet: dict | None) -> str:
    """팩트시트에서 성적 핵심 데이터를 강조 블록으로 추출.

    모든 PART가 동일한 성적 수치를 사용하도록 강제한다.
    """
    if not factsheet or not factsheet.get("grades"):
        return ""

    lines = [
        "╔══════════════════════════════════════════════════╗",
        "║  ⚠️ 성적 절대 기준 — 아래 수치를 반드시 사용하세요  ║",
        "║  이 수치와 다른 성적을 쓰면 오류입니다.            ║",
        "╚══════════════════════════════════════════════════╝",
        "",
    ]

    for subject, semesters in factsheet["grades"].items():
        if isinstance(semesters, list):
            parts = []
            for s in semesters:
                if isinstance(s, dict):
                    sem = s.get("semester", "?")
                    grade = s.get("grade", "?")
                    parts.append(f"{sem} {grade}")
                else:
                    parts.append(str(s))
            grade_str = " → ".join(parts)
        else:
            grade_str = str(semesters)
        lines.append(f"• {subject}: {grade_str}")

    # ── 과목 평균 성적 산출 (deterministic) ──
    grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
    all_scores = []
    for subject, semesters in factsheet["grades"].items():
        if isinstance(semesters, list):
            for s in semesters:
                if isinstance(s, dict):
                    raw_g = s.get("grade", "")
                    letter = re.match(r"([A-E])", raw_g)
                    if letter and letter.group(1) in grade_val:
                        all_scores.append(grade_val[letter.group(1)])

    # ── 과목별 성적 변화 방향 산출 (교차검수와 동일 로직) ──
    grade_val_dir = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
    direction_lines = []
    for subject, semesters in factsheet["grades"].items():
        if isinstance(semesters, list) and len(semesters) >= 2:
            first_grade = None
            last_grade = None
            for s in semesters:
                if isinstance(s, dict):
                    raw_g = s.get("grade", "")
                    letter = re.match(r"([A-E])", raw_g)
                    if letter:
                        if first_grade is None:
                            first_grade = letter.group(1)
                        last_grade = letter.group(1)
            if first_grade and last_grade and first_grade != last_grade:
                first_val = grade_val_dir.get(first_grade, 0)
                last_val = grade_val_dir.get(last_grade, 0)
                if last_val > first_val:
                    direction_lines.append(
                        f"  ▲ {subject}: {first_grade}→{last_grade} (상승) "
                        f"— '향상/개선/상승'으로 표현 OK, '하락/저하' 표현 금지"
                    )
                else:
                    direction_lines.append(
                        f"  ▼ {subject}: {first_grade}→{last_grade} (하락) "
                        f"— '하락/저하'로 표현 OK, '향상/상승/개선' 표현 금지"
                    )

    if direction_lines:
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════╗")
        lines.append("║  🚨 성적 변화 방향 — 상승/하락을 절대 뒤집지 마세요!      ║")
        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.extend(direction_lines)
        lines.append("")
        lines.append("⚠️ 위 방향과 반대로 쓰면 치명적 오류입니다!")
        lines.append("  - 하락(▼)인 과목을 '향상/개선/상승'으로 표현하면 안 됩니다.")
        lines.append("  - 상승(▲)인 과목을 '하락/저하/떨어짐'으로 표현하면 안 됩니다.")
        lines.append("  - 하락 과목은 '성장 가능성' '개선 의지' 등 긍정적 서사로 전환하되,")
        lines.append("    사실 자체(하락)를 뒤집지는 마세요.")

    lines.append("")
    lines.append("위 성적과 다른 수치를 리포트에 쓰면 안 됩니다.")
    lines.append("특히 성취도 A/B/C를 혼동하지 마세요.")

    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        # 등급 매핑 (PART 1 채점 기준표와 동일)
        if avg >= 4.5:
            ref_grade = "A"
        elif avg >= 4.0:
            ref_grade = "B+"
        elif avg >= 3.5:
            ref_grade = "B"
        elif avg >= 3.0:
            ref_grade = "C+"
        elif avg >= 2.5:
            ref_grade = "C"
        elif avg >= 2.0:
            ref_grade = "D"
        else:
            ref_grade = "F"
        lines.append("")
        lines.append(f"📊 과목 성적 참고 평균: {avg:.2f} (성취도 환산 A=5,B=4,C=3,D=2,E=1)")
        lines.append(f"   → ① 전공 관련 교과 성취도 채점 시 이 평균을 참고하세요.")
        lines.append(f"   → 종합 등급은 5개 항목 점수의 산술 평균으로만 산정하세요.")
        lines.append(f"   → 종합 등급 매핑: A(4.5~5.0) / B+(4.0~4.4) / B(3.5~3.9) / C+(3.0~3.4) / C(2.5~2.9) / D(2.0~2.4) / F(1.0~1.9)")

    lines.append("")

    return "\n".join(lines)


def _build_reading_constraint(factsheet: dict | None) -> str:
    """팩트시트에서 독서 권수를 강조 블록으로 추출."""
    if not factsheet:
        return ""
    reading = factsheet.get("reading", [])
    if not isinstance(reading, list) or len(reading) == 0:
        return ""

    total = len(reading)
    major_books = [
        r for r in reading
        if isinstance(r, dict) and "전공" in r.get("category", "")
    ]
    non_major_books = [
        r for r in reading
        if isinstance(r, dict) and "전공" not in r.get("category", "")
    ]
    major_count = len(major_books)
    non_major_count = len(non_major_books)

    # 학년별 그룹핑
    by_year: dict[str, list[dict]] = {}
    for r in reading:
        if isinstance(r, dict):
            year = str(r.get("year", "?"))
            by_year.setdefault(year, []).append(r)

    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║  ⚠️ 독서 절대 기준 — 아래 수치를 반드시 사용하세요        ║",
        "║  이 수치와 다른 독서 권수를 쓰면 오류입니다.              ║",
        "╚══════════════════════════════════════════════════════════╝",
        "",
        f"★ 독서 총 권수: {total}권 (전공 관련 {major_count}권 + 기타 {non_major_count}권)",
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║  🚨 독서 학년 배정 — 학년을 절대 뒤바꾸지 마세요!          ║",
        "╚══════════════════════════════════════════════════════════╝",
    ]

    for year_key in sorted(by_year.keys()):
        books = by_year[year_key]
        titles = [f"「{b.get('title', '?')}」" for b in books]
        lines.append(f"  [{year_key} 도서] {', '.join(titles)}")

    lines.append("")
    lines.append("── 도서 상세 목록 ──")
    for r in reading:
        if isinstance(r, dict):
            title = r.get("title", "?")
            cat = r.get("category", "")
            year = r.get("year", "")
            lines.append(f"  • 「{title}」 | 분류: {cat} | ★학년: {year}")

    lines.append("")
    lines.append("⚠️ 금지 사항:")
    lines.append(f"  - 독서 총 권수를 {total}권이 아닌 다른 숫자로 쓰면 안 됩니다.")
    lines.append(f"  - 전공 관련 독서를 {major_count}권이 아닌 다른 숫자로 쓰면 안 됩니다.")
    lines.append("  - 각 도서의 학년을 위 학년별 그룹과 다르게 쓰면 치명적 오류입니다.")
    lines.append("  - 1학년 도서를 2학년으로, 2학년 도서를 1학년으로 쓰면 안 됩니다!")
    lines.append("  - '독서 1권' '독서 2권' 등 총 권수와 다른 표현을 쓰지 마세요.")
    lines.append("")

    return "\n".join(lines)


def _fix_reading_years(text: str, factsheet: dict) -> str:
    """LLM 생성 텍스트에서 독서 학년·권수 오류를 결정론적으로 교정한다.

    1) 도서명 ±80자 범위에서 잘못된 학년 → 올바른 학년으로 치환
    2) "독서 N권", "전공 관련 도서 N권" 등 잘못된 권수 → 올바른 권수로 치환
    """
    reading = factsheet.get("reading", [])
    if not isinstance(reading, list) or len(reading) == 0:
        return text

    actual_total = len(reading)
    actual_major = sum(
        1 for r in reading
        if isinstance(r, dict) and "전공" in r.get("category", "")
    )

    # ── 0. 중복 학년 태그 정리 ──
    # 이전 실행에서 중복 삽입된 "(N학년)(N학년)" 패턴 제거
    text = re.sub(r'(\(\d학년\))(\(\d학년\))+', r'\1', text)

    # ── 1. 독서 학년 교정 ──
    # 전략: 각 도서명 바로 뒤에 올바른 학년 태그 삽입/교체.
    # 여러 도서가 한 줄에 나열되어도 각 도서에 개별 태그가 붙으므로 충돌 없음.
    # 역순으로 처리하여 삽입으로 인한 위치 이동 영향 방지.
    for r in reversed(reading):
        if not isinstance(r, dict):
            continue
        title = r.get("title", "")
        correct_year = r.get("year", "")
        if not title or not correct_year:
            continue

        # 역순으로 도서명 출현 위치 수집 (뒤에서부터 처리)
        matches = list(re.finditer(re.escape(title), text))
        for m in reversed(matches):
            end_pos = m.end()

            # 닫는 따옴표 건너뛰기 (」』"'）) 등)
            skip = 0
            while end_pos + skip < len(text) and text[end_pos + skip] in "」』\"'）)":
                skip += 1
            check_pos = end_pos + skip
            after = text[check_pos:check_pos + 20]

            # 이미 올바른 학년이 있으면 스킵
            if re.match(r'\s*[\(（]?' + re.escape(correct_year), after):
                continue

            # 잘못된 학년 태그가 있으면 교체
            wrong_yr = re.match(r'(\s*[\(（]?\d학년[\)）]?)', after)
            if wrong_yr:
                text = (
                    text[:check_pos]
                    + f"({correct_year})"
                    + text[check_pos + len(wrong_yr.group(1)):]
                )
            else:
                # 학년 태그가 없으면 삽입
                text = (
                    text[:check_pos]
                    + f"({correct_year})"
                    + text[check_pos:]
                )

    # ── 2. 독서 권수 교정 ──
    # 패턴 A: "전공 관련 도서/독서 N권" → actual_major로 교정
    def _fix_count(pattern: str, correct: int, check_context: bool = False) -> str:
        nonlocal text
        for m in re.finditer(pattern, text):
            mentioned = int(m.group(1))
            if mentioned == correct:
                continue
            if check_context:
                # 전후 40자에서 "전공" 키워드 확인
                ctx_start = max(0, m.start() - 40)
                before = text[ctx_start:m.start()]
                match_text = m.group()
                if "전공" in before or "전공" in match_text:
                    if mentioned != actual_major:
                        text = text[:m.start(1)] + str(actual_major) + text[m.end(1):]
                    continue
                # 전공 아니면 총 권수와 비교
                if mentioned != actual_total:
                    text = text[:m.start(1)] + str(actual_total) + text[m.end(1):]
            else:
                text = text[:m.start(1)] + str(correct) + text[m.end(1):]
        return text

    # 전공 관련 도서 N권
    text = _fix_count(
        r'전공\s*관련\s*(?:도서|독서)[^0-9]*?(\d+)\s*권',
        actual_major,
    )
    # 독서 N권 (맥락 확인)
    text = _fix_count(
        r'독서[^0-9]{0,20}(\d+)\s*권',
        actual_total,
        check_context=True,
    )
    # 도서/읽은/읽기 N권 (맥락 확인)
    text = _fix_count(
        r'(?:도서|읽[은기])[^0-9]{0,10}(\d+)\s*권',
        actual_total,
        check_context=True,
    )

    return text


def _fix_grade_directions(text: str, factsheet: dict) -> str:
    """LLM 생성 텍스트에서 성적 변화 방향 오류를 결정론적으로 교정한다.

    교차검수(reviewer.py)와 동일한 패턴을 사용하여,
    교차검수에서 잡힐 표현을 사전에 교정한다.
    """
    grades = factsheet.get("grades")
    if not grades:
        return text

    grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}

    # 과목별 방향 계산
    grade_direction: dict[str, tuple[str, str, str]] = {}  # subject → (방향, first, last)
    for subject, semesters in grades.items():
        if not isinstance(semesters, list) or len(semesters) < 2:
            continue
        first_grade = None
        last_grade = None
        for s in semesters:
            if isinstance(s, dict):
                raw_g = s.get("grade", "")
                letter = re.match(r"([A-E])", raw_g)
                if letter:
                    if first_grade is None:
                        first_grade = letter.group(1)
                    last_grade = letter.group(1)
        if first_grade and last_grade and first_grade != last_grade:
            first_val = grade_val.get(first_grade, 0)
            last_val = grade_val.get(last_grade, 0)
            direction = "상승" if last_val > first_val else "하락"
            grade_direction[subject] = (direction, first_grade, last_grade)

    if not grade_direction:
        return text

    # 교차검수와 동일한 패턴
    rise_words = ["향상", "상승", "개선", "올랐", "올라", "높아졌", "높아진",
                  "성장했", "성장한"]
    fall_words = ["하락", "저하", "떨어졌", "떨어진", "낮아졌", "낮아진"]

    for subject, (actual_dir, first_g, last_g) in grade_direction.items():
        if subject not in text:
            continue

        if actual_dir == "하락":
            wrong_words = rise_words
        else:
            wrong_words = fall_words

        # 과목명 뒤 100자 이내에서, 성적 맥락 + 잘못된 방향어 → 문장 단위 교체
        for wrong in wrong_words:
            # 과목명...성적맥락...잘못된방향어+어미 패턴
            pattern = re.compile(
                re.escape(subject)
                + r'([^.。\n]{0,100}?)'
                + r'(성적|성취도|등급)'
                + r'([^.。\n]{0,50}?)'
                + re.escape(wrong)
                + r'[가-힣]*'  # 어미 포함 (향상되었습니다, 상승하였으며 등)
            )
            for pm in reversed(list(pattern.finditer(text))):
                # "성적/성취도/등급" 이후 ~ 방향어+어미 끝까지를 통째로 교체
                grade_kw = pm.group(2)
                between = pm.group(3)
                if actual_dir == "하락":
                    new_tail = f" {first_g}에서 {last_g}로 변화하였습니다"
                else:
                    new_tail = f" {first_g}에서 {last_g}로 향상되었습니다"
                # grade_kw 시작 위치 계산
                grade_kw_start = pm.start(2)
                text = text[:grade_kw_start] + grade_kw + new_tail + text[pm.end():]

        # 추가: "B에서 A" 같은 등급 순서 패턴도 교차검수가 잡음
        if actual_dir == "하락":
            # "B에서 A", "C에서 A/B" 같은 상승 표현 → 실제 방향으로 교체
            wrong_order = re.compile(
                re.escape(subject)
                + r'[^.。\n]{0,80}?'
                + re.escape(last_g) + r'\s*에서\s*' + re.escape(first_g)
            )
            for pm in reversed(list(wrong_order.finditer(text))):
                old_part = f"{last_g}에서 {first_g}"
                new_part = f"{first_g}에서 {last_g}"
                idx = text.find(old_part, pm.start())
                if idx >= 0:
                    text = text[:idx] + new_part + text[idx + len(old_part):]
        else:
            wrong_order = re.compile(
                re.escape(subject)
                + r'[^.。\n]{0,80}?'
                + re.escape(last_g) + r'\s*에서\s*' + re.escape(first_g)
            )
            for pm in reversed(list(wrong_order.finditer(text))):
                old_part = f"{last_g}에서 {first_g}"
                new_part = f"{first_g}에서 {last_g}"
                idx = text.find(old_part, pm.start())
                if idx >= 0:
                    text = text[:idx] + new_part + text[idx + len(old_part):]

    return text


def reset_evidence_cache():
    """근거블록 캐시를 초기화한다 (새 학생 분석 시)."""
    global _evidence_block
    _evidence_block = None
