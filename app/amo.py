"""Клиент API amoCRM v4 (долгосрочный токен)."""
import httpx

from .http import RateLimiter, request_json
from .models import Lead, Qualification

PRIORITY_TAG = {"hot": "горячий", "warm": "тёплый", "cold": "холодный"}


class AmoClient:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient, rps: float = 6.0):
        if not base_url or not token:
            raise ValueError("AMO_BASE_URL / AMO_TOKEN не заданы")
        self.base = base_url.rstrip("/") + "/api/v4"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.http = client
        self.limiter = RateLimiter(rps)

    async def _req(self, method: str, path: str, **kw):
        await self.limiter.wait()
        return await request_json(self.http, method, self.base + path, headers=self.headers, **kw)

    async def get_lead(self, lead_id: str) -> Lead:
        data = await self._req("GET", f"/leads/{lead_id}", params={"with": "contacts"})
        name, phone, email = data.get("name", ""), "", ""
        contacts = (data.get("_embedded") or {}).get("contacts") or []
        if contacts:
            c = await self._req("GET", f"/contacts/{contacts[0]['id']}")
            name = c.get("name") or name
            for f in c.get("custom_fields_values") or []:
                val = (f.get("values") or [{}])[0].get("value", "")
                if f.get("field_code") == "PHONE" and not phone:
                    phone = val
                if f.get("field_code") == "EMAIL" and not email:
                    email = val
        return Lead(name=name, phone=phone, email=email, message=data.get("name", ""),
                    source="amo", external_id=str(lead_id))

    async def find_contact_by_phone(self, phone: str) -> bool:
        if not phone:
            return False
        data = await self._req("GET", "/contacts", params={"query": phone})
        return bool(data and (data.get("_embedded") or {}).get("contacts"))

    async def add_lead(self, lead: Lead, q: Qualification) -> str:
        contact_fields = []
        if lead.phone:
            contact_fields.append({"field_code": "PHONE", "values": [{"value": lead.phone, "enum_code": "WORK"}]})
        if lead.email:
            contact_fields.append({"field_code": "EMAIL", "values": [{"value": lead.email, "enum_code": "WORK"}]})
        body = [{
            "name": f"{q.topic}: {lead.name or lead.phone or lead.email}",
            "_embedded": {
                "tags": [{"name": PRIORITY_TAG[q.priority]}, {"name": "ai-bridge"}],
                "contacts": [{"name": lead.name or lead.phone or "Без имени", "custom_fields_values": contact_fields}],
            },
        }]
        data = await self._req("POST", "/leads/complex", json=body)
        return str(data[0]["id"])

    async def add_note(self, lead_id: str, text: str) -> None:
        await self._req("POST", f"/leads/{lead_id}/notes", json=[{"note_type": "common", "params": {"text": text}}])

    async def tag_lead(self, lead_id: str, q: Qualification) -> None:
        tags = [{"name": PRIORITY_TAG[q.priority]}, {"name": "ai-bridge"}]
        if q.is_spam:
            tags.append({"name": "спам?"})
        # tags_to_add — добавляет теги, не затирая уже стоящие на сделке
        await self._req("PATCH", "/leads", json=[{"id": int(lead_id), "tags_to_add": tags}])
