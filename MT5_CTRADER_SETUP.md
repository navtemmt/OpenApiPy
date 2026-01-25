# MT5 to cTrader Copy Trading System Setup Guide

## Overview

This system enables automatic copy trading from MetaTrader 5 (MT5) to cTrader using:
- **MT5_CopyTrader.mq5**: MT5 Expert Advisor that monitors trades and sends signals
- **mt5_bridge_server.py**: Python bridge server that receives MT5 signals and executes on cTrader
- **ctrader_client.py**: Python wrapper for cTrader Open API
- **cTrader Open API**: Official cTrader API for trade execution

## Architecture

```
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
- Python 3.11 (recommended for compatibility)
- cTrader account with Open API access
- Git (for cloning repository)

### 2. cTrader Open API Setup

1. Go to https://openapi.ctrader.com/
2. Login with your cTrader ID
3. Create a new application:
   - Click "Create New App"
   - Set Redirect URI: `http://localhost:5000/callback`
   - Note down your Client ID and Client Secret
4. Wait for approval (usually instant for demo accounts)

## Installation Steps

### Step 1: Clone Repository

```bash
git clone https://github.com/navtemmt/OpenApiPy.git
cd OpenApiPy
```

### Step 2: Python Environment Setup

```bash
# Create virtual environment with Python 3.11
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install twisted protobuf python-dotenv flask
```

### Step 3: Configure Environment Variables

1. Copy the example file:
```bash
cp .env.example .env
```

2. Edit `.env` file with your cTrader credentials:
```
CLIENT_ID=your_client_id_here
CLIENT_SECRET=your_client_secret_here
ACCESS_TOKEN=your_access_token_here
```

**Important**: Never commit your `.env` file to version control!

### Step 4: Get Access Token

Run the test client to authenticate and get your access token:

```bash
python test_client.py
```

Follow the authorization flow and copy the access token to your `.env` file.

### Step 5: Install MT5 Expert Advisor

1. Copy `MT5_CopyTrader.mq5` to your MT5 data folder:
   - Open MT5 > File > Open Data Folder
   - Navigate to `MQL5/Experts/`
   - Paste the `MT5_CopyTrader.mq5` file

2. Compile the EA:
   - Open MT5 MetaEditor (F4 in MT5)
   - Open `MT5_CopyTrader.mq5`
   - Click Compile (F7)

3. Allow WebRequest URL:
   - MT5 > Tools > Options > Expert Advisors
   - Check "Allow WebRequest for listed URLs"
   - Add: `http://localhost:5000`
   - Click OK

### Step 6: Start the Bridge Server

```bash
python mt5_bridge_server.py
```

The server will start on http://localhost:5000 and automatically:
- Connect to cTrader Open API
- Listen for trade signals from MT5
- Execute trades on cTrader account

### Step 7: Attach EA to MT5 Chart

1. In MT5, open any chart
2. Drag `MT5_CopyTrader` EA from Navigator to the chart
3. Configure parameters:
   - **BridgeServerURL**: `http://localhost:5000` (default)
   - **RequestTimeout**: `5000` (default)
   - **MagicNumberFilter**: Leave empty to copy all trades, or specify magic number
   - **CopyPendingOrders**: `true` (default)
4. Click OK

## Usage

### Normal Operation

1. Start the bridge server: `python mt5_bridge_server.py`
2. Attach the EA to MT5 chart
3. Open/close/modify trades in MT5
4. Trades will automatically be copied to cTrader

### Monitoring

- **MT5 Logs**: Check the "Experts" tab in MT5 terminal
- **Bridge Server**: Watch console output for connection status and trade execution
- **cTrader**: Check positions in cTrader terminal

## Configuration Options

### MT5 EA Parameters

- **BridgeServerURL**: URL of the Python bridge server (default: http://localhost:5000)
- **RequestTimeout**: HTTP request timeout in milliseconds (default: 5000)
- **MagicNumberFilter**: Filter trades by magic number (empty = copy all trades)
- **CopyPendingOrders**: Enable/disable copying of pending orders

### Bridge Server Settings

Edit `mt5_bridge_server.py` to customize:
- Port number (default: 5000)
- Trade execution logic
- Error handling behavior
- Position size multiplier

## Troubleshooting

### MT5 EA Issues

**Problem**: "WebRequest error: 4060"
- **Solution**: Add `http://localhost:5000` to allowed URLs in MT5 settings

**Problem**: EA not sending signals
- **Solution**: Check if EA is running (smiley face icon in top-right of chart)
- Check "AutoTrading" is enabled (green button in MT5 toolbar)

### Bridge Server Issues

**Problem**: "Connection timeout"
- **Solution**: Check cTrader credentials in `.env` file
- Verify your cTrader app is approved
- Check internet connection

**Problem**: "Invalid access token"
- **Solution**: Re-run `test_client.py` to get fresh access token
- Update `.env` file with new token

### cTrader Issues

**Problem**: Trades not executing on cTrader
- **Solution**: Check account has sufficient margin
- Verify symbol names match between MT5 and cTrader
- Check if trading is allowed on cTrader account

## Security Notes

1. **Never share your `.env` file** - it contains sensitive credentials
2. **Revoke old credentials** if exposed in version control
3. **Use demo accounts** for testing before live trading
4. **Monitor trades closely** during initial setup
5. **Set appropriate risk limits** in your trading logic

## File Structure

```
OpenApiPy/
├── MT5_CopyTrader.mq5          # MT5 Expert Advisor
├── mt5_bridge_server.py        # Python bridge server
├── ctrader_client.py           # cTrader API wrapper
├── test_client.py              # Authentication test script
├── .env.example                # Environment variables template
├── .env                        # Your credentials (DO NOT COMMIT)
└── MT5_CTRADER_SETUP.md       # This file
```

## Advanced Features

### Position Size Multiplier

You can modify the bridge server to scale position sizes:

```python
# In mt5_bridge_server.py, modify execute_trade:
volume_multiplier = 0.5  # Copy at 50% of MT5 size
adjusted_volume = trade_data['volume'] * volume_multiplier
```

### Symbol Mapping

If symbol names differ between MT5 and cTrader:

```python
# In mt5_bridge_server.py
SYMBOL_MAP = {
    'XAUUSD': 'GOLD',
    'EURUSD': 'EURUSD',
    # Add more mappings
}
```

### Selective Copy Trading

Use magic numbers to copy only specific EAs:
- Set different magic numbers in your MT5 EAs
- Configure `MagicNumberFilter` in MT5_CopyTrader EA
- Only trades with matching magic will be copied

## Testing

### 1. Test Bridge Server Connection

```bash
python test_client.py
```

Should connect successfully and show account information.

### 2. Test MT5 Signal Sending

1. Start bridge server
2. Attach EA to MT5 chart
3. Open a small demo trade in MT5
4. Check bridge server console for received signal
5. Check cTrader for executed trade

### 3. Test Trade Synchronization

- Open position in MT5 → Should open in cTrader
- Modify SL/TP in MT5 → Should modify in cTrader
- Close position in MT5 → Should close in cTrader

## Support

For issues with:
- **OpenAPI Protocol**: Check official cTrader Open API docs
- **MT5 WebRequest**: Consult MetaQuotes MQL5 documentation
- **Python Dependencies**: Check package documentation

## Disclaimer

This software is for educational purposes. Trading involves risk. Always:
- Test on demo accounts first
- Monitor system closely
- Use appropriate risk management
- Understand the code before using it

## License

See LICENSE file for details.
