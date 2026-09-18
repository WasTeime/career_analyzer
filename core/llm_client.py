import json
import logging
import os
import time

from openai import OpenAI

logger = logging.getLogger(__name__)

PROVIDERS = [
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_env": "OPENROUTER_MODELS",
        "keys_env": "OPENROUTER_KEYS",
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_env": "GROQ_MODELS",
        "keys_env": "GROQ_KEYS",
    },
]


def _env_num(name: str, default, cast):
    """Число из окружения. Пустая переменная считается незаданной: в панели
    Vercel её легко создать без значения, а float("") падает."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning("%s=%r — не число, беру %s", name, raw, default)
        return default


def _build_fallback_chain() -> list[dict]:
    """Строит цепочку: провайдер → модель (лучшая→худшая) → ключ (1→2)."""
    chain = []
    for provider in PROVIDERS:
        models_raw = os.getenv(provider["models_env"], "")
        keys_raw = os.getenv(provider["keys_env"], "")
        if not models_raw or not keys_raw:
            continue

        models = [m.strip() for m in models_raw.split(",")]
        keys = [k.strip() for k in keys_raw.split(",")]

        for model in models:
            for key in keys:
                chain.append({
                    "name": provider["name"],
                    "base_url": provider["base_url"],
                    "model": model,
                    "api_key": key,
                })
    return chain


class LLMClient:
    def __init__(self, log_prompts: bool = False):
        self.log_prompts = log_prompts
        self.temperature = _env_num("TEMPERATURE", 0.3, float)
        self.max_tokens = _env_num("MAX_TOKENS", 4096, int)
        self.max_retries = _env_num("MAX_RETRIES", 3, int)

        # Накопленная статистика токенов за всё время жизни клиента
        self.usage_stats: list[dict] = []

        chain = _build_fallback_chain()
        if not chain:
            raise ValueError("Нет провайдеров. Добавь GROQ_MODELS + GROQ_KEYS в .env")

        self.clients = []
        for item in chain:
            self.clients.append({
                "name": f"{item['name']}/{item['model']}",
                "client": OpenAI(api_key=item["api_key"], base_url=item["base_url"]),
                "model": item["model"],
            })

        logger.info("Fallback-цепочка: %d комбинаций", len(self.clients))
        for c in self.clients:
            logger.info("  → %s", c["name"])

    def get_total_usage(self) -> dict:
        """Возвращает суммарную статистику токенов по всем вызовам."""
        if not self.usage_stats:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
        return {
            "input_tokens": sum(s["input_tokens"] for s in self.usage_stats),
            "output_tokens": sum(s["output_tokens"] for s in self.usage_stats),
            "total_tokens": sum(s["total_tokens"] for s in self.usage_stats),
            "calls": len(self.usage_stats),
        }

    def ask(self, system_prompt: str, user_prompt: str) -> str:
        if self.log_prompts:
            logger.debug("--- SYSTEM PROMPT ---\n%s", system_prompt)
            logger.debug("--- USER PROMPT ---\n%s", user_prompt)

        last_error = None

        for provider in self.clients:
            try:
                start = time.time()

                message = provider["client"].chat.completions.create(
                    model=provider["model"],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )

                elapsed = time.time() - start
                response_text = message.choices[0].message.content

                usage = message.usage
                if usage:
                    self.usage_stats.append({
                        "provider": provider["name"],
                        "input_tokens": usage.prompt_tokens,
                        "output_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "elapsed_sec": round(elapsed, 2),
                    })
                    logger.info(
                        "[%s] Токены: %d input + %d output = %d total (%.2f сек)",
                        provider["name"],
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.total_tokens,
                        elapsed,
                    )

                if self.log_prompts:
                    logger.debug("--- RESPONSE [%s] (%.2f сек) ---\n%s", provider["name"], elapsed, response_text)

                return response_text

            except Exception as e:
                last_error = e
                logger.warning(
                    "Провайдер %s упал: %s. Переключаюсь на следующий...",
                    provider["name"], str(e)[:200],
                )
                continue

        raise last_error

    def ask_json(self, system_prompt: str, user_prompt: str) -> dict:
        for attempt in range(1, self.max_retries + 1):
            response_text = self.ask(system_prompt, user_prompt)

            try:
                cleaned = self._strip_markdown_json(response_text)
                result = json.loads(cleaned)
                if attempt > 1:
                    logger.info("JSON успешно распарсен с попытки %d", attempt)
                return result

            except json.JSONDecodeError as e:
                logger.warning("Попытка %d/%d: невалидный JSON — %s", attempt, self.max_retries, e)
                if attempt == self.max_retries:
                    logger.error("Последний ответ:\n%s", response_text)
                    raise ValueError(f"Невалидный JSON после {self.max_retries} попыток: {e}") from e

                user_prompt = (
                    f"{user_prompt}\n\n"
                    f"ВАЖНО: предыдущий ответ содержал невалидный JSON. "
                    f"Верни ТОЛЬКО чистый JSON без markdown и комментариев."
                )

    @staticmethod
    def _strip_markdown_json(text: str) -> str:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return text[start:end + 1]
        return text