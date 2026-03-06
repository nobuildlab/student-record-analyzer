"""OpenAI API 호출 래퍼 (재시도, 토큰 카운트)"""

import time
from openai import OpenAI

# 지연 초기화 (API 키 없이도 import 가능)
_client: OpenAI | None = None

# 누적 토큰 사용량 추적
_usage_log: list[dict] = []


def _get_client() -> OpenAI:
    """OpenAI 클라이언트를 지연 초기화."""
    global _client
    if _client is None:
        from config import OPENAI_API_KEY
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str = "gpt-4o",
    max_retries: int = 3,
    temperature: float = 0.7,
) -> str:
    """OpenAI API를 호출하고 응답 텍스트를 반환한다."""
    client = _get_client()
    last_error = None

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
            )
            # 토큰 사용량 기록
            usage = response.usage
            _usage_log.append(
                {
                    "model": model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                }
            )
            return response.choices[0].message.content

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)

    raise RuntimeError(f"LLM 호출 {max_retries}회 실패: {last_error}")


def get_usage_summary() -> dict:
    """누적 토큰 사용량 요약을 반환한다."""
    total_prompt = sum(u["prompt_tokens"] for u in _usage_log)
    total_completion = sum(u["completion_tokens"] for u in _usage_log)
    total = sum(u["total_tokens"] for u in _usage_log)

    # 대략적 비용 계산 (gpt-4o 기준)
    cost_input = total_prompt / 1_000_000 * 2.50
    cost_output = total_completion / 1_000_000 * 10.00
    # gpt-4o-mini 비용은 훨씬 저렴하지만 여기선 최대 추정
    estimated_cost_usd = cost_input + cost_output

    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total,
        "estimated_cost_usd": round(estimated_cost_usd, 4),
        "calls": len(_usage_log),
    }


def reset_usage():
    """사용량 로그를 초기화한다."""
    _usage_log.clear()
