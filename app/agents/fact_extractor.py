"""팩트 시트 추출 에이전트

생기부 텍스트에서 구조화된 데이터(JSON)를 추출한다.
해석/분석 없이 텍스트에 명시된 데이터만 추출.
이 데이터가 PART 1~6 생성 + 검수의 "단일 진실 소스"가 된다.
"""

import json
import re
from config import MODEL, PROMPT_DIR
from skills.llm_caller import call_llm


def extract_factsheet(transcript: str) -> dict:
    """생기부 텍스트에서 팩트 시트를 추출한다.

    Args:
        transcript: 생기부 전체 텍스트

    Returns:
        구조화된 팩트 시트 dict

    Raises:
        ValueError: JSON 파싱 실패 또는 필수 키 누락
    """
    prompt_path = PROMPT_DIR / "factsheet.txt"
    raw_prompt = prompt_path.read_text(encoding="utf-8")

    # [시스템 프롬프트] / [유저 프롬프트] 분리
    sys_marker = "[시스템 프롬프트]"
    usr_marker = "[유저 프롬프트]"

    sys_idx = raw_prompt.find(sys_marker)
    usr_idx = raw_prompt.find(usr_marker)

    if sys_idx != -1 and usr_idx != -1:
        system_prompt = raw_prompt[sys_idx + len(sys_marker):usr_idx].strip()
        user_prompt = raw_prompt[usr_idx + len(usr_marker):].strip()
    else:
        system_prompt = "데이터 추출 전문가입니다. 해석하지 말고 데이터만 추출하세요."
        user_prompt = raw_prompt

    user_prompt = user_prompt.replace("{생기부 텍스트}", transcript)

    result = call_llm(system_prompt, user_prompt, model=MODEL)

    # JSON 파싱
    factsheet = _parse_factsheet(result)

    # 유효성 검증
    _validate_factsheet(factsheet)

    return factsheet


def _parse_factsheet(text: str) -> dict:
    """LLM 응답에서 JSON을 추출하여 파싱한다."""
    # ```json ... ``` 블록 추출
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if json_match:
        json_str = json_match.group(1)
    else:
        # JSON 블록 없으면 전체 텍스트에서 가장 바깥 { } 추출
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            json_str = json_match.group(0)
        else:
            raise ValueError(
                "팩트 시트 JSON 파싱 실패: JSON 블록을 찾을 수 없습니다.\n"
                f"LLM 응답 앞 500자: {text[:500]}"
            )

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"팩트 시트 JSON 파싱 실패: {e}\nJSON 앞 300자: {json_str[:300]}")


def _validate_factsheet(fs: dict) -> None:
    """팩트 시트 기본 유효성 검증."""
    required_keys = ["awards", "grades", "seukteuk", "clubs", "reading"]
    missing = [k for k in required_keys if k not in fs]
    if missing:
        raise ValueError(f"팩트 시트 필수 키 누락: {missing}")

    # awards가 리스트인지 확인
    if not isinstance(fs["awards"], list):
        raise ValueError(f"awards가 리스트가 아님: {type(fs['awards'])}")

    # grades가 dict인지 확인
    if not isinstance(fs["grades"], dict):
        raise ValueError(f"grades가 dict가 아님: {type(fs['grades'])}")


def factsheet_to_text(fs: dict) -> str:
    """팩트 시트 dict를 PART 생성용 텍스트로 변환한다.

    PART 1~6 프롬프트의 {생기부 텍스트} 앞에 삽입되어,
    GPT가 팩트 시트를 먼저 읽고 원문을 참조하도록 유도한다.
    """
    lines = [
        "=" * 60,
        "  팩트 시트 (생기부 데이터 요약 — 이 데이터를 기준으로 분석하세요)",
        "  ※ 아래 데이터와 모순되는 내용을 생성하지 마세요.",
        "  ※ '부재/부족/없음' 약점을 쓰기 전에 반드시 이 팩트 시트를 확인하세요.",
        "=" * 60,
        "",
    ]

    # ── 수상 경력 ──
    lines.append("[수상 경력]")
    if fs.get("awards"):
        for a in fs["awards"]:
            rank = f" {a['rank']}" if a.get("rank") else ""
            subject = f" ({a['subject']})" if a.get("subject") else ""
            lines.append(f"- {a.get('grade', '?')}: {a.get('name', '?')}{rank}{subject}")
        lines.append(f"→ 총 {len(fs['awards'])}건 수상")
    else:
        lines.append("- 수상 기록 없음")
    lines.append("")

    # ── 과목별 성취도/등급 ──
    lines.append("[과목별 성취도/등급]")
    if fs.get("grades"):
        for subject, semesters in fs["grades"].items():
            if isinstance(semesters, list):
                parts = []
                for s in semesters:
                    if isinstance(s, dict):
                        sem = s.get("semester", "?")
                        grade = s.get("grade", "?")
                        stype = f"({s['type']})" if s.get("type") else ""
                        parts.append(f"{sem} {grade} {stype}".strip())
                    else:
                        parts.append(str(s))
                grade_str = " → ".join(parts)
            else:
                grade_str = str(semesters)
            lines.append(f"- {subject}: {grade_str}")
    lines.append("")

    # ── 세특 활동 목록 ──
    lines.append("[세특 활동 목록]")
    if fs.get("seukteuk"):
        for s in fs["seukteuk"]:
            activities = ", ".join(s.get("activities", []))
            keywords = ", ".join(s.get("keywords", []))
            kw_str = f" [키워드: {keywords}]" if keywords else ""
            lines.append(f"- {s.get('subject', '?')}({s.get('grade', '?')}): {activities}{kw_str}")
    lines.append("")

    # ── 동아리 ──
    lines.append("[동아리/자율활동]")
    if fs.get("clubs"):
        for c in fs["clubs"]:
            if isinstance(c, dict):
                years = ", ".join(c.get("years", []))
                role = f" — {c['role']}" if c.get("role") else ""
                lines.append(f"- {c.get('name', '?')}: {years}{role}")
            else:
                lines.append(f"- {c}")
    lines.append("")

    # ── 독서 ──
    lines.append("[독서]")
    if fs.get("reading"):
        for r in fs["reading"]:
            if isinstance(r, dict):
                cat = f" ({r['category']})" if r.get("category") else ""
                yr = f" [{r['year']}]" if r.get("year") else ""
                lines.append(f"- {r.get('title', '?')}{cat}{yr}")
            else:
                lines.append(f"- {r}")
        lines.append(f"→ 총 {len(fs['reading'])}권")
    else:
        lines.append("- 독서 기록 없음")
    lines.append("")

    # ── 진로 활동 ──
    if fs.get("career_activities"):
        lines.append("[진로 활동]")
        for ca in fs["career_activities"]:
            lines.append(f"- {ca}")
        lines.append("")

    # ── 봉사 ──
    if fs.get("volunteer"):
        lines.append("[봉사 활동]")
        for v in fs["volunteer"]:
            lines.append(f"- {v}")
        lines.append("")

    # ── 행동특성 ──
    if fs.get("behavior"):
        lines.append("[행동특성 및 종합의견]")
        for grade, desc in fs["behavior"].items():
            lines.append(f"- {grade}: {desc}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("")

    return "\n".join(lines)
