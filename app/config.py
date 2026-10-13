from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки — из окружения / .env. Секреты в коде не храним."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: str = "bridge.sqlite3"

    # Битрикс24: входящий вебхук вида https://<portal>.bitrix24.ru/rest/<user>/<code>/
    bitrix_webhook_url: str = ""
    # application_token исходящего вебхука (проверка, что событие пришло от нашего портала)
    bitrix_app_token: str = ""
    bitrix_responsible_id: int = 1  # кому ставить задачи по горячим лидам
    bitrix_rps: float = 2.0  # лимит REST API Битрикс24 ~2 запроса/сек

    # amoCRM: https://<subdomain>.amocrm.ru + долгосрочный токен
    amo_base_url: str = ""
    amo_token: str = ""
    amo_rps: float = 6.0  # лимит amoCRM — 7 запросов/сек на интеграцию
    amo_webhook_secret: str = ""  # передаётся в query (?secret=), т.к. amo не подписывает вебхуки

    # Зеркалирование: лид из одной CRM создаётся и в другой
    mirror_to_bitrix: bool = True
    mirror_to_amo: bool = True

    # LLM через OpenRouter (OpenAI-совместимый API)
    openrouter_api_key: str = ""
    # Через запятую — цепочка: если модель недоступна/упёрлась в лимит, пробуем следующую, затем правила
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free"
    llm_timeout: float = 30.0

    # Очередь повторов
    max_attempts: int = 6
    backoff_base_sec: float = 5.0
    worker_poll_sec: float = 1.0

    form_secret: str = ""  # общий секрет для /webhooks/form (заголовок X-Form-Secret)


settings = Settings()
