from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq
from twisted.internet import reactor
import sys

print("Starting test_client.py...", file=sys.stderr, flush=True)

HOST = EndPoints.PROTOBUF_DEMO_HOST      # or LIVE host
PORT = EndPoints.PROTOBUF_PORT

CLIENT_ID = "8224_vqoFtBR1KoifAsUWHJeN7y3h3FiY1u3VLgFKcAUY8VZyhyC2gQ"
CLIENT_SECRET = "kikw1y9OP0ZDmQ1s4suRhhtD43YmAKUDyduF81DHNrBR4QjTzh"

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
    print("Message:", Protobuf.extract(message))

client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)

print(f"Connecting to {HOST}:{PORT}...", file=sys.stderr, flush=True)
print("About to start service...", file=sys.stderr, flush=True)

# Add timeout to stop reactor if no connection after 10 seconds
def timeout_check():
    print("WARNING: Connection timeout after 10s, stopping reactor", file=sys.stderr, flush=True)
    reactor.stop()

reactor.callLater(10, timeout_check)
client.startService()
print("Service started, running reactor...", file=sys.stderr, flush=True)
reactor.run()
