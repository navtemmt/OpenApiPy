from threading import Thread
from twisted.internet import reactor
from config_loader import get_multi_account_config
from account_manager import get_account_manager
from bridge_server import run_http_servers
from app_state import logger
import traceback
import time


def main():
    logger.info("=" * 70)
    logger.info("MT5 to cTrader Copy Trading Bridge - Multi-Account Version")
    logger.info("=" * 70)
    logger.info("Loading account configurations...")

    try:
        config = get_multi_account_config()
        account_manager = get_account_manager()

        enabled_accounts = config.get_enabled_accounts()
        logger.info(f"Initializing {len(enabled_accounts)} cTrader account(s)...")

        for account_config in enabled_accounts:
            logger.info(f" - {account_config.name}")
            account_manager.add_account(account_config)

        logger.info("Starting cTrader API clients...")
        reactor_thread = Thread(target=reactor.run, args=(False,), daemon=True)
        reactor_thread.start()

        http_host = getattr(config, "http_host", "127.0.0.1")
        http_ports = getattr(config, "http_ports", [80, 3140])

        if isinstance(http_ports, str):
            http_ports = [int(p.strip()) for p in http_ports.split(",") if p.strip()]
        elif isinstance(http_ports, int):
            http_ports = [http_ports]

        logger.info(f"Starting HTTP servers on {http_host}:{http_ports} ...")
        run_http_servers(http_host, http_ports, account_manager)

        while True:
            time.sleep(3600)

    except KeyboardInterrupt:
        logger.info("Shutting down bridge server...")
        reactor.stop()

    except Exception:
        logger.error("Fatal error during startup")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
