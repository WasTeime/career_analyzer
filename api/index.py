"""Vercel-функция: то же демо, но без долгоживущего процесса.

Почему не переиспользуется сервер из serve.py:
  * Vercel не держит процесс между запросами — реестр запусков в памяти и
    опрос /api/run/<id> там работать не могут;
  * файловая система read-only, кроме /tmp — report.json/report.md не записать;
  * полный прогон pipeline не влезает в лимит функции (медиана 58 с,
    максимум 87 с по stats.json), а отдельный агент влезает — худший 57 с.

Поэтому агенты вызываются по одному: контекст хранит браузер и присылает его
на каждом шаге. Логика шага — общая с локальным сервером (serve.run_single_agent),
здесь только HTTP-обёртка, лимиты и защита от чужих запусков на наших ключах.

Переменные окружения (задаются в настройках проекта Vercel):
    OPENROUTER_MODELS, OPENROUTER_KEYS, GROQ_MODELS, GROQ_KEYS — как в .env
    DEMO_ALLOW_RUN=0   — выключить живые запуски, оставить только примеры
    DEMO_TOKEN=...     — пускать к запускам только со ссылкой ?token=...
    DEMO_MAX_RUNS=8    — сколько запусков с одного IP за окно (по умолчанию 8)
    DEMO_WINDOW_SEC=600 — длина окна в секундах
"""

import json
import os
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_client import LLMClient  # noqa: E402
from core.role_clarifier import CLARIFY_SYSTEM  # noqa: E402
from serve import (  # noqa: E402
    AGENTS_META,
    build_report_markdown,
    list_examples,
    llm_available,
    load_example,
    load_stats,
    run_single_agent,
    sanitize_context,
)

REPO_STATS = ROOT / "stats.json"
TMP_STATS = Path("/tmp/stats.json")

MAX_BODY_BYTES = 512 * 1024
AGENTS_PER_RUN = len(AGENTS_META) + 1  # агенты + уточнение роли

# Лучшее, что можно сделать без внешнего хранилища: счётчик живёт в инстансе
# функции. Инстансов может быть несколько, поэтому это мягкий тормоз, а не
# строгий лимит. Жёсткое ограничение — DEMO_TOKEN.
_calls: dict[str, list[float]] = {}


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def run_enabled() -> tuple[bool, str]:
    if not env_flag("DEMO_ALLOW_RUN", True):
        return False, "Живые запуски отключены владельцем демо — доступны готовые отчёты"
    ready, reason = llm_available()
    if not ready:
        return False, reason
    return True, ""


def token_ok(query: dict, headers) -> bool:
    expected = (os.getenv("DEMO_TOKEN") or "").strip()
    if not expected:
        return True
    provided = (headers.get("X-Demo-Token") or (query.get("token") or [""])[0]).strip()
    return provided == expected


def throttled(client_ip: str) -> bool:
    window = env_int("DEMO_WINDOW_SEC", 600)
    limit = env_int("DEMO_MAX_RUNS", 8) * AGENTS_PER_RUN
    now = time.time()
    hits = [t for t in _calls.get(client_ip, []) if now - t < window]
    hits.append(now)
    _calls[client_ip] = hits[-limit - 1:]
    return len(hits) > limit


def stats_path_for_run() -> str:
    """История из репозитория + текущий запуск, в /tmp (единственное, что пишется).

    Между вызовами функции /tmp не гарантируется, поэтому история не копится —
    зато агент статистики считает сводку по реальным данным, а не с нуля.
    """
    try:
        if not TMP_STATS.exists() and REPO_STATS.is_file():
            shutil.copyfile(REPO_STATS, TMP_STATS)
    except OSError:
        pass
    return str(TMP_STATS)


class handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ─── ответы ──────────────────────────────────────────────────────────────

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _route(self) -> str:
        return (self._query().get("route") or [""])[0]

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Слишком большой запрос")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Ожидался JSON")
        return data if isinstance(data, dict) else {}

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For") or ""
        return forwarded.split(",")[0].strip() or self.client_address[0]

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{fmt % args}\n")

    # ─── роутинг ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        route = self._route()

        if route == "config":
            enabled, reason = run_enabled()
            return self._json({
                "mode": "serverless",
                "llm_ready": enabled,
                "run_enabled": enabled,
                "llm_error": reason,
                "needs_token": bool((os.getenv("DEMO_TOKEN") or "").strip()),
                "agents": AGENTS_META,
                "examples": list_examples(),
                "stats_note": "история из репозитория — на Vercel файлы не сохраняются",
            })

        if route == "stats":
            return self._json(load_stats(str(REPO_STATS)))

        if route == "example":
            example_id = (self._query().get("id") or [""])[0]
            data = load_example(example_id)
            return self._json(data) if data else self._error(404, "Пример не найден")

        return self._error(404, "Не найдено")

    def do_POST(self) -> None:
        route = self._route()
        if route not in ("clarify", "agent", "finish"):
            return self._error(404, "Не найдено")

        try:
            body = self._body()
        except ValueError as e:
            return self._error(400, str(e))

        # Сборка markdown не тратит токены — пускаем без лимитов.
        if route == "finish":
            try:
                context = sanitize_context(body.get("context") or {})
            except ValueError as e:
                return self._error(400, str(e))
            return self._json({"markdown": build_report_markdown(context, output_dir="/tmp")})

        if not token_ok(self._query(), self.headers):
            return self._error(401, "Нужен токен доступа к демо")

        enabled, reason = run_enabled()
        if not enabled:
            return self._error(503, reason)

        if throttled(self._client_ip()):
            return self._error(429, "Слишком много запусков с этого адреса — попробуй позже")

        if route == "clarify":
            role = str(body.get("role") or "").strip()[:120]
            if not role:
                return self._error(400, "Пустая роль")
            try:
                result = LLMClient().ask_json(CLARIFY_SYSTEM, f'Роль: "{role}"')
                suggestions = [s for s in result.get("suggestions", []) if isinstance(s, str)]
            except Exception:
                suggestions = []
            return self._json({"role": role, "suggestions": suggestions})

        try:
            context = sanitize_context(body.get("context") or {})
        except ValueError as e:
            return self._error(400, str(e))

        try:
            step = run_single_agent(
                name=body.get("name") or "",
                context=context,
                stats_path=stats_path_for_run(),
                usage_total=body.get("usage_total"),
            )
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, f"Агент упал: {e}")

        return self._json(step)
