"""검수 에이전트

규칙 기반 검수 + LLM 기반 환각 탐지 (경고만, FAIL 아님).
"""

import re
from config import REVIEW_MODEL
from skills.llm_caller import call_llm


def review_part(
    part_num: int,
    text: str,
    name: str,
    major: str,
    transcript: str,
    factsheet: dict | None = None,
) -> dict:
    """PART 결과물을 검수한다.

    Returns:
        {
            "passed": bool,
            "issues": list[str],     # FAIL 사유 (규칙 기반)
            "warnings": list[str],   # 경고 (환각 탐지 등, FAIL 아님)
        }
    """
    issues: list[str] = []
    warnings: list[str] = []

    # 1. 규칙 기반 검수 (FAIL 판정)
    rule_issues = _rule_check(part_num, text, name, major, factsheet)
    issues.extend(rule_issues)

    # 2. 표현 규칙 검수 (경고만, FAIL 아님)
    expression_warnings = _expression_check(text)
    warnings.extend(expression_warnings)

    # 3. LLM 기반 환각 탐지 (경고만, FAIL 아님)
    hallucination_warnings = _llm_hallucination_check(text, transcript)
    warnings.extend(hallucination_warnings)

    # 4. 부재/부족 주장 정합성 검증 (FAIL 판정)
    absence_issues = _absence_claim_check(text, transcript)
    issues.extend(absence_issues)

    # 5. 팩트 시트 교차 검증 (FAIL 판정 + 학년 경고)
    if factsheet:
        factsheet_issues, factsheet_warnings = _factsheet_cross_check(text, factsheet)
        issues.extend(factsheet_issues)
        warnings.extend(factsheet_warnings)

    # 6. 창체 활동 학년 교차 검증 (FAIL — 생기부 원문 섹션 기반)
    year_issues = _activity_year_cross_check(text, transcript)
    issues.extend(year_issues)

    # 7-b. 숫자 포맷 오류 검증 (FAIL — 재생성 트리거)
    num_format_issues = _number_format_check(text)
    issues.extend(num_format_issues)

    # 8. 핵심 활동 누락 검증 (PART 1만 적용 — 경고로만 표시, FAIL 아님)
    # 전공명 매칭의 한계로 비전공 키워드가 오탐되는 경우가 있어 참고용으로만 사용
    if factsheet and part_num == 1:
        activity_warnings = _check_key_activities(text, factsheet, major)
        warnings.extend(activity_warnings)

    # 9. 학년/과목 lookup 검증 (경고 — factsheet에 없는 학년+과목 조합 감지)
    if factsheet:
        lookup_warnings = _subject_year_lookup_check(text, factsheet)
        warnings.extend(lookup_warnings)

    # 10. 전공 연결 태깅 검증 (경고 — [해석] 비율 모니터링)
    conn_warnings = _connection_type_check(text)
    warnings.extend(conn_warnings)

    return {
        "passed": len(issues) == 0,
        "issues": issues + warnings,  # UI에는 둘 다 보여줌
        "warnings": warnings,
    }


def _rule_check(
    part_num: int, text: str, name: str, major: str,
    factsheet: dict | None = None,
) -> list[str]:
    """규칙 기반 검수."""
    issues = []

    # ── 공통 검수 ──
    # 이름 일관성 (띄어쓰기 무시) — PART 5(질문리스트), 6(부록)은 이름 불포함이 정상
    if name and part_num not in (5, 6):
        name_normalized = name.replace(" ", "")
        text_normalized = text.replace(" ", "")
        if name_normalized not in text_normalized:
            issues.append(f"학생 이름 '{name}'이(가) 결과물에 포함되지 않았습니다.")

    # 전공 일관성 (띄어쓰기 무시, 부분 매칭 허용)
    if major:
        major_normalized = major.replace(" ", "")
        text_normalized = text.replace(" ", "")
        # "컴퓨터공학" → "컴퓨터" and "공학" 둘 다 있으면 OK
        major_words = [w for w in re.split(r'[\s·/]', major) if len(w) >= 2]
        found = major_normalized in text_normalized or all(w in text for w in major_words)
        if not found:
            issues.append(f"희망 전공 '{major}'이(가) 결과물에 포함되지 않았습니다.")

    # 금지 항목: 특정 대학 이름
    universities = [
        "서울대", "고려대", "연세대", "성균관대", "한양대",
        "중앙대", "경희대", "이화여대", "KAIST", "포항공대", "POSTECH",
    ]
    for uni in universities:
        if uni in text:
            issues.append(f"금지 항목: 대학 이름 '{uni}'이 포함되어 있습니다.")

    # 금지 항목: 합격 확률
    if re.search(r"합격\s*(?:확률|가능성)\s*\d", text):
        issues.append("금지 항목: 합격 확률/가능성 수치가 포함되어 있습니다.")

    # 길이 검증 (너무 짧으면 FAIL)
    if len(text) < 300:
        issues.append(f"결과물이 너무 짧습니다. ({len(text)}자)")

    # ── PART별 검수 ──
    part_checkers = {
        2: _check_part2,
        3: _check_part3,
        4: _check_part4,
        5: _check_part5,
        6: _check_part6,
    }
    if part_num == 1:
        issues.extend(_check_part1(text, factsheet))
    else:
        checker = part_checkers.get(part_num)
        if checker:
            issues.extend(checker(text))

    return issues


def _check_part1(text: str, factsheet: dict | None = None) -> list[str]:
    """PART 1 핵심 진단 요약 검수."""
    issues = []

    # 종합 등급 존재 (다양한 포맷 허용)
    grade_patterns = [
        r"종합\s*등급\s*[:：│|]?\s*[A-F][+]?",
        r"[A-F][+]?\s*\(",                       # "B+ (평균 4.0~4.4)"
        r"등급\s*[:：│|]?\s*[A-F][+]?",
        r"\|\s*[A-F][+]?\s*\|",                   # 표 안의 등급
    ]
    found_grade = any(re.search(p, text) for p in grade_patterns)
    if not found_grade:
        issues.append("종합 등급(A~F)이 확인되지 않습니다.")

    # ── 종합 등급 산술 검증 (deterministic) ──
    issues.extend(_validate_part1_grade_arithmetic(text))

    # ── 점수 보정: 팩트시트 교차검증 (만점 남발 방지) ──
    if factsheet:
        issues.extend(_validate_part1_scores(text, factsheet))

    return issues


def _validate_part1_grade_arithmetic(text: str) -> list[str]:
    """PART 1 종합 등급의 산술 정합성을 검증한다 (deterministic).

    5개 항목 점수 합산 → 평균 → 등급 매핑이 일치하는지 확인.
    LLM이 평균을 잘못 계산하거나 등급을 잘못 매핑하면 FAIL.
    """
    issues = []

    # 5개 항목 점수 추출
    item_markers = ["①", "②", "③", "④", "⑤"]
    scores: list[int] = []
    for marker in item_markers:
        for line in text.split("\n"):
            if marker in line:
                m = re.search(r"(\d)\s*(?:/\s*5|점)", line)
                if m:
                    scores.append(int(m.group(1)))
                break

    if len(scores) != 5:
        return issues  # 점수를 5개 추출 못하면 스킵 (포맷 문제는 다른 체크에서)

    # deterministic 평균 계산
    correct_avg = sum(scores) / 5

    # 등급 매핑
    def _avg_to_grade(avg: float) -> str:
        if avg >= 4.5:
            return "A"
        elif avg >= 4.0:
            return "B+"
        elif avg >= 3.5:
            return "B"
        elif avg >= 3.0:
            return "C+"
        elif avg >= 2.5:
            return "C"
        elif avg >= 2.0:
            return "D"
        else:
            return "F"

    correct_grade = _avg_to_grade(correct_avg)

    # LLM이 산출한 평균 추출
    avg_match = re.search(r"종합\s*평균\s*[:：│|]?\s*(\d+\.?\d*)\s*(?:/\s*5)?", text)
    if avg_match:
        stated_avg = float(avg_match.group(1))
        if abs(stated_avg - correct_avg) > 0.15:
            issues.append(
                f"[종합등급 산술 오류] 5개 항목 합계={sum(scores)}, "
                f"정확한 평균={correct_avg:.1f}이나, "
                f"표기된 평균={stated_avg}입니다. "
                f"평균을 {correct_avg:.1f}으로 수정하세요."
            )

    # LLM이 산출한 등급 추출
    grade_match = re.search(r"종합\s*등급\s*[:：│|]?\s*([A-F][+]?)", text)
    if grade_match:
        stated_grade = grade_match.group(1)
        if stated_grade != correct_grade:
            issues.append(
                f"[종합등급 매핑 오류] 평균 {correct_avg:.1f}에 대한 "
                f"정확한 등급은 {correct_grade}이나, "
                f"{stated_grade}로 표기되었습니다. "
                f"{correct_grade}로 수정하세요."
            )

    return issues


