from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DROP_", env_file=".env")

    # Postgres in docker-compose; SQLite is the zero-setup local default.
    database_url: str = "sqlite+aiosqlite:///./dropengine.db"

    # Reservations
    reservation_ttl_seconds: int = 120
    max_qty_per_product: int = 3   # per customer, across all their carts in a drop
    max_cart_lines: int = 5        # different products in a single cart
    sweeper_interval_seconds: float = 5.0

    # Waiting room
    queue_required: bool = True
    queue_batch_size: int = 5
    queue_interval_seconds: float = 2.0
    queue_max_active: int = 20
    queue_admit_window_seconds: int = 180

    # Auth
    jwt_secret: str = "dev-secret-change-me-before-deploying-0123456789"
    jwt_ttl_seconds: int = 86400

    # Payments (a fake gateway signs webhooks with this shared secret)
    webhook_secret: str = "whsec_dev"
    dev_mode: bool = True  # enables the payment simulator endpoint

    # Rate limits (token buckets)
    rate_limit_enabled: bool = True
    rl_user_capacity: int = 10
    rl_user_refill_per_sec: float = 2.0
    rl_ip_capacity: int = 60
    rl_ip_refill_per_sec: float = 20.0

    # Outbox
    outbox_interval_seconds: float = 1.0


settings = Settings()
