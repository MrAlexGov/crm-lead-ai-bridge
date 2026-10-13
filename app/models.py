import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field


class Lead(BaseModel):
    """Нормализованный лид — общий формат для формы, Битрикс24 и amoCRM."""

    name: str = ""
    phone: str = ""
    email: str = ""
    message: str = ""
    source: Literal["form", "bitrix", "amo"] = "form"
    external_id: str = ""  # ID в исходной CRM (для bitrix/amo)

    def normalized_phone(self) -> str:
        digits = re.sub(r"\D", "", self.phone)
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        return digits

    def dedup_key(self) -> str:
        """Один и тот же человек с тем же запросом = один лид, сколько бы раз ни пришёл вебхук."""
        if self.external_id:
            raw = f"{self.source}:{self.external_id}"
        else:
            raw = "|".join([self.normalized_phone(), self.email.strip().lower(), self.message.strip().lower()[:200]])
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Qualification(BaseModel):
    is_spam: bool = False
    topic: str = "другое"
    priority: Literal["hot", "warm", "cold"] = "warm"
    summary: str = ""
    next_action: str = ""
    by: Literal["llm", "rules"] = "llm"

    def as_text(self) -> str:
        label = {"hot": "ГОРЯЧИЙ", "warm": "тёплый", "cold": "холодный"}[self.priority]
        spam = " · ПОХОЖЕ НА СПАМ" if self.is_spam else ""
        return (
            f"ИИ-разбор лида ({self.by}): {label}{spam}\n"
            f"Тема: {self.topic}\nКратко: {self.summary}\nСледующий шаг: {self.next_action}"
        )


class JobPayload(BaseModel):
    lead: Lead
    fetched: bool = True  # False — пришёл только ID из вебхука CRM, данные подтянем в воркере
    qualification: Qualification | None = None
    done_steps: list[str] = Field(default_factory=list)  # шаги, уже выполненные в прошлых попытках
