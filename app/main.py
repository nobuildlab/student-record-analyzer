"""생기부 AI 분석 리포트 생성기 - Streamlit GUI"""

import sys
import tempfile
from pathlib import Path

import streamlit as st

# app/ 디렉토리를 모듈 경로에 추가
sys.path.insert(0, str(Path(__file__).parent))

from config import PART_NAMES, OUTPUT_DIR
from orchestrator import run_pipeline, regenerate_part, PipelineResult
from agents.generator import reset_evidence_cache
from skills.llm_caller import reset_usage

# ── 페이지 설정 ──
st.set_page_config(
    page_title="생기부 AI 분석 리포트",
    page_icon="📊",
    layout="centered",
)

# ── 커스텀 CSS ──
st.markdown("""
<style>
    /* 전체 배경 */
    .stApp {
        background: #f8fafc;
    }

    /* 헤더 영역 */
    .main-header {
        text-align: center;
        padding: 40px 0 10px;
    }
    .main-header h1 {
        font-size: 28px;
        font-weight: 700;
        color: #1a365d;
        margin-bottom: 4px;
    }
    .main-header p {
        font-size: 14px;
        color: #718096;
        margin-top: 0;
    }

    /* 입력 카드 */
    .input-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 28px 32px;
        margin: 20px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    .input-card h3 {
        font-size: 15px;
        font-weight: 600;
        color: #2d3748;
        margin-bottom: 16px;
    }

    /* Streamlit 기본 요소 미세 조정 */
    .stTextInput > div > div > input {
        border-radius: 8px;
        border: 1px solid #cbd5e0;
        padding: 10px 14px;
        font-size: 14px;
    }
    .stTextInput > div > div > input:focus {
        border-color: #1a365d;
        box-shadow: 0 0 0 2px rgba(26,54,93,0.1);
    }

    /* 파일 업로더 축소 */
    .stFileUploader {
        max-width: 100%;
    }
    .stFileUploader > div > div {
        padding: 16px !important;
    }
    .stFileUploader label {
        font-size: 13px !important;
    }

    /* 버튼 스타일 */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="stBaseButton-primary"] {
        background-color: #1a365d !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 8px;
        padding: 12px 24px;
        font-size: 15px;
        font-weight: 600;
        letter-spacing: 0.5px;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="stBaseButton-primary"]:hover {
        background-color: #2d4a7c !important;
        color: #ffffff !important;
    }

    /* 결과 카드 */
    .result-metric {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 16px;
        text-align: center;
    }

    /* 구분선 스타일 */
    hr {
        border: none;
        border-top: 1px solid #e2e8f0;
        margin: 24px 0;
    }

    /* 진행 상태 영역 */
    .status-area {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 20px;
        margin: 16px 0;
    }

    /* 다운로드 버튼 */
    .stDownloadButton > button {
        border-radius: 8px;
        font-weight: 600;
    }

    /* expander 깔끔하게 */
    .streamlit-expanderHeader {
        font-size: 14px;
        font-weight: 500;
    }

    /* 불필요한 Streamlit 하단 메뉴 숨김 */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


def main():
    # ── 헤더 ──
    st.markdown("""
    <div class="main-header">
        <h1>생기부 AI 분석 리포트</h1>
        <p>학교생활기록부 기반 면접 대비 분석 · PART 1~6 자동 생성</p>
    </div>
    """, unsafe_allow_html=True)

    # ── 입력 영역 ──
    st.markdown("#### 분석 정보 입력")

    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("학생 이름", placeholder="예: 홍길동", label_visibility="visible")
    with col2:
        major = st.text_input("희망 전공", placeholder="예: 컴퓨터공학", label_visibility="visible")

    uploaded_file = st.file_uploader(
        "생기부 PDF 파일",
        type=["pdf"],
        help="학교생활기록부 PDF 파일을 업로드하세요.",
    )

    # ── 분석 시작 버튼 ──
    st.markdown("")  # 살짝 여백
    start_disabled = not (uploaded_file and name and major)
    if st.button(
        "분석 시작",
        type="primary",
        disabled=start_disabled or st.session_state.get("running", False),
        use_container_width=True,
    ):
        _run_analysis(uploaded_file, name, major)

    st.divider()

    # ── 진행 상태 표시 ──
    if st.session_state.get("status_messages"):
        with st.container():
            st.markdown("**진행 상태**")
            for msg in st.session_state.status_messages:
                st.text(msg)

    # ── 결과 표시 ──
    result: PipelineResult | None = st.session_state.get("pipeline_result")
    if result and result.parts:
        _show_results(result, name, major)


def _run_analysis(uploaded_file, name: str, major: str):
    """파이프라인 실행."""
    st.session_state.running = True
    st.session_state.status_messages = []
    st.session_state.pipeline_result = None

    # 업로드 파일을 임시 파일로 저장
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    # 진행 상태 UI
    status_container = st.container()
    progress_bar = st.progress(0)

    def on_status(step: str, detail: str, progress: float):
        msg = f"{detail}"
        st.session_state.status_messages.append(msg)
        with status_container:
            st.text(msg)
        progress_bar.progress(min(progress, 1.0))

    # 파이프라인 실행
    result = run_pipeline(
        pdf_path=tmp_path,
        name=name,
        major=major,
        on_status=on_status,
    )

    st.session_state.pipeline_result = result
    st.session_state.transcript = result.student_info.get("raw_text", "")
    st.session_state.running = False

    # 완료 메시지
    if result.pdf_path:
        st.success(f"리포트 생성 완료! ({result.usage.get('calls', 0)}회 API 호출, ~${result.usage.get('estimated_cost_usd', 0):.2f})")
    elif result.parts:
        st.warning("PART 생성은 완료했으나 PDF 변환에 실패했습니다.")
    else:
        st.error("파이프라인 실행에 실패했습니다. 로그를 확인하세요.")

    st.rerun()


def _show_results(result: PipelineResult, name: str, major: str):
    """결과 미리보기 + 다운로드."""

    # ── 요약 ──
    st.markdown("**결과 요약**")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("생성된 PART", f"{len(result.parts)} / 6")
    with col2:
        manual_count = len(result.manual_review)
        st.metric("수동 검토 필요", f"{manual_count}개", delta=None)
    with col3:
        st.metric("API 비용", f"${result.usage.get('estimated_cost_usd', 0):.2f}")

    st.divider()

    # ── PDF 다운로드 (상단 배치) ──
    if result.pdf_path and Path(result.pdf_path).exists():
        with open(result.pdf_path, "rb") as f:
            st.download_button(
                label="📥 리포트 PDF 다운로드",
                data=f.read(),
                file_name=Path(result.pdf_path).name,
                mime="application/pdf",
                use_container_width=True,
                type="primary",
            )
    else:
        st.info("PDF가 아직 생성되지 않았습니다.")

    st.divider()

    # ── 각 PART 미리보기 ──
    st.markdown("**PART별 상세 보기**")

    for part_num in range(1, 7):
        if part_num not in result.parts:
            continue

        part_name = PART_NAMES[part_num]
        review = result.reviews.get(part_num, {})
        passed = review.get("passed", False)
        issues = review.get("issues", [])

        # 상태 아이콘
        if part_num in result.manual_review:
            icon = "⚠️"
            status_text = "수동 검토 필요"
        elif passed:
            icon = "✅"
            status_text = "PASS"
        else:
            icon = "❌"
            status_text = "FAIL"

        with st.expander(f"{icon} PART {part_num}: {part_name} — {status_text}"):
            # 검수 이슈 표시
            if issues:
                st.warning("검수 이슈:")
                for issue in issues:
                    st.text(f"  • {issue}")
                st.divider()

            # PART 내용 표시
            st.markdown(result.parts[part_num])

            # 재생성 버튼
            st.divider()
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button(f"🔄 PART {part_num} 재생성", key=f"regen_{part_num}"):
                    with st.spinner(f"PART {part_num} 재생성 중..."):
                        new_review = regenerate_part(
                            result=result,
                            part_num=part_num,
                            name=name,
                            major=major,
                            transcript=st.session_state.get("transcript", ""),
                        )
                        if new_review["passed"]:
                            st.success("재생성 → 검수 PASS")
                        else:
                            st.warning(f"재생성 → 검수 FAIL: {', '.join(new_review['issues'][:3])}")
                        st.rerun()
            with col_b:
                if part_num in result.manual_review:
                    if st.button(f"✅ PART {part_num} 승인", key=f"approve_{part_num}"):
                        result.manual_review.remove(part_num)
                        st.success("승인 완료")
                        st.rerun()

    st.divider()

    # ── PDF 재생성 (PART 수정 후) ──
    if st.button("📄 PDF 재생성 (PART 수정 반영)", use_container_width=True):
        with st.spinner("PDF 생성 중..."):
            try:
                from agents.compiler import compile_report
                pdf_path = compile_report(result.parts, name, major)
                result.pdf_path = pdf_path
                st.success("PDF 재생성 완료!")
                st.rerun()
            except Exception as e:
                st.error(f"PDF 생성 실패: {e}")

    # ── 감사 로그 / 토큰 ──
    with st.expander("📋 감사 로그"):
        for log in result.audit_log:
            st.text(log)

    with st.expander("💰 토큰 사용량"):
        usage = result.usage
        st.json(usage)


# ── 세션 상태 초기화 ──
if "pipeline_result" not in st.session_state:
    st.session_state.pipeline_result = None
if "running" not in st.session_state:
    st.session_state.running = False
if "status_messages" not in st.session_state:
    st.session_state.status_messages = []
if "transcript" not in st.session_state:
    st.session_state.transcript = ""


if __name__ == "__main__":
    main()