def _validate_part1_scores(text: str, factsheet: dict) -> list[str]:
    """PART 1 항목별 점수를 팩트시트와 교차검증하여 과대 점수를 감지한다."""
    issues = []

    # 각 항목 점수 추출 (표 형식 또는 본문 형식)
    item_markers = ['①', '②', '③', '④', '⑤']
    item_labels = [
        '전공 관련 교과 성취도',
        '세특 전공 연결도',
        '창체 활동 전공 관련성',
        '활동의 깊이 및 주도성',
        '성장 서사 일관성',
    ]
    scores: dict[str, int] = {}
    for marker, label in zip(item_markers, item_labels):
        for line in text.split('\n'):
            if marker in line:
                m = re.search(r'(\d)\s*(?:/\s*5|점)', line)
                if m:
                    scores[label] = int(m.group(1))
                break

    fs_grades = factsheet.get("grades", {})
    grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}

    # ── 체크 1: 전공 핵심 교과에 성취도 C 이하 존재 시 ① 4점 초과 → FAIL ──
    low_grade_subjects = []
    for subj, sems in fs_grades.items():
        if isinstance(sems, list):
            for s in sems:
                if isinstance(s, dict):
                    raw_g = s.get("grade", "")
                    letter = re.match(r"([A-E])", raw_g)
                    if letter and letter.group(1) in ("C", "D", "E"):
                        sem_label = s.get("semester", "")
                        low_grade_subjects.append(
                            f"{subj} {sem_label} 성취도 {letter.group(1)}"
                        )

    score_1 = scores.get('전공 관련 교과 성취도')
    if score_1 is not None and score_1 > 4 and low_grade_subjects:
        issues.append(
            f"[점수 보정] 전공 핵심 교과 성취도 C 이하가 있으므로 "
            f"① 전공 관련 교과 성취도 항목을 4점 이하로 조정하세요. "
            f"(현재 {score_1}점, 근거: {', '.join(low_grade_subjects[:3])})"
        )

    # ── 체크 2: 종합 평균 4.5 초과인데 성적 하락 기록 있으면 → FAIL ──
    if scores:
        avg = sum(scores.values()) / len(scores)

        has_decline = False
        for subj, sems in fs_grades.items():
            if isinstance(sems, list) and len(sems) > 1:
                prev_val = None
                for s in sems:
                    if isinstance(s, dict):
                        raw_g = s.get("grade", "")
                        letter = re.match(r"([A-E])", raw_g)
                        if letter:
                            curr_val = grade_val.get(letter.group(1), 0)
                            if prev_val is not None and curr_val < prev_val:
                                has_decline = True
                                break
                            prev_val = curr_val
                if has_decline:
                    break

        if avg > 4.5 and has_decline:
            issues.append(
                f"[점수 보정] 종합 평균이 {avg:.1f}점이나 성적 하락 기록이 있습니다. "
                f"종합 평균 4.5점 이하로 재조정을 검토하세요."
            )

    return issues


def _check_part2(text: str) -> list[str]:
    """PART 2 강점·리스크 분석 검수."""
    issues = []

    # 강점 확인 (다양한 포맷)
    strength_patterns = [r"\[강점\s*\d\]", r"강점\s*\d", r"\*\*강점\s*\d", r"###.*강점"]
    strength_count = max(
        len(re.findall(p, text)) for p in strength_patterns
    )
    if strength_count < 2:
        issues.append(f"강점이 충분하지 않습니다. (최소 2개 필요)")

    # 리스크 확인 (다양한 포맷)
    risk_patterns = [r"\[리스크\s*\d\]", r"리스크\s*\d", r"\*\*리스크\s*\d", r"보완점\s*\d", r"###.*리스크"]
    risk_count = max(
        len(re.findall(p, text)) for p in risk_patterns
    )
    if risk_count < 1:
        issues.append(f"리스크/보완점이 확인되지 않습니다. (최소 1개 필요)")

    # ── 리스크 중복 검증: 같은 과목/소재를 2개 리스크에 반복 사용 방지 ──
    if risk_count >= 2:
        risk_sections = re.split(r"\[리스크\s*\d\]|\*\*리스크\s*\d|###.*리스크\s*\d|리스크\s*(?:요인\s*)?\d", text)
        # 첫 번째는 리스크 이전 텍스트이므로 제외, 리스크 섹션만 추출
        risk_texts = [s.strip() for s in risk_sections[1:] if s.strip()]
        if len(risk_texts) >= 2:
            # 각 리스크에서 과목명 추출 (한글 과목명 + 로마자 포함)
            subject_pattern = r"(국어|수학[ⅠⅡ]?|영어[ⅠⅡ]?|물리학[ⅠⅡ]?|화학[ⅠⅡ]?|생명과학[ⅠⅡ]?|지구과학[ⅠⅡ]?|통합과학|통합사회|한국사|정보|기술[·\s]?가정|사회[·\s]?문화|생활과\s*윤리|윤리와\s*사상|한국지리|세계지리|동아시아사|세계사|경제|정치와\s*법|미적분|확률과\s*통계|기하)"
            risk1_subjects = set(re.findall(subject_pattern, risk_texts[0]))
            risk2_subjects = set(re.findall(subject_pattern, risk_texts[1]))
            overlap = risk1_subjects & risk2_subjects
            if overlap:
                issues.append(
                    f"리스크 1과 리스크 2가 동일 과목({', '.join(overlap)})을 반복합니다. "
                    f"서로 다른 소재의 리스크를 제시하세요."
                )
            else:
                # 과목명이 다르더라도 핵심 키워드가 겹치면 경고
                risk1_keywords = set(re.findall(r'성취도|성적|변동|하락|개선|부족|낮', risk_texts[0]))
                risk2_keywords = set(re.findall(r'성취도|성적|변동|하락|개선|부족|낮', risk_texts[1]))
                keyword_overlap = risk1_keywords & risk2_keywords
                if len(keyword_overlap) >= 3:
                    issues.append(
                        f"리스크 1과 리스크 2가 유사한 소재(성적 변동)를 반복하고 있습니다. "
                        f"하나는 성적 외 영역(세특 연결, 창체 다양성, 독서 편중 등)에서 찾으세요."
                    )

    return issues


def _check_part3(text: str) -> list[str]:
    """PART 3 전략 방향 코멘트 검수."""
    # 전략 방향은 내용만 있으면 PASS (포맷 자유)
    return []


def _check_part4(text: str) -> list[str]:
    """PART 4 자기소개 서사 설계도 검수."""
    issues = []
    # 1분/3분 설계도가 모두 포함되어 있는지
    if "1분" not in text and "1 분" not in text:
        issues.append("1분 자기소개 설계도가 확인되지 않습니다.")
    if "3분" not in text and "3 분" not in text:
        issues.append("3분 자기소개 설계도가 확인되지 않습니다.")

    # 서사 구조 핵심 요소 확인 (계기/탐구/확장/비전 or 핵심 경험/전공 연결)
    structure_keywords = ["계기", "탐구", "확장", "비전", "핵심 경험", "전공 연결", "핵심 논지"]
    found = sum(1 for kw in structure_keywords if kw in text)
    if found < 2:
        issues.append("서사 구조 설계도의 핵심 요소(계기/탐구/확장/비전)가 부족합니다.")

    # 완성 대본이 아닌 설계도 형식인지 확인 — 활동/키워드 bullet이 있어야 함
    bullet_count = len(re.findall(r'[-·•][\s]*[가-힣]', text))
    if bullet_count < 5:
        issues.append("각 단계별 꺼낼 활동/키워드가 bullet 형태로 충분히 제시되지 않았습니다.")

    return issues


def _check_part5(text: str) -> list[str]:
    """PART 5 예상질문 + 답변 가이드 검수."""
    issues = []
    # "질문" 키워드가 충분히 있는지만 확인
    question_indicators = re.findall(r"질문|Q\d|[A-C]-\d|\[A|\[B|\[C", text)
    if len(question_indicators) < 5:
        issues.append("질문이 충분하지 않습니다.")
    return issues


def _check_part6(text: str) -> list[str]:
    """PART 6 부록 검수."""
    # 내용만 있으면 PASS
    return []


