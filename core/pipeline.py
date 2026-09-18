import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from agents.base_agent import BaseAgent
from core.llm_client import LLMClient

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], None]


class Pipeline:
    def __init__(
        self,
        role: str,
        llm_client: LLMClient,
        extra_context: dict = None,
        on_event: Optional[EventHandler] = None,
    ):
        self.role = role
        self.llm_client = llm_client
        self._agents: list[BaseAgent] = []
        self._extra_context = extra_context or {}
        self._on_event = on_event
        logger.info("Pipeline создан для роли: %s", role)

    def add_agent(self, agent: BaseAgent) -> "Pipeline":
        if not isinstance(agent, BaseAgent):
            raise TypeError(f"Ожидался BaseAgent, получен: {type(agent)}")
        self._agents.append(agent)
        logger.debug("Добавлен агент: %s (всего: %d)", agent.name, len(self._agents))
        return self

    def _emit(self, event: str, payload: dict) -> None:
        """Отдаёт событие наблюдателю (веб-демо). Без наблюдателя — no-op."""
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:
            logger.warning("Обработчик события '%s' упал", event, exc_info=True)

    def run(self) -> dict:
        if not self._agents:
            raise RuntimeError("Pipeline пуст — добавь хотя бы одного агента.")

        pipeline_start = time.time()

        context: dict = {
            "role": self.role,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "_agent_timings": {},
            "_agent_tokens": {},
            **self._extra_context,
        }

        logger.info("=" * 50)
        logger.info("Запуск Pipeline: %s", self.role)
        logger.info("Агентов: %d", len(self._agents))
        logger.info("=" * 50)

        total = len(self._agents)
        self._emit("pipeline_start", {
            "role": self.role,
            "agents": [a.name for a in self._agents],
        })

        for i, agent in enumerate(self._agents, start=1):
            logger.info("[%d/%d] Запуск: %s", i, total, agent.name)
            self._emit("agent_start", {"name": agent.name, "index": i, "total": total})

            agent_start = time.time()
            tokens_before = self.llm_client.get_total_usage()["total_tokens"]

            try:
                result = agent.run(context)
            except Exception as e:
                logger.error("Агент %s упал: %s", agent.name, e)
                self._emit("agent_error", {
                    "name": agent.name, "index": i, "total": total, "error": str(e),
                })
                raise RuntimeError(f"Агент '{agent.name}' завершился с ошибкой: {e}") from e

            agent_elapsed = round(time.time() - agent_start, 2)
            tokens_after = self.llm_client.get_total_usage()["total_tokens"]
            agent_tokens = tokens_after - tokens_before

            context["_agent_timings"][agent.name] = agent_elapsed
            context["_agent_tokens"][agent.name] = agent_tokens

            if not isinstance(result, dict):
                raise TypeError(f"Агент '{agent.name}' вернул {type(result)}, ожидался dict.")

            context.update(result)
            logger.info(
                "[%d/%d] Готово: %s (%.2f сек, %d токенов)",
                i, total, agent.name, agent_elapsed, agent_tokens,
            )
            self._emit("agent_done", {
                "name": agent.name,
                "index": i,
                "total": total,
                "elapsed_sec": agent_elapsed,
                "tokens": agent_tokens,
                "keys": sorted(result.keys()),
            })

        context["_pipeline_elapsed"] = round(time.time() - pipeline_start, 2)

        logger.info("=" * 50)
        logger.info("Pipeline завершён за %.2f сек", context["_pipeline_elapsed"])
        logger.info("=" * 50)

        self._emit("pipeline_done", {
            "elapsed_sec": context["_pipeline_elapsed"],
            "tokens": self.llm_client.get_total_usage(),
        })
        return context
