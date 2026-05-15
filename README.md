Notes: risk percent not working because did not get cache balance, does not remap existing order when startup

---

## MT4/MT5 to cTrader Copy Trading System

This repository includes a copy trading bridge that forwards master trade events from MetaTrader to cTrader accounts through the cTrader Open API. The current runtime uses `main.py` as the entrypoint and `bridge_server.py` as the HTTP receiver. Legacy file `mt5_bridge_server.py` is no longer the active server path.

### Quick Start

See [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md) for full setup and account configuration details.

### Runtime Components

1. **MT4/MT5 CopyTrader EA** - Sends trade events by HTTP/JSON
2. **main.py** - Starts the Twisted reactor, loads accounts, and launches the HTTP listener(s)
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

### Port and URL setup

The bridge can be configured to listen on both port `80` and port `3140` at the same time.

Recommended local setup:

- **MT4 EA base URL**: `http://127.0.0.1`
- **MT4 allowed WebRequest URL**: `http://127.0.0.1`
- **MT5 EA base URL**: `http://127.0.0.1:3140`
- **MT5 allowed WebRequest URL**: `http://127.0.0.1:3140`

Reason:
- MT4 `WebRequest()` is commonly used with protocol-default ports, so `http://127.0.0.1` is the safer MT4 setting.
- MT5 may continue using `http://127.0.0.1:3140` if that path is already working in your environment.

Important:
- Set only the **base URL** in the EA input.
- Do **not** append `/trade_signal` manually in the EA settings, because the EA code appends that path automatically.

### Platform side

- Attach your MT4 or MT5 CopyTrader EA to a chart.
- Point each EA to the correct base URL for its platform.
- Make sure the matching base URL is added to the terminal WebRequest allow-list.
- If you run only one listener, make sure both platforms use a URL that matches the actual bound port.

### Supported event flow

- New market positions
- Pending order opens
- Pending order cancels
- SL/TP modifications
- Full and partial closes

### Notes

- `bridge_server.py` is the active HTTP server in the current refactor branch.
- `main.py` should be the startup entrypoint.
- `mt5_bridge_server.py` is a legacy file and should not be used as the main startup command unless you intentionally maintain the old path.
- Incoming MT4/MT5 payload differences should be normalized before processing so downstream logic works with one canonical schema.
- If you enable dual listeners, MT4 can use port `80` while MT5 continues using port `3140`.

### Security

- Credentials are loaded from `.env`
- cTrader authentication uses OAuth/Open API credentials
- Sensitive secrets should never be committed to the repository

For detailed setup, troubleshooting, and account configuration, see [MT5_CTRADER_SETUP.md](MT5_CTRADER_SETUP.md).