def _expression_check(text: str) -> list[str]:
    """표현 규칙 검수 (경고만, FAIL 아님)."""
    warnings = []

    # 성취도/등급 혼용 체크: "성취도 C" 는 OK, "C등급" 은 혼용 의심
    mixed_grade = re.findall(r"[A-E]\s*등급", text)
    if mixed_grade:
        warnings.append(
            f"[표현 경고] 성취도/등급 혼용 의심: {mixed_grade[:3]}. "
            "성취평가 과목은 '성취도 A/B/C', 상대평가 과목은 'N등급'으로 구분 필요"
        )

    # 결과 예측/보장 표현 체크
    prediction_patterns = [
        r"충분히\s*좋은\s*결과",
        r"합격\s*가능성이\s*높",
        r"좋은\s*결과를?\s*기대",
        r"합격이?\s*유력",
    ]
    for p in prediction_patterns:
        if re.search(p, text):
            warnings.append(
                f"[표현 경고] 결과 예측 뉘앙스 표현 발견. "
                "'준비도에 따라 설득력이 높아질 수 있음' 식으로 완화 권장"
            )
            break

    # 리스크 부정 프레이밍 체크
    negative_patterns = [
        r"인식\s*부족",
        r"이해\s*부족",
        r"경험\s*부족",
        r"역량이?\s*미흡",
        r"능력이?\s*부족",
    ]
    found_negatives = []
    for p in negative_patterns:
        matches = re.findall(p, text)
        found_negatives.extend(matches)
    if found_negatives:
        warnings.append(
            f"[표현 경고] 단정적 부정 표현 {found_negatives[:3]} 발견. "
            "'면접에서 질문 가능성이 있는 지점' 등 코칭 톤으로 수정 권장"
        )

    # 추정형 리스크 문장 체크
    speculative = [
        r"이해가\s*충분하지\s*않을\s*수\s*있",
        r"깊이가?\s*부족할?\s*수\s*있",
    ]
    for p in speculative:
        if re.search(p, text):
            warnings.append(
                "[표현 경고] 추정형 리스크 문장 발견. "
                "'면접에서 확장 질문이 나올 수 있으므로 준비 필요' 식으로 전환 권장"
            )
            break

    # 단정형 인증 표현 체크
    if re.search(r"보여주는\s*학생입니다", text):
        warnings.append(
            "[표현 경고] 단정형 인증 표현 '~를 보여주는 학생입니다' 발견. "
            "'기록상 ~가 나타납니다' 식으로 완화 권장"
        )

    # RC-FINAL v2: 실무/현업/전문가 수준 금칙어 체크
    silmu_matches = re.findall(r"실무|현업|전문가\s*수준|프로\s*수준", text)
    if silmu_matches:
        warnings.append(
            f"[표현 경고] 금칙어 '{', '.join(silmu_matches[:3])}' 발견. "
            "'교내 프로젝트 기반 구현 경험' 등으로 치환 필요"
        )

    # RC-FINAL v2: 성취도 하락/퇴보 부정 프레이밍
    if re.search(r"성취도[가는은이]?\s*[^.。\n]*(?:하락|퇴보)", text):
        warnings.append(
            "[표현 경고] 성취도 '하락/퇴보' 부정 프레이밍 발견. "
            "성장 서사(C→B 개선)로 전환 필요"
        )

    return warnings


def _llm_hallucination_check(text: str, transcript: str) -> list[str]:
    """LLM으로 환각 탐지. 결과는 경고(warning)로만 반환, FAIL 아님.

    0단계 근거 블록에서 이미 인용된 문장은 검증 대상에서 제외한다.
    (근거 블록은 생기부 원문 인용이므로 환각이 아님)
    """
    warnings = []

    # 0단계 근거 블록 추출 — 분석 결과물에서 근거 블록 부분을 제거하고 검사
    text_for_check = text
    evidence_block = re.search(
        r"(?:#{1,3}\s*)?0단계[\s\S]*?(?=#{1,3}\s*[1-9A]|[A-C]\.\s|$)",
        text,
    )
    if evidence_block:
        # 근거 블록 자체는 제외하고, 나머지 분석 텍스트만 검사
        text_for_check = text[:evidence_block.start()] + text[evidence_block.end():]

    # 텍스트가 너무 짧으면 (근거 블록만 있었던 경우) 스킵
    if len(text_for_check.strip()) < 200:
        return warnings

    system_prompt = (
        "당신은 검수 전문가입니다. "
        "분석 결과물에서 두 가지 유형의 오류를 확인합니다:\n"
        "1. 환각(추가): 원본 생기부에 없는 활동, 수상, 도서명을 새로 만들어낸 경우\n"
        "2. 허위 부재: 원본 생기부에 있는 활동, 수상, 프로젝트를 '없다/부재/부족'으로 잘못 주장한 경우\n"
        "단, 원본 생기부의 내용을 요약하거나 재구성한 것은 오류가 아닙니다. "
        "완전히 새로 만들어낸 활동이나, 있는 것을 없다고 주장한 경우만 지적하세요.\n"
        "⚠️ 중요: 생기부 원문에 '해당 없음', '해당사항 없음', '미기재' 등으로 기록된 항목은 "
        "진짜 부재입니다. 이 경우 '부재/없음'으로 쓴 것은 정확한 서술이므로 [허위부재]로 지적하지 마세요."
    )

    user_prompt = f"""아래 '원본 생기부'와 '분석 결과물'을 비교하세요.

두 가지를 확인해주세요:
1. 분석 결과물에서 원본 생기부에 없는 활동, 수상, 도서명이 추가되어 있는지
2. 분석 결과물에서 원본 생기부에 있는 활동, 수상, 프로젝트를 "없다/부재/부족"으로 잘못 주장하는지

특히 수상 경력은 생기부 앞부분에 있으므로 주의깊게 확인하세요.
원문을 요약·재구성한 것은 OK입니다.

문제가 없으면 "문제 없음"이라고만 답하세요.
있으면 각 항목을 아래 형식으로 나열하세요. (최대 5개)
- [환각] 없는 내용을 추가한 경우
- [허위부재] 있는 내용을 없다고 주장한 경우

--- 원본 생기부 ---
{transcript[:6000]}

--- 분석 결과물 (일부) ---
{text_for_check[:2000]}
"""

    try:
        result = call_llm(system_prompt, user_prompt, model=REVIEW_MODEL)
        if "문제 없음" not in result:
            hallucinations = re.findall(r"-\s*\[환각\]\s*(.+)", result)
            for h in hallucinations[:3]:
                warnings.append(f"[환각 의심·경고] {h.strip()}")
            false_absences = re.findall(r"-\s*\[허위부재\]\s*(.+)", result)
            for fa in false_absences[:3]:
                warnings.append(f"[허위 부재·경고] {fa.strip()}")
    except Exception:
        pass

    return warnings


def _absence_claim_check(text: str, transcript: str) -> list[str]:
    """'부재/부족/없음' 주장이 생기부 원문과 모순되는지 규칙 기반 검증.

    생기부에 관련 기록이 있는데 '없다/부재/부족'으로 쓴 경우 FAIL.
    """
    issues = []

    # (패턴, 생기부에서 확인할 키워드, 오류 설명)
    absence_checks = [
        # 수상 관련
        (r"수상\s*(?:경력|기록|내역)?\s*(?:의\s*)?(?:부재|없음|부족)",
         r"수상|표창|대회.*(?:상|입상|수상)",
         "수상 경력 부재/부족"),
        (r"수상\s*(?:경력|기록|내역)?\s*(?:이|가)\s*(?:없|부족)",
         r"수상|표창|대회.*(?:상|입상|수상)",
         "수상 경력 없음"),
        # 프로젝트 관련
        (r"프로젝트\s*(?:경험)?\s*(?:의\s*)?(?:부재|없음|부족)",
         r"프로젝트|프로그램|개발|설계|제작",
         "프로젝트 경험 부재/부족"),
        (r"프로젝트\s*(?:경험)?\s*(?:이|가)\s*(?:없|부족)",
         r"프로젝트|프로그램|개발|설계|제작",
         "프로젝트 경험 없음"),
        # 실험/연구 관련
        (r"(?:실험|연구|탐구)\s*(?:경험)?\s*(?:의\s*)?(?:부재|없음|부족)",
         r"실험|연구|탐구|조사|분석",
         "실험/연구 경험 부재/부족"),
        # 동아리 관련
        (r"동아리\s*(?:활동)?\s*(?:의\s*)?(?:부재|없음|부족)",
         r"동아리|부장|차장",
         "동아리 활동 부재/부족"),
        # 독서 관련
        (r"독서\s*(?:활동|기록)?\s*(?:의\s*)?(?:부재|없음|부족)",
         r"독서|도서|읽",
         "독서 활동 부재/부족"),
    ]

    for claim_pattern, evidence_pattern, description in absence_checks:
        if re.search(claim_pattern, text):
            # 생기부 원문에 해당 키워드가 존재하면 → 허위 부재 = FAIL
            match = re.search(evidence_pattern, transcript)
            if match:
                # "해당 없음/해당사항 없음" 근처(전후 60자)면 진짜 부재 → skip
                ctx_start = max(0, match.start() - 60)
                ctx_end = min(len(transcript), match.end() + 60)
                context = transcript[ctx_start:ctx_end]
                if re.search(r"해당\s*(?:사항\s*)?없", context):
                    continue  # 진짜 부재이므로 에러 아님
                issues.append(
                    f"[데이터 정합성 오류] '{description}' 주장이 있으나, "
                    f"생기부 원문에 관련 기록이 존재합니다. "
                    "허위 진술 위험 — 해당 약점을 삭제하거나 수정해야 합니다."
                )

    return issues


