"""MT5 to cTrader Copy Trading Bridge Server - Multi-Account Version

Receives trade events from MT5 EA via JSON and forwards to multiple cTrader accounts.
"""
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from twisted.internet import reactor

from config_loader import get_multi_account_config
from account_manager import get_account_manager
from symbol_mapper import SymbolMapper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MT5BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MT5 trade events."""

    # Class-level reference to account manager
    account_manager = None

    def log_message(self, format, *args):
        """Override to use Python logging instead of printing."""
        logger.info(f"{self.address_string()} - {format%args}")

    def do_POST(self):
        """Handle POST request with trade event JSON."""
        try:
            # Read request body
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)

            # Parse JSON
            trade_event = json.loads(post_data.decode('utf-8'))
            logger.info(f"Received trade event: {trade_event}")

            # Process the trade event
            self._process_trade_event(trade_event)

            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = json.dumps({"status": "success", "message": "Trade event received"})
            self.wfile.write(response.encode('utf-8'))

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            logger.error(f"Error processing request: {e}", exc_info=True)
            self.send_error(500, str(e))

    def do_GET(self):
        """Handle GET request (health check)."""
        accounts = self.account_manager.get_all_accounts() if self.account_manager else {}
        account_status = {
            name: {
                "account_id": config.account_id,
                "enabled": config.enabled,
                "connected": client.is_app_authed if client else False,
                "daily_trades": config.daily_trade_count,
                "current_positions": config.current_positions
            }
            for name, (client, config) in accounts.items()
        }

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = json.dumps({
            "status": "online",
            "service": "MT5 to cTrader Bridge (Multi-Account)",
            "version": "2.0.0",
            "accounts": account_status
        }, indent=2)
        self.wfile.write(response.encode('utf-8'))

    def _process_trade_event(self, event):
        """Process trade event and forward to all enabled cTrader accounts."""
        if not self.account_manager:
            logger.error("Account manager not initialized")
            return

        # Get action from either 'action' or 'event' field, normalize to lowercase
        event_type = event.get('action', event.get('event'))
        if event_type:
            event_type = event_type.lower()

        if event_type == 'open':
            self._handle_open(event)
        elif event_type == 'modify':
            self._handle_modify(event)
        elif event_type == 'close':
            self._handle_close(event)
        else:
            logger.error(f"Unknown event type: {event_type}")

    def _handle_open(self, event):
        """Handle new order event - copy to all enabled accounts."""
        ticket = event.get('ticket')
        mt5_symbol = event.get('symbol')
        side = event.get('type', event.get('side', 'BUY')).lower()
        volume = event.get('volume', event.get('lots', 0.01))
        sl = event.get('sl', 0.0)
        tp = event.get('tp', 0.0)
        magic = event.get('magic', 0)

        logger.info(f"Opening {side} order: {volume} lots of {mt5_symbol} (ticket #{ticket}, magic {magic})")

        accounts = self.account_manager.get_all_accounts()

        for account_name, (client, config) in accounts.items():
            try:
                self._copy_open_to_account(
                    account_name, client, config,
                    ticket, mt5_symbol, side, volume, sl, tp, magic
                )
            except Exception as e:
                logger.error(f"Failed to copy trade to {account_name}: {e}", exc_info=True)

    def _copy_open_to_account(self, account_name, client, config, ticket, mt5_symbol, side, volume, sl, tp, magic):
        """Copy open order to a specific account."""
        if not client or not client.is_app_authed:
            logger.warning(f"[{account_name}] Client not ready, skipping")
            return

        multi_config = get_multi_account_config()

        should_copy, reason = multi_config.should_copy_trade(config, mt5_symbol, magic, volume)
        if not should_copy:
            logger.info(f"[{account_name}] Skipping: {reason}")
            return

        mapper = SymbolMapper(
            prefix=config.symbol_prefix,
            suffix=config.symbol_suffix,
            custom_map=config.custom_symbols
        )

        # Get cTrader symbol ID
        symbol_id = mapper.get_symbol_id(mt5_symbol)
        if symbol_id is None:
            logger.error(f"[{account_name}] Unknown symbol {mt5_symbol}, skipping")
            return

        # Apply account-specific lot multiplier and limits (still in MT5 lots)
        adjusted_lots = config.lot_multiplier * volume
        adjusted_lots = max(config.min_lot_size, min(adjusted_lots, config.max_lot_size))

        # IMPORTANT FIX:
        # cTrader Open API expects volume in "units". For FX, the common convention is:
        # 1.0 lot = 100,000 units, so 0.01 lot = 1,000 units.
        volume_units = int(round(adjusted_lots * 100000))

        # Enforce minimum volume from cTrader error message (1000 units)
        # (This avoids TRADING_BAD_VOLUME for small trades.)
        MIN_UNITS = 1000
        if volume_units < MIN_UNITS:
            logger.warning(
                f"[{account_name}] Volume {volume_units} below minimum {MIN_UNITS}, "
                f"adjusting to {MIN_UNITS} units"
            )
            volume_units = MIN_UNITS

        # Check if SL/TP should be copied
        final_sl = sl if (sl > 0 and config.copy_sl) else None
        final_tp = tp if (tp > 0 and config.copy_tp) else None

        logger.info(
            f"[{account_name}] Sending: symbol_id={symbol_id}, side={side}, "
            f"volume={volume_units} units (from {volume} lots * {config.lot_multiplier})"
        )

        client.send_market_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=side,
            volume=volume_units,
            sl=final_sl,
            tp=final_tp,
            label=f"MT5_{ticket}"
        )

        config.daily_trade_count += 1
        config.current_positions += 1

        logger.info(
            f"✓ [{account_name}] Order sent successfully "
            f"(daily: {config.daily_trade_count}/{config.max_daily_trades})"
        )

    def _handle_modify(self, event):
        """Handle position modification event."""
        ticket = event.get('ticket')
        sl = event.get('sl', 0.0)
        tp = event.get('tp', 0.0)

        logger.info(f"Modifying position {ticket}: SL={sl}, TP={tp}")
        logger.warning("Position modification not yet implemented for multi-account")

    def _handle_close(self, event):
        """Handle position close event."""
        ticket = event.get('ticket')
        volume = event.get('volume', event.get('lots', 0))

        logger.info(f"Closing position {ticket}: {volume} lots")
        logger.warning("Position close not yet implemented for multi-account")


def run_http_server(host='127.0.0.1', port=3140):
    """Run HTTP server in a separate thread."""
    server = HTTPServer((host, port), MT5BridgeHandler)
    logger.info(f"MT5 Bridge Server listening on {host}:{port}")
    logger.info("Waiting for trade events from MT5 EA...")
    server.serve_forever()


def main():
    """Main entry point for the bridge server."""
    logger.info("=" * 70)
    logger.info("MT5 to cTrader Copy Trading Bridge - Multi-Account Version")
    logger.info("=" * 70)

    logger.info("Loading account configurations...")
    try:
        config = get_multi_account_config()
    except FileNotFoundError as e:
        logger.error(str(e))
        logger.error("Please create accounts_config.ini file")
        return

    enabled_accounts = config.get_enabled_accounts()
    if not enabled_accounts:
        logger.error("No enabled accounts found in accounts_config.ini")
        return

    logger.info(f"Found {len(enabled_accounts)} enabled account(s):")
    for acc in enabled_accounts:
        logger.info(f"  - {acc.name}: Account ID {acc.account_id} ({acc.environment})")

    logger.info("\nInitializing cTrader connections...")
    account_manager = get_account_manager()

    for account in enabled_accounts:
        account_manager.add_account(account)

    MT5BridgeHandler.account_manager = account_manager

    logger.info("\nStarting HTTP server...")
    server_thread = Thread(target=run_http_server, daemon=True)
    server_thread.start()

    logger.info("Bridge server is running. Press Ctrl+C to stop.")
    logger.info("=" * 70)
    reactor.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nShutting down bridge server...")
        reactor.stop()
