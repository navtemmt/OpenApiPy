"""Global application state and logging configuration.
Shared state for the MT5 to cTrader bridge server.
"""
import html
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


if load_dotenv is not None:
    try:
        load_dotenv()
    except Exception:
        pass


_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

if not root_logger.handlers:
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root_logger.addHandler(console_handler)
else:
    for handler in root_logger.handlers:
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))

logger = logging.getLogger("mt5_ctrader_bridge")
logger.setLevel(logging.INFO)
logger.propagate = True


TELEGRAM_ENABLED = _to_bool(os.getenv("TELEGRAM_ENABLED", "false"), False)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
TELEGRAM_PARSE_MODE = (os.getenv("TELEGRAM_PARSE_MODE") or "HTML").strip().upper()
TELEGRAM_DISABLE_WEB_PREVIEW = _to_bool(
    os.getenv("TELEGRAM_DISABLE_WEB_PREVIEW", "true"), True
)
TELEGRAM_TIMEOUT_SEC = float(os.getenv("TELEGRAM_TIMEOUT_SEC", "8") or 8)
TELEGRAM_ALERT_LEVEL = (os.getenv("TELEGRAM_ALERT_LEVEL") or "ERROR").strip().upper()
TELEGRAM_DEDUP_WINDOW_SEC = int(float(os.getenv("TELEGRAM_DEDUP_WINDOW_SEC", "60") or 60))
TELEGRAM_MAX_TEXT_LEN = 3500

_TELEGRAM_LAST_SENT = {}
_TELEGRAM_LOCK = threading.Lock()


def _telegram_is_configured() -> bool:
    return bool(TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _alert_level_enabled(level_name: str) -> bool:
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    current = levels.get(str(TELEGRAM_ALERT_LEVEL).upper(), 40)
    incoming = levels.get(str(level_name).upper(), 40)
    return incoming >= current


def _escape_html(value) -> str:
    return html.escape(str(value), quote=False)


def _truncate_text(text: str, max_len: int = TELEGRAM_MAX_TEXT_LEN) -> str:
    text = str(text or "")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _normalize_context(context: dict) -> dict:
    normalized = {}
    for key, value in (context or {}).items():
        if value is None:
            continue
        normalized[str(key)] = value
    return normalized


def _build_context_text(context: dict) -> str:
    context = _normalize_context(context)
    if not context:
        return ""
    parts = []
    for key, value in context.items():
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _build_telegram_message(
    level_name: str,
    event: str,
    message: str,
    context: dict,
) -> str:
    context = _normalize_context(context)

    account_name = context.get("account_name", "-")
    action = context.get("action", event or "-")
    ticket = context.get("ticket", 0)

    lines = [
        f"<b>{_escape_html(level_name)}</b> trade sync alert",
        f"<b>account</b>: <code>{_escape_html(account_name)}</code>",
        f"<b>action</b>: <code>{_escape_html(action)}</code>",
        f"<b>ticket</b>: <code>{_escape_html(ticket)}</code>",
        f"<b>event</b>: <code>{_escape_html(event)}</code>",
        f"<b>message</b>: <code>{_escape_html(message)}</code>",
    ]

    extra_context = {
        k: v
        for k, v in context.items()
        if k not in {"account_name", "action", "ticket"}
    }
    ctx_text = _build_context_text(extra_context)
    if ctx_text:
        lines.append(
            f"<b>context</b>: <code>{_escape_html(_truncate_text(ctx_text, 1500))}</code>"
        )

    return _truncate_text("\n".join(lines), TELEGRAM_MAX_TEXT_LEN)


def _telegram_dedupe_key(level_name: str, event: str, message: str, context: dict) -> str:
    context = _normalize_context(context)
    account_name = context.get("account_name", "-")
    action = context.get("action", event or "-")
    ticket = context.get("ticket", 0)
    return f"{level_name}|{account_name}|{action}|{ticket}|{event}|{message}"


def _telegram_should_send(level_name: str, event: str, message: str, context: dict) -> bool:
    if not _telegram_is_configured():
        return False
    if not _alert_level_enabled(level_name):
        return False

    key = _telegram_dedupe_key(level_name, event, message, context)
    now = time.time()

    with _TELEGRAM_LOCK:
        last_sent = _TELEGRAM_LAST_SENT.get(key, 0)
        if (now - last_sent) < TELEGRAM_DEDUP_WINDOW_SEC:
            return False
        _TELEGRAM_LAST_SENT[key] = now

    return True


def _send_telegram_message(text: str) -> bool:
    if not _telegram_is_configured():
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _truncate_text(text, TELEGRAM_MAX_TEXT_LEN),
        "parse_mode": TELEGRAM_PARSE_MODE,
        "disable_web_page_preview": "true" if TELEGRAM_DISABLE_WEB_PREVIEW else "false",
    }

    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SEC) as response:
            status_code = getattr(response, "status", 200)
            if 200 <= int(status_code) < 300:
                return True
            logger.error("Telegram send failed with HTTP status %s", status_code)
            return False
    except Exception as e:
        logger.error("Telegram send exception: %s", e)
        return False