def _factsheet_cross_check(text: str, factsheet: dict) -> tuple[list[str], list[str]]:
    """팩트 시트와 결과물의 교차 검증. 구조화된 데이터로 정밀 대조.

    팩트 시트의 확정 데이터와 모순되면 FAIL.
    Returns:
        (issues, warnings) 튜플
    """
    issues = []
    warnings = []

    # ── 1. "수상 부재" 주장 vs 팩트 시트 수상 목록 ──
    absence_pattern = r"수상\s*(?:경력|기록|내역)?\s*(?:의\s*)?(?:부재|없음|부족)"
    absence_pattern2 = r"수상\s*(?:경력|기록|내역)?\s*(?:이|가)\s*(?:없|부족)"
    if re.search(absence_pattern, text) or re.search(absence_pattern2, text):
        awards = factsheet.get("awards", [])
        if awards and len(awards) > 0:
            awards_str = ", ".join(
                f"{a.get('name', '?')}({a.get('grade', '?')})"
                for a in awards[:4]
            )
            issues.append(
                f"[팩트시트 모순] '수상 경력 부재' 주장이나, "
                f"팩트 시트에 수상 {len(awards)}건 존재: {awards_str}"
            )

    # ── 2. "프로젝트 부족" 주장 vs 팩트 시트 활동 목록 ──
    project_absence = r"프로젝트\s*(?:경험)?\s*(?:의\s*)?(?:부재|없음|부족)"
    project_absence2 = r"프로젝트\s*(?:경험)?\s*(?:이|가)\s*(?:없|부족)"
    if re.search(project_absence, text) or re.search(project_absence2, text):
        projects = []
        for s in factsheet.get("seukteuk", []):
            for a in s.get("activities", []):
                if any(kw in a for kw in ["프로젝트", "설계", "개발", "제작", "구현"]):
                    projects.append(f"{a}({s.get('subject', '?')})")
        if projects:
            issues.append(
                f"[팩트시트 모순] '프로젝트 경험 부족' 주장이나, "
                f"팩트 시트에 프로젝트 {len(projects)}건 존재: {', '.join(projects[:3])}"
            )

    # ── 3. 성취도 불일치 체크 ──
    # 결과물에서 "과목 성취도 X" 패턴 추출 → 팩트 시트와 대조
    grade_mentions = re.findall(
        r"([\w가-힣Ⅰ-Ⅹ]+)\s*(?:과목\s*)?(?:에서\s*)?성취도\s*([A-E])", text
    )
    for subject_raw, grade_in_text in grade_mentions:
        # 조사 제거 (정보과학에서 → 정보과학, 화학Ⅰ의 → 화학Ⅰ)
        subject = re.sub(r"(?:에서|에|의|는|은|이|가|도|과|와)$", "", subject_raw)
        fs_grades = factsheet.get("grades", {})
        # 과목명 매칭 — 정확 매칭 우선 (정보과학 ≠ 정보)
        matched_subject = None
        # 1차: 정확 매칭
        for fs_subj in fs_grades:
            if subject == fs_subj:
                matched_subject = fs_subj
                break
        # 2차: 부분 매칭 (정확 매칭 실패 시만)
        if not matched_subject:
            for fs_subj in fs_grades:
                if subject in fs_subj or fs_subj in subject:
                    matched_subject = fs_subj
                    break

        if matched_subject:
            all_grades = []
            for sg in fs_grades[matched_subject]:
                if isinstance(sg, dict):
                    raw_grade = sg.get("grade", "")
                    # 점수 정보 제거: "C(75/67.2)" → "C" (RC-FINAL)
                    letter = re.match(r"([A-E])", raw_grade)
                    all_grades.append(letter.group(1) if letter else raw_grade)
                else:
                    all_grades.append(str(sg))

            if grade_in_text not in all_grades:
                issues.append(
                    f"[팩트시트 모순] '{subject} 성취도 {grade_in_text}' 언급이나, "
                    f"팩트 시트에는 {matched_subject}: {' / '.join(all_grades)}"
                )

    # ── 4. "동아리 부재" 주장 vs 팩트 시트 ──
    if re.search(r"동아리\s*(?:활동)?\s*(?:의\s*)?(?:부재|없음|부족)", text):
        clubs = factsheet.get("clubs", [])
        if clubs:
            club_names = ", ".join(
                c.get("name", "?") if isinstance(c, dict) else str(c)
                for c in clubs[:3]
            )
            issues.append(
                f"[팩트시트 모순] '동아리 활동 부재' 주장이나, "
                f"팩트 시트에 동아리 {len(clubs)}개 존재: {club_names}"
            )

    # ── 5. "높지 않은/낮은" 성취도 주장 vs 팩트 시트 고등급 (RC v5) ──
    low_claim_patterns = re.findall(
        r"([\w가-힣Ⅰ-Ⅹ·]+)\s*(?:과목\s*)?(?:성취도|성적|점수)?\s*(?:가|이|는|도)?\s*(?:높지\s*않|낮은|낮고|저조|미흡|부진)",
        text,
    )
    for subject_raw in low_claim_patterns:
        # 조사 제거
        subject_claim = re.sub(r"(?:에서|에|의|는|은|이|가|도|과|와)$", "", subject_raw)
        fs_grades = factsheet.get("grades", {})
        # 정확 매칭 우선
        matched_fs_subj = None
        for fs_subj in fs_grades:
            if subject_claim == fs_subj:
                matched_fs_subj = fs_subj
                break
        if not matched_fs_subj:
            for fs_subj in fs_grades:
                if subject_claim in fs_subj or fs_subj in subject_claim:
                    matched_fs_subj = fs_subj
                    break
        if matched_fs_subj:
            semesters = fs_grades[matched_fs_subj]
            high_grades = []
            if isinstance(semesters, list):
                for s in semesters:
                    if isinstance(s, dict):
                        raw_g = s.get("grade", "")
                        # 점수 정보 제거: "A(93/63.1)" → "A" (RC-FINAL)
                        letter_m = re.match(r"([A-E]|\d)", raw_g)
                        g = letter_m.group(1) if letter_m else raw_g
                        if g in ("A", "1", "2"):
                            high_grades.append(
                                f"{s.get('semester', '?')} {g}"
                            )
            if high_grades:
                issues.append(
                    f"[팩트시트 모순] '{subject_claim}' 성취도가 낮다는 주장이나, "
                    f"팩트 시트에 고등급 존재: {', '.join(high_grades)}"
                )

    # ── 6. 학년 레이블 교차 검증 (FAIL — 서사적 텍스트는 compiler 교정 불가) ──
    # 같은 과목명의 모든 학년을 수집 (수학이 1학년+2학년에 있을 수 있음)
    subj_all_years: dict[str, set[str]] = {}
    for entry in factsheet.get("seukteuk", []):
        subj = entry.get("subject", "")
        grade_str = entry.get("grade", "")
        ym = re.search(r"(\d)", grade_str)
        if subj and ym:
            subj_all_years.setdefault(subj, set()).add(ym.group(1))
    for subj, valid_years in subj_all_years.items():
        if len(subj) < 2:
            continue
        subj_esc = re.escape(subj)
        # "과목명 (N학년)" 패턴 — 정확 매칭만 (부분 매칭 방지)
        year_mentions = re.findall(rf"(?<![가-힣]){subj_esc}(?![가-힣Ⅰ-Ⅹ])\s*\((\d)학년\)", text)
        for mentioned in year_mentions:
            if mentioned not in valid_years:
                issues.append(
                    f"[학년 오류] '{subj} ({mentioned}학년)' → "
                    f"팩트시트에는 {', '.join(sorted(valid_years))}학년으로 기록됨. "
                    f"'{', '.join(sorted(valid_years))}학년'으로 수정하세요."
                )
        # "N학년 과목명" 패턴
        year_prefix = re.findall(rf"(\d)학년\s+(?<![가-힣]){subj_esc}(?![가-힣Ⅰ-Ⅹ])", text)
        for mentioned in year_prefix:
            if mentioned not in valid_years:
                issues.append(
                    f"[학년 오류] '{mentioned}학년 {subj}' → "
                    f"팩트시트에는 {', '.join(sorted(valid_years))}학년으로 기록됨. "
                    f"'{', '.join(sorted(valid_years))}학년'으로 수정하세요."
                )

    # ── 7. "독서 부재" 주장 vs 팩트 시트 ──
    if re.search(r"독서\s*(?:활동|기록)?\s*(?:의\s*)?(?:부재|없음|부족)", text):
        reading = factsheet.get("reading", [])
        if reading:
            issues.append(
                f"[팩트시트 모순] '독서 활동 부재' 주장이나, "
                f"팩트 시트에 도서 {len(reading)}권 존재"
            )

    return issues, warnings


