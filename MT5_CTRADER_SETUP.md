# MT4/MT5 to cTrader Copy Trading System Setup Guide

## Overview

This system enables automatic copy trading from MetaTrader to cTrader using:

- **MT4/MT5 CopyTrader EA**: Expert Advisor that monitors trades and sends HTTP/JSON trade events
- **main.py**: Current runtime entrypoint that initializes accounts, starts the Twisted reactor, and launches the HTTP server
- **bridge_server.py**: HTTP receiver that accepts trade events, normalizes payloads, de-duplicates events, and forwards them for processing
- **trade_processor.py**: Business-logic layer that handles OPEN, PENDING_OPEN, PENDING_CANCEL, MODIFY, and CLOSE events
- **trade_executor.py**: Executes the corresponding actions on follower cTrader accounts
- **cTrader Open API**: Official API used for authentication, symbol access, and order execution

## Architecture

```text
MT4/MT5 Terminal
    |
    | (JSON over HTTP)
    v
bridge_server.py
    |
    v
trade_processor.py
    |
    v
trade_executor.py
    |
    | (cTrader Open API)
    v
cTrader Account(s)
```

## Prerequisites

### 1. Software Requirements

- MetaTrader 4 or MetaTrader 5 terminal
- Python 3.11 recommended
- cTrader account with Open API access
- Git

### 2. cTrader Open API Setup

1. Go to https://openapi.ctrader.com/
2. Log in with your cTrader ID
3. Create a new application
4. Save your Client ID and Client Secret
5. Use the redirect URI required by the authentication helper in this repository

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

pip install -r requirements.txt
```

### Step 3: Configure Environment Variables

Copy the example file and edit it with your cTrader credentials:

```bash
cp .env.example .env
```

### Step 4: Configure Account Settings

Set up your account and symbol settings in the repository config files used by the current runtime, then confirm the enabled accounts load correctly when starting the bridge.

### Step 5: Install the CopyTrader EA

1. Copy the EA source file into your MT4 or MT5 Experts folder
2. Open MetaEditor
3. Compile the EA
4. In MetaTrader, go to **Tools > Options > Expert Advisors**
5. Enable **Allow WebRequest for listed URLs**
6. Add the exact bridge base URL you will use, for example:

```text
http://127.0.0.1:3140
```

### Step 6: Start the Bridge

Run the current entrypoint:

```bash
python main.py
```

The bridge host and port are provided to `bridge_server.py` by `main.py`.  
If you do not override them in config, the default startup values are:

```text
Host: 127.0.0.1
Port: 3140
```

### Step 7: Attach EA to Chart

1. Open any chart in MT4 or MT5
2. Attach the CopyTrader EA
3. Configure parameters:
   - **BridgeServerURL**: `http://127.0.0.1:3140`
   - **RequestTimeout**: `5000`
   - **MagicNumberFilter**: leave empty to copy all trades, or set a magic number filter
   - **CopyPendingOrders**: `true`
4. Click OK

## Usage

1. Start the bridge:
   ```bash
   python main.py
   ```
2. Attach the EA to a chart
3. Open, modify, or close trades in MetaTrader
4. The bridge receives the JSON event through `bridge_server.py`
5. The event is processed and copied to the configured cTrader follower account(s)

## Supported Event Flow

- New market positions
- Pending order opens
- Pending order cancels
- SL/TP modifications
- Full closes
- Partial closes

## Configuration Options

### EA Parameters

- **BridgeServerURL**: Base URL of the Python bridge, for example `http://127.0.0.1:3140`
- **RequestTimeout**: HTTP request timeout in milliseconds
- **MagicNumberFilter**: Filter by magic number
- **CopyPendingOrders**: Enable or disable pending-order copying

### Bridge Runtime

The current bridge runtime is controlled by:

- **main.py**: application startup
- **bridge_server.py**: HTTP receive, normalization, de-duplication
- **trade_processor.py**: routing and business logic
- Account/config files: credentials, account enablement, sizing, symbol rules, and risk settings

Do not use `mt5_bridge_server.py` as the main startup command in the current refactor path unless you intentionally maintain the old legacy flow.

## Troubleshooting

### MetaTrader WebRequest errors

**Problem**: `WebRequest error: 5200`  
- Cause: URL format is invalid or not whitelisted
- Fix: Use the exact same URL in both EA settings and MetaTrader WebRequest allow-list, for example `http://127.0.0.1:3140`

**Problem**: `WebRequest error: 5203`  
- Cause: Request failed because the bridge is unreachable
- Fix: Make sure `python main.py` is running and listening on the configured host and port

**Problem**: EA not sending signals  
- Fix: Confirm AutoTrading is enabled, the EA is attached correctly, and the bridge URL is allowed in MetaTrader

### Bridge issues

**Problem**: Bridge does not receive requests  
- Fix: Confirm startup is done through `python main.py` and verify the configured host/port values

**Problem**: Trade event reaches the bridge but logs `Unknown event type`  
- Fix: Confirm the payload is being normalized into canonical event names before processing

## File Structure

```text
OpenApiPy/
├── main.py
├── bridge_server.py
├── trade_processor.py
├── trade_executor.py
├── account_manager.py
├── config_loader.py
├── MT5_CTRADER_SETUP.md
├── .env.example
└── .env
```

## Notes

- `main.py` is the current startup entrypoint
- `bridge_server.py` is the active HTTP server
- `trade_processor.py` handles normalized canonical trade events
- `mt5_bridge_server.py` is a legacy file and is not the active runtime path in the current refactor branch
