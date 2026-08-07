from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from twisted.internet import reactor
from twisted.python import log
from dotenv import load_dotenv

import os
import sys


# ---------------------------------------------------------------------
# Logging and .env loading
# ---------------------------------------------------------------------

log.startLogging(sys.stderr)

print("Starting test_client.py...", file=sys.stderr, flush=True)

load_dotenv(override=True)


# ---------------------------------------------------------------------
# Environment / account selection
# ---------------------------------------------------------------------

ACCOUNT_ALIAS = (
    os.getenv("TEST_ACCOUNT_ALIAS")
    or os.getenv("ACCOUNT_ALIAS")
    or "DEMO"
).strip().upper()

# Set CTRADER_HOST=live.ctraderapi.com to test a live endpoint.
HOST = (
    os.getenv("CTRADER_HOST")
    or EndPoints.PROTOBUF_DEMO_HOST
).strip()

PORT = int(os.getenv("CTRADER_PORT") or EndPoints.PROTOBUF_PORT)


def _env(*names):
    """Return the first non-empty environment variable in names."""
    for name in names:
        value = os.getenv(name)
        if value is not None:
            value = value.strip()
            if value:
                return value
    return None


CLIENT_ID = _env(
    f"ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_ID",
    "CTRADER_CLIENT_ID",
    "CLIENT_ID",
)

CLIENT_SECRET = _env(
    f"ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_SECRET",
    "CTRADER_CLIENT_SECRET",
    "CLIENT_SECRET",
)

ACCESS_TOKEN = _env(
    f"ACCOUNT_{ACCOUNT_ALIAS}_ACCESS_TOKEN",
    "ACCESS_TOKEN",
)

REFRESH_TOKEN = _env(
    f"ACCOUNT_{ACCOUNT_ALIAS}_REFRESH_TOKEN",
    "REFRESH_TOKEN",
)

ACCOUNT_ID = _env(
    f"ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID",
    "CTRADER_ACCOUNT_ID",
    "ACCOUNT_ID",
)

print(f"DEBUG: ACCOUNT_ALIAS = {ACCOUNT_ALIAS}", file=sys.stderr, flush=True)
print(f"DEBUG: HOST = {HOST}", file=sys.stderr, flush=True)
print(f"DEBUG: PORT = {PORT}", file=sys.stderr, flush=True)
print(f"DEBUG: CLIENT_ID loaded = {bool(CLIENT_ID)}", file=sys.stderr, flush=True)
print(f"DEBUG: CLIENT_SECRET loaded = {bool(CLIENT_SECRET)}", file=sys.stderr, flush=True)
print(f"DEBUG: ACCESS_TOKEN loaded = {bool(ACCESS_TOKEN)}", file=sys.stderr, flush=True)
print(f"DEBUG: ACCOUNT_ID loaded = {ACCOUNT_ID}", file=sys.stderr, flush=True)

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError(
        f"Missing cTrader app credentials for account alias '{ACCOUNT_ALIAS}'. "
        f"Expected ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_ID and "
        f"ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_SECRET in .env."
    )

if not ACCESS_TOKEN:
    raise RuntimeError(
        f"Missing access token for account alias '{ACCOUNT_ALIAS}'. "
        f"Expected ACCOUNT_{ACCOUNT_ALIAS}_ACCESS_TOKEN in .env."
    )


# ---------------------------------------------------------------------
# cTrader client callbacks
# ---------------------------------------------------------------------

client = Client(HOST, PORT, TcpProtocol)

timeout_call = None
app_authenticated = False
account_list_received = False


def stop_reactor(reason=None):
    """Stop the client service and Twisted reactor safely."""
    global timeout_call

    if timeout_call is not None and timeout_call.active():
        timeout_call.cancel()

    if reason:
        print(f"\nStopping: {reason}", file=sys.stderr, flush=True)

    try:
        client.stopService()
    except Exception as exc:
        print(f"DEBUG: client.stopService failed: {exc}", file=sys.stderr, flush=True)

    if reactor.running:
        reactor.stop()


def on_error(failure):
    print("\n=== cTrader request error ===", file=sys.stderr, flush=True)
    print(failure, file=sys.stderr, flush=True)


def on_connected(c):
    print("\n=== TCP/TLS CONNECTED ===", flush=True)
    print("Sending ProtoOAApplicationAuthReq...", flush=True)

    try:
        req = ProtoOAApplicationAuthReq()
        req.clientId = str(CLIENT_ID).strip()
        req.clientSecret = str(CLIENT_SECRET).strip()

        deferred = c.send(req)
        deferred.addErrback(on_error)

    except Exception as exc:
        print(f"Failed to send application authentication request: {exc}", file=sys.stderr)
        stop_reactor("application auth request construction/send failed")


