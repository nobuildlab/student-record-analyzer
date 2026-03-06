"""리포트 컴파일 에이전트

PART 1~6 텍스트를 합쳐서 HTML 렌더링 → PDF 변환.
0단계 근거 블록은 제거 (내부 검수용이므로 고객 납품물에 불포함).
최종 PDF 전 금지 표현 자동 스캔.
"""

import re
from datetime import datetime
from config import OUTPUT_DIR
from skills.html_renderer import render_report_html
from skills.pdf_writer import html_to_pdf


def compile_report(
    parts: dict[int, str],
    student_name: str,
    major: str,
    factsheet: dict | None = None,
) -> str:
    """PART 1~6을 합쳐서 최종 PDF를 생성한다."""
    # 0단계 근거 블록 제거
    cleaned_parts = {}
    for num, text in parts.items():
        cleaned = _remove_evidence_block(text)
        cleaned = _fix_score_format(cleaned)
        # ── 확정 표현 완곡화 (보호 블록 전 선처리) ──
        cleaned = cleaned.replace("답변이 가능합니다", "답변이 가능할 것으로 보입니다")
        cleaned = cleaned.replace("어필할 수 있습니다", "어필할 수 있을 것으로 보입니다")
        # ── RC-FINAL v9: P 플레이스홀더 핵(nuclear) 제거 ──
        # v6~v8 regex 3종이 전부 실패 → 줄 단위 스캔으로 확실히 잡기
        _p_replacement = "(이 영역에 대해 면접에서 '구체적으로 어떤 역할을 했는지', '과정에서 무엇을 배웠는지' 등의 후속 질문이 나올 수 있습니다. 관련 활동의 동기·과정·성과를 정리해두세요.)"
        _lines = cleaned.split("\n")
        _new_lines = []
        for _line in _lines:
            _s = _line.strip().rstrip(".")
            # 줄 전체가 P+숫자만 (옵션: 마크다운 볼드**, 공백, 마침표)
            if _s and re.fullmatch(r"\*{0,2}\s*P\d{1,2}\s*\*{0,2}", _s):
                print(f"  [v9-early] PART {num}: P플레이스홀더 조기 제거 → {repr(_s)}")
                _new_lines.append(_p_replacement)
            else:
                # 인라인 "예상...질문..." 뒤에 P숫자 (** 포함 대응)
                _line = re.sub(
                    r"(\*{0,2}예상[^\n]*?질문[^\n]*?)\*{0,2}\s*P\d{1,2}\b",
                    rf"\1 {_p_replacement}",
                    _line,
                )
                _new_lines.append(_line)
        cleaned = "\n".join(_new_lines)
        # ── RC-FINAL v5: 보호 블록 추출 (인용문·질문·출처) ──
        cleaned, protected = _protect_blocks(cleaned)
        cleaned = _sanitize_expressions(cleaned, part_num=num)
        # 팩트시트 기반 환각 교정 + 성장 궤적 보충 (RC-FINAL v2)
        if factsheet:
            cleaned = _fix_incorrect_grade_years(cleaned, factsheet)
            cleaned = _apply_grade_growth(cleaned, factsheet)
            cleaned = _fix_activity_year_labels(cleaned, factsheet)
        # PART별 전용 후처리
        if num == 3:
            cleaned = _fix_part3_positive_density(cleaned)
        elif num == 4:
            cleaned = _fix_part4_script(cleaned)
        elif num == 6:
            cleaned = _fix_part6_disclaimer(cleaned)
        # ── 보호 블록 복원 ──
        cleaned = _restore_blocks(cleaned, protected)
        # ── RC-FINAL v6: 복원 후 2차 정리 (절대 금칙어 + 플레이스홀더) ──
        cleaned = _post_restore_cleanup(cleaned)
        # ── 확정 표현 최종 완곡화 (보호 블록 복원 후) ──
        cleaned = cleaned.replace("답변이 가능합니다", "답변이 가능할 것으로 보입니다")
        cleaned = cleaned.replace("어필할 수 있습니다", "어필할 수 있을 것으로 보입니다")
        cleaned_parts[num] = cleaned

    # ── RC-FINAL v9: 최종 P 플레이스홀더 안전망 (모든 처리 완료 후) ──
    _p_final = "(이 영역에 대해 면접에서 '구체적으로 어떤 역할을 했는지', '과정에서 무엇을 배웠는지' 등의 후속 질문이 나올 수 있습니다. 관련 활동의 동기·과정·성과를 정리해두세요.)"
    for num in list(cleaned_parts.keys()):
        text = cleaned_parts[num]
        lines = text.split("\n")
        rebuilt = []
        for line in lines:
            s = line.strip()
            # \x02P숫자\x03 잔류 (보호 블록 복원 실패) → 교체
            if re.fullmatch(r"\x02P\d{1,3}\x03", s):
                print(f"  [v9-final] PART {num}: 보호블록 잔류 발견 → {repr(s)}")
                rebuilt.append(_p_final)
            # 순수 P+숫자만 있는 줄 → 교체
            elif s and re.fullmatch(r"\*{0,2}\s*P\d{1,2}\s*\*{0,2}\.?\s*", s):
                print(f"  [v9-final] PART {num}: P플레이스홀더 발견 → {repr(s)}")
                rebuilt.append(_p_final)
            else:
                rebuilt.append(line)
        cleaned_parts[num] = "\n".join(rebuilt)

    # ── connection_type 태그 정리 (PDF용) ──
    for num in list(cleaned_parts.keys()):
        text = cleaned_parts[num]
        # [직접] → 제거 (기본이므로 표시 불필요)
        text = text.replace("[직접]", "")
        # [해석] → "(해석 기반 연결)" 으로 완화 표기
        text = text.replace("[해석]", "(해석 기반 연결)")
        # 연속 공백 정리
        text = re.sub(r"  +", " ", text)
        cleaned_parts[num] = text

    # ── 최종 단계: _sanitize_expressions에서 line-level 보호로 누락된 패턴 처리 ──
    for num in list(cleaned_parts.keys()):
        text = cleaned_parts[num]
        # ★★★★★ 잔존 치환
        text = text.replace("★★★★★", "★★★★☆")
        text = re.sub(r"[★☆]{5,}", lambda m: m.group(0)[:5], text)  # 5개 초과 방지
        # 띄어쓰기 누락 자동 수정 ("하면준비" → "하면 준비" 등)
        text = re.sub(r"(하면)([가-힣])", r"\1 \2", text)
        text = re.sub(r"(하고)([가-힣])", r"\1 \2", text)
        text = re.sub(r"(하며)([가-힣])", r"\1 \2", text)
        text = re.sub(r"(니다)([가-힣])", r"\1 \2", text)
        text = re.sub(r"(습니다)([가-힣])", r"\1 \2", text)
        # 연속 공백 정리
        text = re.sub(r"  +", " ", text)
        cleaned_parts[num] = text

    # 최종 금지 표현 스캔
    warnings = _final_content_scan("\n".join(cleaned_parts.values()))
    if warnings:
        print(f"⚠️ 최종 스캔 경고: {warnings}")

    # HTML 렌더링
    html = render_report_html(cleaned_parts, student_name, major)

    # PDF 변환
    today = datetime.now().strftime("%Y%m%d")
    filename = f"{student_name}_{today}_리포트.pdf"
    output_path = str(OUTPUT_DIR / filename)

    html_to_pdf(html, output_path)
    return output_path


