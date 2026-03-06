"""전체 파이프라인 제어

1. PDF 텍스트 추출
2. 팩트 시트 추출 (데이터 구조화)
3. PART 1~6 순차 생성 + 검수 + 재생성
4. 리포트 컴파일 (HTML → PDF)
"""

from typing import Callable
from config import MAX_RETRIES, PART_NAMES
from agents.extractor import extract_transcript
from agents.fact_extractor import extract_factsheet, factsheet_to_text
from agents.generator import generate_part, reset_evidence_cache
from agents.reviewer import review_part
from agents.compiler import compile_report
from skills.llm_caller import get_usage_summary, reset_usage


# 콜백 타입: (step: str, detail: str, progress: float)
StatusCallback = Callable[[str, str, float], None]


class PipelineResult:
    """파이프라인 실행 결과."""

    def __init__(self):
        self.student_info: dict = {}
        self.factsheet: dict | None = None         # 팩트 시트 (구조화된 데이터)
        self.parts: dict[int, str] = {}           # PART 번호 → 최종 텍스트
        self.reviews: dict[int, dict] = {}         # PART 번호 → 검수 결과
        self.manual_review: list[int] = []         # 수동 검토 필요 PART 목록
        self.pdf_path: str | None = None
        self.usage: dict = {}
        self.audit_log: list[str] = []
        self.cross_check: list[dict] = []        # 최종 교차 검수 결과


