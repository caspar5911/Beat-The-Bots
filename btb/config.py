"""Settings from the game's own .env (never the trading bot's)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
VAR = Path(os.environ.get("BTB_VAR_DIR", ROOT / "var"))
NY = ZoneInfo("America/New_York")
SG = ZoneInfo("Asia/Singapore")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    chat_id: str = ""
    glm_base_url: str = ""
    glm_model: str = "gx10-current"
    glm_api_key: str = ""
    opend_host: str = "127.0.0.1"
    opend_port: int = 11111
    db_path: Path = VAR / "btb.db"
    stocks_per_day: int = 5
    game_name: str = "Huat or Not?"
    bot_name: str = "🤖 Ah Huat"
    max_groups: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        e = os.environ.get
        return cls(telegram_token=e("BTB_TELEGRAM_TOKEN", ""), chat_id=e("BTB_CHAT_ID", ""),
                   glm_base_url=e("GLM_BASE_URL", ""), glm_model=e("GLM_MODEL", "gx10-current"),
                   glm_api_key=e("GLM_API_KEY", ""), opend_host=e("MOOMOO_OPEND_HOST", "127.0.0.1"),
                   opend_port=int(e("MOOMOO_OPEND_PORT", "11111")),
                   game_name=e("BTB_GAME_NAME", "") or "Huat or Not?", bot_name=e("BTB_BOT_NAME", "") or "🤖 Ah Huat",
                   max_groups=int(e("BTB_MAX_GROUPS", "20") or 20))