def _activity_year_cross_check(text: str, transcript: str) -> list[str]:
    """생기부 원문의 창체 섹션별 활동 키워드를 기반으로 학년 교차 검증 (FAIL).

    두 가지 체크:
    A) 유일 키워드 체크: 하나의 학년에만 등장하는 키워드가 다른 학년으로 언급되면 FAIL
    B) 카테고리 특정 체크: "N학년 동아리" 컨텍스트에서 해당 섹션에 없는 활동 키워드 FAIL
    """
    issues = []

    # ── 원문 창체 섹션 파싱 ──
    section_re = r"\[(\d)학년\s*(자율활동|동아리활동|진로활동)\]"
    section_spans = list(re.finditer(section_re, transcript))

    sections: dict[tuple[str, str], str] = {}
    for i, m in enumerate(section_spans):
        year, cat = m.group(1), m.group(2)
        start = m.end()
        if i + 1 < len(section_spans):
            end = section_spans[i + 1].start()
        else:
            # 마지막 창체 섹션: 다음 \n[ 까지 (교과세특 침범 방지)
            remaining = transcript[start:]
            next_header = re.search(r"\n\[", remaining)
            end = start + next_header.start() if next_header else len(transcript)
        sections[(year, cat)] = transcript[start:end]

    if not sections:
        return issues

    # 일반 개념어 제외 (학년 특정 활동이 아닌 단어들)
    _generic = {
        "프로젝트", "프로그래밍", "하드웨어", "소프트웨어", "알고리즘",
        "인공지능", "프로그램", "컴퓨터공학", "데이터베이스", "코딩",
        "정보통신", "빅데이터", "머신러닝", "자율주행",
    }

    # 동사/형용사 어미 → 활동 키워드가 아닌 서술어
    _verb_suffixes = (
        "합니다", "습니다", "됩니다", "입니다", "겠습니",
        "하고", "했고", "하며", "했으며", "하여", "했다", "한다", "했다",
        "하는", "했는", "되는", "되어", "되며", "되었",
        "라고", "라는", "라며", "이라",
        "처럼", "같은", "같이", "만큼", "위해",
        "어서", "아서", "으며", "으면", "으로", "면서",
        "들어서", "만들어서", "들여서", "하면서",
    )

    def _is_verb_form(w: str) -> bool:
        return any(w.endswith(s) for s in _verb_suffixes)

    # 조사 제거 (중화반응 vs 중화반응은 → 동일 키워드로 통합)
    _particles = (
        "에서는", "에서의", "으로써", "으로는", "에서", "으로",
        "에는", "에도", "에의", "이며", "이고", "이라", "이나",
        "까지", "에게", "부터",
        "은", "는", "이", "가", "을", "를", "에", "로", "과", "와", "의", "도",
    )

    def _strip_particle(w: str) -> str:
        for p in _particles:
            if w.endswith(p) and len(w) - len(p) >= 2:
                return w[:-len(p)]
        return w

    # ── A) 유일 키워드 체크 ──
    kw_years: dict[str, set[str]] = {}
    for (year, _cat), sec_text in sections.items():
        for w in re.findall(r"[가-힣]{4,}", sec_text):
            stripped = _strip_particle(w)
            if (stripped not in _generic
                    and len(stripped) >= 4
                    and not _is_verb_form(stripped)):
                kw_years.setdefault(stripped, set()).add(year)

    unique_kws = {kw: list(years)[0] for kw, years in kw_years.items() if len(years) == 1}
    checked = set()

    for kw, correct_year in unique_kws.items():
        if kw in checked:
            continue
        kw_esc = re.escape(kw)
        # 키워드 출현 위치마다 가장 가까운 "N학년"을 찾아 비교
        for m in re.finditer(kw_esc, text):
            ctx_start = max(m.start() - 80, 0)
            ctx_end = min(m.end() + 80, len(text))
            context = text[ctx_start:ctx_end]

            # 컨텍스트 내 모든 "N학년" 찾기
            year_mentions = list(re.finditer(r"(\d)학년", context))
            if not year_mentions:
                continue

            # 키워드와 가장 가까운 학년 선택
            kw_pos = m.start() - ctx_start
            closest = min(year_mentions, key=lambda ym: abs(ym.start() - kw_pos))
            mentioned = closest.group(1)

            if mentioned != correct_year:
                checked.add(kw)
                issues.append(
                    f"[학년 오류] '{kw}'이(가) {mentioned}학년으로 언급되었으나, "
                    f"생기부 원문에는 {correct_year}학년에 기록됨. "
                    f"'{correct_year}학년'으로 수정하세요."
                )
                break

    # ── B) 카테고리 특정 체크 (동아리/진로/자율 + 학년 컨텍스트) ──
    cat_triggers = [
        (r"(\d)학년\s*(?:때\s*)?(?:부터\s*)?(?:에?서?\s*)?(?:과학실험반|동아리)", "동아리활동"),
        (r"동아리(?:활동)?\s*\(?(\d)학년\)?", "동아리활동"),
        (r"(\d)학년\s*(?:때\s*)?진로", "진로활동"),
        (r"진로(?:활동)?\s*\(?(\d)학년\)?", "진로활동"),
        (r"(\d)학년\s*(?:때\s*)?자율", "자율활동"),
        (r"자율(?:활동)?\s*\(?(\d)학년\)?", "자율활동"),
    ]

    for pat, category in cat_triggers:
        for m in re.finditer(pat, text):
            mentioned_year = m.group(1)
            section_key = (mentioned_year, category)
            if section_key not in sections:
                continue

            section_text = sections[section_key]

            # 매치 위치 전후 80자 컨텍스트 (양방향)
            ctx_start = max(m.start() - 80, 0)
            ctx_end = min(m.end() + 80, len(text))
            context = text[ctx_start:ctx_end]
            context_kws = {_strip_particle(w) for w in re.findall(r"[가-힣]{4,}", context)
                          if len(_strip_particle(w)) >= 4
                          and not _is_verb_form(_strip_particle(w))}

            for ckw in context_kws:
                if ckw in checked or ckw in _generic:
                    continue
                # 이 키워드가 해당 섹션에 없는데, 같은 카테고리의 다른 학년에 있는 경우
                if ckw not in section_text:
                    correct_locs = []
                    for (y, c), st in sections.items():
                        if c == category and y != mentioned_year and ckw in st:
                            correct_locs.append(f"{y}학년 {c}")
                    if correct_locs:
                        checked.add(ckw)
                        issues.append(
                            f"[학년/영역 오류] '{ckw}'이(가) {mentioned_year}학년 {category}으로 "
                            f"언급되었으나, 생기부 원문에는 {', '.join(correct_locs)}에 기록됨. "
                            f"정확한 학년으로 수정하세요."
                        )

    return issues


def _number_format_check(text: str) -> list[str]:
    """숫자 포맷 오류를 검출한다 (FAIL — 재생성 트리거).

    "79점3.5", "3.5.2" 등 숫자 서식 깨짐을 감지.
    """
    issues = []

    bad_patterns = [
        # "79점3.5" — 점수 뒤에 소수점 숫자가 바로 붙는 오류
        (r"\d+점\d+\.\d+", "점수+소수 결합 오류"),
        # "3.5.2" — 이중 소수점
        (r"\d+\.\d+\.\d+", "이중 소수점 오류"),
        # "79점35점" — 이중 점수 표기 (같은 위치에 2개)
        (r"\d{1,3}점\d{2,}점", "이중 점수 표기 오류"),
        # "A(93/63.1/72)" — 슬래시 3개 이상 (비정상 점수 포맷)
        (r"[A-E]\(\d+/\d+[./]\d+/\d+", "비정상 점수 포맷"),
        # 성취도 뒤에 숫자가 바로 붙음: "성취도C75" (공백/괄호 누락)
        (r"성취도\s*[A-E]\d{2,}", "성취도+점수 결합 오류"),
    ]

    for pattern, desc in bad_patterns:
        matches = re.findall(pattern, text)
        if matches:
            issues.append(
                f"[숫자 포맷 오류] {desc}: {', '.join(matches[:3])}. "
                "수치 표기를 정규화하세요."
            )

    return issues


