# MT4/MT5 to cTrader Copy Trading System Setup Guide

## Overview

This system enables automatic copy trading from MetaTrader to cTrader using:

- **MT4/MT5 CopyTrader EA**: Expert Advisor that monitors trades and sends HTTP/JSON trade events.
- **main.py**: Current runtime entrypoint that initializes accounts, starts the Twisted reactor, and launches the HTTP server.
- **bridge_server.py**: HTTP receiver that accepts trade events, normalizes payloads, de-duplicates events, and forwards them for processing.
- **trade_processor.py**: Business-logic layer that handles OPEN, PENDING_OPEN, PENDING_CANCEL, MODIFY, and CLOSE events.
- **trade_executor.py**: Executes the corresponding actions on follower cTrader accounts.
- **cTrader Open API**: Official API used for authentication, account discovery, symbol access, and order execution. [1]

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

- MetaTrader 4 or MetaTrader 5 terminal.
- Python 3.11 recommended.
- cTrader account with Open API access.
- Git.

### 2. cTrader Open API Setup

1. Go to [cTrader Open API](https://openapi.ctrader.com/) and sign in with your cTrader ID. [1]
2. Create a new Open API application and save the **Client ID** and **Client Secret**. [1]
3. Add the redirect URI required by the authentication helper used in this repository. [1]
4. Make sure the app has the permissions you need, typically trading access if the bridge will place and manage orders. [1]

## Installation Steps

### Step 1: Clone Repository

```bash
git clone https://github.com/navtemmt/OpenApiPy.git
cd OpenApiPy
```

### Step 2: Install Python Dependencies

If you do **not** use a virtual environment, install the requirements directly into your current Python environment:

```bash
pip install -r requirements.txt
```

If you prefer to use a virtual environment, that is optional, not required.

### Step 3: Configure Environment Variables

Copy the example file and edit it with your cTrader credentials:

```bash
cp .env.example .env
```

Use the alias-based format expected by the current `test_client.py` and refactor runtime:

```ini
# Default alias used by test_client.py if TEST_ACCOUNT_ALIAS is not set
ACCOUNT_ALIAS=DEMO

ACCOUNT_DEMO_CLIENT_ID=your_openapi_client_id
ACCOUNT_DEMO_CLIENT_SECRET=your_openapi_client_secret
ACCOUNT_DEMO_ACCESS_TOKEN=your_access_token
ACCOUNT_DEMO_REFRESH_TOKEN=your_refresh_token
ACCOUNT_DEMO_ACCOUNT_ID=your_ctid_trader_account_id
```

For additional accounts, use the same pattern with another alias:

```ini
ACCOUNT_LIVE_CLIENT_ID=...
ACCOUNT_LIVE_CLIENT_SECRET=...
ACCOUNT_LIVE_ACCESS_TOKEN=...
ACCOUNT_LIVE_REFRESH_TOKEN=...
ACCOUNT_LIVE_ACCOUNT_ID=...
```

Use the numeric **cTrader trading account ID** as `ACCOUNT_<ALIAS>_ACCOUNT_ID`, not only the cTrader ID login. Open API account authorization is performed against `ctidTraderAccountId`. [1]

Never commit `.env`, access tokens, refresh tokens, or client secrets.

### Step 4: Find Your cTrader Account ID

If you do not know the numeric cTrader trading account ID required by `.env` or `accounts_config.ini`, run the repository test client first:

```bash
python test_client.py
```

The current `test_client.py` flow is:

1. Connect to the cTrader Open API endpoint.
2. Send `ProtoOAApplicationAuthReq`.
3. Send `ProtoOAGetAccountListByAccessTokenReq` using the configured access token.
4. Print the authorized accounts returned by cTrader. [1]

Important details from the current working behavior:

- The account-list response is handled as `payloadType=2150` in this repository's current flow.
- `payloadType=51` is heartbeat traffic and can be ignored.
- The value you need for `.env` is **`ctidTraderAccountId`**.
- `traderLogin` is a broker login reference and is **not** the same thing as `ctidTraderAccountId`.

Example output fields:

```text
Account ID: 46020977
  Account Type: DEMO
  Trader Login: 5747047
```

For a demo follower account, choose one where `Account Type: DEMO` or `isLive: false`. Then place that numeric ID into your `.env` file:

```ini
ACCOUNT_DEMO_ACCOUNT_ID=46020977
```

Do not leave `your_account_id_here` in a real runtime configuration.

### Step 5: Configure Account Settings

Set up your account and symbol settings in the repository config files used by the current runtime, then confirm the enabled accounts load correctly when starting the bridge.

Typical places to review:

- `.env`
- `accounts_config.ini`
- Any per-account symbol prefix, suffix, or custom symbol mapping settings used by your runtime.

### Step 6: Install the CopyTrader EA

1. Copy the EA source file into your MT4 or MT5 `Experts` folder.
2. Open MetaEditor.
3. Compile the EA.
4. In MetaTrader, go to **Tools > Options > Expert Advisors**.
5. Enable **Allow WebRequest for listed URLs**.
6. Add the exact bridge base URL you will use, for example:

```text
http://127.0.0.1:3140
```

### Step 7: Start the Bridge

Run the current entrypoint:

```bash
python main.py
```

The bridge host and port are provided to `bridge_server.py` by `main.py`. If you do not override them in config, the default startup values are:

```text
Host: 127.0.0.1
Port: 3140
```

### Step 8: Attach EA to Chart

1. Open any chart in MT4 or MT5.
2. Attach the CopyTrader EA.
3. Configure parameters:
   - **BridgeServerURL**: `http://127.0.0.1:3140`
   - **RequestTimeout**: `5000`
   - **MagicNumberFilter**: leave empty to copy all trades, or set a magic number filter.
   - **CopyPendingOrders**: `true`
4. Click **OK**.

## Usage

1. Start the bridge:

   ```bash
   python main.py
   ```

2. Attach the EA to a chart.
3. Open, modify, or close trades in MetaTrader.
4. The bridge receives the JSON event through `bridge_server.py`.
5. The event is processed and copied to the configured cTrader follower account(s).

## Supported Event Flow

- New market positions.
- Pending order opens.
- Pending order cancels.
- SL/TP modifications.
- Full closes.
- Partial closes.

## Configuration Options

### EA Parameters

- **BridgeServerURL**: Base URL of the Python bridge, for example `http://127.0.0.1:3140`.
- **RequestTimeout**: HTTP request timeout in milliseconds.
- **MagicNumberFilter**: Filter by magic number.
- **CopyPendingOrders**: Enable or disable pending-order copying.

### Bridge Runtime

The current bridge runtime is controlled by:

- **main.py**: application startup.
- **bridge_server.py**: HTTP receive, normalization, de-duplication.
- **trade_processor.py**: routing and business logic.
- Account/config files: credentials, account enablement, sizing, symbol rules, and risk settings.

Do not use `mt5_bridge_server.py` as the main startup command in the current refactor path unless you intentionally maintain the old legacy flow. [1]

## Demo vs Live Endpoints

Keep the environment and account type aligned:

- Demo endpoint: `demo.ctraderapi.com:5035`
- Live endpoint: `live.ctraderapi.com:5035`

Do not use a live account through the demo endpoint, or a demo account through the live endpoint. The account list returned by `test_client.py` clearly shows whether an account is live or demo. [1]

## Troubleshooting

### MetaTrader WebRequest errors

**Problem**: `WebRequest error: 5200`  
- Cause: URL format is invalid or not whitelisted.  
- Fix: Use the exact same URL in both EA settings and MetaTrader WebRequest allow-list, for example `http://127.0.0.1:3140`.

**Problem**: `WebRequest error: 5203`  
- Cause: Request failed because the bridge is unreachable.  
- Fix: Make sure `python main.py` is running and listening on the configured host and port.

**Problem**: EA not sending signals  
- Fix: Confirm AutoTrading is enabled, the EA is attached correctly, and the bridge URL is allowed in MetaTrader.

### cTrader authentication issues

**Problem**: App authentication succeeds, but account operations fail.  
- Fix: Confirm you are using the correct numeric trading account ID, not only your cTrader ID login, because account authorization uses `ctidTraderAccountId`. [1]

**Problem**: You are unsure which account ID to use in config.  
- Fix: Run `python test_client.py` and use the returned account list to identify the correct account. Use `ctidTraderAccountId`, not `traderLogin`. [1]

**Problem**: `test_client.py` prints accounts but still waits and times out.  
- Cause: The script is handling the wrong account-list payload type.  
- Fix: Make sure the current file treats `payloadType=2150` as the account-list response and ignores `payloadType=51` heartbeat traffic.

**Problem**: `test_client.py` says `Connected` and `APPLICATION AUTHENTICATED` but still times out waiting for account list.  
- Fix: Verify the access token belongs to the same Open API application as the configured Client ID and Client Secret, then regenerate the token if needed. [1]

### Bridge issues

**Problem**: Bridge does not receive requests.  
- Fix: Confirm startup is done through `python main.py` and verify the configured host/port values.

**Problem**: Trade event reaches the bridge but logs `Unknown event type`.  
- Fix: Confirm the payload is being normalized into canonical event names before processing.

## File Structure

```text
OpenApiPy/
├── main.py
├── bridge_server.py
├── trade_processor.py
├── trade_executor.py
├── account_manager.py
├── config_loader.py
├── test_client.py
├── MT5_CTRADER_SETUP.md
├── .env.example
└── .env
```

## Notes

- `main.py` is the current startup entrypoint. [1]
- `bridge_server.py` is the active HTTP server. [1]
- `trade_processor.py` handles normalized canonical trade events. [1]
- `test_client.py` is useful for validating Open API connectivity and discovering the numeric trading account ID needed for local configuration. [1]
- `mt5_bridge_server.py` is a legacy file and is not the active runtime path in the current refactor branch. [1]
- If you are not using a virtual environment, install dependencies into your active Python installation and make sure `python` and `pip` refer to the same interpreter.
- Do not paste live access tokens or client secrets into screenshots, commit history, issues, or chat logs.
