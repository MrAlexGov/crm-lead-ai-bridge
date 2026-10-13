"""Клиент REST API Битрикс24 через входящий вебхук."""
import httpx

from .errors import IntegrationError
from .http import RateLimiter, request_json
from .models import Lead, Qualification

# Ошибки Битрикс24, после которых имеет смысл повторить запрос
RETRYABLE_ERRORS = {"QUERY_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "OVERLOAD_LIMIT"}


class BitrixClient:
    def __init__(self, webhook_url: str, client: httpx.AsyncClient, rps: float = 2.0):
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL не задан")
        self.base = webhook_url.rstrip("/") + "/"
        self.http = client
        self.limiter = RateLimiter(rps)

    async def call(self, method: str, params: dict) -> object:
        await self.limiter.wait()
        data = await request_json(self.http, "POST", f"{self.base}{method}.json", json=params)
        if isinstance(data, dict) and "error" in data:
            code = str(data.get("error"))
            raise IntegrationError(
                f"Bitrix {method}: {code}: {data.get('error_description', '')}",
                retryable=code in RETRYABLE_ERRORS,
            )
        return data.get("result") if isinstance(data, dict) else data

    async def get_lead(self, lead_id: str) -> Lead:
        f = await self.call("crm.lead.get", {"id": lead_id})
        phone = (f.get("PHONE") or [{}])[0].get("VALUE", "")
        email = (f.get("EMAIL") or [{}])[0].get("VALUE", "")
        name = " ".join(x for x in [f.get("NAME"), f.get("LAST_NAME")] if x) or f.get("TITLE", "")
        return Lead(
            name=name, phone=phone, email=email, message=f.get("COMMENTS") or f.get("TITLE", ""),
            source="bitrix", external_id=str(lead_id),
        )

    async def find_lead_by_phone(self, phone: str) -> str | None:
        """Защита от дублей на стороне CRM: ищем существующий лид по телефону."""
        if not phone:
            return None
        res = await self.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone], "entity_type": "LEAD"})
        ids = (res or {}).get("LEAD") if isinstance(res, dict) else None
        return str(ids[0]) if ids else None

    async def add_lead(self, lead: Lead, q: Qualification) -> str:
        fields = {
            "TITLE": f"[{q.priority}] {q.topic}: {lead.name or lead.phone or lead.email}",
            "NAME": lead.name,
            "COMMENTS": lead.message,
            "SOURCE_ID": "WEB" if lead.source == "form" else "OTHER",
            "SOURCE_DESCRIPTION": f"crm-lead-ai-bridge ({lead.source})",
        }
        if lead.phone:
            fields["PHONE"] = [{"VALUE": lead.phone, "VALUE_TYPE": "WORK"}]
        if lead.email:
            fields["EMAIL"] = [{"VALUE": lead.email, "VALUE_TYPE": "WORK"}]
        return str(await self.call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}))

    async def add_comment(self, lead_id: str, text: str) -> None:
        await self.call("crm.timeline.comment.add", {
            "fields": {"ENTITY_ID": int(lead_id), "ENTITY_TYPE": "lead", "COMMENT": text},
        })

    async def add_task(self, lead_id: str, title: str, description: str, responsible_id: int) -> str:
        res = await self.call("tasks.task.add", {"fields": {
            "TITLE": title, "DESCRIPTION": description, "RESPONSIBLE_ID": responsible_id,
            "UF_CRM_TASK": [f"L_{lead_id}"],  # привязка задачи к лиду
        }})
        return str(res["task"]["id"])
