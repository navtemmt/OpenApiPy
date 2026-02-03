"""Main entry point for the MT5 to cTrader bridge server.
Initializes accounts and starts the HTTP server.
"""
from threading import Thread
from twisted.internet import reactor
from config_loader import get_multi_account_config
from account_manager import get_account_manager
from bridge_server import run_http_server
from app_state import logger
import traceback


def main():
    """Main entry point for the bridge server."""
    logger.info("=" * 70)
    logger.info("MT5 to cTrader Copy Trading Bridge - Multi-Account Version")
    logger.info("=" * 70)
    logger.info("Loading account configurations...")

    try:
        # Load config and account manager
        config = get_multi_account_config()
        account_manager = get_account_manager()

        # Initialize all enabled cTrader accounts
        # Use config.get_enabled_accounts() method for proper filtering
        enabled_accounts = config.get_enabled_accounts()
        logger.info(f"Initializing {len(enabled_accounts)} cTrader account(s)...")

        for account_config in enabled_accounts:
            logger.info(f"  - {account_config.name}")
            account_manager.add_account(account_config)

        # Start cTrader clients (Twisted reactor in a separate thread)
        logger.info("Starting cTrader API clients...")
        reactor_thread = Thread(target=reactor.run, args=(False,), daemon=True)
        reactor_thread.start()

        # Start HTTP server for MT5 events (blocking)
        http_host = getattr(config, "http_host", "127.0.0.1")
        http_port = getattr(config, "http_port", 3140)
        logger.info(f"Starting HTTP server on {http_host}:{{http_port}}...")
        run_http_server(http_host, http_port, account_manager)

    except KeyboardInterrupt:
        logger.info("Shutting down bridge server...")
        reactor.stop()

    except Exception as e:
        logger.error("Fatal error during startup")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