def on_disconnected(c, reason):
    print("\n=== DISCONNECTED ===", file=sys.stderr, flush=True)
    print(f"Reason: {reason}", file=sys.stderr, flush=True)


def on_message(c, message):
    global app_authenticated
    global account_list_received

    msg_type = message.payloadType
    msg_data = Protobuf.extract(message)

    print(f"\n=== MESSAGE RECEIVED: payloadType={msg_type} ===", flush=True)
    print(msg_data, flush=True)

    # ProtoOAApplicationAuthRes
    if msg_type == 2101:
        app_authenticated = True

        print("\n=== APPLICATION AUTHENTICATED ===", flush=True)
        print(f"Requesting account list for alias: {ACCOUNT_ALIAS}", flush=True)

        try:
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = str(ACCESS_TOKEN).strip()

            deferred = c.send(req)
            deferred.addErrback(on_error)

        except Exception as exc:
            print(f"Failed to request account list: {exc}", file=sys.stderr, flush=True)
            stop_reactor("account-list request failed")

    # ProtoOAGetAccountListByAccessTokenRes
    elif msg_type == 2142:
        account_list_received = True

        print("\n========== AVAILABLE CTRADER ACCOUNTS ==========")
        matched = False
        account_count = 0

        if hasattr(msg_data, "ctidTraderAccount"):
            for account in msg_data.ctidTraderAccount:
                account_count += 1

                account_id = getattr(account, "ctidTraderAccountId", None)
                is_live = getattr(account, "isLive", None)
                trader_login = getattr(account, "traderLogin", None)
                broker_name = getattr(account, "brokerName", None)
                balance = getattr(account, "balance", None)

                print(f"\nAccount ID: {account_id}")
                print(f"  Type: {'LIVE' if is_live else 'DEMO'}")

                if trader_login is not None:
                    print(f"  Trader Login: {trader_login}")

                if broker_name is not None:
                    print(f"  Broker: {broker_name}")

                if balance is not None:
                    print(f"  Balance: {balance / 100:.2f}")

                if (
                    ACCOUNT_ID
                    and ACCOUNT_ID != "your_account_id_here"
                    and str(account_id) == str(ACCOUNT_ID)
                ):
                    matched = True
                    print("  >>> MATCHES ACCOUNT_ID IN .env")

        print("\n===============================================")

        if account_count == 0:
            print("No accounts were returned by this access token.")

        elif not ACCOUNT_ID or ACCOUNT_ID == "your_account_id_here":
            print(
                f"\nCopy the target numeric ID above into:\n"
                f"ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID=<account_id>"
            )

        elif matched:
            print(
                f"\nConfigured ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID "
                f"matches an account returned by this access token."
            )

        else:
            print(
                f"\nWARNING: configured ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID="
                f"{ACCOUNT_ID} was not returned by this access token."
            )

        stop_reactor("account list received successfully")

    else:
        print(
            f"\nINFO: Received unhandled payload type {msg_type}. "
            f"Waiting for the expected account-list response...",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------
# Connection diagnostics
# ---------------------------------------------------------------------

def timeout_check():
    status = (
        f"app_authenticated={app_authenticated}, "
        f"account_list_received={account_list_received}"
    )

    print(
        f"\nWARNING: Timed out after 30 seconds ({status}).",
        file=sys.stderr,
        flush=True,
    )

    if not app_authenticated:
        print(
            "No application-auth response was received. "
            "Check Twisted logs above for TLS/connection errors.",
            file=sys.stderr,
            flush=True,
        )
    elif not account_list_received:
        print(
            "Application authentication completed, but no account-list response arrived. "
            "Check the access token and application authorization.",
            file=sys.stderr,
            flush=True,
        )

    stop_reactor("timeout")


# ---------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------

client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)

print(f"Connecting to {HOST}:{PORT}...", file=sys.stderr, flush=True)

timeout_call = reactor.callLater(30, timeout_check)

try:
    start_result = client.startService()
    print(
        f"DEBUG: client.startService() returned: {start_result!r}",
        file=sys.stderr,
        flush=True,
    )
except Exception as exc:
    print(f"Failed to start cTrader client service: {exc}", file=sys.stderr, flush=True)
    raise

print("Service started, running reactor...", file=sys.stderr, flush=True)
reactor.run()