def _remove_evidence_block(text: str) -> str:
    """0단계 근거 블록 섹션을 완전히 제거한다."""
    result = text

    # 패턴 1: "### 0단계" 또는 "## 0단계" 로 시작 → 다음 "### 1" 또는 "## 1" 또는 "### A." 또는 "A. " 이전
    result = re.sub(
        r"(?:#{1,3}\s*)?0단계[^\n]*\n[\s\S]*?(?=#{1,3}\s*[1-9A]|[A-C]\.\s)",
        "",
        result,
    )

    # 패턴 2: "0단계: 근거 블록 추출" 으로 시작하는 줄 ~ [근거 N] 블록 끝
    result = re.sub(
        r"0단계\s*[:：]?\s*근거\s*블록[\s\S]*?(?=#{1,3}\s*[1-9A]|[A-C]\.\s|$)",
        "",
        result,
    )

    # 패턴 3: 혹시 남은 [근거 1] ~ [근거 N] 블록
    result = re.sub(
        r"\[근거\s*\d+\]\s*\n(?:[-\s].*\n)*",
        "",
        result,
    )

    # "---" 구분선 중복 제거
    result = re.sub(r"(\n---\s*){2,}", "\n---\n", result)
    # 앞쪽 빈 줄 정리
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()


def _fix_score_format(text: str) -> str:
    """점수 표기 슬래시 깨짐 방지.
    '4/5' → '4점', '4.0/5' → '4.0점'
    단, "76/57.6" 같은 원점수/평균 형식은 건드리지 않음.
    """
    # 표 안의 점수: 단독 "N/5" → "N점" (N은 1자리, 뒤에 소수점/숫자 없음)
    # "76/57.6" 같은 원점수/평균 보호: 분모 뒤에 숫자·소수점 있으면 스킵
    text = re.sub(r"(?<!\d)(\d)\s*/\s*5(?![\d.])", r"\1점", text)
    # 소수점 포함: "N.N/5" → "N.N점" (분모 뒤에 숫자·소수점 없음)
    text = re.sub(r"(?<!\d)(\d\.\d+)\s*/\s*5(?![\d.])", r"\1점", text)
    return text


def _protect_blocks(text: str) -> tuple[str, list[str]]:
    """인용문·예상 질문·출처 등 치환 보호 영역을 플레이스홀더로 교체한다 (RC-FINAL v5).

    보호된 영역은 _sanitize_expressions, _fix_incorrect_grade_years 등
    안전망 함수의 치환 대상에서 제외된다.
    """
    blocks: list[str] = []

    def _save(m):
        idx = len(blocks)
        blocks.append(m.group(0))
        return f"\x02P{idx}\x03"

    # 1. 큰따옴표 인용문 (10자 이상 — 생기부 원문 인용 보호)
    text = re.sub(r'"[^"\n]{10,}"', _save, text)

    # 2. 예상 (공격) 질문 라벨 + 직후 내용 줄 (마크다운 볼드 ** 대응)
    text = re.sub(r'\*{0,2}예상\s*(?:공격\s*)?질문\s*[:：]\*{0,2}\s*\n?[^\n]+', _save, text)

    # 3. "— 출처:" 줄
    text = re.sub(r'[-—]\s*출처\s*[:：][^\n]*', _save, text)

    return text, blocks


def _restore_blocks(text: str, blocks: list[str]) -> str:
    """플레이스홀더를 원본 보호 블록으로 복원한다.

    역순으로 복원: P1 내부에 P0이 중첩될 수 있으므로,
    바깥(큰 인덱스)부터 복원해야 내부 플레이스홀더가 노출된 후 순서대로 처리됨.
    """
    for idx in reversed(range(len(blocks))):
        text = text.replace(f"\x02P{idx}\x03", blocks[idx])
    return text


def _post_restore_cleanup(text: str) -> str:
    """보호 블록 복원 후 절대 금칙어·플레이스홀더를 2차 정리한다 (RC-FINAL v6).

    보호 블록 내부에 있던 금칙어(실무/현업 등)도 여기서 최종 치환.
    LLM이 생성한 "예상 공격 질문: P4" 같은 플레이스홀더도 제거.
    """
    # 1. 절대 금칙어 (보호 블록 내부에 있어도 무조건 치환)
    text = re.sub(r"실무\s*경험을\s*쌓[가-힣]*",
                  "교내 탐구·프로젝트를 통해 구현 경험을 축적했", text)
    text = re.sub(r"실무\s*경험", "교내 프로젝트 기반 구현 경험", text)
    text = re.sub(r"실무", "교내 실습", text)
    text = re.sub(r"현업", "교내 실습", text)
    text = re.sub(r"전문가\s*수준", "심화 탐구 수준", text)
    text = re.sub(r"프로\s*수준", "심화 수준", text)

    # 2. LLM 플레이스홀더 질문 제거 (RC-FINAL v9: 핵 방식 — 줄 단위 스캔)
    _p_repl = "(이 영역에 대해 면접에서 '구체적으로 어떤 역할을 했는지', '과정에서 무엇을 배웠는지' 등의 후속 질문이 나올 수 있습니다. 관련 활동의 동기·과정·성과를 정리해두세요.)"
    _lines2 = text.split("\n")
    _rebuilt = []
    for _ln in _lines2:
        _st = _ln.strip().rstrip(".")
        if _st and re.fullmatch(r"\*{0,2}\s*P\d{1,2}\s*\*{0,2}", _st):
            _rebuilt.append(_p_repl)
        else:
            _ln = re.sub(
                r"(\*{0,2}예상[^\n]*?질문[^\n]*?)\*{0,2}\s*P\d{1,2}\b",
                rf"\1 {_p_repl}",
                _ln,
            )
            _rebuilt.append(_ln)
    text = "\n".join(_rebuilt)

    return text


