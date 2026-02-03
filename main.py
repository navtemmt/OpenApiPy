"""Main entry point for the MT5 to cTrader bridge server.
Initializes accounts and starts the HTTP server.
"""
from threading import Thread
from twisted.internet import reactor
from config_loader import get_multi_account_config
from account_manager import get_account_manager
from bridge_server import run_http_server
from app_state import logger


def main():
    """Main entry point for the bridge server."""
    logger.info("=" * 70)
    logger.info("MT5 to cTrader Copy Trading Bridge - Multi-Account Version")
    logger.info("=" * 70)
    logger.info("Loading account configurations...")

    try:
        config = get_multi_account_config()
        account_manager = get_account_manager()

        # Initialize all cTrader accounts
        logger.info(f"Initializing {len(config.accounts)} cTrader account(s)...")
        for account_config in config.accounts:
            account_name = account_config.name
            logger.info(f"  - {account_name}")
            account_manager.initialize_account(account_name, account_config)

        # Start cTrader clients
        logger.info("Starting cTrader API clients...")
        reactor_thread = Thread(
            target=reactor.run,
            args=(False,),
            daemon=True,
        )
        reactor_thread.start()

        # Start HTTP server
        http_host = config.http_host
        http_port = config.http_port
        logger.info(f"Starting HTTP server on {http_host}:{http_port}...")
        run_http_server(http_host, http_port, account_manager)

    except KeyboardInterrupt:
        logger.info("Shutting down bridge server...")
        reactor.stop()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

        logger.info(f"Starting HTTP server on {http_host}:{http_port}...")
        run_http_server(http_host, http_port, account_manager)

    except KeyboardInterrupt:
        logger.info("Shutting down bridge server...")
        reactor.stop()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