def notify(level_name: str, event: str, message: str, exc: Optional[Exception] = None, **context):
    level_name = str(level_name or "ERROR").upper()
    context = _normalize_context(context)

    if exc is not None and "error" not in context:
        context["error"] = str(exc)

    context_text = _build_context_text(context)
    terminal_msg = f"{level_name} | event={event} message={message}"
    if context_text:
        terminal_msg += f" | {context_text}"

    if exc is not None and level_name in ("ERROR", "CRITICAL"):
        logger.exception(terminal_msg)
    else:
        level_method = getattr(logger, level_name.lower(), logger.error)
        level_method(terminal_msg)

    if _telegram_should_send(level_name, event, message, context):
        telegram_text = _build_telegram_message(
            level_name=level_name,
            event=event,
            message=message,
            context=context,
        )
        ok = _send_telegram_message(telegram_text)
        if ok:
            logger.info("Telegram alert sent | event=%s", event)
        else:
            logger.error("Telegram alert send failed | event=%s", event)


def notify_debug(event: str, message: str, **context):
    notify("DEBUG", event, message, **context)


def notify_info(event: str, message: str, **context):
    notify("INFO", event, message, **context)


def notify_warning(event: str, message: str, **context):
    notify("WARNING", event, message, **context)


def notify_error(event: str, message: str, exc: Optional[Exception] = None, **context):
    notify("ERROR", event, message, exc=exc, **context)


def notify_critical(event: str, message: str, exc: Optional[Exception] = None, **context):
    notify("CRITICAL", event, message, exc=exc, **context)


# Backward-compatible aliases for older modules
def alert_event(level_name: str, account_name: str, action: str, ticket: int, message: str, **context):
    notify(
        level_name=level_name,
        event=action,
        message=message,
        account_name=account_name,
        action=action,
        ticket=ticket,
        **context,
    )


def log_trade_critical(account_name: str, action: str, ticket: int, exc: Exception, **context):
    notify_critical(
        event=action,
        message=str(exc),
        exc=exc,
        account_name=account_name,
        action=action,
        ticket=ticket,
        **context,
    )


def alert_trade_failure(account_name: str, action: str, ticket: int, exc: Exception, **context):
    notify_error(
        event=action,
        message=str(exc),
        exc=exc,
        account_name=account_name,
        action=action,
        ticket=ticket,
        **context,
    )


def alert_trade_warning(account_name: str, action: str, ticket: int, message: str, **context):
    notify_warning(
        event=action,
        message=message,
        account_name=account_name,
        action=action,
        ticket=ticket,
        **context,
    )


def alert_trade_info(account_name: str, action: str, ticket: int, message: str, **context):
    notify_info(
        event=action,
        message=message,
        account_name=account_name,
        action=action,
        ticket=ticket,
        **context,
    )


# Global pending SL/TP map:
# account_name -> {mt5_ticket -> dict(symbol, sl, tp, created_ms, attempts, next_retry_ms, last_error)}
PENDING_SLTP = {}

# Track live pending mapping to allow cancellation on PENDING_CLOSE.
# mt5_ticket -> dict(symbol, side, pending_type, volume, label, ctrader_order_id, created_ts)
PENDING_MAP = {}

# Simple dedupe to avoid double-processing when MT5 sends both TT-DELETE and polling close.
# (mt5_ticket, event_type) -> last_seen_epoch_ms
EVENT_DEDUPE = {}
DEDUPE_WINDOW_MS = 1500

# Store the master (MT5) original OPEN lots so we can compute partial-close percent later:
# pct = close_lots / master_open_lots
# mt5_ticket -> float lots
MASTER_OPEN_LOTS = {}

# Track cumulative lots closed on the master side so we can base each proportional
# follower close on the remaining master size instead of the original size.
# mt5_ticket -> float lots_closed_so_far
MASTER_CLOSED_LOTS = {}