def _sanitize_expressions(text: str, part_num: int = 0) -> str:
    """판매 리스크가 있는 표현을 자동 치환한다 (최종 안전망)."""
    # ── 보호 블록 플레이스홀더(\x02P{n}\x03) 포함 줄 보호 ──
    # _protect_blocks의 플레이스홀더가 치환 패턴에 걸려 손상되는 것을 방지
    _ph_store: dict[str, str] = {}
    _ph_counter = 0

    def _save_ph_line(m):
        nonlocal _ph_counter
        key = f"\x04SAFE{_ph_counter}\x05"
        _ph_store[key] = m.group(0)
        _ph_counter += 1
        return key

    text = re.sub(r"[^\n]*\x02[^\n]*", _save_ph_line, text)

    replacements = [
        # "좋은 결과 기대" 문장 전체 삭제 (공백 유지 → 문장 결합 깨짐 방지)
        (r"[^.。\n]*좋은\s*결과를?\s*기대[^.。\n]*[.。]?\s*", " "),
        (r"충분히\s*좋은\s*(?:면접\s*)?결과[^.。\n]*[.。]?\s*", " "),
        # "좋은 평가를 받을 수 있" 변형 (RC-FINAL v6)
        (r"좋은\s*평가를?\s*받을\s*수\s*있[^.。\n]*[.。]?\s*",
         "준비한 만큼 면접에서 충분히 강점을 어필할 수 있습니다. "),
        # "긍정적인 결과" 문장 전체 삭제 (판매 리스크)
        (r"[^.。\n]*긍정적인\s*결과로?\s*이어[^.。\n]*[.。]?\s*", " "),
        (r"합격\s*가능성이\s*높습니다",
         "설득력 있는 답변이 가능할 것으로 보입니다"),
        # 결과 예측 뉘앙스 추가 완화 (RC v5 + RC-FINAL v4)
        (r"긍정적으로\s*평가받을\s*가능성이?\s*(?:있|높|큽)습니다",
         "설득력 있는 답변이 가능할 것으로 보입니다"),
        (r"긍정적으로\s*평가받을\s*가능성이?\s*(?:있|높|크)다",
         "설득력 있는 답변이 가능할 것으로 보입니다"),
        # "긍정적인 평가를 받을 가능성" 변형 (RC-FINAL v5 → v7 확장)
        (r"긍정적인\s*평가를\s*받을\s*가능성이?\s*(?:있|높|큽)습니다",
         "설득력 있는 답변이 가능할 것으로 보입니다"),
        (r"[^.。\n]*긍정적인\s*평가를?\s*받을\s*가능성[^.。\n]*[.。]?\s*",
         "면접에서 강점으로 활용할 수 있습니다. "),
        # "가능성이 높습니다/큽니다/높음/큼" 단독 변형 (RC-FINAL v4 → v5 확장)
        (r"[^.。\n]*가능성이\s*(?:높습니다|큽니다|높다|크다|높음|큼)",
         "면접에서 충분히 어필할 수 있는 요소입니다"),
        (r"높은\s*가능성", "충분한 어필 요소"),
        # 단정형 "가장 유리/적합"
        (r"가장\s*유리합니다",
         "강점이 잘 드러나는 편입니다"),
        (r"가장\s*적합합니다",
         "상대적으로 적합도가 높은 편입니다"),
        # 성취도/등급 혼용 자동 수정
        (r"성취도\s*([A-E])\s*등급", r"성취도 \1"),
        # 생기부에 없는 확장 키워드 완화
        (r"빅데이터", "데이터 활용"),
        # 추정형 리스크 문장 → 면접 대비 프레임
        (r"이해가\s*충분하지\s*않을\s*수\s*있(?:으며|어|습니다)",
         "관련 주제는 면접에서 확장 질문이 나올 수 있습니다"),
        # 깊이 부족 — 문장 단위 교체 (구 단위 시 "~있습니다는 점이" 문장 붕괴 방지, RC-FINAL v9)
        (r"[^.。\n]*깊이가?\s*부족할?\s*수\s*있(?:다|습니다|어)[^.。\n]*[.。]?\s*",
         "면접에서 과정 설명을 준비하면 유리할 수 있습니다. "),
        # 이해 부족 계열 — 문장 단위 교체 (구 단위 교체 시 문장 붕괴 방지, RC v5)
        # 구체적 패턴 먼저, 포괄 패턴 나중에
        (r"[^.。\n]*윤리적\s*(?:문제\s*)?이해\s*(?:가\s*)?부족[^.。\n]*[.。]?\s*",
         "윤리적 관점에 대해 면접에서 추가 질문이 나올 수 있는 영역입니다. "),
        (r"[^.。\n]*이해\s*부족\s*가능성[^.。\n]*[.。]?\s*",
         "면접에서 질문 가능성이 있는 지점입니다. "),
        (r"[^.。\n]*이해도?가?\s*(?:다소\s*)?부족[^.。\n]*[.。]?\s*",
         "면접에서 확인 질문이 나올 수 있는 영역입니다. "),
        # 단정형 인증/평가 완화 (넓은 패턴)
        (r"[가-힣\s]+(?:을|를)\s*보여주는\s*학생입니다",
         "기록상 전공 관련 탐구가 꾸준히 나타납니다"),
        (r"[가-힣\s]+(?:을|를|도)\s*입증하였습니다",
         "활동 흐름에서 관련 역량이 확인됩니다"),
        (r"[가-힣\s]+(?:을|를|도)\s*입증합니다",
         "활동 흐름에서 관련 역량이 확인됩니다"),
        (r"[가-힣\s]+(?:을|를)\s*증명합니다",
         "관련 활동이 기록에서 확인됩니다"),
        (r"확실히\s*드러납니다",
         "기록에서 확인됩니다"),
        (r"[가-힣\s]+(?:을|를)\s*갖추고\s*있음을\s*보여줍니다",
         "관련 경험이 기록에 나타납니다"),
        # (정보과학은 3학년 진로선택 과목으로 실제 존재 → 치환 제거, RC v5)
        # ── 정보 성취도 불일치 방지 (B/C → C) ──
        (r"정보\s*(?:과목\s*)?(?:의\s*)?성취도\s*B\s*/\s*C", "정보 성취도 C"),
        (r"성취도\s*B\s*/\s*C", "성취도 C"),
        (r"B\s*/\s*C\s*(?:편차|혼재|변동)", "C"),
        (r"B\s*/\s*C\s*등급", "C"),  # B/C등급 묶음 표현 금지
        # 성취도 콜론 표기 정규화 (성취도: X → 성취도 X)
        (r"성취도\s*[:：]\s*([A-E])", r"성취도 \1"),
        # ── 추가 단정형 패턴 (Release Fix v2) ──
        (r"[가-힣\s,]+(?:이|가)\s*돋보이는\s*학생입니다",
         "기록상 전공 관련 탐구가 꾸준히 나타납니다"),
        # "보여주며" → PART 4·5 제외 조건부 블록으로 이동 (RC-FINAL v7)
        # "학생입니다" catch-all → PART 4 제외 (아래 조건부 블록에서 처리, RC v5)
        # ── N 플레이스홀더 치환 ──
        (r"리스크\s+N\s+참고", "리스크 1 참고"),
        (r"C-N\s+참고", "C-1 참고"),
        # ── 확정 표현 완곡화 (Release Fix v4) ──
        # "설득력 있는 답변이 가능합니다" → 완곡 표현
        (r"설득력\s*있는\s*답변이\s*가능합니다",
         "설득력 있는 답변이 가능할 것으로 보입니다"),
        (r"답변이\s*가능합니다",
         "답변이 가능할 것으로 보입니다"),
        (r"어필할\s*수\s*있습니다",
         "어필할 수 있을 것으로 보입니다"),
        # ── 톤 완화 패턴 (Release Fix v3) ──
        (r"적합도가\s*높습니다",
         "강점이 드러나는 편입니다"),
        (r"[가-힣]+(?:이|가)\s*돋보입니다",
         "이 기록에서 확인됩니다"),
        # "돋보이며" 연결형 (v4 잔존 패치)
        (r"[가-힣]+(?:이|가)\s*돋보이며",
         "이 기록에서 나타나며"),
        # "돋보임" 종결형 (표 셀·명사형 종결)
        (r"(?:더\s*)?돋보임", "기록에서 확인됨"),
        # "보여줍니다/보여줌/보여주고" → PART 4·5 제외 조건부 블록으로 이동 (RC-FINAL v7)
        # ── RC-FINAL v2: 실무/현업/전문가 수준 표현 전면 제거 ──
        # (순서 중요: 부정맥락 문장삭제 → 구문치환 → 단어치환)
        # 1. 부정적 맥락 문장 전체 교체
        (r"[^.。\n]*실무\s*경험이?\s*(?:나|이)?\s*(?:부족|없|미흡)[^.。\n]*[.。]?\s*",
         "교내 탐구·프로젝트 경험을 중심으로 면접 답변을 준비하면 효과적입니다. "),
        (r"[^.。\n]*실무\s*경험이?\s*(?:나|과)?\s*프로젝트\s*실적이?\s*(?:부족|없)[^.。\n]*[.。]?\s*",
         "교내 탐구·프로젝트 경험을 중심으로 면접 답변을 준비하면 효과적입니다. "),
        # 2. "실무 경험을 쌓았다/축적했다"
        (r"실무\s*경험을\s*쌓[가-힣]*",
         "교내 탐구·프로젝트를 통해 구현 경험을 축적했"),
        # 3. "실무 경험" (구 단위)
        (r"실무\s*경험", "교내 프로젝트 기반 구현 경험"),
        # 4. "실무" 단독
        (r"실무", "교내 실습"),
        # 5. 현업/전문가 수준/프로 수준
        (r"현업", "교내 실습"),
        (r"전문가\s*수준", "심화 탐구 수준"),
        (r"프로\s*수준", "심화 수준"),
        # ── RC-FINAL v3: 등급 과장 표현 방지 ──
        (r"(\d)등급\s*다수", r"\1등급 포함, 교과 성취 안정적"),
        # ── RC-FINAL v3: "연결성이 드러나지 않을 수 있다" → 준비 방향 톤 ──
        (r"[^.。\n]*연결성이?\s*(?:충분히\s*)?드러나지\s*않[^.。\n]*[.。]?\s*",
         "기록에 드러난 연결성을 면접에서 더 구체적으로 설명할 준비가 필요합니다. "),
        # NOTE: 성취도 하락/퇴보/머물 패턴은 PART별 조건 분기로 이동 (아래 참조)
        # "보여주었습니다/보여주었고/보여주었으며" → PART 4·5 제외 조건부 블록으로 이동 (RC-FINAL v7)
        # ── 문법 오류 자동 수정 ──
        ("활용는", "활용은"),
        ("활용와", "활용과"),
        (r"활용의\s*활용", "활용의"),
        # RC-FINAL v4: "활용 활용" 단어 중복 제거
        (r"활용\s+활용", "활용"),
        # ★★★★★ → ★★★★☆ (만점 방지, 판매 리스크 완화)
        ("★★★★★", "★★★★☆"),
        # v5: LLM 오타 자동 수정
        ("프로그램밍", "프로그래밍"),
        ("프로그램링", "프로그래밍"),
        ("알고리듬", "알고리즘"),
        # v5: "~보입니다는 것입니다" 어색한 중복 문장 수정
        (r"보입니다는\s*것입니다", "보입니다"),
        (r"있습니다는\s*것입니다", "있습니다"),
        # v5: "긍정적인 평가를 받을 수 있을 것으로 보입니다" → 완화
        (r"긍정적인\s*평가를?\s*받을\s*수\s*있[^.。\n]*[.。]?\s*",
         "면접에서 설득력 있게 설명하면 강점으로 작용할 수 있습니다. "),
        # v6: "긍정적인 결과를 기대할 수 있습니다" → 완화
        (r"[^.。\n]*긍정적인?\s*결과를?\s*기대[^.。\n]*[.。]?\s*",
         "준비를 정교화하면 면접에서 강점을 충분히 어필할 수 있습니다. "),
        # v6→v7: "긍정적(인) 평가를 받을 가능성이 높아질" → 완화
        (r"[^.。\n]*긍정적인?\s*평가를?\s*받을\s*가능성[^.。\n]*[.。]?\s*",
         "면접에서 강점으로 활용할 수 있습니다. "),
        # v6: "가능할 것으로 보입니다" → 살짝 완화 (예측 뉘앙스 축소)
        (r"가능할\s*것으로\s*보입니다",
         "가능합니다"),
    ]
    result = text
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result)

    # ── RC-FINAL v6: 성취도 부정 프레이밍 — PART별 분기 ──
    if part_num == 2:
        # PART 2 (리스크 분석): 단어 수준만 교체 → 문장 구조 보존
        result = re.sub(r"성취도[가는은이]?\s*(?:에\s*)?(?:하락|퇴보)", "성취도 변동", result)
        result = re.sub(r"성취도[가는은이]?\s*(?:에\s*)?(?:떨어|낮아)", "성취도가 다소 낮아", result)
        result = re.sub(r"성취도[가는은이]?\s*[^.。\n]{0,20}머물", "성취도가 초기에 낮았으나 이후 변화", result)
    else:
        result = re.sub(
            r"[^.。\n]*성취도[가는은이]?\s*[^.。\n]*(?:하락|퇴보|떨어|낮아)[^.。\n]*[.。]?\s*",
            "성취도의 변동이 있었으나 이후 개선된 흐름이 확인됩니다. ",
            result,
        )
        result = re.sub(
            r"[^.。\n]*성취도[가는은이]?\s*[^.。\n]*머물[^.。\n]*[.。]?\s*",
            "초기 성취도에 대한 질문이 나올 수 있으므로, 성장 과정과 학습 전략을 중심으로 대비하는 것이 필요합니다. ",
            result,
        )

    # PART 4(서사 설계도), PART 5(답변 가이드)는 조언 문맥 → "학생입니다" 정상
    if part_num not in (4, 5):
        result = re.sub(
            r"[^.。\n]+학생입니다\s*[.。]?",
            "기록상 전공 관련 역량이 꾸준히 나타납니다.",
            result,
        )

    # ── RC-FINAL v8: "보여줍니다" 계열 — PART 4·5 제외, 을/를 조사 분리, 부사 허용 ──
    _adv = r"(?:잘\s*|매우\s*|충분히\s*|특히\s*)?"  # 선택적 부사
    if part_num not in (4, 5):
        show_patterns = [
            # 을 → 이 (받침 있는 명사: 능력을, 역량을, 관심을 등)
            (rf"을\s*{_adv}보여주며", "이 기록에서 나타나며"),
            (rf"을\s*{_adv}보여주고\s*있습니다", "이 기록에서 나타납니다"),
            (rf"을\s*{_adv}보여줍니다", "이 기록에서 나타납니다"),
            (rf"을\s*{_adv}보여줌으로써", "이 기록에서 나타나며"),
            (rf"을\s*{_adv}보여줌", "이 기록에서 나타남"),
            (rf"을\s*{_adv}보여주었습니다", "이 기록에서 나타납니다"),
            (rf"을\s*{_adv}보여주었고", "이 기록에서 나타나며"),
            (rf"을\s*{_adv}보여주었으며", "이 기록에서 나타나며"),
            # 를 → 가 (받침 없는 명사: 이해를, 태도를, 자세를 등)
            (rf"를\s*{_adv}보여주며", "가 기록에서 나타나며"),
            (rf"를\s*{_adv}보여주고\s*있습니다", "가 기록에서 나타납니다"),
            (rf"를\s*{_adv}보여줍니다", "가 기록에서 나타납니다"),
            (rf"를\s*{_adv}보여줌으로써", "가 기록에서 나타나며"),
            (rf"를\s*{_adv}보여줌", "가 기록에서 나타남"),
            (rf"를\s*{_adv}보여주었습니다", "가 기록에서 나타납니다"),
            (rf"를\s*{_adv}보여주었고", "가 기록에서 나타나며"),
            (rf"를\s*{_adv}보여주었으며", "가 기록에서 나타나며"),
        ]
        for p, r in show_patterns:
            result = re.sub(p, r, result)

    # ── 보호 블록 플레이스홀더 포함 줄 복원 ──
    for key, original in _ph_store.items():
        result = result.replace(key, original)

    # 컴파일러 치환 후 아티팩트 수정
    result = _post_compile_fix(result)

    return result


