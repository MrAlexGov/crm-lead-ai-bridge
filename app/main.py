"""HTTP-вход: вебхуки формы, Битрикс24 и amoCRM + фоновый воркер очереди."""
import asyncio
import hmac
import logging
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from .amo import AmoClient
from .bitrix import BitrixClient
from .config import Settings, settings as default_settings
from .models import JobPayload, Lead
from .pipeline import Pipeline, process_due
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridge")


def _secret_ok(given: str | None, expected: str) -> bool:
    return not expected or hmac.compare_digest((given or "").encode(), expected.encode())


def _form_value(form: dict, pattern: str) -> str | None:
    """Достаёт значение из «плоской» формы вида data[FIELDS][ID]=... / leads[add][0][id]=..."""
    for k, v in form.items():
        if re.fullmatch(pattern, k):
            return str(v)
    return None


def create_app(s: Settings = default_settings, run_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=20)
        store = Store(s.db_path)
        bitrix = BitrixClient(s.bitrix_webhook_url, http, s.bitrix_rps) if s.bitrix_webhook_url else None
        amo = AmoClient(s.amo_base_url, s.amo_token, http, s.amo_rps) if s.amo_base_url and s.amo_token else None
        app.state.store, app.state.pipeline = store, Pipeline(store, s, http, bitrix, amo)
        log.info("старт: Битрикс24=%s, amoCRM=%s, LLM=%s", bool(bitrix), bool(amo), bool(s.openrouter_api_key))
        task = asyncio.create_task(_worker(app)) if run_worker else None
        yield
        if task:
            task.cancel()
        await http.aclose()

    async def _worker(app: FastAPI):
        while True:
            try:
                await process_due(app.state.store, app.state.pipeline, s)
            except Exception:
                log.exception("сбой цикла воркера")
            await asyncio.sleep(s.worker_poll_sec)

    app = FastAPI(title="crm-lead-ai-bridge", lifespan=lifespan)

    def enqueue(request: Request, lead: Lead, fetched: bool = True) -> dict:
        job_id, created = request.app.state.store.enqueue(
            lead.dedup_key(), JobPayload(lead=lead, fetched=fetched).model_dump())
        return {"ok": True, "job_id": job_id, "duplicate": not created}

    @app.post("/webhooks/form")
    async def form_webhook(lead: Lead, request: Request, x_form_secret: str | None = Header(None)):
        if not _secret_ok(x_form_secret, s.form_secret):
            raise HTTPException(403, "bad secret")
        if not (lead.phone or lead.email):
            raise HTTPException(422, "нужен телефон или email")
        return enqueue(request, lead.model_copy(update={"source": "form", "external_id": ""}))

    @app.post("/webhooks/bitrix")
    async def bitrix_webhook(request: Request):
        form = dict(await request.form())
        if not _secret_ok(_form_value(form, r"auth\[application_token\]"), s.bitrix_app_token):
            raise HTTPException(403, "bad application_token")
        if form.get("event", "").upper() != "ONCRMLEADADD":
            return {"ok": True, "ignored": "event"}
        lead_id = _form_value(form, r"data\[FIELDS\]\[ID\]")
        if not lead_id:
            raise HTTPException(422, "нет ID лида")
        if request.app.state.store.is_created_by_us("bitrix", lead_id):
            return {"ok": True, "ignored": "created_by_bridge"}  # наше же зеркало — не зацикливаемся
        return enqueue(request, Lead(source="bitrix", external_id=lead_id), fetched=False)

    @app.post("/webhooks/amo")
    async def amo_webhook(request: Request, secret: str | None = None):
        if not _secret_ok(secret, s.amo_webhook_secret):
            raise HTTPException(403, "bad secret")
        form = dict(await request.form())
        ids = [str(v) for k, v in form.items() if re.fullmatch(r"leads\[add\]\[\d+\]\[id\]", k)]
        if not ids:
            return {"ok": True, "ignored": "event"}
        results = []
        for lead_id in ids:  # amo может прислать пачку лидов в одном вебхуке
            if request.app.state.store.is_created_by_us("amo", lead_id):
                results.append({"lead_id": lead_id, "ignored": "created_by_bridge"})
            else:
                results.append({"lead_id": lead_id, **enqueue(request, Lead(source="amo", external_id=lead_id), fetched=False)})
        return {"ok": True, "results": results}

    @app.get("/health")
    async def health():
        return {"ok": True}

    def admin(x_form_secret: str | None) -> None:
        if not _secret_ok(x_form_secret, s.form_secret):
            raise HTTPException(403, "bad secret")

    @app.get("/stats")
    async def stats(request: Request, x_form_secret: str | None = Header(None)):
        admin(x_form_secret)
        return request.app.state.store.stats()

    @app.post("/jobs/{job_id}/retry")
    async def retry(job_id: int, request: Request, x_form_secret: str | None = Header(None)):
        admin(x_form_secret)
        if not request.app.state.store.retry_dead(job_id):
            raise HTTPException(404, "нет такой задачи в статусе dead")
        return {"ok": True}

    return app


app = create_app()
