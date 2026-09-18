"""Веб-демо для Career Market Analyzer.

Тонкая обёртка над Pipeline: принимает роль, запускает агентов в отдельном
потоке и отдаёт прогресс в браузер. Только стандартная библиотека.

    uv run serve.py
    uv run serve.py --port 8080 --no-browser
"""

import argparse
import json
import logging
import mimetypes
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from agents.career_advisor import CareerAdvisorAgent
from agents.critic import CriticAgent
from agents.market_analyst import MarketAnalystAgent
from agents.salary_estimator import SalaryEstimatorAgent
from agents.stats_collector import StatsCollectorAgent
from core.llm_client import LLMClient
from core.pipeline import Pipeline
from core.role_clarifier import CLARIFY_SYSTEM
from output.report_writer import ReportWriter

logger = logging.getLogger("web")

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web" / "static"
EXAMPLES_DIR = ROOT / "examples"

AGENTS_META = [
    {"name": "market_analyst", "label": "Аналитик рынка", "output": "skill_map",
     "about": "Карта навыков: языки, фреймворки, инфраструктура, soft skills"},
    {"name": "salary_estimator", "label": "Оценщик зарплат", "output": "salary_table",
     "about": "Зарплаты по грейдам и регионам на данных hh.ru API"},
    {"name": "career_advisor", "label": "Карьерный советник", "output": "learning_path",
     "about": "План на 90 дней, gap-анализ, портфолио-проект"},
    {"name": "critic", "label": "Критик", "output": "critic_result",
     "about": "Проверка согласованности отчёта, quality score"},
    {"name": "stats_collector", "label": "Сбор статистики", "output": "run_stats",
     "about": "Время и токены по агентам, история запусков"},
]

AGENT_CLASSES = {
    "market_analyst": MarketAnalystAgent,
    "salary_estimator": SalaryEstimatorAgent,
    "career_advisor": CareerAdvisorAgent,
    "critic": CriticAgent,
    "stats_collector": StatsCollectorAgent,
}

# Ключи контекста, которые принимаются от клиента в пошаговом режиме.
CONTEXT_KEYS = {
    "role", "generated_at", "skill_level", "goal",
    "skill_map", "salary_table", "learning_path", "critic_result",
    "run_stats", "stats_summary",
    "_agent_timings", "_agent_tokens", "_pipeline_elapsed",
}

MAX_ROLE_LEN = 120
MAX_TEXT_LEN = 600
EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-").lower()
    return cleaned[:40] or "run"


class Run:
    """Состояние одного запуска pipeline."""

    def __init__(self, role: str, skill_level: str, goal: str):
        self.id = uuid.uuid4().hex[:12]
        self.role = role
        self.skill_level = skill_level
        self.goal = goal
        self.status = "running"
        self.error: str | None = None
        self.report: dict | None = None
        self.markdown: str | None = None
        self.started_at = time.time()
        self._events: list[dict] = []
        self._lock = threading.Lock()

    def on_event(self, event: str, payload: dict) -> None:
        with self._lock:
            self._events.append({
                "seq": len(self._events),
                "event": event,
                "payload": payload,
                "at": round(time.time() - self.started_at, 2),
            })

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            data = {
                "run_id": self.id,
                "role": self.role,
                "status": self.status,
                "elapsed_sec": round(time.time() - self.started_at, 2),
                "events": self._events[since:],
                "event_count": len(self._events),
                "error": self.error,
            }
        if self.status == "done":
            data["report"] = self.report
            data["markdown"] = self.markdown
        return data


