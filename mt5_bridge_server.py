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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def convert_mt5_lots_to_ctrader_cents(
    mt5_lots: float,
    mt5_contract_size: float,
    mt5_volume_min: float,
    mt5_volume_step: float,
    lot_size_cents: int,
    min_volume_cents: int,
    max_volume_cents: int,
    step_volume_cents: int,
) -> int:
    """
    Convert MT5 lots to cTrader volume in *cents of units*, using
    both MT5 contract info and cTrader symbol specs.
    """

    # 1) Underlying units represented on MT5 side
    #    e.g. forex: 1 lot = 100000; metals: 1 lot = 100; indices: broker-specific
    mt5_units = mt5_lots * mt5_contract_size

    if lot_size_cents <= 0:
        # Fallback: treat 1 lot as 1 unit if lotSize is missing
        units_per_lot_ctrader = mt5_contract_size or 1.0
    else:
        # lotSize is stored as cents of units
        units_per_lot_ctrader = lot_size_cents / 100.0

    # 2) Map MT5 units into cTrader "lots" for this symbol
    #    target_lots_ctrader = how many cTrader lots correspond to mt5_units
    if units_per_lot_ctrader <= 0:
        target_lots_ctrader = mt5_lots
    else:
        target_lots_ctrader = mt5_units / units_per_lot_ctrader

    # 3) Convert cTrader lots back to units, then to cents-of-units
    target_units = target_lots_ctrader * units_per_lot_ctrader
    target_cents = int(round(target_units * 100))

    # 4) Clamp to broker [min, max] in cents
    if min_volume_cents and min_volume_cents > 0:
        target_cents = max(target_cents, min_volume_cents)
    if max_volume_cents and max_volume_cents > 0:
        target_cents = min(target_cents, max_volume_cents)

    # 5) Snap to stepVolume in cents
    if step_volume_cents and step_volume_cents > 0:
        # If min is defined, snap relative to it; otherwise snap from zero
        base = min_volume_cents if (min_volume_cents and min_volume_cents > 0) else 0
        steps = (target_cents - base) / step_volume_cents
        steps = round(steps)
        target_cents = base + int(steps) * step_volume_cents

    # Ensure non-negative
    return max(target_cents, min_volume_cents or 0)


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
            content_length = int(self.headers["Content-Length"])
            post_data = self.rfile.read(content_length)

            trade_event = json.loads(post_data.decode("utf-8"))
            logger.info(f"Received trade event: {trade_event}")

            self._process_trade_event(trade_event)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            response = json.dumps(
                {"status": "success", "message": "Trade event received"}
            )
            self.wfile.write(response.encode("utf-8"))

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
                "current_positions": config.current_positions,
            }
            for name, (client, config) in accounts.items()
        }

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = json.dumps(
            {
                "status": "online",
                "service": "MT5 to cTrader Bridge (Multi-Account)",
                "version": "2.0.0",
                "accounts": account_status,
            },
            indent=2,
        )
        self.wfile.write(response.encode("utf-8"))

    def _process_trade_event(self, event):
        """Process trade event and forward to all enabled cTrader accounts."""
        if not self.account_manager:
            logger.error("Account manager not initialized")
            return

        event_type = event.get("action", event.get("event"))
        if event_type:
            event_type = event_type.lower()

        if event_type == "open":
            self._handle_open(event)
        elif event_type == "modify":
            self._handle_modify(event)
        elif event_type == "close":
            self._handle_close(event)
        else:
            logger.error(f"Unknown event type: {event_type}")

    def _handle_open(self, event):
        """Handle new order event - copy to all enabled accounts."""
        ticket = event.get("ticket")
        mt5_symbol = event.get("symbol")
        side = event.get("type", event.get("side", "BUY")).lower()
        volume = event.get("volume", event.get("lots", 0.01))
        sl = event.get("sl", 0.0)
        tp = event.get("tp", 0.0)
        magic = event.get("magic", 0)

        logger.info(
            f"Opening {side} order: {volume} lots of {mt5_symbol} "
            f"(ticket #{ticket}, magic {magic})"
        )

        accounts = self.account_manager.get_all_accounts()

        for account_name, (client, config) in accounts.items():
            try:
                self._copy_open_to_account(
                    account_name,
                    client,
                    config,
                    ticket,
                    mt5_symbol,
                    side,
                    volume,
                    sl,
                    tp,
                    magic,
                    event,
                )
            except Exception as e:
                logger.error(
                    f"Failed to copy trade to {account_name}: {e}", exc_info=True
                )

    def _copy_open_to_account(
        self,
        account_name,
        client,
        config,
        ticket,
        mt5_symbol,
        side,
        volume,
        sl,
        tp,
        magic,
        raw_event,
    ):
        """Copy open order to a specific account."""
        if not client or not client.is_app_authed:
            logger.warning(f"[{account_name}] Client not ready, skipping")
            return

        multi_config = get_multi_account_config()

        should_copy, reason = multi_config.should_copy_trade(
            config, mt5_symbol, magic, volume
        )
        if not should_copy:
            logger.info(f"[{account_name}] Skipping: {reason}")
            return

        mapper = SymbolMapper(
            prefix=config.symbol_prefix,
            suffix=config.symbol_suffix,
            custom_map=config.custom_symbols,
            broker_symbol_map=client.symbol_name_to_id,
        )

        symbol_id = mapper.get_symbol_id(mt5_symbol)
        if symbol_id is None:
            logger.error(f"[{account_name}] Unknown symbol {mt5_symbol}, skipping")
            return

        # Account-specific lot multiplier and limits (still in MT5 lots)
        adjusted_lots = config.lot_multiplier * volume
        adjusted_lots = max(
            config.min_lot_size, min(adjusted_lots, config.max_lot_size)
        )

        # --- New: use MT5 metadata + cTrader symbol specs to compute volume in cents-of-units ---

        # MT5 side metadata sent by EA (with safe defaults)
        mt5_contract_size = float(raw_event.get("mt5_contract_size", 1.0))
        mt5_volume_min = float(raw_event.get("mt5_volume_min", 0.01))
        mt5_volume_step = float(raw_event.get("mt5_volume_step", 0.01))

        # cTrader side symbol specs (in cents-of-units)
        symbol = client.symbol_details.get(symbol_id) if hasattr(client, "symbol_details") else None

        if symbol is None:
            # Fallback to old behavior if we don't have symbol details
            logger.warning(
                f"[{account_name}] No symbol details for {mt5_symbol} "
                f"(id={symbol_id}), falling back to lots_to_units"
            )
            base_units = mapper.lots_to_units(adjusted_lots, mt5_symbol)
            sym_upper = (mt5_symbol or "").upper()

            if any(metal in sym_upper for metal in ["XAU", "XAG", "GOLD", "SILVER"]):
                min_units = 100  # 0.01 lot metals
                units = int(base_units)
                if units < min_units:
                    logger.warning(
                        f"[{account_name}] Volume {units} below minimum {min_units}, "
                        f"adjusting to {min_units} units"
                    )
                    units = min_units
                volume_to_send = units
            else:
                units = int(base_units)
                cents = units * 100
                min_units = 1000  # 0.01 lot forex
                min_cents = min_units * 100
                if cents < min_cents:
                    logger.warning(
                        f"[{account_name}] Volume {cents} below minimum {min_cents}, "
                        f"adjusting to {min_cents}"
                    )
                    cents = min_cents
                volume_to_send = cents
        else:
            lot_size_cents = getattr(symbol, "lotSize", 0)
            min_volume_cents = getattr(symbol, "minVolume", 0)
            max_volume_cents = getattr(symbol, "maxVolume", 0)
            step_volume_cents = getattr(symbol, "stepVolume", 0)

            volume_to_send = convert_mt5_lots_to_ctrader_cents(
                mt5_lots=adjusted_lots,
                mt5_contract_size=mt5_contract_size,
                mt5_volume_min=mt5_volume_min,
                mt5_volume_step=mt5_volume_step,
                lot_size_cents=lot_size_cents,
                min_volume_cents=min_volume_cents,
                max_volume_cents=max_volume_cents,
                step_volume_cents=step_volume_cents,
            )

            logger.info(
                f"[{account_name}] Volume conversion: symbol_id={symbol_id}, "
                f"mt5_lots={adjusted_lots:.4f}, mt5_contract_size={mt5_contract_size}, "
                f"lotSize={lot_size_cents}, min={min_volume_cents}, "
                f"max={max_volume_cents}, step={step_volume_cents} -> "
                f"volume_cents={volume_to_send}"
            )

        # Broker restriction: absolute SL/TP are not allowed on MARKET orders.
        # Send a naked market order; SL/TP can be applied later via modify_position.
        final_sl = None
        final_tp = None

        logger.info(
            f"[{account_name}] Sending: symbol_id={symbol_id}, side={side}, "
            f"volume={volume_to_send} (from {volume} lots * {config.lot_multiplier})"
        )

        client.send_market_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=side,
            volume=volume_to_send,
            sl=final_sl,
            tp=final_tp,
            label=f"MT5_{ticket}",
        )

        config.daily_trade_count += 1
        config.current_positions += 1

        logger.info(
            f"✓ [{account_name}] Order sent successfully "
            f"(daily: {config.daily_trade_count}/{config.max_daily_trades})"
        )

    def _handle_modify(self, event):
        """Handle position modification event."""
        ticket = event.get("ticket")
        sl = event.get("sl", 0.0)
        tp = event.get("tp", 0.0)

        logger.info(f"Modifying position {ticket}: SL={sl}, TP={tp}")

        if not self.account_manager:
            logger.error("Account manager not initialized")
            return

        accounts = self.account_manager.get_all_accounts()

        for account_name, (client, config) in accounts.items():
            try:
                position_id = self.account_manager.get_position_id(account_name, ticket)
                if not position_id:
                    logger.warning(
                        f"[{account_name}] No cTrader positionId mapped for MT5 "
                        f"ticket {ticket}, skipping modify"
                    )
                    continue

                if not client or not client.is_app_authed:
                    logger.warning(f"[{account_name}] Client not ready, skipping modify")
                    continue

                logger.info(
                    f"[{account_name}] Applying modify: ticket {ticket} -> "
                    f"positionId {position_id}, SL={sl}, TP={tp}"
                )

                # Adjust signature if your CTraderClient differs
                client.modify_position(
                    account_id=config.account_id,
                    position_id=position_id,
                    sl=sl if sl > 0 else None,
                    tp=tp if tp > 0 else None,
                )
            except Exception as e:
                logger.error(
                    f"[{account_name}] Failed to modify position for ticket {ticket}: {e}",
                    exc_info=True,
                )

    def _handle_close(self, event):
        """Handle position close event."""
        ticket = event.get("ticket")
        volume = event.get("volume", event.get("lots", 0))

        logger.info(f"Closing position {ticket}: {volume} lots")

        if not self.account_manager:
            logger.error("Account manager not initialized")
            return

        accounts = self.account_manager.get_all_accounts()

        for account_name, (client, config) in accounts.items():
            try:
                position_id = self.account_manager.get_position_id(account_name, ticket)
                if not position_id:
                    logger.warning(
                        f"[{account_name}] No cTrader positionId mapped for MT5 "
                        f"ticket {ticket}, skipping close"
                    )
                    continue

                if not client or not client.is_app_authed:
                    logger.warning(f"[{account_name}] Client not ready, skipping close")
                    continue

                logger.info(
                    f"[{account_name}] Applying close: ticket {ticket} -> "
                    f"positionId {position_id}"
                )

                # If your API supports partial close, map MT5 volume to cTrader volume here
                client.close_position(
                    account_id=config.account_id,
                    position_id=position_id,
                )
            except Exception as e:
                logger.error(
                    f"[{account_name}] Failed to close position for ticket {ticket}: {e}",
                    exc_info=True,
                )


def run_http_server(host: str = "127.0.0.1", port: int = 3140):
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