def _fix_incorrect_grade_years(text: str, factsheet: dict) -> str:
    """팩트시트에 없는 학년+과목+성적 환각을 교정한다 (RC-FINAL v2).

    예: 팩트시트에 1학년 정보가 없는데 "1학년 정보 B" 언급 → 올바른 성장 서사로 교체.
    """
    fs_grades = factsheet.get("grades", {})

    for subj, sems in fs_grades.items():
        if not isinstance(sems, list) or len(subj) < 2:
            continue

        # 이 과목이 존재하는 학년 + 학기별 성적
        valid_years = set()
        grade_by_sem = []
        for s in sems:
            if isinstance(s, dict):
                sem = s.get("semester", "")
                raw_g = s.get("grade", "")
                letter = re.match(r"([A-E])", raw_g)
                year_m = re.match(r"(\d)", sem)
                if year_m:
                    valid_years.add(year_m.group(1))
                if letter:
                    grade_by_sem.append((sem, letter.group(1)))

        if not valid_years or not grade_by_sem:
            continue

        # 성장 궤적 텍스트 (예: "2-1 C → 2-2 B")
        trajectory = " → ".join(f"{sem} {g}" for sem, g in grade_by_sem)

        # 관련 과목 (정보 → 정보과학 등) 정보 수집
        related_info = []
        for fs_subj, fs_sems in fs_grades.items():
            if subj in fs_subj and fs_subj != subj and len(fs_subj) > len(subj):
                if isinstance(fs_sems, list):
                    for s in fs_sems:
                        if isinstance(s, dict):
                            raw = s.get("grade", "")
                            lm = re.match(r"([A-E])", raw)
                            if lm:
                                related_info.append(f"{fs_subj} {lm.group(1)}")
                                break

        # 실제 성장이 있는지 확인 (A→A 등은 "개선"이 아님, RC-FINAL v5)
        grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
        grades_only = [g for _, g in grade_by_sem]
        lowest_val = min(grade_val.get(g, 0) for g in grades_only)
        highest_val = max(grade_val.get(g, 0) for g in grades_only)
        has_improvement = lowest_val < highest_val

        # 교체 문장 생성
        if not has_improvement:
            # 변동 없음 (A→A 등) → "개선" 표현 사용 금지
            if related_info:
                replacement = (
                    f"{subj} 과목은 성취도 {grades_only[0]}으로 안정적인 성취를 기록했으며, "
                    f"이후 {', '.join(related_info)} 성취를 이어갔습니다. "
                )
            else:
                replacement = f"{subj} 과목은 성취도 {grades_only[0]}으로 안정적인 성취를 유지했습니다. "
        elif related_info:
            replacement = (
                f"{subj} 과목은 {trajectory} 흐름으로 개선되었고, "
                f"이후 {', '.join(related_info)} 성취를 기록했습니다. "
            )
        else:
            replacement = f"{subj} 과목은 {trajectory} 흐름으로 성취도가 개선되었습니다. "

        # 없는 학년 체크: 해당 학년+과목+성적 언급 문장 교체
        subj_esc = re.escape(subj)
        for year in ["1", "2", "3"]:
            if year not in valid_years:
                # "1학년 ... 정보 ... [성취도|A-E]" (순서 무관, 같은 문장 내)
                p1 = rf"[^.。\n]*{year}학년[^.。\n]*{subj_esc}[^.。\n]*(?:성취|[A-E](?:\s|로|에서|,|를))[^.。\n]*[.。]?\s*"
                p2 = rf"[^.。\n]*{subj_esc}[^.。\n]*{year}학년[^.。\n]*(?:성취|[A-E](?:\s|로|에서|,|를))[^.。\n]*[.。]?\s*"
                text = re.sub(p1, replacement, text)
                text = re.sub(p2, replacement, text)

    return text


