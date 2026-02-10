#!/usr/bin/env python3
"""
Heartbeat/health helpers extracted from ctrader_client.py.

All functions operate on the CTraderClient instance ("self") and keep using:
  - self.heartbeat_task
  - self.health_check_task
  - self.heartbeat_interval
  - self.last_message_time
  - self.max_idle_time
  - self.is_connected
  - self.is_app_authed
"""

import time
import logging
from twisted.internet import task

logger = logging.getLogger(__name__)


def start_heartbeat(self) -> None:
    if self.heartbeat_task is None or not self.heartbeat_task.running:
        self.heartbeat_task = task.LoopingCall(lambda: send_heartbeat(self))
        self.heartbeat_task.start(self.heartbeat_interval, now=False)
        logger.info("Heartbeat started")


def send_heartbeat(self) -> None:
    if self.is_connected and self.is_app_authed:
        logger.debug("Heartbeat OK")
    else:
        logger.debug("Heartbeat: not ready")


def start_health_check(self) -> None:
    if self.health_check_task is None or not self.health_check_task.running:
        self.health_check_task = task.LoopingCall(lambda: check_connection_health(self))
        self.health_check_task.start(30, now=False)
        logger.info("Health check started")


def check_connection_health(self) -> None:
    idle = time.time() - self.last_message_time
    if idle > self.max_idle_time:
        logger.warning("Connection idle for %.0fs", idle)


def stop_periodic_tasks(self) -> None:
    if self.heartbeat_task and self.heartbeat_task.running:
        self.heartbeat_task.stop()
        logger.info("Heartbeat stopped")
    if self.health_check_task and self.health_check_task.running:
        self.health_check_task.stop()
        logger.info("Health check stopped")
