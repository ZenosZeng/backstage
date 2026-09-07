"""Feishu webhook delivery; independent from App-token remote control."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import time
import urllib.request

from tools.config import load_env_file, resolve_path


CARD_TEMPLATES = {
    "start": ("blue", "开始"),
    "finished": ("green", "完成"),
    "failed": ("red", "故障"),
    "stalled": ("orange", "停滞"),
    "heartbeat": ("turquoise", "整点提醒"),
    "watchdog_started": ("violet", "监控已启动"),
    "watchdog_stopped": ("yellow", "监控已停止"),
}


def legacy_value(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    pattern = re.compile(
        rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*[\"']?([^\"'\s#]+)"
    )
    for path in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        try:
            for line in path.read_text().splitlines():
                match = pattern.match(line)
                if match:
                    return match.group(1)
        except OSError:
            continue
    return None


def build_card(
    card_type: str,
    title: str,
    content: str,
    *,
    machine: str = "local",
    label: str = "监控",
) -> dict:
    if card_type not in CARD_TEMPLATES:
        raise ValueError(f"unknown card type: {card_type}")
    color, state = CARD_TEMPLATES[card_type]
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"[{machine}][{label}]{state}",
                },
                "template": color,
            },
            "body": {
                "elements": [{"tag": "markdown", "content": f"**{title}**\n{content}"}]
            },
        },
    }


def sign_payload(payload: dict, secret: str, timestamp: int | None = None) -> dict:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = base64.b64encode(
        hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    return {**payload, "timestamp": str(timestamp), "sign": signature}


class Notifier:
    def __init__(self, config: dict, config_dir: Path, label: str) -> None:
        self.enabled = bool(config.get("notify", True))
        self.label = str(config.get("label", label))
        self.machine = str(config.get("machine", socket.gethostname()))
        self.dedup_seconds = float(config.get("dedup_seconds", 600))
        self._last_sent: dict[str, float] = {}
        self.url = self.secret = None
        if not self.enabled:
            return
        if config.get("credentials_file"):
            load_env_file(resolve_path(config["credentials_file"], config_dir))
        key = legacy_value("FEISHU_WEBHOOK") or legacy_value("FEISHU_WEBHOOK_KEY")
        self.url = (
            key
            if key and key.startswith("https://")
            else f"https://open.feishu.cn/open-apis/bot/v2/hook/{key}"
            if key
            else None
        )
        self.secret = legacy_value("FEISHU_SECRET")
        if not self.url:
            raise ValueError(
                "Feishu webhook is not configured; configure credentials or use --no-notify"
            )

    def send_card(self, card_type: str, title: str, content: str) -> bool:
        if not self.enabled:
            return True
        payload = build_card(
            card_type, title, content, machine=self.machine, label=self.label
        )
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        now = time.monotonic()
        if now - self._last_sent.get(fingerprint, float("-inf")) < self.dedup_seconds:
            return True
        if self.secret:
            payload = sign_payload(payload, self.secret)
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.loads(response.read())
                if not isinstance(result, dict) or result.get("code") != 0:
                    print("watchdog notification rejected by Feishu", flush=True)
                    return False
                self._last_sent = {
                    key: stamp
                    for key, stamp in self._last_sent.items()
                    if now - stamp < self.dedup_seconds
                }
                self._last_sent[fingerprint] = now
                return True
            except Exception as error:
                # Exceptions may include the secret-bearing URL; never log their message.
                print(
                    f"watchdog notification failed: {type(error).__name__}", flush=True
                )
                if attempt == 0:
                    time.sleep(1)
        return False