def _fix_activity_year_labels(text: str, factsheet: dict) -> str:
    """팩트시트 기반으로 활동·과목의 학년 레이블을 교정한다 (Stage 1).

    LLM이 올바른 활동명/과목명을 사용하되 학년을 잘못 배정한 경우를 교정.
    예: "영어Ⅰ (1학년)" → "영어Ⅰ (2학년)" (팩트시트 세특에 2학년으로 기록)
    """
    seukteuk = factsheet.get("seukteuk", [])
    clubs = factsheet.get("clubs", [])
    reading = factsheet.get("reading", [])

    # ── 1. 과목명 → 유효 학년(들) 매핑 ──
    subj_years: dict[str, set[str]] = {}
    for entry in seukteuk:
        subj = entry.get("subject", "")
        grade_str = entry.get("grade", "")
        ym = re.search(r"(\d)", grade_str)
        if subj and ym:
            subj_years.setdefault(subj, set()).add(ym.group(1))

    # ── 2. 활동 키워드 → 학년 매핑 (세특 + 동아리) ──
    kw_to_years: dict[str, set[str]] = {}
    for entry in seukteuk:
        grade_str = entry.get("grade", "")
        ym = re.search(r"(\d)", grade_str)
        if not ym:
            continue
        y = ym.group(1)
        for act in entry.get("activities", []):
            for w in re.findall(r"[가-힣a-zA-Z]{4,}", act):
                kw_to_years.setdefault(w, set()).add(y)
    for c in clubs:
        if not isinstance(c, dict):
            continue
        cname = c.get("name", "")
        for year_str in c.get("years", []):
            ym = re.search(r"(\d)", year_str)
            if ym and cname:
                for w in re.findall(r"[가-힣a-zA-Z]{3,}", cname):
                    kw_to_years.setdefault(w, set()).add(ym.group(1))

    # 한 학년에서만 등장하는 고유 키워드
    unique_kw: dict[str, str] = {
        kw: next(iter(yrs))
        for kw, yrs in kw_to_years.items()
        if len(yrs) == 1
    }

    # ── 3. 독서 도서 → 세특 출처 학년 매핑 ──
    book_year: dict[str, str] = {}
    for r in reading:
        title = r.get("title", "") if isinstance(r, dict) else str(r)
        if len(title) < 2:
            continue
        for entry in seukteuk:
            grade_str = entry.get("grade", "")
            ym = re.search(r"(\d)", grade_str)
            if not ym:
                continue
            acts_text = " ".join(entry.get("activities", []))
            if title in acts_text:
                book_year[title] = ym.group(1)
                break

    # ═══ 교정 실행 ═══

    # A. 과목명 + 학년 교정 (유일 학년인 과목만)
    for subj, valid in subj_years.items():
        if len(valid) != 1:
            continue  # 다년도 과목은 건너뜀
        correct = next(iter(valid))
        esc = re.escape(subj)
        for wrong in ("1", "2", "3"):
            if wrong == correct:
                continue
            # "영어Ⅰ (1학년)" → "영어Ⅰ (2학년)"
            text = re.sub(
                rf"({esc}\s*)\({wrong}학년\)",
                rf"\g<1>({correct}학년)",
                text,
            )
            # "1학년 영어Ⅰ" → "2학년 영어Ⅰ" (직접 인접)
            text = re.sub(
                rf"(?<![0-9·~]){wrong}학년(\s{{1,3}}){esc}(?=[\s,.)·])",
                f"{correct}학년\\g<1>{subj}",
                text,
            )

    # B. 활동 키워드 + 학년 교정 (4자 이상 고유 키워드)
    for kw, correct in unique_kw.items():
        if len(kw) < 4:
            continue
        esc = re.escape(kw)
        for wrong in ("1", "2", "3"):
            if wrong == correct:
                continue
            # "아두이노 발열 실험 (1학년 동아리활동)" → "(2학년 동아리활동)"
            text = re.sub(
                rf"({esc}[^()\n]{{0,40}})\({wrong}(학년[^)]*)\)",
                rf"\g<1>({correct}\g<2>)",
                text,
            )

    # C. 독서 도서명 + 학년 교정
    for title, correct in book_year.items():
        t_esc = re.escape(title)
        for wrong in ("1", "2", "3"):
            if wrong == correct:
                continue
            # "『컴퓨터 구조』 (3학년)" → "(2학년)"
            text = re.sub(
                rf"({t_esc}[^()\n]{{0,15}})\({wrong}(학년[^)]*)\)",
                rf"\g<1>({correct}\g<2>)",
                text,
            )

    return text


