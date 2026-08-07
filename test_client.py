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

ACCOUNT_ALIAS = (os.getenv("TEST_ACCOUNT_ALIAS") or os.getenv("ACCOUNT_ALIAS") or "DEMO").strip().upper()


def _env(*names):
    for name in names:
        value = os.getenv(name)
        if value is not None:
            value = value.strip()
            if value != "":
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
        f"Expected ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_ID and ACCOUNT_{ACCOUNT_ALIAS}_CLIENT_SECRET in .env"
    )

client = Client(HOST, PORT, TcpProtocol)


def on_error(failure):
    print("Error:", failure)


def on_connected(c):
    print("Connected")
    req = ProtoOAApplicationAuthReq()
    req.clientId = str(CLIENT_ID)
    req.clientSecret = str(CLIENT_SECRET)
    d = c.send(req)
    d.addErrback(on_error)


def on_disconnected(c, reason):
    print("Disconnected:", reason)


def on_message(c, message):
    global timeout_call
    print("Message:", Protobuf.extract(message))
    msg_type = message.payloadType

    if timeout_call is not None and timeout_call.active():
        timeout_call.cancel()
        timeout_call = None

    if msg_type == 2101:
        print("\n=== Application authenticated successfully ===")
        print(f"Requesting account list for alias {ACCOUNT_ALIAS}...")

        if not ACCESS_TOKEN:
            print("\n=== ERROR: ACCESS_TOKEN not found ===")
            print(
                f"Add ACCOUNT_{ACCOUNT_ALIAS}_ACCESS_TOKEN to your .env file "
                f"or set a fallback ACCESS_TOKEN."
            )
            reactor.stop()
            return

        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = str(ACCESS_TOKEN).strip()
        d = c.send(req)
        d.addErrback(on_error)

    elif msg_type == 2142:
        print("\n========== AVAILABLE ACCOUNTS ==========")
        msg_data = Protobuf.extract(message)
        matched = False

        if hasattr(msg_data, "ctidTraderAccount"):
            for account in msg_data.ctidTraderAccount:
                acct_id = getattr(account, "ctidTraderAccountId", None)
                is_live = getattr(account, "isLive", None)
                trader_login = getattr(account, "traderLogin", None)
                broker_name = getattr(account, "brokerName", None)
                balance = getattr(account, "balance", None)

                print(f"\nAccount ID: {acct_id}")
                print(f"  Account Type: {'LIVE' if is_live else 'DEMO'}")
                if trader_login is not None:
                    print(f"  Trader Login: {trader_login}")
                if broker_name is not None:
                    print(f"  Broker: {broker_name}")
                if balance is not None:
                    print(f"  Balance: {balance / 100:.2f}")

                if ACCOUNT_ID and str(acct_id) == str(ACCOUNT_ID):
                    matched = True
                    print("  >>> MATCHES ACCOUNT_ID IN .env")

        print("\n========================================")
        if ACCOUNT_ID:
            if matched:
                print(f"Configured ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID matches one returned account.")
            else:
                print(f"Configured ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID={ACCOUNT_ID} was NOT found in returned accounts.")
        else:
            print(
                f"Set ACCOUNT_{ACCOUNT_ALIAS}_ACCOUNT_ID=<one of the IDs above> in your .env file."
            )

        print("\nConnection test successful! Stopping...\n")
        reactor.stop()


client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)

print(f"Connecting to {HOST}:{PORT}...", file=sys.stderr, flush=True)
print("About to start service...", file=sys.stderr, flush=True)


def timeout_check():
    print("WARNING: Connection timeout after 30s, stopping reactor", file=sys.stderr, flush=True)
    reactor.stop()


timeout_call = reactor.callLater(30, timeout_check)
client.startService()
print("Service started, running reactor...", file=sys.stderr, flush=True)
reactor.run()
