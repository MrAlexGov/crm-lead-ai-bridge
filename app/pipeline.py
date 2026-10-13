"""Обработка одного лида по шагам. Каждый выполненный шаг сохраняется, поэтому при повторе
после сбоя уже сделанное не повторяется (нет дублей сделок, комментариев и задач)."""
import logging
from collections.abc import Awaitable, Callable

import httpx

from .amo import AmoClient
from .bitrix import BitrixClient
from .config import Settings
from .errors import IntegrationError
from .llm import qualify
from .models import JobPayload, Qualification
from .store import Store

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, store: Store, settings: Settings, http: httpx.AsyncClient,
                 bitrix: BitrixClient | None, amo: AmoClient | None):
        self.store, self.s, self.http, self.bitrix, self.amo = store, settings, http, bitrix, amo

    def plan(self, p: JobPayload) -> list[str]:
        """Список шагов зависит от источника, разбора LLM и того, какие CRM подключены."""
        q, src = p.qualification, p.lead.source
        steps = [] if p.fetched else ["fetch"]
        steps.append("qualify")
        if q is None:
            return steps
        hot = q.priority == "hot" and not q.is_spam
        if src == "bitrix" and self.bitrix:
            steps += ["bitrix_comment"] + (["bitrix_task"] if hot else [])
        if src == "amo" and self.amo:
            steps += ["amo_note", "amo_tag"]
        if q.is_spam:
            return steps  # спам не размножаем по другим CRM
        if self.bitrix and src != "bitrix" and (src == "form" or self.s.mirror_to_bitrix):
            steps += ["bitrix_create", "bitrix_comment"] + (["bitrix_task"] if hot else [])
        if self.amo and src != "amo" and (src == "form" or self.s.mirror_to_amo):
            steps += ["amo_create", "amo_note"]
        return steps

    async def run(self, job_id: int, p: JobPayload) -> None:
        key = p.lead.dedup_key()
        if p.lead.source == "bitrix":
            self.store.set_mapping(key, "bitrix", p.lead.external_id, created_by_us=False)
        if p.lead.source == "amo":
            self.store.set_mapping(key, "amo", p.lead.external_id, created_by_us=False)

        while True:
            pending = [s for s in self.plan(p) if s not in p.done_steps]
            if not pending:
                return
            step = pending[0]
            if step != "fetch" and not p.fetched:
                raise IntegrationError("лид не загружен из CRM", retryable=False)
            await self._step(step, key, p)
            p.done_steps.append(step)
            self.store.save_progress(job_id, p.model_dump())

    async def _step(self, step: str, key: str, p: JobPayload) -> None:
        lead, q = p.lead, p.qualification
        if step == "fetch":
            client = self.bitrix if lead.source == "bitrix" else self.amo
            p.lead = await client.get_lead(lead.external_id)
            p.fetched = True
            return
        if step == "qualify":
            p.qualification = await qualify(lead, self.http, self.s.openrouter_api_key, self.s.llm_model, self.s.llm_timeout)
            log.info("лид %s: %s/%s (%s)", key, p.qualification.priority, p.qualification.topic, p.qualification.by)
            return
        assert q is not None
        handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "bitrix_create": lambda: self._bitrix_create(key, p),
            "bitrix_comment": lambda: self.bitrix.add_comment(self._need(key, "bitrix"), q.as_text()),
            "bitrix_task": lambda: self._bitrix_task(key, p),
            "amo_create": lambda: self._amo_create(key, p),
            "amo_note": lambda: self.amo.add_note(self._need(key, "amo"), q.as_text()),
            "amo_tag": lambda: self.amo.tag_lead(self._need(key, "amo"), q),
        }
        await handlers[step]()

    def _need(self, key: str, target: str) -> str:
        entity_id = self.store.get_mapping(key, target)
        if not entity_id:
            raise IntegrationError(f"нет связи {target} для лида {key}", retryable=False)
        return entity_id

    async def _bitrix_create(self, key: str, p: JobPayload) -> None:
        if self.store.get_mapping(key, "bitrix"):
            return
        existing = await self.bitrix.find_lead_by_phone(p.lead.phone)
        if existing:  # такой лид уже есть в Битрикс24 — дописываем в него, а не плодим дубль
            self.store.set_mapping(key, "bitrix", existing, created_by_us=False)
            return
        new_id = await self.bitrix.add_lead(p.lead, p.qualification)
        self.store.set_mapping(key, "bitrix", new_id, created_by_us=True)

    async def _bitrix_task(self, key: str, p: JobPayload) -> None:
        if self.store.get_mapping(key, "bitrix_task"):
            return
        q: Qualification = p.qualification
        task_id = await self.bitrix.add_task(
            self._need(key, "bitrix"),
            f"Горячий лид: {q.topic} — {p.lead.name or p.lead.phone}",
            f"{q.summary}\n\nСледующий шаг: {q.next_action}\nТелефон: {p.lead.phone}\nEmail: {p.lead.email}",
            self.s.bitrix_responsible_id,
        )
        self.store.set_mapping(key, "bitrix_task", task_id)

    async def _amo_create(self, key: str, p: JobPayload) -> None:
        if self.store.get_mapping(key, "amo"):
            return
        new_id = await self.amo.add_lead(p.lead, p.qualification)
        self.store.set_mapping(key, "amo", new_id, created_by_us=True)


def backoff(attempt: int, base: float) -> float:
    """5с, 10с, 20с, 40с ... но не больше 15 минут."""
    return min(base * (2 ** attempt), 900.0)


async def process_due(store: Store, pipeline: Pipeline, settings: Settings) -> int:
    """Один проход воркера: берём созревшие задачи и выполняем. Возвращает число обработанных."""
    rows = store.claim_due()
    for row in rows:
        p = JobPayload.model_validate_json(row["payload"])
        try:
            await pipeline.run(row["id"], p)
            store.mark_done(row["id"])
        except IntegrationError as e:
            last = row["attempts"] + 1 >= settings.max_attempts
            delay = None if (not e.retryable or last) else backoff(row["attempts"], settings.backoff_base_sec)
            log.warning("задача %s: %s (повтор через %s)", row["id"], e, delay)
            store.mark_failed(row["id"], str(e), e.retryable, delay)
        except Exception as e:  # баг в коде — не крутим бесконечно, отправляем в dead с текстом ошибки
            log.exception("задача %s упала", row["id"])
            store.mark_failed(row["id"], f"{type(e).__name__}: {e}", False, None)
    return len(rows)