def _apply_grade_growth(text: str, factsheet: dict) -> str:
    """성취도를 최저만 언급한 경우, 팩트시트 기반으로 성장 궤적을 보충한다 (RC-FINAL)."""
    fs_grades = factsheet.get("grades", {})
    grade_val = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}

    for m in list(re.finditer(r"([\w가-힣]+)\s*(?:과목\s*)?(?:에서\s*)?성취도\s*([A-E])", text)):
        subj = m.group(1)
        mentioned = m.group(2)

        # 같은 과목의 모든 성적 수집
        subj_grades = []
        for fs_subj, sems in fs_grades.items():
            if subj == fs_subj or (len(subj) >= 2 and subj == fs_subj):
                if isinstance(sems, list):
                    for s in sems:
                        if isinstance(s, dict):
                            raw_g = s.get("grade", "")
                            # 점수 정보 제거: "C(75/67.2)" → "C" (RC-FINAL)
                            letter = re.match(r"([A-E])", raw_g)
                            g = letter.group(1) if letter else raw_g
                            if g in grade_val:
                                subj_grades.append(g)

        if not subj_grades:
            continue

        best = max(subj_grades, key=lambda g: grade_val[g])

        # 성장이 있으면 궤적 추가
        if grade_val[mentioned] < grade_val[best]:
            unique = list(dict.fromkeys(subj_grades))
            trajectory = "→".join(unique)

            # 관련 과목 체크 (정보 → 정보과학 등)
            related = []
            for fs_subj, sems in fs_grades.items():
                if subj in fs_subj and fs_subj != subj and len(fs_subj) > len(subj):
                    if isinstance(sems, list):
                        rel_g = []
                        for s in sems:
                            if isinstance(s, dict):
                                raw = s.get("grade", "")
                                lm = re.match(r"([A-E])", raw)
                                if lm:
                                    rel_g.append(lm.group(1))
                        if rel_g:
                            related.append(f"{fs_subj} {rel_g[0]}")

            replacement = f"{subj} 성취도 {trajectory}"
            if related:
                replacement += f"(이후 {', '.join(related)})"

            text = text[:m.start()] + replacement + text[m.end():]
            break  # 위치 변동 방지 — 한 번에 하나씩

    return text


def _post_compile_fix(text: str) -> str:
    """컴파일러 치환 후 문장 결합 깨짐 등 아티팩트를 수정한다 (RC v5)."""
    # 1a. 마침표 직후 한글이 공백 없이 붙은 경우 → 공백 삽입
    text = re.sub(r"([.。])([가-힣])", r"\1 \2", text)
    # 1b. 쉼표 직후 한글/영문이 공백 없이 붙은 경우 → 공백 삽입
    text = re.sub(r",([가-힣a-zA-Z])", r", \1", text)

    # 2. 중복 공백 정리
    text = re.sub(r"  +", " ", text)

    # 3. 빈 줄 3개 이상 → 2개
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 4. 줄 시작 공백 정리 (치환으로 생긴 의도하지 않은 들여쓰기)
    text = re.sub(r"\n +(\S)", r"\n\1", text)

    # 5. 이중 치환 아티팩트: "기록에서 나타남...기록에서 확인/나타남" → 하나로 (RC-FINAL v5)
    text = re.sub(
        r"기록에서\s*나타남으로써[,.]?\s*(?:관련\s*)?(?:활동이?\s*)?기록에서\s*(?:확인|나타[남납])",
        "기록에서 확인",
        text,
    )

    # 6. 조사 잔류 아티팩트 (RC-FINAL v6 → v8 확장)
    # 구체적 패턴 먼저 (순서 중요)
    text = re.sub(r"에\s*대한\s*이\s*기록에서", "에 대한 역량이 기록에서", text)
    text = re.sub(r"이해이\s*기록에서", "이해가 기록에서", text)
    text = re.sub(r"자세이\s*기록에서", "자세가 기록에서", text)
    text = re.sub(r"태도이\s*기록에서", "태도가 기록에서", text)
    text = re.sub(r"탐구한\s*이\s*기록에서", "탐구가 기록에서", text)
    text = re.sub(r"([가-힣])한\s*이\s*기록에서", r"\1한 점이 기록에서", text)

    return text