def run_pipeline(
    pdf_path: str,
    name: str,
    major: str,
    on_status: StatusCallback | None = None,
) -> PipelineResult:
    """전체 파이프라인을 실행한다.

    Args:
        pdf_path: 생기부 PDF 경로
        name: 학생 이름
        major: 희망 전공
        on_status: 진행 상태 콜백 (Streamlit UI 업데이트용)

    Returns:
        PipelineResult 객체
    """
    result = PipelineResult()
    reset_usage()
    reset_evidence_cache()

    def status(step: str, detail: str, progress: float):
        result.audit_log.append(f"[{progress:.0%}] {step}: {detail}")
        if on_status:
            on_status(step, detail, progress)

    # ── Step 1: PDF 텍스트 추출 ──
    status("추출", "PDF 텍스트 추출 중...", 0.0)
    try:
        info = extract_transcript(pdf_path)
        transcript = info["raw_text"]
        result.student_info = info

        if not transcript or len(transcript) < 100:
            status("추출", "❌ 텍스트 추출 실패 (텍스트가 너무 짧음)", 0.0)
            return result

        status("추출", f"✅ 텍스트 추출 완료 ({len(transcript):,}자)", 0.05)
    except Exception as e:
        status("추출", f"❌ PDF 읽기 실패: {e}", 0.0)
        return result

    # ── Step 2: 팩트 시트 추출 ──
    factsheet = None
    factsheet_text = None
    status("팩트시트", "📊 팩트 시트 추출 중 (데이터 구조화)...", 0.06)
    try:
        factsheet = extract_factsheet(transcript)
        factsheet_text = factsheet_to_text(factsheet)
        result.factsheet = factsheet

        awards_count = len(factsheet.get("awards", []))
        grades_count = len(factsheet.get("grades", {}))
        seukteuk_count = len(factsheet.get("seukteuk", []))
        status(
            "팩트시트",
            f"✅ 팩트 시트 추출 완료 (수상 {awards_count}건, 과목 {grades_count}개, 세특 {seukteuk_count}개)",
            0.10,
        )
    except Exception as e:
        status("팩트시트", f"⚠️ 팩트 시트 추출 실패: {e} → 원문만으로 진행", 0.10)

    # ── Step 3: PART 1~6 순차 생성 + 검수 ──
    total_parts = 6
    for part_num in range(1, total_parts + 1):
        part_name = PART_NAMES[part_num]
        base_progress = 0.10 + (part_num - 1) * 0.13  # 0.10 ~ 0.88

        status(f"PART {part_num}", f"🔄 {part_name} 생성 중...", base_progress)

        generated = False
        for attempt in range(MAX_RETRIES + 1):
            try:
                # a. 생성
                feedback = None
                if attempt > 0:
                    # 재생성: 이전 검수 피드백 포함
                    feedback = "\n".join(result.reviews[part_num]["issues"])

                text = generate_part(
                    part_num=part_num,
                    name=name,
                    major=major,
                    transcript=transcript,
                    factsheet_text=factsheet_text,
                    feedback=feedback,
                    factsheet=factsheet,
                )
                result.parts[part_num] = text

                # b. 검수
                status(
                    f"PART {part_num}",
                    f"🔍 {part_name} 검수 중... (시도 {attempt + 1}/{MAX_RETRIES + 1})",
                    base_progress + 0.07,
                )

                review = review_part(
                    part_num=part_num,
                    text=text,
                    name=name,
                    major=major,
                    transcript=transcript,
                    factsheet=factsheet,
                )
                result.reviews[part_num] = review

                if review["passed"]:
                    status(
                        f"PART {part_num}",
                        f"✅ {part_name} → 검수 PASS",
                        base_progress + 0.14,
                    )
                    generated = True
                    break
                else:
                    issues_str = ", ".join(review["issues"][:3])
                    if attempt < MAX_RETRIES:
                        status(
                            f"PART {part_num}",
                            f"⚠️ 검수 FAIL → 재생성 ({issues_str})",
                            base_progress + 0.07,
                        )
                    else:
                        status(
                            f"PART {part_num}",
                            f"⚠️ {part_name} → 검수 FAIL (수동 검토 필요: {issues_str})",
                            base_progress + 0.14,
                        )
                        result.manual_review.append(part_num)
                        generated = True  # 텍스트는 있으므로 계속 진행

            except Exception as e:
                status(f"PART {part_num}", f"❌ 생성 실패: {e}", base_progress)
                if attempt >= MAX_RETRIES:
                    result.manual_review.append(part_num)
                break

    # ── Step 3.5: 교차검수 전 독서 학년·권수·성적 방향 최종 후처리 ──
    if len(result.parts) == total_parts and factsheet:
        from agents.generator import _fix_reading_years, _fix_grade_directions
        for pn in result.parts:
            result.parts[pn] = _fix_reading_years(result.parts[pn], factsheet)
            result.parts[pn] = _fix_grade_directions(result.parts[pn], factsheet)

    # ── Step 4: 최종 교차 검수 (FACTSHEET vs 전체 리포트) ──
    MAX_CROSS_ROUNDS = 2  # 교차검수 → 재생성 최대 라운드
    if len(result.parts) == total_parts and factsheet:
        status("교차검수", "🔎 팩트시트 대 리포트 교차 검수 중...", 0.85)
        from agents.reviewer import cross_check_report
        cross_issues = cross_check_report(result.parts, factsheet)

        for cross_round in range(MAX_CROSS_ROUNDS):
            if not cross_issues:
                break
            error_issues = [i for i in cross_issues if i["severity"] == "error"]
            if not error_issues:
                break

            round_label = f"(라운드 {cross_round + 1}/{MAX_CROSS_ROUNDS})"

            # error가 있는 PART 번호 수집 (0 = 전체 → 언급된 모든 PART 대상)
            error_parts: set[int] = set()
            has_reading_error = False
            for ei in error_issues:
                p = ei.get("part", 0)
                if p > 0:
                    error_parts.add(p)
                # part=0 (전체 이슈)인 경우, PART 1과 3에 재생성 요청
                if p == 0:
                    error_parts.update({1, 3})
                # 독서 관련 오류가 있으면 플래그
                if "독서" in ei.get("issue", ""):
                    has_reading_error = True

            # 독서 오류 → PART 3(전략 방향 코멘트)에서 주로 권수/학년 언급
            if has_reading_error and 3 not in error_parts:
                error_parts.add(3)

            if not error_parts:
                break

            # 전체 에러 피드백 (part=0 전체 이슈용)
            global_feedback = "\n".join(
                f"- {ei['issue']}" for ei in error_issues if ei.get("part", 0) == 0
            )
            for regen_part in sorted(error_parts):
                if regen_part not in result.parts:
                    continue
                part_name = PART_NAMES.get(regen_part, f"PART {regen_part}")
                status(
                    "교차검수",
                    f"🔄 교차 검수 오류 → {part_name} 재생성 중... {round_label}",
                    0.86,
                )
                # 해당 PART 에러만 필터링 + 전체(part=0) 에러도 포함
                part_feedback = "\n".join(
                    f"- {ei['issue']}"
                    for ei in error_issues
                    if ei.get("part", 0) == regen_part or ei.get("part", 0) == 0
                )
                if not part_feedback:
                    part_feedback = global_feedback
                # 원본 백업 (재생성 실패/FAIL 시 복원용)
                original_text = result.parts[regen_part]
                original_review = result.reviews.get(regen_part)
                try:
                    regen_text = generate_part(
                        part_num=regen_part,
                        name=name,
                        major=major,
                        transcript=transcript,
                        factsheet_text=factsheet_text,
                        feedback=f"[교차 검수 오류 {round_label} — 아래 사항을 반드시 수정하세요]\n{part_feedback}",
                        factsheet=factsheet,
                    )
                    # 검수 먼저 수행
                    regen_review = review_part(
                        part_num=regen_part,
                        text=regen_text,
                        name=name,
                        major=major,
                        transcript=transcript,
                        factsheet=factsheet,
                    )

                    if regen_review["passed"]:
                        # 검수 PASS → 재생성 텍스트 채택
                        result.parts[regen_part] = regen_text
                        result.reviews[regen_part] = regen_review
                        status(
                            "교차검수",
                            f"✅ {part_name} 교차검수 오류 수정 채택 (검수 PASS) {round_label}",
                            0.87,
                        )
                    else:
                        # 검수 FAIL → 원본 유지 (교차검수 오류보다 검수 FAIL이 더 큰 문제)
                        result.parts[regen_part] = original_text
                        if original_review is not None:
                            result.reviews[regen_part] = original_review
                        issues_str = ", ".join(regen_review["issues"][:2])
                        status(
                            "교차검수",
                            f"⚠️ {part_name} 재생성 검수 FAIL → 원본 유지 ({issues_str}) {round_label}",
                            0.87,
                        )
                except Exception as e:
                    # 생성 자체 실패 → 원본 유지
                    result.parts[regen_part] = original_text
                    if original_review is not None:
                        result.reviews[regen_part] = original_review
                    status("교차검수", f"⚠️ {part_name} 재생성 실패 → 원본 유지: {e} {round_label}", 0.87)

            # 재교차 검수 전 독서·성적 방향 후처리 재적용
            for pn in result.parts:
                result.parts[pn] = _fix_reading_years(result.parts[pn], factsheet)
                result.parts[pn] = _fix_grade_directions(result.parts[pn], factsheet)
            status("교차검수", f"🔎 재교차 검수 중... {round_label}", 0.88)
            cross_issues = cross_check_report(result.parts, factsheet)

        # 최종 결과 저장
        if cross_issues:
            error_issues = [i for i in cross_issues if i["severity"] == "error"]
            warn_issues = [i for i in cross_issues if i["severity"] == "warning"]
            result.cross_check = cross_issues

            if error_issues:
                issue_summary = "; ".join(i["issue"][:80] for i in error_issues[:3])
                status("교차검수", f"⚠️ 교차 검수 불일치 {len(error_issues)}건: {issue_summary}", 0.89)
            else:
                status("교차검수", f"✅ 교차 검수 완료 (경고 {len(warn_issues)}건)", 0.89)
        else:
            result.cross_check = []
            status("교차검수", "✅ 교차 검수 PASS — 불일치 없음", 0.89)

    # ── Step 5: PDF 컴파일 ──
    if len(result.parts) == total_parts:
        status("컴파일", "📄 PDF 리포트 생성 중...", 0.90)
        try:
            pdf_output = compile_report(result.parts, name, major, factsheet=result.factsheet)
            result.pdf_path = pdf_output
            status("컴파일", f"✅ PDF 생성 완료: {pdf_output}", 0.95)
        except Exception as e:
            status("컴파일", f"❌ PDF 생성 실패: {e}", 0.90)
    else:
        status(
            "컴파일",
            f"⚠️ {total_parts - len(result.parts)}개 PART 누락 → PDF 미생성",
            0.90,
        )

    # ── Step 5: 사용량 기록 ──
    result.usage = get_usage_summary()
    status("완료", f"🏁 파이프라인 완료 (API 호출 {result.usage['calls']}회, ~${result.usage['estimated_cost_usd']})", 1.0)

    return result


def regenerate_part(
    result: PipelineResult,
    part_num: int,
    name: str,
    major: str,
    transcript: str,
    feedback: str | None = None,
) -> dict:
    """특정 PART만 재생성한다 (수동 재생성 버튼용).

    Returns:
        검수 결과 dict
    """
    # 팩트 시트 텍스트 재생성
    fs_text = None
    if result.factsheet:
        fs_text = factsheet_to_text(result.factsheet)

    text = generate_part(
        part_num=part_num,
        name=name,
        major=major,
        transcript=transcript,
        factsheet_text=fs_text,
        feedback=feedback,
        factsheet=result.factsheet,
    )
    result.parts[part_num] = text

    review = review_part(
        part_num=part_num,
        text=text,
        name=name,
        major=major,
        transcript=transcript,
        factsheet=result.factsheet,
    )
    result.reviews[part_num] = review
    result.usage = get_usage_summary()

    return review
