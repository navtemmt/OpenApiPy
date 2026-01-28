from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq, ProtoOAGetAccountListByAccessTokenReq
from twisted.internet import reactor
import sys
import os
from dotenv import load_dotenv

print("Starting test_client.py...", file=sys.stderr, flush=True)

# Load environment variables from .env file
load_dotenv()

HOST = EndPoints.PROTOBUF_DEMO_HOST      # or LIVE host
PORT = EndPoints.PROTOBUF_PORT

CLIENT_ID = os.getenv("CTRADER_CLIENT_ID")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")  # Required to get account list
client = Client(HOST, PORT, TcpProtocol)

def on_error(failure):
    print("Error:", failure)

def on_connected(c):
    print("Connected")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    d = c.send(req)
    d.addErrback(on_error)

def on_disconnected(c, reason):
    print("Disconnected:", reason)

def on_message(c, message):
    global timeout_call
    print("Message:", Protobuf.extract(message))
        
    # Check message type
    msg_type = message.payloadType
    
    # After successful app authentication, request account list
    if msg_type == 2101:  # ProtoOAApplicationAuthRes
        print("\n=== Application authenticated successfully ===")
        print("Requesting account list...")
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = ACCESS_TOKEN
        d = c.send(req)
        d.addErrback(on_error)
    
    # Display account list
    elif msg_type == 2142:  # ProtoOAGetAccountListByAccessTokenRes
        print("\n========== AVAILABLE ACCOUNTS ==========")
        msg_data = Protobuf.extract(message)
        if hasattr(msg_data, 'ctidTraderAccount'):
            for account in msg_data.ctidTraderAccount:
                print(f"\nAccount ID: {account.ctidTraderAccountId}")
                print(f"  Account Type: {'DEMO' if account.isLive == False else 'LIVE'}")
                if hasattr(account, 'traderLogin'):
                    print(f"  Trader Login: {account.traderLogin}")
                if hasattr(account, 'brokerName'):
                    print(f"  Broker: {account.brokerName}")
                print(f"  Balance: {account.balance / 100:.2f}")  # Balance in cents
        print("\n========================================")
        print("\nTo use a specific account, add this to your .env file:")
        print("CTRADER_ACCOUNT_ID=<your_account_id>")
        print("\nConnection test successful! Stopping...\n")
        reactor.stop()
        # Cancel timeout on first message

client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)

print(f"Connecting to {HOST}:{PORT}...", file=sys.stderr, flush=True)
print("About to start service...", file=sys.stderr, flush=True)

# Add timeout to stop reactor if no connection after 10 seconds
def timeout_check():
    print("WARNING: Connection timeout after 10s, stopping reactor", file=sys.stderr, flush=True)
    reactor.stop()

timeout_call = None
timeout_call = reactor.callLater(30, timeout_check)
client.startService()
print("Service started, running reactor...", file=sys.stderr, flush=True)
reactor.run()