class RunRegistry:
    def __init__(self, output_dir: Path, stats_path: str):
        self.output_dir = output_dir
        self.stats_path = stats_path
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def start(self, role: str, skill_level: str, goal: str) -> Run:
        run = Run(role, skill_level, goal)
        with self._lock:
            self._runs[run.id] = run
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run

    def _execute(self, run: Run) -> None:
        try:
            llm = LLMClient()
            pipeline = (
                Pipeline(
                    role=run.role,
                    llm_client=llm,
                    extra_context={"skill_level": run.skill_level, "goal": run.goal},
                    on_event=run.on_event,
                )
                .add_agent(MarketAnalystAgent(llm))
                .add_agent(SalaryEstimatorAgent(llm))
                .add_agent(CareerAdvisorAgent(llm))
                .add_agent(CriticAgent(llm))
                .add_agent(StatsCollectorAgent(llm, stats_path=self.stats_path))
            )
            context = pipeline.run()

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_dir = self.output_dir / f"{stamp}-{_slug(run.role)}"
            writer = ReportWriter(output_dir=str(out_dir))
            json_path, md_path = writer.save(context)

            run.report = context
            run.markdown = md_path.read_text(encoding="utf-8")
            run.status = "done"
            logger.info("Запуск %s готов: %s", run.id, json_path)
        except Exception as e:
            logger.exception("Запуск %s упал", run.id)
            run.error = str(e)
            run.status = "error"
            run.on_event("pipeline_error", {"error": str(e)})


class _UsageOnlyLLM:
    """Заглушка LLM для stats_collector в пошаговом режиме.

    Агент статистики не обращается к модели — ему нужен только суммарный
    расход токенов. На Vercel каждый агент запускается отдельным вызовом
    функции со своим LLMClient, поэтому сумму приносит клиент.
    """

    def __init__(self, usage: dict):
        self._usage = usage

    def get_total_usage(self) -> dict:
        return self._usage


def sanitize_context(raw: dict) -> dict:
    """Оставляет только известные ключи контекста и подрезает длины текстов."""
    if not isinstance(raw, dict):
        return {}

    context = {k: v for k, v in raw.items() if k in CONTEXT_KEYS}

    role = str(context.get("role") or "").strip()[:MAX_ROLE_LEN]
    if not role:
        raise ValueError("В контексте нет роли")
    context["role"] = role

    for key in ("skill_level", "goal"):
        if key in context:
            context[key] = str(context[key] or "").strip()[:MAX_TEXT_LEN]

    for key in ("_agent_timings", "_agent_tokens"):
        value = context.get(key)
        context[key] = value if isinstance(value, dict) else {}

    return context


def sanitize_usage(raw: dict) -> dict:
    """Приводит присланный клиентом расход токенов к целым числам."""
    if not isinstance(raw, dict):
        return dict(EMPTY_USAGE)
    usage = dict(EMPTY_USAGE)
    for key in usage:
        try:
            usage[key] = max(0, int(raw.get(key, 0)))
        except (TypeError, ValueError):
            usage[key] = 0
    return usage


def run_single_agent(name: str, context: dict, stats_path: str, usage_total: dict = None) -> dict:
    """Запускает одного агента на присланном контексте.

    Основа пошагового режима: полный прогон не влезает в лимит serverless-функции
    (медиана 58 с), а самый долгий агент укладывается примерно в 57 с.
    """
    if name not in AGENT_CLASSES:
        raise ValueError(f"Неизвестный агент: {name}")

    agent_cls = AGENT_CLASSES[name]

    if name == "stats_collector":
        agent = agent_cls(_UsageOnlyLLM(sanitize_usage(usage_total)), stats_path=stats_path)
        step_usage = dict(EMPTY_USAGE)
        start = time.time()
        result = agent.run(context)
        elapsed = round(time.time() - start, 2)
    else:
        llm = LLMClient()
        agent = agent_cls(llm)
        start = time.time()
        result = agent.run(context)
        elapsed = round(time.time() - start, 2)
        step_usage = llm.get_total_usage()

    return {
        "name": name,
        "result": result,
        "elapsed_sec": elapsed,
        "tokens": step_usage["total_tokens"],
        "usage": step_usage,
    }


def build_report_markdown(context: dict, output_dir: str) -> str:
    """Собирает report.md из готового контекста, не записывая файлы."""
    return ReportWriter(output_dir=output_dir).build_markdown(context)


