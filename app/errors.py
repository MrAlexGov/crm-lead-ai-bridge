class IntegrationError(Exception):
    """retryable=True — временная проблема (сеть, 429, 5xx, лимит), задачу надо повторить позже."""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable
