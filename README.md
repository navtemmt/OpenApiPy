---

## MT5 to cTrader Copy Trading System

This repository includes a copy trading bridge that forwards master trade events from MetaTrader to cTrader accounts through the cTrader Open API. The current runtime uses `main.py` as the entrypoint and `bridge_server.py` as the HTTP receiver. Legacy file `mt5_bridge_server.py` is no longer the active server path.

### Quick Start

See [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md) for full setup and account configuration details.

### Runtime Components

1. **MT4/MT5 CopyTrader EA** - Sends trade events by HTTP/JSON
2. **main.py** - Starts the Twisted reactor, loads accounts, and launches the HTTP server
3. **bridge_server.py** - Receives HTTP trade events, normalizes payloads, de-duplicates events, and forwards them for processing
4. **trade_processor.py** - Applies business logic for OPEN, PENDING_OPEN, PENDING_CANCEL, MODIFY, and CLOSE events
5. **trade_executor.py** - Sends the corresponding actions to cTrader accounts
6. **account_manager.py** - Tracks account clients, mappings, and runtime state

### Architecture

```text
MT4/MT5 Terminal
    -> HTTP/JSON
    -> bridge_server.py
    -> trade_processor.py
    -> trade_executor.py
    -> cTrader Open API
    -> cTrader Account(s)
```

### Installation

```bash
git clone https://github.com/navtemmt/OpenApiPy.git
cd OpenApiPy

pip install -r requirements.txt

cp .env.example .env
# Edit .env with your cTrader API credentials and account values
```

### Run the bridge

```bash
python main.py
```

### Platform side

- Attach your MT4 or MT5 CopyTrader EA to a chart.
- Point the EA HTTP endpoint to the configured bridge host and port, commonly `http://127.0.0.1:3140`.
- Make sure the same host and port are allowed in the terminal WebRequest settings.

### Supported event flow

- New market positions
- Pending order opens
- Pending order cancels
- SL/TP modifications
- Full and partial closes

### Notes

- `bridge_server.py` is the active HTTP server in the current refactor branch.
- `mt5_bridge_server.py` is a legacy file and should not be used as the main startup command unless you intentionally maintain the old path.
- Incoming MT4/MT5 payload differences should be normalized before processing so downstream logic works with one canonical schema.

### Security

- Credentials are loaded from `.env`
- cTrader authentication uses OAuth/Open API credentials
- Sensitive secrets should never be committed to the repository

For detailed setup, troubleshooting, and account configuration, see [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md).
