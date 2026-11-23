from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_JWT_SECRET = "dev-secret-change-me-before-deploying-0123456789"
DEV_WEBHOOK_SECRET = "whsec_dev"
DEV_ADMIN_PASSWORD = "admin-dev-change-me"


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
    jwt_secret: str = DEV_JWT_SECRET
    jwt_ttl_seconds: int = 86400

    # Dashboard (operator) sign-in. Separate from the shop's emailed-code login: one
    # shared operator account, short-lived tokens that only work on admin endpoints.
    admin_username: str = "admin"
    admin_password: str = DEV_ADMIN_PASSWORD
    admin_jwt_ttl_seconds: int = 3600

    # Passwordless sign-in: a one-time code is emailed, then exchanged for a JWT
    otp_ttl_seconds: int = 600          # a code is valid for 10 minutes
    otp_max_attempts: int = 3           # wrong guesses before a code is dead
    otp_resend_cooldown_seconds: int = 60
    otp_max_codes_per_hour: int = 3     # caps total guesses per address: codes/hour x attempts per code (3 x 3 = 9)

    # Email delivery. "console" prints the code to the server log (local dev),
    # "memory" keeps it in-process and exposes it to the dev endpoint (tests and
    # the load test), "smtp" really sends it.
    email_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "DropEngine <no-reply@example.com>"

    # Payments (a fake gateway signs webhooks with this shared secret)
    webhook_secret: str = DEV_WEBHOOK_SECRET
    dev_mode: bool = True  # enables the payment simulator endpoint

    # Rate limits (token buckets)
    rate_limit_enabled: bool = True
    rl_user_capacity: int = 10
    rl_user_refill_per_sec: float = 2.0
    rl_ip_capacity: int = 60
    rl_ip_refill_per_sec: float = 20.0
    rl_auth_capacity: int = 10          # request-code + verify-code, per IP
    rl_auth_refill_per_sec: float = 0.2

    # Outbox
    outbox_interval_seconds: float = 1.0

    def assert_safe_for_production(self) -> None:
        """Refuse to boot with the built-in dev secrets unless dev_mode is on."""
        if self.dev_mode:
            return
        weak = [name for name, dev in (("DROP_JWT_SECRET", DEV_JWT_SECRET),
                                       ("DROP_WEBHOOK_SECRET", DEV_WEBHOOK_SECRET),
                                       ("DROP_ADMIN_PASSWORD", DEV_ADMIN_PASSWORD))
                if getattr(self, name[5:].lower()) == dev]
        if weak:
            raise RuntimeError(f"dev_mode is off but these still have their default value: {', '.join(weak)}")


settings = Settings()