def llm_available() -> tuple[bool, str]:
    """Проверяет, есть ли в .env хотя бы один провайдер с ключом."""
    try:
        LLMClient()
        return True, ""
    except Exception as e:
        return False, str(e)


def list_examples() -> list[dict]:
    items = []
    if not EXAMPLES_DIR.is_dir():
        return items
    for path in sorted(EXAMPLES_DIR.iterdir()):
        report = path / "report.json"
        if not report.is_file():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append({
            "id": path.name,
            "role": data.get("role", path.name),
            "quality_score": data.get("critic_result", {}).get("quality_score"),
            "elapsed_sec": data.get("_pipeline_elapsed"),
            "total_tokens": data.get("run_stats", {}).get("tokens", {}).get("total_tokens"),
            "generated_at": data.get("generated_at"),
        })
    return items


def load_example(example_id: str) -> dict | None:
    safe = re.sub(r"[^\w\-]", "", example_id)
    folder = EXAMPLES_DIR / safe
    report = folder / "report.json"
    if not report.is_file():
        return None
    data = json.loads(report.read_text(encoding="utf-8"))
    md = folder / "report.md"
    return {
        "run_id": f"example:{safe}",
        "status": "done",
        "role": data.get("role", safe),
        "report": data,
        "markdown": md.read_text(encoding="utf-8") if md.is_file() else "",
        "events": [],
        "event_count": 0,
    }


def load_stats(stats_path: str, limit: int = 12) -> dict:
    path = Path(stats_path)
    if not path.is_file():
        return {"history": [], "summary": {}}
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"history": [], "summary": {}}
    if not isinstance(history, list):
        return {"history": [], "summary": {}}

    successful = [r for r in history if r.get("success")]
    elapsed = [r["total_elapsed_sec"] for r in successful if r.get("total_elapsed_sec")]
    tokens = [r["tokens"]["total_tokens"] for r in successful if r.get("tokens")]
    scores = [r["quality_score"] for r in successful if r.get("quality_score") is not None]

    per_agent_time: dict[str, list[float]] = {}
    per_agent_tokens: dict[str, list[float]] = {}
    for record in successful:
        for name, value in (record.get("agent_timings_sec") or {}).items():
            per_agent_time.setdefault(name, []).append(value)
        for name, value in (record.get("agent_tokens") or {}).items():
            per_agent_tokens.setdefault(name, []).append(value)

    def avg(values):
        return round(sum(values) / len(values), 2) if values else None

    summary = {
        "total_runs": len(history),
        "successful_runs": len(successful),
        "success_rate_pct": round(len(successful) / len(history) * 100, 1) if history else None,
        "avg_elapsed_sec": avg(elapsed),
        "avg_total_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        "avg_quality_score": avg(scores),
        "agents": [
            {
                "name": name,
                "avg_elapsed_sec": avg(values),
                "avg_tokens": round(avg(per_agent_tokens.get(name, [])) or 0),
            }
            for name, values in per_agent_time.items()
        ],
    }
    return {"history": history[-limit:][::-1], "summary": summary}


