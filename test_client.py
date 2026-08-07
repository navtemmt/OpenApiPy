from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from twisted.internet import reactor
import sys
import os
from dotenv import load_dotenv

print("Starting test_client.py...", file=sys.stderr, flush=True)

load_dotenv(override=True)

HOST = EndPoints.PROTOBUF_DEMO_HOST
PORT = EndPoints.PROTOBUF_PORT

ACCOUNT_ALIAS = (
    os.getenv("TEST_ACCOUNT_ALIAS")
    or os.getenv("ACCOUNT_ALIAS")
    or "DEMO"
).strip().upper()


def _env(*names):
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
print(f"DEBUG: CLIENT_ID loaded = {bool(CLIENT_ID)}", file=sys.stderr, flush=True)
print(f"DEBUG: CLIENT_SECRET loaded = {bool(CLIENT_SECRET)}", file=sys.stderr, flush=True)
print(f"DEBUG: ACCESS_TOKEN loaded = {bool(ACCESS_TOKEN)}", file=sys.stderr, flush=True)
print(f"DEBUG: ACCOUNT_ID loaded = {ACCOUNT_ID}", file=sys.stderr, flush=True)

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError(
        f"Missing credentials for account alias '{ACCOUNT_ALIAS}'. "
        f"Expected ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_ID and "
        f"ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_SECRET in .env"
    )

if not ACCESS_TOKEN:
    raise RuntimeError(
        f"Missing token for account alias '{ACCOUNT_ALIAS}'. "
        f"Expected ACCOUNT_{ACCOUNT_ALIAS}_ACCESS_TOKEN in .env"
    )

client = Client(HOST, PORT, TcpProtocol)

timeout_call = None
shutdown_scheduled = False


def stop_cleanly(reason):
    """Stop after callbacks/output have finished."""
    global timeout_call

    print(f"\nStopping: {reason}", file=sys.stderr, flush=True)

    if timeout_call is not None and timeout_call.active():
        timeout_call.cancel()

    try:
        client.stopService()
    except Exception as exc:
        print(f"DEBUG: client.stopService error: {exc}", file=sys.stderr, flush=True)

    if reactor.running:
        reactor.stop()


def schedule_exit(reason, delay=0.5):
    """Schedule one clean exit; never stop reactor mid-message callback."""
    global shutdown_scheduled

    if shutdown_scheduled:
        return

    shutdown_scheduled = True
    reactor.callLater(delay, stop_cleanly, reason)


def on_error(failure):
    print("\n=== REQUEST ERROR ===", file=sys.stderr, flush=True)
    print(failure, file=sys.stderr, flush=True)


def on_connected(c):
    print("Connected", flush=True)

    try:
        req = ProtoOAApplicationAuthReq()
        req.clientId = str(CLIENT_ID).strip()
        req.clientSecret = str(CLIENT_SECRET).strip()

        deferred = c.send(req)
        deferred.addErrback(on_error)

    except Exception as exc:
        print(f"Application authentication send failed: {exc}", file=sys.stderr, flush=True)
        schedule_exit("application authentication send failed", delay=0)


def on_disconnected(c, reason):
    print("Disconnected:", reason, file=sys.stderr, flush=True)


def on_message(c, message):
    global timeout_call
    msg_type = message.payloadType
    msg_data = Protobuf.extract(message)

    if msg_type == 2101:
        print("\n=== APPLICATION AUTHENTICATED ===", flush=True)
        print(f"Requesting account list for alias {ACCOUNT_ALIAS}...", flush=True)

        if timeout_call is not None and timeout_call.active():
            timeout_call.cancel()
        timeout_call = reactor.callLater(90, timeout_check)

        try:
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = str(ACCESS_TOKEN).strip()

            print("DEBUG: sending ProtoOAGetAccountListByAccessTokenReq", file=sys.stderr, flush=True)

            deferred = c.send(req)
            deferred.addErrback(on_error)

        except Exception as exc:
            print(f"Account-list request failed: {exc}", file=sys.stderr, flush=True)
            schedule_exit("account-list request failed", delay=0)

    elif msg_type == 2142:
        ...

def timeout_check():
    print(
        "\nWARNING: Timed out after 30 seconds without receiving an account list.",
        file=sys.stderr,
        flush=True,
    )
    schedule_exit("timeout", delay=0)


client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)

print(f"Connecting to {HOST}:{PORT}...", file=sys.stderr, flush=True)
print("About to start service...", file=sys.stderr, flush=True)

timeout_call = reactor.callLater(30, timeout_check)

client.startService()

print("Service started, running reactor...", file=sys.stderr, flush=True)

reactor.run()
