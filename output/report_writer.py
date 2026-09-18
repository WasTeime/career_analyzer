import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LEVEL_ORDER = {"critical": 0, "important": 1, "nice-to-have": 2}
TREND_ORDER = {"growing": 0, "stable": 1, "declining": 2}
LEVEL_LABELS = {"critical": "необходимый", "important": "есть большой спрос", "nice-to-have": "небольшой спрос"}
TREND_LABELS = {"growing": "постоянно требуется", "stable": "стабильно", "declining": "снижается"}
CATEGORY_NAMES = {
    "languages": "Языки программирования",
    "frameworks": "Фреймворки и библиотеки",
    "infrastructure": "Инфраструктура",
    "soft_skills": "Soft skills",
}
GRADE_LABELS = ["junior", "middle", "senior", "lead"]
SCORE_BREAKDOWN_LABELS = {
    "salary_market_match": "Зарплаты соответствуют рынку",
    "skills_consistency": "Согласованность навыков",
    "learning_path_quality": "Качество плана обучения",
    "portfolio_relevance": "Релевантность портфолио",
}


class ReportWriter:
    def __init__(self, output_dir: str = "."):
        self.output_dir = Path(output_dir)
        if self.output_dir.exists() and not self.output_dir.is_dir():
            self.output_dir.unlink()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(self, context: dict) -> tuple[Path, Path]:
        json_path = self._save_json(context)
        md_path = self._save_markdown(context)
        return json_path, md_path

    def _save_json(self, context: dict) -> Path:
        path = self.output_dir / "report.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(context, f, ensure_ascii=False, indent=2)
        logger.info("JSON сохранён: %s", path)
        return path

    def build_markdown(self, context: dict) -> str:
        """Собирает report.md и возвращает текстом, ничего не записывая.

        Нужно веб-демо на Vercel: там файловая система read-only, отчёт
        отдаётся клиенту в ответе и скачивается браузером.
        """
        return self._build_markdown(context)

    def _save_markdown(self, context: dict) -> Path:
        path = self.output_dir / "report.md"
        md = self._build_markdown(context)
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info("Markdown сохранён: %s", path)
        return path

    def _build_markdown(self, ctx: dict) -> str:
        sections = [
            self._section_header(ctx),
            self._section_skills(ctx.get("skill_map", {})),
            self._section_salary(ctx.get("salary_table", {})),
            self._section_learning_path(ctx.get("learning_path", {})),
            self._section_gap_analysis(ctx.get("learning_path", {})),
            self._section_portfolio(ctx.get("learning_path", {})),
            self._section_quality(ctx.get("critic_result", {})),
        ]
        return "\n".join(sections)

    def _section_header(self, ctx: dict) -> str:
        role = ctx.get("role", "Неизвестная роль")
        generated_at = ctx.get("generated_at", "")
        return "\n".join([
            f"# Карьерный отчёт: {role}",
            f"\n_Сгенерировано: {generated_at}_\n",
            "---\n",
        ])

    def _section_skills(self, skill_map: dict) -> str:
        def skill_sort_key(s):
            return (LEVEL_ORDER.get(s.get("level", ""), 9), TREND_ORDER.get(s.get("trend", ""), 9))

        lines = ["## Карта навыков\n"]
        for key, title in CATEGORY_NAMES.items():
            skills = sorted(skill_map.get(key, []), key=skill_sort_key)
            if not skills:
                continue
            lines.append(f"### {title}\n")
            is_soft = key == "soft_skills"
            if is_soft:
                lines.append("| Навык | Востребованность | Тренд |")
                lines.append("|-------|-----------------|-------|")
            else:
                lines.append("| Навык | Востребованность | Тренд | Почему |")
                lines.append("|-------|-----------------|-------|--------|")
            for s in skills:
                lvl = s.get("level", "")
                trnd = s.get("trend", "")
                if is_soft:
                    lines.append(f"| {s.get('name')} | {lvl} | {trnd} |")
                else:
                    reason = s.get("trend_reason", "")
                    lines.append(f"| {s.get('name')} | {lvl} | {trnd} | {reason} |")
            lines.append("")
        return "\n".join(lines)

    def _section_salary(self, salary_table: dict) -> str:
        def fmt(r: dict) -> str:
            mn, med, mx = int(r.get("min", 0)), int(r.get("median", 0)), int(r.get("max", 0))
            return f"{mn}–{mx} (средняя: {med})"

        trend = salary_table.get("market_trend", "")
        reason = salary_table.get("market_trend_reason", "")
        lines = [
            "## Зарплатная таблица\n",
            f"**Тренд рынка:** {TREND_LABELS.get(trend, trend)}",
            f"\n_{reason}_\n",
            "| Грейд | Москва (тыс. ₽) | Регионы (тыс. ₽) | Remote (USD) |",
            "|-------|----------------|-----------------|--------------|",
        ]
        for grade in GRADE_LABELS:
            data = salary_table.get(grade, {})
            lines.append(
                f"| {grade.capitalize()} "
                f"| {fmt(data.get('moscow', {}))} "
                f"| {fmt(data.get('regions_rub', {}))} "
                f"| {fmt(data.get('remote_usd', {}))} |"
            )

        employers = salary_table.get("top_employers", [])
        if employers:
            lines.append("\n**Топ работодателей:**\n")
            for e in sorted(employers, key=lambda e: (0 if e.get("type") == "российская" else 1)):
                lines.append(f"- **{e.get('name')}** ({e.get('type')}) — {e.get('description')}")
            lines.append("")
        return "\n".join(lines)

    def _section_learning_path(self, learning_path: dict) -> str:
        lines = ["## План обучения (90 дней)\n"]
        for i, phase in enumerate(learning_path.get("phases", []), 1):
            lines.append(f"### Фаза {i}: {phase.get('name')} ({phase.get('duration_days')} дней)\n")
            lines.append("**Темы:**")
            for topic in phase.get("topics", []):
                lines.append(f"- {topic}")
            lines.append("\n**Маршрут:**")
            for step in phase.get("path", []):
                lines.append(f"- {step}")
            lines.append(f"\n**Milestone:** {phase.get('milestone')}\n")
            projects = phase.get("practice_projects", [])
            if projects:
                lines.append("\n**Практика:**")
                for p in projects:
                    lines.append(f"- **{p.get('name')}** — {p.get('description')}")
            lines.append("")
            lines.append("\n**Ресурсы:**")
            for res in sorted(phase.get("resources", []), key=lambda r: (0 if r.get("is_free", True) else 1)):
                url = res.get("url")
                name = res.get("name")
                badge = "бесплатно" if res.get("is_free", True) else "платно"
                link = f"[{name}]({url})" if url else name
                lines.append(f"- {link} _{res.get('type')}_ ({badge})")
            lines.append("")
        return "\n".join(lines)

    def _section_gap_analysis(self, learning_path: dict) -> str:
        gap = learning_path.get("gap_analysis", {})
        lines = [
            "## Gap-анализ\n",
            "### Quick Wins (2–4 недели)\n",
        ]
        for item in gap.get("quick_wins", []):
            lines.append(f"- {item}")
        lines.append("\n### Long Term (3+ месяца)\n")
        for item in gap.get("long_term", []):
            lines.append(f"- {item}")
        lines.append("")
        return "\n".join(lines)

    def _section_portfolio(self, learning_path: dict) -> str:
        project = learning_path.get("portfolio_project", {})
        lines = [
            "## Портфолио-проект\n",
            f"### {project.get('name', '')}\n",
            f"**Проблема:** {project.get('problem', '')}\n",
        ]

        user_stories = project.get("user_stories", [])
        if user_stories:
            lines.append("**Сценарии использования:**")
            for s in user_stories:
                lines.append(f"- {s}")
            lines.append("")

        challenges = project.get("technical_challenges", [])
        if challenges:
            lines.append("**Технические задачи:**")
            for c in challenges:
                lines.append(f"- {c}")
            lines.append("")

        skills = project.get("skills_demonstrated", [])
        if skills:
            lines.append("**Навыки в проекте:**")
            for s in skills:
                lines.append(f"- {s}")
            lines.append("")

        return "\n".join(lines)

    def _section_quality(self, critic: dict) -> str:
        score = critic.get("quality_score", 0)
        lines = [
            "## Оценка качества\n",
            f"**Итоговая оценка:** {score}/100\n",
        ]
        breakdown = critic.get("score_breakdown", {})
        if breakdown:
            lines.append("| Критерий | Баллы | Комментарий |")
            lines.append("|----------|-------|-------------|")
            for key, label in SCORE_BREAKDOWN_LABELS.items():
                item = breakdown.get(key, {})
                lines.append(f"| {label} | {item.get('score', 0)}/25 | {item.get('reason', '')} |")
        lines.append(f"_{critic.get('quality_score_reason', '')}_\n")
        for w in critic.get("warnings", []):
            lines.append(f"- {w}")
        return "\n".join(lines)
