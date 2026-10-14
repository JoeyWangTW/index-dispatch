import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet


@dataclass
class Settings:
    base_url: str
    admin_token: str
    ingest_token: str
    encryption_key: str
    database: str = "data/dispatch.sqlite3"
    # Named destinations are Notion PAGE IDs. V1 appends blocks, not database rows.
    destinations: dict[str, str] = field(default_factory=dict)
    default_destination: str = "inbox"
    notion_token: str = ""
    notion_timezone: str = "UTC"
    router_mode: str = "rules"
    typesafe_key: str = ""
    jev_model: str = "jev-latest"
    jev_min_confidence: float = 0.75
    dots_agent_aliases: list[str] = field(default_factory=lambda: ["kiro", "hiro"])
    dots_ack_timeout: int = 300
    dots_progress_timeout: int = 1800
    destination_descriptions: dict[str, str] = field(default_factory=dict)
    openai_key: str = ""
    router_model: str = ""
    oauth_redirects: list[str] = field(default_factory=list)
    callback_hosts: list[str] = field(default_factory=lambda: ["chatgpt.com"])
    worker_enabled: bool = True

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        url = urlsplit(self.base_url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("BASE_URL must use HTTPS (HTTP is allowed only on loopback for development)")
        if url.path or url.query or url.fragment or url.username:
            raise ValueError("BASE_URL must be an origin, with no path or credentials")
        if min(len(self.admin_token), len(self.ingest_token)) < 32 or self.admin_token == self.ingest_token:
            raise ValueError("Use two distinct random tokens of at least 32 characters")
        Fernet(self.encryption_key.encode())
        ZoneInfo(self.notion_timezone)
        if self.router_mode not in {"rules", "openai", "jev"}:
            raise ValueError("ROUTER_MODE must be rules, jev or openai")
        if self.router_mode == "openai" and not (self.openai_key and self.router_model):
            raise ValueError("OpenAI routing requires OPENAI_API_KEY and ROUTER_MODEL")
        if self.router_mode == "jev" and not (self.typesafe_key and self.jev_model):
            raise ValueError("Jev routing requires TYPESAFE_API_KEY (or JEV_API_KEY) and JEV_MODEL")
        if not math.isfinite(self.jev_min_confidence) or not 0 <= self.jev_min_confidence <= 1:
            raise ValueError("JEV_MIN_CONFIDENCE must be a finite number from 0 to 1")
        if min(self.dots_ack_timeout, self.dots_progress_timeout) < 1:
            raise ValueError("Dots acknowledgment and progress timeouts must be positive seconds")
        if not isinstance(self.destination_descriptions, dict) or any(
            alias not in self.destinations or not isinstance(description, str) or not description.strip()
            for alias, description in self.destination_descriptions.items()
        ):
            raise ValueError("NOTE_DESTINATION_DESCRIPTIONS must map configured aliases to descriptions")
        for alias, page in self.destinations.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", alias):
                raise ValueError("Destination names must be lowercase slugs")
            if not re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}", page):
                raise ValueError("Each destination must contain a Notion page ID, not a URL")
        for redirect in self.oauth_redirects:
            parsed = urlsplit(redirect)
            if parsed.scheme != "https" or not parsed.hostname or parsed.fragment or parsed.username:
                raise ValueError("OAuth redirect URLs must be exact HTTPS URLs without fragments")

    @classmethod
    def from_env(cls):
        def csv(name, default=""):
            return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]

        return cls(
            base_url=os.getenv("BASE_URL", "http://127.0.0.1:8787"),
            admin_token=os.environ["ADMIN_TOKEN"],
            ingest_token=os.environ["INDEX_TOKEN"],
            encryption_key=os.environ["ENCRYPTION_KEY"],
            database=os.getenv("DATABASE_PATH", "data/dispatch.sqlite3"),
            destinations=json.loads(os.getenv("NOTE_DESTINATIONS", "{}")),
            default_destination=os.getenv("DEFAULT_DESTINATION", "inbox"),
            notion_token=os.getenv("NOTION_TOKEN", ""),
            notion_timezone=os.getenv("NOTION_TIMEZONE", "UTC"),
            router_mode=os.getenv("ROUTER_MODE", "rules"),
            typesafe_key=os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY", ""),
            jev_model=os.getenv("JEV_MODEL", "jev-latest"),
            jev_min_confidence=float(os.getenv("JEV_MIN_CONFIDENCE", "0.75")),
            dots_agent_aliases=csv("DOTS_AGENT_ALIASES", "kiro,hiro"),
            dots_ack_timeout=int(os.getenv("DOTS_ACK_TIMEOUT_SECONDS", "300")),
            dots_progress_timeout=int(os.getenv("DOTS_PROGRESS_TIMEOUT_SECONDS", "1800")),
            destination_descriptions=json.loads(os.getenv("NOTE_DESTINATION_DESCRIPTIONS", "{}")),
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            router_model=os.getenv("ROUTER_MODEL", ""),
            oauth_redirects=csv("OAUTH_REDIRECT_URIS"),
            callback_hosts=csv("DOTS_CALLBACK_HOSTS", "chatgpt.com"),
        )

    def prepare_directory(self):
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