def _connection_type_check(text: str) -> list[str]:
    """전공 연결 태깅([직접]/[해석])의 비율을 모니터링한다 (경고).

    [해석] 태그가 과반이면 경고 — 면접에서 과잉 연결 지적 리스크.
    """
    warnings = []

    direct_count = len(re.findall(r"\[직접\]", text))
    inferred_count = len(re.findall(r"\[해석\]", text))
    total = direct_count + inferred_count

    if total == 0:
        # 태깅이 없으면 경고 (프롬프트에 태깅 지시가 있는데 미이행)
        # PART 4, 5, 6은 태깅 대상이 아니므로 스킵
        return warnings

    if total > 0 and inferred_count / total > 0.5:
        warnings.append(
            f"[전공 연결 경고] [해석] 태그 비율이 {inferred_count}/{total}로 과반입니다. "
            "면접에서 과잉 연결 지적 리스크가 있으므로, [직접] 연결 중심으로 재구성을 권장합니다."
        )

    return warnings


def _subject_year_lookup_check(text: str, factsheet: dict) -> list[str]:
    """리포트에서 언급된 'N학년 과목' 조합이 factsheet에 존재하는지 검증한다 (경고).

    factsheet의 seukteuk + grades에서 유효한 (학년, 과목) 쌍을 구축하고,
    리포트에서 추출한 (학년, 과목) 쌍이 여기에 없으면 경고.
    """
    warnings = []

    # ── 유효한 (학년, 과목) 쌍 구축 ──
    valid_pairs: set[tuple[str, str]] = set()

    # seukteuk에서
    for entry in factsheet.get("seukteuk", []):
        subj = entry.get("subject", "")
        grade_str = entry.get("grade", "")
        ym = re.search(r"(\d)", grade_str)
        if subj and ym:
            valid_pairs.add((ym.group(1), subj))

    # grades에서
    for subj, semesters in factsheet.get("grades", {}).items():
        if isinstance(semesters, list):
            for s in semesters:
                if isinstance(s, dict):
                    sem = s.get("semester", "")
                    ym = re.search(r"(\d)", sem)
                    if ym:
                        valid_pairs.add((ym.group(1), subj))

    if not valid_pairs:
        return warnings

    # 유효 과목명 목록 (매칭용)
    valid_subjects = {subj for _, subj in valid_pairs}

    # ── 리포트에서 "N학년 과목" 패턴 추출 ──
    # "1학년 수학" / "수학 (2학년)" / "2학년 영어Ⅰ" 패턴
    checked: set[tuple[str, str]] = set()

    # 패턴 1: "N학년 과목명"
    for m in re.finditer(r"(\d)학년\s+([\w가-힣Ⅰ-Ⅹ]+)", text):
        year, subj_raw = m.group(1), m.group(2)
        # 조사 제거
        subj = re.sub(r"(?:에서|에|의|는|은|이|가|도|과|와|때)$", "", subj_raw)
        if len(subj) < 2:
            continue
        # 정확 매칭
        matched_subj = None
        for vs in valid_subjects:
            if subj == vs:
                matched_subj = vs
                break
        if not matched_subj:
            for vs in valid_subjects:
                if subj in vs or vs in subj:
                    matched_subj = vs
                    break
        if matched_subj and (year, matched_subj) not in valid_pairs:
            pair_key = (year, matched_subj)
            if pair_key not in checked:
                checked.add(pair_key)
                # 해당 과목의 유효 학년 찾기
                valid_years = sorted(y for y, s in valid_pairs if s == matched_subj)
                warnings.append(
                    f"[학년-과목 lookup 경고] '{year}학년 {matched_subj}' 조합이 "
                    f"팩트시트에 없습니다. 유효 학년: {', '.join(valid_years)}학년. "
                    "학년 배정을 확인하세요."
                )

    # 패턴 2: "과목명 (N학년)"
    for m in re.finditer(r"([\w가-힣Ⅰ-Ⅹ]+)\s*\((\d)학년\)", text):
        subj_raw, year = m.group(1), m.group(2)
        subj = re.sub(r"(?:에서|에|의|는|은|이|가|도|과|와)$", "", subj_raw)
        if len(subj) < 2:
            continue
        matched_subj = None
        for vs in valid_subjects:
            if subj == vs:
                matched_subj = vs
                break
        if not matched_subj:
            for vs in valid_subjects:
                if subj in vs or vs in subj:
                    matched_subj = vs
                    break
        if matched_subj and (year, matched_subj) not in valid_pairs:
            pair_key = (year, matched_subj)
            if pair_key not in checked:
                checked.add(pair_key)
                valid_years = sorted(y for y, s in valid_pairs if s == matched_subj)
                warnings.append(
                    f"[학년-과목 lookup 경고] '{matched_subj} ({year}학년)' 조합이 "
                    f"팩트시트에 없습니다. 유효 학년: {', '.join(valid_years)}학년. "
                    "학년 배정을 확인하세요."
                )

    return warnings


def _check_key_activities(
    text: str, factsheet: dict, major: str
) -> list[str]:
    """팩트시트의 전공 관련 핵심 활동이 PART 1 스토리라인에 반영되었는지 검증한다.

    seukteuk에서 전공명(major)이 포함된 엔트리를 찾아 키워드를 추출하고,
    PART 1 텍스트에 3개 이상 누락 시 FAIL.
    전공명 매칭 실패 시 (전공 관련 엔트리를 특정할 수 없는 경우) 검증 스킵.
    """
    issues = []
    if not major:
        return issues

    # 전공명 분리: "컴퓨터공학" → ["컴퓨터공학", "컴퓨터"]
    # 공백/점/슬래시 분리 + 학과명 접미사에서 접두사 추출 (접미사는 매칭에 사용 안 함)
    # "공학/과학" 같은 접미사는 너무 일반적 → "생명공학"처럼 비관련 엔트리에 오매칭
    major_words = [w for w in re.split(r'[\s·/]', major) if len(w) >= 2]
    _academic_suffixes = ['공학', '과학', '학과', '학부']
    for mw in list(major_words):
        for suffix in _academic_suffixes:
            if mw.endswith(suffix) and len(mw) > len(suffix):
                prefix = mw[:-len(suffix)]
                if len(prefix) >= 2 and prefix not in major_words:
                    major_words.append(prefix)
                break

    # 어느 학생에게나 등장할 수 있는 일반 용어 → 핵심 활동 판별 제외
    _generic = {
        "프로젝트", "프로그래밍", "프로그램", "과학기술", "알고리즘",
        "소프트웨어", "하드웨어", "인공지능", "컴퓨터", "데이터베이스",
        "자율활동", "동아리활동", "진로활동", "봉사활동",
    }

    key_activities: list[str] = []

    # ── seukteuk에서 전공 관련 활동 키워드 추출 ──
    all_seukteuk = factsheet.get("seukteuk", [])
    matched_entries = []
    for entry in all_seukteuk:
        keywords = entry.get("keywords", [])
        subject = entry.get("subject", "")
        # activities는 매칭 풀에서 제외 — "컴퓨터를 이용하여 실험" 같은
        # 비전공 맥락에서 전공 키워드가 등장하면 오매칭 발생
        pool = subject + " " + " ".join(keywords)

        if any(mw in pool for mw in major_words):
            matched_entries.append(entry)

    # major_words 매칭 실패 시 → 전공 관련 엔트리를 특정할 수 없으므로 검증 스킵
    # (비전공 과목 키워드 "로봇세/산술기하평균/복소수" 등이 오탐을 일으킴)
    if not matched_entries:
        return issues

    for entry in matched_entries:
        # keywords 필드 (GPT가 큐레이션한 태그)
        for kw in entry.get("keywords", []):
            for w in re.findall(r'[가-힣a-zA-Z0-9]{3,}', kw):
                if w not in _generic and w not in major_words:
                    key_activities.append(w)

        # activities 필드
        for act in entry.get("activities", []):
            for w in re.findall(r'[가-힣a-zA-Z0-9]{4,}', act):
                if w not in _generic and w not in major_words:
                    key_activities.append(w)

    # ── clubs에서 전공 관련 동아리명 추출 ──
    for club in factsheet.get("clubs", []):
        if isinstance(club, dict):
            name = club.get("name", "")
            if len(name) >= 3:
                if any(mw in name for mw in major_words):
                    key_activities.append(name)

    # 중복 제거 (순서 유지)
    seen: set[str] = set()
    unique: list[str] = []
    for kw in key_activities:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    # 텍스트에서 누락 확인 (공백 무시 매칭 포함: "시간복잡도" = "시간 복잡도")
    text_no_space = text.replace(" ", "")
    missing = [kw for kw in unique if kw not in text and kw not in text_no_space]

    if len(missing) >= 3:
        issues.append(
            f"[핵심 활동 누락] 핵심 활동이 스토리라인에 반영되지 않았습니다: "
            f"{', '.join(missing[:5])}"
        )

    return issues


