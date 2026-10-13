import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Lead
from app.pipeline import process_due

B = "https://test.bitrix24.ru/rest/1/abc/"
A = "https://test.amocrm.ru/api/v4"
HOT = {"name": "Иван", "phone": "8 (900) 111-22-33", "email": "ivan@example.com",
       "message": "Хотим заказать внедрение CRM, бюджет есть, нужно срочно"}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=str(tmp_path / "t.sqlite3"), bitrix_webhook_url=B,
                    amo_base_url="https://test.amocrm.ru", amo_token="tok", bitrix_app_token="apptok",
                    bitrix_rps=1000, amo_rps=1000, backoff_base_sec=0, openrouter_api_key="")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, run_worker=False)) as c:
        yield c


def drain(client, settings):
    st = client.app.state
    return asyncio.run(process_due(st.store, st.pipeline, settings))


def crm_mocks(router):
    router.post(B + "crm.duplicate.findbycomm.json").respond(json={"result": []})
    router.post(B + "crm.lead.add.json").respond(json={"result": 101})
    router.post(B + "crm.timeline.comment.add.json").respond(json={"result": 1})
    router.post(B + "tasks.task.add.json").respond(json={"result": {"task": {"id": 555}}})
    router.post(A + "/leads/complex").respond(json=[{"id": 202, "contact_id": 9}])
    router.post(A + "/leads/202/notes").respond(json={"_embedded": {"notes": [{"id": 1}]}})


@respx.mock
def test_hot_form_lead_goes_to_both_crms_once(client, settings):
    crm_mocks(respx)
    r = client.post("/webhooks/form", json=HOT)
    assert r.json()["duplicate"] is False
    # повторный вебхук с тем же лидом (форма отправлена дважды) — дубль
    assert client.post("/webhooks/form", json=HOT).json()["duplicate"] is True
    drain(client, settings)

    calls = [c.request.url.path for c in respx.calls]
    assert calls.count("/rest/1/abc/crm.lead.add.json") == 1
    assert calls.count("/rest/1/abc/tasks.task.add.json") == 1  # горячий → задача менеджеру
    assert calls.count("/api/v4/leads/complex") == 1
    assert client.app.state.store.stats()["jobs"] == {"done": 1}


@respx.mock
def test_rate_limit_is_retried_without_duplicates(client, settings):
    crm_mocks(respx)
    respx.post(B + "crm.timeline.comment.add.json").mock(side_effect=[
        httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"}),
        httpx.Response(200, json={"result": 1}),
    ])
    client.post("/webhooks/form", json=HOT)
    drain(client, settings)
    assert client.app.state.store.stats()["jobs"] == {"pending": 1}
    drain(client, settings)
    assert client.app.state.store.stats()["jobs"] == {"done": 1}
    calls = [c.request.url.path for c in respx.calls]
    # лид в Битрикс24 создан один раз, хотя задача выполнялась дважды
    assert calls.count("/rest/1/abc/crm.lead.add.json") == 1


@respx.mock
def test_permanent_error_goes_to_dead_and_can_be_retried(client, settings):
    crm_mocks(respx)
    respx.post(A + "/leads/complex").respond(400, json={"title": "Bad Request"})
    client.post("/webhooks/form", json=HOT)
    drain(client, settings)
    stats = client.app.state.store.stats()
    assert stats["jobs"] == {"dead": 1}
    assert "HTTP 400" in stats["last_errors"][0]["message"]
    assert client.post("/jobs/1/retry").json()["ok"] is True


@respx.mock
def test_bitrix_lead_is_annotated_and_mirrored_without_loop(client, settings):
    crm_mocks(respx)
    respx.post(B + "crm.lead.get.json").respond(json={"result": {
        "ID": "77", "NAME": "Пётр", "PHONE": [{"VALUE": "+79001112233"}], "COMMENTS": "интересует цена"}})
    form = {"event": "ONCRMLEADADD", "data[FIELDS][ID]": "77", "auth[application_token]": "apptok"}
    assert client.post("/webhooks/bitrix", data=form).json()["ok"]
    drain(client, settings)
    calls = [c.request.url.path for c in respx.calls]
    assert "/rest/1/abc/crm.lead.add.json" not in calls  # в исходной CRM новый лид не создаём
    assert calls.count("/api/v4/leads/complex") == 1    # зеркало в amoCRM
    # amoCRM присылает вебхук о лиде 202, который создали мы сами, — игнорируем
    r = client.post("/webhooks/amo", data={"leads[add][0][id]": "202"})
    assert r.json()["results"][0]["ignored"] == "created_by_bridge"


def test_bitrix_webhook_rejects_foreign_token(client):
    r = client.post("/webhooks/bitrix", data={"event": "ONCRMLEADADD", "data[FIELDS][ID]": "1",
                                              "auth[application_token]": "чужой"})
    assert r.status_code == 403


@respx.mock
def test_llm_failure_falls_back_to_rules(client, settings, tmp_path):
    settings.openrouter_api_key = "key"
    crm_mocks(respx)
    respx.post("https://openrouter.ai/api/v1/chat/completions").respond(500)
    client.post("/webhooks/form", json={**HOT, "message": "посмотреть каталог"})
    drain(client, settings)
    assert client.app.state.store.stats()["jobs"] == {"done": 1}


@respx.mock
def test_llm_result_is_used(client, settings):
    settings.openrouter_api_key = "key"
    crm_mocks(respx)
    content = '```json\n{"is_spam": true, "topic": "реклама", "priority": "cold", "summary": "спам", "next_action": "-"}\n```'
    respx.post("https://openrouter.ai/api/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": content}}]})
    client.post("/webhooks/form", json={**HOT, "message": "Предлагаем SEO"})
    drain(client, settings)
    calls = [c.request.url.path for c in respx.calls]
    assert "/rest/1/abc/crm.lead.add.json" not in calls  # спам не заводим в CRM


def test_dedup_key_normalizes_phone():
    a = Lead(phone="8 (900) 111-22-33", message="Привет")
    b = Lead(phone="+7 900 111 22 33", message="привет ")
    assert a.dedup_key() == b.dedup_key()
