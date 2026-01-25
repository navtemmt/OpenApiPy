# OpenApiPy


[![PyPI version](https://badge.fury.io/py/ctrader-open-api.svg)](https://badge.fury.io/py/ctrader-open-api)
![versions](https://img.shields.io/pypi/pyversions/ctrader-open-api.svg)
[![GitHub license](https://img.shields.io/github/license/spotware/OpenApiPy.svg)](https://github.com/spotware/OpenApiPy/blob/main/LICENSE)

A Python package for interacting with cTrader Open API.

This package uses Twisted and it works asynchronously.

- Free software: MIT
- Documentation: https://spotware.github.io/OpenApiPy/.


## Features

* Works asynchronously by using Twisted

* Methods return Twisted deferreds

* It contains the Open API messages files so you don't have to do the compilation

* Makes handling request responses easy by using Twisted deferreds

## Insallation

```
pip install ctrader-open-api
```

# Usage

```python

from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor

hostType = input("Host (Live/Demo): ")
host = EndPoints.PROTOBUF_LIVE_HOST if hostType.lower() == "live" else EndPoints.PROTOBUF_DEMO_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

def onError(failure): # Call back for errors
    print("Message Error: ", failure)

def connected(client): # Callback for client connection
    print("\nConnected")
    # Now we send a ProtoOAApplicationAuthReq
    request = ProtoOAApplicationAuthReq()
    request.clientId = "Your application Client ID"
    request.clientSecret = "Your application Client secret"
    # Client send method returns a Twisted deferred
    deferred = client.send(request)
    # You can use the returned Twisted deferred to attach callbacks
    # for getting message response or error backs for getting error if something went wrong
    # deferred.addCallbacks(onProtoOAApplicationAuthRes, onError)
    deferred.addErrback(onError)

def disconnected(client, reason): # Callback for client disconnection
    print("\nDisconnected: ", reason)

def onMessageReceived(client, message): # Callback for receiving all messages
    print("Message received: \n", Protobuf.extract(message))

# Setting optional client callbacks
client.setConnectedCallback(connected)
client.setDisconnectedCallback(disconnected)
client.setMessageReceivedCallback(onMessageReceived)
# Starting the client service
client.startService()
# Run Twisted reactor
reactor.run()

```

Please check documentation or samples for a complete example.

## Dependencies

* <a href="https://pypi.org/project/twisted/">Twisted</a>
* <a href="https://pypi.org/project/protobuf/">Protobuf</a>


---

## MT5 to cTrader Copy Trading System

This repository now includes a complete copy trading solution that automatically copies trades from MetaTrader 5 to cTrader.

### Quick Start

See [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md) for complete setup instructions.

### System Components

1. **MT5_CopyTrader.mq5** - MQL5 Expert Advisor for MT5
2. **mt5_bridge_server.py** - Python bridge server (Flask + OpenAPI)
3. **ctrader_client.py** - Simplified cTrader API wrapper
4. **test_client.py** - Authentication and connection testing

### Architecture

```
MT5 Terminal → HTTP/JSON → Python Bridge → cTrader OpenAPI → cTrader Account
```

### Installation

```bash
# Clone and setup
git clone https://github.com/navtemmt/OpenApiPy.git
cd OpenApiPy

# Install Python dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your cTrader API credentials

# Run bridge server
python mt5_bridge_server.py

# Attach MT5_CopyTrader.mq5 to any MT5 chart
```

### Features

- Real-time trade copying from MT5 to cTrader
- Automatic synchronization of:
  - New positions (BUY/SELL)
  - Position modifications (SL/TP changes)
  - Position closures
- Magic number filtering for selective copying
- Configurable position size multiplier
- Symbol name mapping support
- Comprehensive error handling and logging

### Security

- Credentials stored in `.env` file (not committed to repo)
- OAuth2 authentication with cTrader
- Secure token management

For detailed setup instructions, troubleshooting, and advanced configuration, see [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md).
