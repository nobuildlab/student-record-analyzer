import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── API 설정 ──
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-4o"
REVIEW_MODEL = "gpt-4o-mini"

# ── 재시도 설정 ──
MAX_RETRIES = 1  # FAIL 시 재생성 최대 횟수 (1회만)

# ── 경로 설정 ──
BASE_DIR = Path(__file__).parent
PROMPT_DIR = BASE_DIR / "prompts"
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATE_DIR = BASE_DIR / "templates"

# ── PART 정보 ──
PART_NAMES = {
    1: "핵심 진단 요약",
    2: "강점·리스크 분석",
    3: "전략 방향 코멘트",
    4: "자기소개 서사 설계도",
    5: "예상질문 + 답변 가이드",
    6: "부록",
}

# output 디렉토리 자동 생성
OUTPUT_DIR.mkdir(exist_ok=True)