def _fix_part3_positive_density(text: str) -> str:
    """PART 3 종합 코멘트에서 긍정 표현 과밀을 방지한다.

    전체 긍정 표현 합산 최대 2개까지만 허용. 3번째 이후 삭제.
    """
    positive_patterns = [
        r"긍정적으로\s*평가받을\s*가능성이?\s*있습니다",
        r"설득력\s*있는\s*답변이\s*가능할\s*것으로\s*보입니다",  # RC v5 치환 후 표현
        r"경쟁력으로\s*작용할\s*수\s*있습니다",
        r"강점으로\s*작용할\s*수\s*있습니다",
        r"설득력이?\s*높아질\s*수\s*있습니다",
        r"설득력이?\s*강화될\s*수\s*있습니다",
        r"적합도가\s*높[은습]",
    ]
    # 모든 긍정 표현의 위치를 수집
    all_matches = []
    for pattern in positive_patterns:
        for m in re.finditer(pattern, text):
            all_matches.append(m)
    # 위치 순 정렬
    all_matches.sort(key=lambda m: m.start())

    # 3번째 이후 삭제 (최대 2개만 유지)
    if len(all_matches) > 2:
        for m in reversed(all_matches[2:]):
            start = m.start()
            end = m.end()
            while end < len(text) and text[end] in " .。\n":
                end += 1
            while start > 0 and text[start - 1] == " ":
                start -= 1
            text = text[:start] + text[end:]

    # 빈 줄 중복 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _fix_part4_script(text: str) -> str:
    """PART 4 서사 설계도 본문에서 내부 라벨을 제거하고 상단 안내문을 추가한다."""
    # 본문 내 "※ 생기부 기록 요약" 라벨 제거
    text = re.sub(r"\s*※\s*생기부\s*기록\s*요약\s*", " ", text)
    # 혹시 남은 라벨 변형도 제거
    text = re.sub(r"\s*\(※\s*생기부[^)]*\)\s*", " ", text)

    # 1분 자기소개 설계도 섹션 앞에 안내문 삽입 (이미 있으면 스킵)
    notice = "※ 아래 서사 설계도는 생기부 기록을 바탕으로 구성한 자기소개 구조 가이드입니다. 완성 대본이 아니라 구조와 키워드를 참고하여 자기 말로 연습하세요.\n\n"
    if "서사 설계도" not in text and "생기부 기록을 바탕으로 구성한" not in text:
        # "1분 자기소개" 헤더 앞에 삽입
        text = re.sub(
            r"(#{1,3}\s*1분\s*자기소개)",
            notice + r"\1",
            text,
            count=1,
        )
    return text


def _fix_part6_disclaimer(text: str) -> str:
    """PART 6 면책 조항에서 큰따옴표 제거 + 별점 disclaimer 추가."""
    # "본 분석은..." 으로 시작하는 면책 블록의 앞뒤 큰따옴표 제거
    text = re.sub(
        r'"(본 분석은 생기부 원문을[\s\S]*?포함될 수 있습니다)\s*\.?"',
        r"\1",
        text,
    )
    # 개별 줄 시작/끝의 큰따옴표도 정리
    text = re.sub(r'^"(본 분석은)', r"\1", text, flags=re.MULTILINE)
    text = re.sub(r'(포함될 수 있습니다)\s*\.?\s*"', r"\1.", text)

    # 별점(★) disclaimer — 면책 조항 앞에 삽입
    star_disclaimer = "\n\n※ 본 리포트의 별점(★)은 생기부 기록 기반의 상대적 진단 지표이며, 절대적 평가가 아닙니다.\n"
    if "별점" not in text and "★" in text:
        # 면책 조항 앞에 삽입
        if "본 분석은" in text:
            text = text.replace("본 분석은", star_disclaimer + "본 분석은", 1)
        else:
            text += star_disclaimer

    return text