class Handler(BaseHTTPRequestHandler):
    server_version = "CareerAnalyzerDemo"
    protocol_version = "HTTP/1.1"

    registry: RunRegistry
    stats_path: str

    # ─── утилиты ответа ──────────────────────────────────────────────────────

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ─── роутинг ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if not path.startswith("/api/") and (STATIC_DIR / path.lstrip("/")).is_file():
            # На Vercel статика раздаётся из web/static как из корня,
            # поэтому страница ссылается на styles.css и app.js без префикса.
            return self._static(path.lstrip("/"))
        if path == "/api/config":
            ready, reason = llm_available()
            return self._json({
                "mode": "server",
                "llm_ready": ready,
                "run_enabled": ready,
                "llm_error": reason,
                "agents": AGENTS_META,
                "examples": list_examples(),
            })
        if path == "/api/stats":
            return self._json(load_stats(self.stats_path))
        if path.startswith("/api/examples/"):
            data = load_example(path[len("/api/examples/"):])
            return self._json(data) if data else self._error(HTTPStatus.NOT_FOUND, "Пример не найден")
        if path.startswith("/api/run/"):
            run = self.registry.get(path[len("/api/run/"):])
            if run is None:
                return self._error(HTTPStatus.NOT_FOUND, "Запуск не найден")
            try:
                since = int((query.get("since") or ["0"])[0] or 0)
            except ValueError:
                since = 0
            return self._json(run.snapshot(since=since))

        return self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/clarify":
            return self._clarify()
        if path == "/api/run":
            return self._start_run()
        if path == "/api/agent":
            return self._run_agent()
        if path == "/api/finish":
            return self._finish()

        return self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    # ─── обработчики ─────────────────────────────────────────────────────────

    def _static(self, rel_path: str) -> None:
        base = STATIC_DIR.resolve()
        target = (base / rel_path).resolve()
        if base not in target.parents or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "Файл не найден")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    def _clarify(self) -> None:
        role = (self._read_json().get("role") or "").strip()
        if not role:
            return self._error(HTTPStatus.BAD_REQUEST, "Пустая роль")
        try:
            llm = LLMClient()
            result = llm.ask_json(CLARIFY_SYSTEM, f'Роль: "{role}"')
            suggestions = [s for s in result.get("suggestions", []) if isinstance(s, str)]
        except Exception as e:
            logger.warning("Уточнение роли не удалось: %s", e)
            suggestions = []
        self._json({"role": role, "suggestions": suggestions})

    def _start_run(self) -> None:
        data = self._read_json()
        role = (data.get("role") or "").strip()
        if not role:
            return self._error(HTTPStatus.BAD_REQUEST, "Укажи специальность")

        ready, reason = llm_available()
        if not ready:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, reason or "LLM не настроен")

        run = self.registry.start(
            role=role,
            skill_level=(data.get("skill_level") or "").strip(),
            goal=(data.get("goal") or "").strip(),
        )
        logger.info("Запуск %s: %s", run.id, role)
        self._json({"run_id": run.id, "agents": AGENTS_META})

    # Пошаговый режим — тот же путь, что использует Vercel-функция.

    def _run_agent(self) -> None:
        data = self._read_json()
        try:
            context = sanitize_context(data.get("context") or {})
        except ValueError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))

        ready, reason = llm_available()
        if not ready:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, reason or "LLM не настроен")

        try:
            step = run_single_agent(
                name=data.get("name") or "",
                context=context,
                stats_path=self.stats_path,
                usage_total=data.get("usage_total"),
            )
        except ValueError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:
            logger.exception("Агент упал")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

        self._json(step)

    def _finish(self) -> None:
        try:
            context = sanitize_context(self._read_json().get("context") or {})
        except ValueError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        self._json({"markdown": build_report_markdown(context, output_dir=str(self.registry.output_dir))})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Веб-демо Career Market Analyzer")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес сервера")
    parser.add_argument("--port", type=int, default=8000, help="Порт")
    parser.add_argument("--output", default="results/web", help="Куда складывать отчёты запусков")
    parser.add_argument("--stats", default="stats.json", help="Путь к файлу статистики")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    parser.add_argument("--verbose", action="store_true", help="Подробное логирование")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "groq", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    Handler.registry = RunRegistry(output_dir=output_dir, stats_path=args.stats)
    Handler.stats_path = args.stats

    ready, reason = llm_available()
    url = f"http://{args.host}:{args.port}"

    print("=" * 60)
    print(f"  Career Market Analyzer — демо: {url}")
    print(f"  LLM: {'готов' if ready else 'не настроен, доступны только примеры'}")
    if not ready:
        print(f"       {reason}")
    print(f"  Отчёты: {output_dir}/")
    print("  Остановить: Ctrl+C")
    print("=" * 60)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
