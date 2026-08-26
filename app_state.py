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


def _build_context_text(context: dict) -> str:
    if not context:
        return ""
    parts = []
    for key, value in context.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _build_telegram_message(level_name: str, account_name: str, action: str, ticket: int, message: str, context: dict) -> str:
    lines = [
        f"<b>{_escape_html(level_name)}</b> trade sync alert",
        f"<b>account</b>: <code>{_escape_html(account_name)}</code>",
        f"<b>action</b>: <code>{_escape_html(action)}</code>",
        f"<b>ticket</b>: <code>{_escape_html(ticket)}</code>",
        f"<b>message</b>: <code>{_escape_html(message)}</code>",
    ]

    ctx_text = _build_context_text(context)
    if ctx_text:
        lines.append(f"<b>context</b>: <code>{_escape_html(_truncate_text(ctx_text, 1500))}</code>")

    return _truncate_text("\n".join(lines), TELEGRAM_MAX_TEXT_LEN)


def _telegram_dedupe_key(level_name: str, account_name: str, action: str, ticket: int, message: str) -> str:
    return f"{level_name}|{account_name}|{action}|{ticket}|{message}"


def _telegram_should_send(level_name: str, account_name: str, action: str, ticket: int, message: str) -> bool:
    if not _telegram_is_configured():
        return False
    if not _alert_level_enabled(level_name):
        return False

    key = _telegram_dedupe_key(level_name, account_name, action, ticket, message)
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
            if int(status_code) >= 200 and int(status_code) < 300:
                return True
            logger.error(f"Telegram send failed with HTTP status {status_code}")
            return False
    except Exception as e:
        logger.error(f"Telegram send exception: {e}")
        return False


def alert_event(level_name: str, account_name: str, action: str, ticket: int, message: str, **context):
    level_name = str(level_name or "ERROR").upper()
    context_text = _build_context_text(context)

    terminal_msg = (
        f"[{account_name}] {level_name} | action={action} ticket={ticket} "
        f"message={message}"
    )
    if context_text:
        terminal_msg += f" {context_text}"

    level_method = getattr(logger, level_name.lower(), logger.error)
    level_method(terminal_msg)

    if _telegram_should_send(level_name, account_name, action, ticket, message):
        telegram_text = _build_telegram_message(
            level_name=level_name,
            account_name=account_name,
            action=action,
            ticket=ticket,
            message=message,
            context=context,
        )
        ok = _send_telegram_message(telegram_text)
        if ok:
            logger.info(
                f"[{account_name}] Telegram alert sent | action={action} ticket={ticket}"
            )
        else:
            logger.error(
                f"[{account_name}] Telegram alert send failed | action={action} ticket={ticket}"
            )


def log_trade_critical(account_name: str, action: str, ticket: int, exc: Exception, **context):
    ctx = " ".join(f"{k}={v}" for k, v in context.items() if v is not None)
    logger.error(
        f"[{account_name}] CRITICAL trade sync failure | action={action} ticket={ticket} {ctx} error={exc}"
    )
    logger.exception(
        f"[{account_name}] TRACEBACK | action={action} ticket={ticket} {ctx}"
    )

    if _telegram_should_send("CRITICAL", account_name, action, ticket, str(exc)):
        telegram_text = _build_telegram_message(
            level_name="CRITICAL",
            account_name=account_name,
            action=action,
            ticket=ticket,
            message=str(exc),
            context=context,
        )
        ok = _send_telegram_message(telegram_text)
        if ok:
            logger.info(
                f"[{account_name}] Telegram critical alert sent | action={action} ticket={ticket}"
            )
        else:
            logger.error(
                f"[{account_name}] Telegram critical alert send failed | action={action} ticket={ticket}"
            )


def alert_trade_failure(account_name: str, action: str, ticket: int, exc: Exception, **context):
    log_trade_critical(
        account_name=account_name,
        action=action,
        ticket=ticket,
        exc=exc,
        **context,
    )


def alert_trade_warning(account_name: str, action: str, ticket: int, message: str, **context):
    alert_event(
        level_name="WARNING",
        account_name=account_name,
        action=action,
        ticket=ticket,
        message=message,
        **context,
    )


def alert_trade_info(account_name: str, action: str, ticket: int, message: str, **context):
    alert_event(
        level_name="INFO",
        account_name=account_name,
        action=action,
        ticket=ticket,
        message=message,
        **context,
    )


# Global pending SL/TP map:
# account_name -> {mt5_ticket -> dict(symbol, sl, tp, created_ms, attempts, next_retry_ms, last_error)}
PENDING_SLTP = {}

# --- PATCH: pending lifecycle support ---
# Track live pending mapping to allow cancellation on PENDING_CLOSE.
# mt5_ticket -> dict(symbol, side, pending_type, volume, label, ctrader_order_id, created_ts)
PENDING_MAP = {}

# Simple dedupe to avoid double-processing when MT5 sends both TT-DELETE and polling close.
# (mt5_ticket, event_type) -> last_seen_epoch_ms
EVENT_DEDUPE = {}
DEDUPE_WINDOW_MS = 1500

# --- PATCH: close-proportional support (for FIXED_LOT / FIXED_USD / PERCENT_EQUITY) ---
# Store the master (MT5) original OPEN lots so we can compute partial-close percent later:
# pct = close_lots / master_open_lots
# mt5_ticket -> float lots
MASTER_OPEN_LOTS = {}

# Track cumulative lots closed on the master side so we can base each proportional
# follower close on the *remaining* master size instead of the original size.
# mt5_ticket -> float lots_closed_so_far
MASTER_CLOSED_LOTS = {}