# ════════════════════════════════════════════════════════════
#  최종 교차 검수 — 전체 PART 생성 후 FACTSHEET vs 리포트 대조
# ════════════════════════════════════════════════════════════

def cross_check_report(parts: dict, factsheet: dict) -> list[dict]:
    """전체 리포트(PART 1~6)를 FACTSHEET와 교차 검수한다.

    Returns:
        불일치 목록: [{"part": int, "issue": str, "severity": "error"|"warning"}, ...]
    """
    issues: list[dict] = []

    if not factsheet or not factsheet.get("grades"):
        return issues

    # ── 1. 성적 정합성 검수 ──
    # 팩트시트에서 각 과목별 성취도 추출
    grade_map: dict[str, list[str]] = {}  # {과목명: [성취도들]}
    for subject, semesters in factsheet["grades"].items():
        grades_list = []
        if isinstance(semesters, list):
            for s in semesters:
                if isinstance(s, dict):
                    g = s.get("grade", "")
                    if g:
                        grades_list.append(str(g))
        else:
            grades_list.append(str(semesters))
        grade_map[subject] = grades_list

    # 각 PART에서 성적 언급 확인
    for part_num, text in parts.items():
        if not text:
            continue
        # "성취도 X" 패턴 검색
        for subject, valid_grades in grade_map.items():
            # 과목명이 텍스트에 있는 경우만 검사
            if subject not in text:
                continue
            # 해당 과목 주변에서 성취도 언급 찾기
            pattern = re.escape(subject) + r'[^.。\n]{0,50}성취도\s*([A-F])'
            matches = re.findall(pattern, text)
            # valid_grades는 "A(95/58.3)" 형태일 수 있으므로 첫 글자만 비교
            valid_letters = {g[0] for g in valid_grades if g and g[0] in "ABCDEF"}
            for mentioned_grade in matches:
                if mentioned_grade not in valid_letters:
                    issues.append({
                        "part": part_num,
                        "issue": f"[성적 불일치] PART {part_num}에서 '{subject}' 성취도를 "
                                 f"'{mentioned_grade}'로 표기했으나, 팩트시트 기준 "
                                 f"성취도는 {'/'.join(valid_grades)}입니다.",
                        "severity": "error",
                    })

    # ── 2. PART 간 성적 일관성 검수 ──
    # 모든 PART에서 언급된 성적을 모아서 PART 간 모순 확인
    part_grades: dict[str, dict[int, str]] = {}  # {과목: {part_num: 언급된 성취도}}
    for part_num, text in parts.items():
        if not text:
            continue
        for subject in grade_map:
            if subject not in text:
                continue
            pattern = re.escape(subject) + r'[^.。\n]{0,30}(?:성취도|성적)\s*([A-F])'
            match = re.search(pattern, text)
            if match:
                if subject not in part_grades:
                    part_grades[subject] = {}
                part_grades[subject][part_num] = match.group(1)

    for subject, mentions in part_grades.items():
        unique_grades = set(mentions.values())
        if len(unique_grades) > 1:
            detail = ", ".join(f"PART {p}: {g}" for p, g in sorted(mentions.items()))
            issues.append({
                "part": 0,  # 전체 리포트 이슈
                "issue": f"[PART 간 불일치] '{subject}' 성취도가 PART마다 다릅니다: {detail}",
                "severity": "error",
            })

    # ── 3. 성적 변화 방향 검증 (상승/하락 뒤집기 방지) ──
    # 팩트시트에서 과목별 성취도 변화 방향 계산
    grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, "F": 0}
    grade_direction: dict[str, str] = {}  # {과목명: "상승"|"하락"|"유지"}
    for subject, semesters in factsheet["grades"].items():
        if isinstance(semesters, list) and len(semesters) >= 2:
            first_grade = None
            last_grade = None
            for s in semesters:
                if isinstance(s, dict):
                    g = s.get("grade", "")
                    letter = re.match(r"([A-E])", g)
                    if letter:
                        if first_grade is None:
                            first_grade = letter.group(1)
                        last_grade = letter.group(1)
            if first_grade and last_grade and first_grade != last_grade:
                if grade_val.get(last_grade, 0) > grade_val.get(first_grade, 0):
                    grade_direction[subject] = "상승"
                else:
                    grade_direction[subject] = "하락"

    # 리포트에서 "상승/향상/개선" 또는 "하락/저하/떨어" 표현 + 과목명 조합 검색
    rise_pattern = r"(향상|상승|개선|올[랐렸]|높아[졌진]|B에서\s*A|C에서\s*[AB])"
    fall_pattern = r"(하락|저하|떨어[졌진]|낮아[졌진]|A에서\s*B|[AB]에서\s*C)"

    for part_num, text in parts.items():
        if not text:
            continue
        for subject, actual_dir in grade_direction.items():
            if subject not in text:
                continue
            # 과목명 주변 100자 범위에서 방향 표현 검색
            subject_contexts = re.finditer(re.escape(subject), text)
            for m in subject_contexts:
                start = max(0, m.start() - 50)
                end = min(len(text), m.end() + 100)
                context = text[start:end]

                has_rise = re.search(rise_pattern, context)
                has_fall = re.search(fall_pattern, context)

                if actual_dir == "하락" and has_rise and not has_fall:
                    issues.append({
                        "part": part_num,
                        "issue": f"[성적 방향 오류] PART {part_num}에서 '{subject}' 성적이 "
                                 f"상승/향상한 것으로 기재되었으나, 팩트시트 기준 실제로는 "
                                 f"하락했습니다. 즉시 수정 필요.",
                        "severity": "error",
                    })
                elif actual_dir == "상승" and has_fall and not has_rise:
                    issues.append({
                        "part": part_num,
                        "issue": f"[성적 방향 오류] PART {part_num}에서 '{subject}' 성적이 "
                                 f"하락한 것으로 기재되었으나, 팩트시트 기준 실제로는 "
                                 f"상승했습니다. 즉시 수정 필요.",
                        "severity": "error",
                    })

    # ── 4. 독서 권수 검증 ──
    all_text = "\n".join(text for text in parts.values() if text)
    reading = factsheet.get("reading", [])
    if isinstance(reading, list) and len(reading) > 0:
        actual_total = len(reading)
        actual_major = sum(
            1 for r in reading
            if isinstance(r, dict) and "전공" in r.get("category", "")
        )

        def _check_reading_context(text: str, match_obj, mentioned: int) -> dict | None:
            """매치 주변 맥락을 분석하여 전공/기타/총 중 올바른 기준과 비교."""
            start_ctx = max(0, match_obj.start() - 40)
            end_ctx = min(len(text), match_obj.end() + 40)
            before = text[start_ctx:match_obj.start()]
            after = text[match_obj.end():end_ctx]
            match_text = match_obj.group()  # 매치 텍스트 자체도 검사

            # "전공 관련 N권" → 전공 기준과 비교
            # before 또는 매치 텍스트 안에 "전공"이 있으면 전공 맥락
            if "전공" in before or "전공" in match_text:
                if actual_major > 0 and mentioned != actual_major:
                    return {"compare": "major", "actual": actual_major}
                return None  # 정확하거나 전공=0

            # "기타 N권" → 기타 기준과 비교
            if "기타" in before or "기타" in match_text:
                actual_etc = actual_total - actual_major
                if mentioned != actual_etc:
                    return {"compare": "etc", "actual": actual_etc}
                return None

            # "총 N권" 뒤에 "(전공 관련..."이 따라오면 → 총 기준
            # 이미 총 권수가 맞으면 통과
            if mentioned == actual_total:
                return None

            # 일반 맥락 → 총 기준과 비교
            if mentioned < actual_total:
                return {"compare": "total", "actual": actual_total}
            return None

        # 각 PART 개별로 검색하여 정확한 part_num 반환
        found_issue = False
        for part_num, text in parts.items():
            if not text or found_issue:
                continue

            # 패턴 1: "전공 관련 도서/독서 N권"
            for m in re.finditer(
                r'전공\s*관련\s*(?:도서|독서)[^0-9]*?(\d+)\s*권', text
            ):
                mentioned = int(m.group(1))
                if mentioned < actual_major:
                    issues.append({
                        "part": part_num,
                        "issue": f"[독서 권수 오류] PART {part_num}에서 전공 관련 도서를 "
                                 f"'{mentioned}권'으로 표기했으나, 팩트시트 기준 "
                                 f"전공 관련 도서는 {actual_major}권입니다.",
                        "severity": "error",
                    })
                    found_issue = True
                    break

            if found_issue:
                break

            # 패턴 2: "독서 N권" (일반) — 전공/기타 맥락 확인 포함
            for m in re.finditer(r'독서[^0-9]{0,20}(\d+)\s*권', text):
                mentioned = int(m.group(1))
                result = _check_reading_context(text, m, mentioned)
                if result:
                    if result["compare"] == "major":
                        issues.append({
                            "part": part_num,
                            "issue": f"[독서 권수 오류] PART {part_num}에서 전공 관련 독서를 "
                                     f"'{mentioned}권'으로 표기했으나, 팩트시트 기준 "
                                     f"전공 관련 도서는 {result['actual']}권입니다.",
                            "severity": "error",
                        })
                    else:
                        issues.append({
                            "part": part_num,
                            "issue": f"[독서 권수 오류] PART {part_num}에서 독서를 "
                                     f"'{mentioned}권'으로 표기했으나, 팩트시트 기준 "
                                     f"총 {result['actual']}권입니다.",
                            "severity": "error",
                        })
                    found_issue = True
                    break

            if found_issue:
                break

            # 패턴 3: "독서/도서/읽은/읽기 ... N권" (넓은 맥락) — 전공/기타 맥락 확인 포함
            for m in re.finditer(
                r'(?:독서|도서|읽[은기])[^0-9]{0,10}(\d+)\s*권', text
            ):
                mentioned = int(m.group(1))
                result = _check_reading_context(text, m, mentioned)
                if result:
                    if result["compare"] == "major":
                        issues.append({
                            "part": part_num,
                            "issue": f"[독서 권수 오류] PART {part_num}에서 전공 관련 도서를 "
                                     f"'{mentioned}권'으로 표기했으나, 팩트시트 기준 "
                                     f"전공 관련 도서는 {result['actual']}권입니다.",
                            "severity": "error",
                        })
                    else:
                        issues.append({
                            "part": part_num,
                            "issue": f"[독서 권수 오류] PART {part_num}에서 독서를 "
                                     f"'{mentioned}권'으로 표기했으나, 팩트시트 기준 "
                                     f"총 {result['actual']}권입니다.",
                            "severity": "error",
                        })
                    found_issue = True
                    break

    # ── 4-2. 독서 학년 검증 ──
    # 팩트시트 reading에 year가 있으면, 리포트에서 해당 도서의 학년 표기가 맞는지 확인
    for r in reading:
        if not isinstance(r, dict):
            continue
        title = r.get("title", "")
        correct_year = r.get("year", "")
        if not title or not correct_year:
            continue
        # 학년 숫자 추출 (예: "2학년" → "2")
        yr_match = re.search(r"(\d)", correct_year)
        if not yr_match:
            continue
        correct_yr_num = yr_match.group(1)

        for part_num, text in parts.items():
            if not text or title not in text:
                continue
            # 도서명 주변에서 학년 표기 찾기
            for m in re.finditer(re.escape(title), text):
                start = max(0, m.start() - 80)
                end = min(len(text), m.end() + 80)
                snippet = text[start:end]
                title_center = m.start() - start + len(title) // 2
                # 도서명에 가장 가까운 "N학년"만 검증 (그룹 나열 시 다른 도서의 학년 무시)
                yr_candidates = []
                for yr_m in re.finditer(r"(\d)학년", snippet):
                    dist = abs(yr_m.start() - title_center)
                    yr_candidates.append((dist, yr_m.group(1)))
                if yr_candidates:
                    yr_candidates.sort(key=lambda x: x[0])
                    nearest_yr = yr_candidates[0][1]
                    if nearest_yr != correct_yr_num:
                        issues.append({
                            "part": part_num,
                            "issue": f"[독서 학년 오류] PART {part_num}에서 '{title}'을(를) "
                                     f"{nearest_yr}학년 활동으로 표기했으나, 팩트시트 기준 "
                                     f"{correct_year} 독서활동입니다.",
                            "severity": "error",
                        })
                        break  # 도서당 PART당 1회만
                else:
                    continue

    # ── 5. 등급 학기별 구분 검증 (뭉뚱그림 방지) ──
    # 같은 과목인데 학기별로 등급이 다른 경우, 리포트에서 단일 등급으로 뭉뚱그렸는지 확인
    subjects_with_varying_grades: dict[str, list[tuple[str, str]]] = {}
    for subject, semesters in factsheet["grades"].items():
        if not isinstance(semesters, list) or len(semesters) < 2:
            continue
        sem_grades = []
        for s in semesters:
            if not isinstance(s, dict):
                continue
            raw_g = s.get("grade", "")
            sem_label = s.get("semester", "")
            g_type = s.get("type", "")
            # 등급 추출: 성취평가 → A/B/C, 상대평가 → 숫자
            if "상대" in g_type:
                m = re.match(r"(\d)", str(raw_g))
                if m:
                    sem_grades.append((sem_label, m.group(1) + "등급"))
            else:
                m = re.match(r"([A-E])", str(raw_g))
                if m:
                    sem_grades.append((sem_label, "성취도 " + m.group(1)))
        if len(sem_grades) >= 2:
            unique = set(g for _, g in sem_grades)
            if len(unique) > 1:
                subjects_with_varying_grades[subject] = sem_grades

    for part_num, text in parts.items():
        if not text:
            continue
        for subject, sem_grades in subjects_with_varying_grades.items():
            if subject not in text:
                continue
            # 과목명 주변에서 단일 등급만 언급했는지 확인
            # "N등급" 또는 "성취도 X" 단독 언급 패턴
            contexts = list(re.finditer(re.escape(subject), text))
            for ctx in contexts:
                start = max(0, ctx.start() - 20)
                end = min(len(text), ctx.end() + 80)
                snippet = text[start:end]
                # 학기별 구분 없이 단일 등급만 쓴 경우 감지
                # "N등급" 단독 (앞에 학기 표현 없음)
                single_grade = re.search(
                    r'(?<!\d학년\s)(?<!\d학기\s)(?<!\d-\d\s)(\d)등급', snippet
                )
                single_achieve = re.search(
                    r'(?<!\d학년\s)(?<!\d학기\s)성취도\s*([A-E])(?!\s*[→에])', snippet
                )
                if single_grade or single_achieve:
                    mentioned_val = (
                        single_grade.group(1) + "등급" if single_grade
                        else "성취도 " + single_achieve.group(1)
                    )
                    grade_list_str = ", ".join(
                        f"{sem}: {g}" for sem, g in sem_grades
                    )
                    # 언급된 등급이 실제 학기 중 하나와 일치하더라도,
                    # 다른 학기 등급이 다르면 뭉뚱그림 경고
                    issues.append({
                        "part": part_num,
                        "issue": f"[등급 뭉뚱그림] PART {part_num}에서 '{subject}'을(를) "
                                 f"'{mentioned_val}'로 단일 표기했으나, 팩트시트 기준 "
                                 f"학기별 등급이 다릅니다: {grade_list_str}. "
                                 f"학기별 구분 표기가 필요합니다.",
                        "severity": "warning",
                    })
                    break  # 과목당 1회만

    # ── 6. 수치 환각 검수 ──
    # 리포트에서 "N시간", "N회", "N건" 등 수치를 추출하여 팩트시트에 근거 있는지 확인
    number_claims = re.findall(r'(\d{2,})\s*시간', all_text)
    for num in number_claims:
        # 팩트시트 전체 텍스트에서 이 숫자가 있는지 확인
        fs_text = str(factsheet)
        if num not in fs_text:
            issues.append({
                "part": 0,
                "issue": f"[수치 검증 필요] '{num}시간'이 리포트에 언급되었으나, "
                         f"팩트시트에서 확인되지 않습니다. 생기부 원문 확인 필요.",
                "severity": "warning",
            })

    # ── 7. 역량 과대평가 검수 ──
    # ★★★★★ (5점 만점)이 부여된 역량의 근거 확인
    five_star_matches = re.findall(r'│\s*([^│]+?)\s*│\s*★★★★★\s*│', all_text)
    if five_star_matches:
        for capability in five_star_matches:
            capability = capability.strip()
            if capability and len(capability) > 2:
                issues.append({
                    "part": 6,
                    "issue": f"[과대평가 확인] '{capability}' 역량에 ★★★★★(만점)이 "
                             f"부여되었습니다. 생기부 근거가 충분한지 확인 필요.",
                    "severity": "warning",
                })

    return issues
