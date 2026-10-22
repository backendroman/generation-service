from pydantic_settings import BaseSettings

PROVIDERS = ("alpha", "beta")


class Settings(BaseSettings):
    database_url: str = "postgresql://app:app@db:5432/app"
    webhook_secret: str = "dev-secret"

    provider_timeout: float = 60
    provider_http_timeout: float = 5
    watchdog_interval: float = 1
    watchdog_batch: int = 50
    dispatch_lease: float = 30
    dispatch_interval: float = 1
    dispatch_batch: int = 20

    alpha_url: str = "http://alpha:8001"
    beta_url: str = "http://beta:8002"
    public_base_url: str = "http://app:8000"

    max_active_tasks: int = 3
    db_pool_min: int = 2
    db_pool_max: int = 10

    def provider_url(self, name: str) -> str:
        return {"alpha": self.alpha_url, "beta": self.beta_url}[name]


settings = Settings()
