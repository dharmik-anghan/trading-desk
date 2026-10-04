"""Typed access to configuration loaded from the environment / .env file."""

import sys

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for every venue the desk can talk to.

    Values are read from a `.env` file (see `.env.example`) or real
    environment variables. Each venue's credentials are optional: a venue left
    blank is simply not configured, and asking for its adapter says so - see
    `broker/factory.py`. A setup with one broker does not need another's keys.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fyers, for the index options desk.
    fyers_client_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = ""
    fyers_access_token: str = ""

    # Optional: enables TOTP auto-login (broker/fyers/auth.py) so the daily
    # token refresh doesn't need a manual browser step. All three or none -
    # partial credentials fall back to the manual flow. These are more
    # sensitive than the API key/secret above (your TOTP secret alone gives
    # permanent 2FA-bypass capability if it ever leaked) - never log or
    # print these values.
    fyers_username: str = ""
    fyers_totp_key: str = ""
    fyers_pin: str = ""

    # Shark Exchange, for the perpetuals desk. Requests are signed with the
    # secret (HMAC-SHA256); it is never transmitted. Leave both blank and the
    # venue is simply unavailable.
    shark_api_key: str = ""
    shark_api_secret: str = ""

    # Caps on what this program may do on that venue, per order. They are
    # seatbelts against this software being wrong rather than opinions about a
    # good trade, which is why they are small: a cap too tight costs a retyped
    # order, a cap too loose costs whatever the bug was.
    #
    # Leverage is checked against the venue's own per-contract maximum, which it
    # publishes and which differs sharply - 150x on BTCUSDT, 75x on gold, 50x on
    # oil. SHARK_MAX_LEVERAGE is an optional ceiling of your own on top of that;
    # zero means you are not imposing one.
    # Notional is the cap that means something: it is in money, so one figure
    # covers every instrument, and it catches a fat finger - 0.002 typed as 2 is
    # 168,000 of notional. A quantity cap cannot be compared across contracts
    # worth 840 USDT and 94 cents apiece, so it is off unless you set one.
    shark_max_quantity: float = 0.0
    shark_max_notional: float = 2000.0
    shark_max_leverage: float = 0.0

    # Real orders on Shark's options, from the live strategy builder. Off unless
    # set: the page offers a live session only when this is true, and the
    # server refuses one otherwise. The notional cap above applies to each leg
    # (its quantity at the index price); the daily loss stops new live orders
    # once today's live legs are down this much, in USDT, fees included.
    shark_options_live: bool = False
    shark_options_daily_loss: float = 50.0

    # Optional: where alerts are delivered when nobody is watching the screen.
    # Both or neither - with either missing, the desk still records alerts and
    # shows them, it just sends nothing. The bot token is a bearer credential:
    # anyone holding it controls the bot, so never log or print it.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def has_fyers(self) -> bool:
        return bool(self.fyers_client_id and self.fyers_secret_key and self.fyers_redirect_uri)

    @property
    def has_auto_login_credentials(self) -> bool:
        return bool(self.fyers_username and self.fyers_totp_key and self.fyers_pin)

    @property
    def has_shark(self) -> bool:
        return bool(self.shark_api_key and self.shark_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class MissingSettingsError(SystemExit):
    """Raised (as a clean exit, not a traceback) when required config is absent."""


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
        print(
            f"Missing or invalid settings: {missing}.\n"
            "Copy .env.example to .env and fill it in — see docs/SETUP.md.",
            file=sys.stderr,
        )
        raise MissingSettingsError(1) from None