def _final_content_scan(text: str) -> list[str]:
    """최종 PDF 생성 전 금지 표현 자동 스캔."""
    warnings = []

    # 합격 보장/확률
    if re.search(r"합격\s*(?:보장|확률|가능성)\s*\d", text):
        warnings.append("'합격 보장/확률' 수치 표현 포함")

    # 특정 대학명
    universities = ["서울대", "고려대", "연세대", "성균관대", "한양대", "KAIST", "포항공대"]
    for uni in universities:
        if uni in text:
            warnings.append(f"대학 이름 '{uni}' 포함")

    # 등급컷
    if "등급컷" in text:
        warnings.append("'등급컷' 표현 포함")

    # 성취도/등급 혼용 ("A등급", "B등급", "C등급" 등 — 성취평가 과목에 "등급" 혼용)
    mixed_grade = re.findall(r"[A-E]\s*등급", text)
    if mixed_grade:
        warnings.append(f"성취도/등급 혼용 의심: {mixed_grade[:5]}. '성취도 A/B/C' 또는 'N등급'으로 통일 필요")

    # 결과 예측/보장 뉘앙스
    prediction_patterns = [
        (r"충분히\s*좋은\s*결과", "충분히 좋은 결과"),
        (r"합격\s*가능성이\s*높", "합격 가능성이 높다"),
        (r"좋은\s*결과를?\s*기대", "좋은 결과 기대"),
        (r"탁월한\s*경쟁력", "탁월한 경쟁력"),
    ]
    for pattern, label in prediction_patterns:
        if re.search(pattern, text):
            warnings.append(f"결과 예측 뉘앙스: '{label}' 표현 포함")

    # 단정적 부정 표현 (리스크 프레이밍)
    negative_expressions = re.findall(r"(?:인식|이해|경험|역량|능력)\s*(?:부족|미흡)", text)
    if negative_expressions:
        warnings.append(f"단정적 부정 표현: {negative_expressions[:3]}. 코칭 톤으로 완화 필요")

    # 추정형 리스크 잔존 체크
    speculative_patterns = [
        (r"이해가\s*충분하지\s*않을\s*수\s*있", "이해가 충분하지 않을 수 있"),
        (r"깊이가?\s*부족할?\s*수\s*있", "깊이가 부족할 수 있"),
    ]
    for pattern, label in speculative_patterns:
        if re.search(pattern, text):
            warnings.append(f"추정형 리스크 문장 잔존: '{label}'. 면접 대비 프레임으로 전환 필요")

    # 긍정 표현 반복 체크
    positive_phrases = re.findall(
        r"긍정적으로\s*평가받을\s*가능성|적합도가\s*높|경쟁력을?\s*높일\s*수\s*있|설득력이?\s*높아질\s*수\s*있",
        text,
    )
    if len(positive_phrases) > 3:
        warnings.append(f"긍정 표현 반복 {len(positive_phrases)}회. 동일 의미 1회 원칙 권장")

    # 단정형 인증 표현 체크
    if re.search(r"학생입니다", text):
        warnings.append("단정형 표현 '~학생입니다' 잔존")
    if re.search(r"보여주며", text):
        warnings.append("단정형 표현 '~보여주며' 잔존")
    if re.search(r"보여줍니다", text):
        warnings.append("단정형 표현 '~보여줍니다' 잔존")
    if re.search(r"보여줌", text):
        warnings.append("단정형 표현 '~보여줌' 잔존")
    if re.search(r"보여주었습니다", text):
        warnings.append("단정형 표현 '~보여주었습니다' 잔존")
    if re.search(r"보여주고\s*있습니다", text):
        warnings.append("단정형 표현 '~보여주고 있습니다' 잔존")
    if re.search(r"돋보이[는며]|돋보입니다", text):
        warnings.append("단정형 표현 '돋보이는/돋보이며/돋보입니다' 잔존")
    if re.search(r"B\s*/\s*C", text):
        warnings.append("'B/C' 표현 잔존. 정보 과목은 '성취도 C'로 통일 필요")
    if re.search(r"좋은\s*결과", text):
        warnings.append("'좋은 결과' 표현 잔존")
    if re.search(r"긍정적인\s*결과", text):
        warnings.append("'긍정적인 결과' 표현 잔존")
    # (정보과학은 3학년 진로선택 과목으로 정상 → 체크 제거, RC v5)
    if re.search(r"성취도\s*[:：]\s*A", text):
        warnings.append("'성취도: A' 콜론 표기 잔존")
    # 부재/부족 약점 경고 (컴파일러는 사실 확인 불가 → 경고만)
    absence_keywords = re.findall(r"(?:수상|프로젝트|실험|동아리|독서)\s*(?:경력|경험|활동|기록)?\s*(?:의\s*)?(?:부재|없음)", text)
    if absence_keywords:
        warnings.append(f"'부재/없음' 약점 표현 {absence_keywords[:3]} — 생기부 원문과 정합성 확인 필요")

    # ── RC-FINAL v4: 가능성 변형 + 단어 중복 ──
    if re.search(r"가능성이\s*(?:높습니다|큽니다|높다|크다|높음|큼)", text):
        warnings.append("'가능성이 높습니다/큽니다/높음' 변형 잔존")
    if re.search(r"긍정적인?\s*평가를?\s*받을\s*가능성", text):
        warnings.append("'긍정적(인) 평가를 받을 가능성' 변형 잔존")
    if re.search(r"높은\s*가능성", text):
        warnings.append("'높은 가능성' 표현 잔존")
    dup_words = re.findall(r"(활용|분석|탐구|연구)\s+\1", text)
    if dup_words:
        warnings.append(f"단어 중복: {dup_words[:3]}")

    # ── RC-FINAL v2 금칙어 검수 ──
    silmu_count = len(re.findall(r"실무|현업|전문가\s*수준|프로\s*수준", text))
    if silmu_count > 0:
        warnings.append(f"금칙어 '실무/현업/전문가 수준/프로 수준' {silmu_count}건 잔존")
    # 성취도 하락/퇴보/머물러 부정 프레이밍
    if re.search(r"성취도[가는은이]?\s*[^.。\n]*(?:하락|퇴보|머물)", text):
        warnings.append("성취도 '하락/퇴보/머물러' 부정 프레이밍 잔존")
    # 등급 과장 표현
    if re.search(r"\d등급\s*다수", text):
        warnings.append("'N등급 다수' 과장 표현 잔존")

    # ── 학년 레이블 잔존 경고 (Stage 1) ──
    # 이 시점에서는 이미 _fix_activity_year_labels가 실행된 후이므로,
    # 잔존하면 교정 실패한 것
    year_mismatch_note = []
    # "(N학년)" 패턴을 모두 수집 — 자체적으로는 경고 불가 (팩트시트 없음)
    # 대신, 흔한 오류 패턴만 체크
    if re.search(r"1학년\s*(?:영어Ⅰ|영어Ⅱ|수학Ⅱ)", text):
        year_mismatch_note.append("'1학년 영어Ⅰ/영어Ⅱ/수학Ⅱ' 의심 — 2학년 과목일 가능성")
    if year_mismatch_note:
        warnings.extend(year_mismatch_note)

    # ── Release Candidate 검증 체크리스트 (RC-FINAL) ──
    rc_checks = {
        "입증": len(re.findall(r"입증", text)),
        "증명": len(re.findall(r"증명합니다", text)),
        "학생입니다": len(re.findall(r"학생입니다", text)),
        "보여주며": len(re.findall(r"보여주며", text)),
        "보여줍니다": len(re.findall(r"보여줍니다", text)),
        "보여줌": len(re.findall(r"보여줌", text)),
        "보여주었습니다": len(re.findall(r"보여주었습니다", text)),
        "보여주고 있습니다": len(re.findall(r"보여주고\s*있습니다", text)),
        "돋보이는/돋보이며/돋보입니다": len(re.findall(r"돋보이는|돋보이며|돋보입니다", text)),
        "적합도가 높": len(re.findall(r"적합도가\s*높", text)),
        "좋은 결과": len(re.findall(r"좋은\s*결과", text)),
        "긍정적인 결과": len(re.findall(r"긍정적인\s*결과", text)),
        "B/C": len(re.findall(r"B\s*/\s*C", text)),
        "B/C등급": len(re.findall(r"B\s*/\s*C\s*등급", text)),
        "혼재": len(re.findall(r"혼재", text)),
        "편차": len(re.findall(r"편차", text)),
        "성취도: A (콜론형)": len(re.findall(r"성취도\s*[:：]\s*A", text)),
        "부재/없음 약점": len(re.findall(r"(?:수상|프로젝트|실험|동아리|독서)\s*(?:경력|경험|활동|기록)?\s*(?:의\s*)?(?:부재|없음)", text)),
        "실무/현업": len(re.findall(r"실무|현업", text)),
        "전문가/프로 수준": len(re.findall(r"전문가\s*수준|프로\s*수준", text)),
        "성취도 하락/퇴보/머물": len(re.findall(r"성취도[가는은이]?\s*[^.。\n]*(?:하락|퇴보|머물)", text)),
        "N등급 다수": len(re.findall(r"\d등급\s*다수", text)),
        "★★★★★": text.count("★★★★★"),
        "가능성이 높/큽니다": len(re.findall(r"가능성이\s*(?:높습니다|큽니다|높다|크다|높음|큼)", text)),
        "긍정적(인) 평가를 받을 가능성": len(re.findall(r"긍정적인?\s*평가를?\s*받을\s*가능성", text)),
        "높은 가능성": len(re.findall(r"높은\s*가능성", text)),
        "단어 중복(활용 활용 등)": len(re.findall(r"(활용|분석|탐구|연구)\s+\1", text)),
    }
    total_rc = sum(rc_checks.values())
    rc_status = "RELEASE SAFE" if total_rc == 0 else "NEEDS FIX"
    rc_report = "\n".join(f"  {k}: {v}건" for k, v in rc_checks.items())
    print(f"\n[검증 결과]\n{rc_report}\n→ {rc_status}")

    return warnings
