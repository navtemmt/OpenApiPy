from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq
from twisted.internet import reactor

HOST = EndPoints.PROTOBUF_DEMO_HOST      # or LIVE host
PORT = EndPoints.PROTOBUF_PORT

CLIENT_ID = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"

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

client.startService()
reactor.run()
