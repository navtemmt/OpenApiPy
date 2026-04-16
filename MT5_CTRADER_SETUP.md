# MT5 to cTrader Copy Trading System Setup Guide

## Overview

This system enables automatic copy trading from MetaTrader to cTrader using:
- **MT5_CopyTrader.mq5**: MetaTrader 5 Expert Advisor that monitors trades and sends signals
- **mt5_bridge_server.py**: Python bridge server that receives trade signals and executes them on cTrader
- **ctrader_client.py**: Python wrapper for cTrader Open API
- **cTrader Open API**: Official cTrader API for trade execution

## Architecture

```text
MT5 Terminal
    |
    | (JSON over HTTP)
    v
Python Bridge Server (mt5_bridge_server.py)
    |
    | (cTrader OpenAPI Protocol)
    v
cTrader Account
```

## Prerequisites

### 1. Software Requirements
- MetaTrader 5 terminal
- Python 3.11 (recommended)
- cTrader account with Open API access
- Git

### 2. cTrader Open API Setup

1. Go to https://openapi.ctrader.com/
2. Log in with your cTrader ID
3. Create a new application
4. Note your Client ID and Client Secret
5. Use the redirect URI required by your authentication helper

## Installation Steps

### Step 1: Clone Repository

```bash
git clone https://github.com/navtemmt/OpenApiPy.git
cd OpenApiPy
```

### Step 2: Python Environment Setup

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

python -m pip install requests pyOpenSSL "Twisted==21.7.0" protobuf python-dotenv flask
```

### Step 3: Configure Environment Variables

1. Copy the example file:

```bash
cp .env.example .env
```

2. Edit `.env` with your cTrader credentials.

### Step 4: Get Access Token

Run the authentication helper you use in this repo, then save the resulting token in `.env`.

### Step 5: Install MT5 Expert Advisor

1. Copy `MT5_CopyTrader.mq5` to your MT5 data folder
2. Open MetaEditor
3. Compile the EA
4. In MT5, go to **Tools > Options > Expert Advisors**
5. Enable **Allow WebRequest for listed URLs**
6. Add:

```text
http://127.0.0.1
```

### Step 6: Start the Bridge Server

```bash
python mt5_bridge_server.py
```

The bridge should listen on:

```text
http://127.0.0.1
```

If you changed the server code to use port 80 explicitly, the startup log should show:

```text
MT5 Bridge Server listening on 127.0.0.1:80
```

### Step 7: Attach EA to MT5 Chart

1. Open any chart in MT5
2. Drag `MT5_CopyTrader` from Navigator onto the chart
3. Configure parameters:
   - **BridgeServerURL**: `http://127.0.0.1`
   - **RequestTimeout**: `5000`
   - **MagicNumberFilter**: leave empty to copy all trades, or set a magic number
   - **CopyPendingOrders**: `true`
4. Click OK

## Usage

1. Start the bridge server:
   ```bash
   python mt5_bridge_server.py
   ```
2. Attach the EA to an MT5 chart
3. Open, modify, or close trades in MT5
4. The bridge will receive them at `/trade_signal`

## Configuration Options

### MT5 EA Parameters

- **BridgeServerURL**: Base URL of the Python bridge server, for example `http://127.0.0.1`
- **RequestTimeout**: HTTP request timeout in milliseconds
- **MagicNumberFilter**: Filter trades by magic number
- **CopyPendingOrders**: Enable or disable pending-order copying

### Bridge Server Settings

Edit `mt5_bridge_server.py` to customize:
- Host
- Port
- Trade execution logic
- Error handling behavior
- Position sizing rules

## Troubleshooting

### MT5 EA Issues

**Problem**: `WebRequest error: 5200`
- **Cause**: Invalid address format for the URL used by MetaTrader
- **Fix**: Use `http://127.0.0.1` when the bridge is listening on port 80

**Problem**: `WebRequest error: 5203`
- **Cause**: Request failed even though the URL format is valid
- **Fix**: Make sure the bridge is actually running and listening on `127.0.0.1:80`

**Problem**: EA not sending signals
- **Fix**: Confirm AutoTrading is enabled and the EA is attached successfully

### Bridge Server Issues

**Problem**: Bridge does not receive requests
- **Fix**: Confirm the bridge startup log shows:
  `MT5 Bridge Server listening on 127.0.0.1:80`

**Problem**: Trade event reaches the bridge but logs `Unknown event type`
- **Fix**: Update the bridge to handle both `action` and `event_type` payload fields

## File Structure

```text
OpenApiPy/
├── MQL5/
│   └── MT5_CopyTrader.mq5
├── mt5_bridge_server.py
├── ctrader_client.py
├── test_client.py
├── .env.example
├── .env
└── MT5_CTRADER_SETUP.md
```
