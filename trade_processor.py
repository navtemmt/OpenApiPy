"""Backward-compatible facade for the refactored trade processor."""

from refactored_trade_processor.common import *
from refactored_trade_processor.risk import *
from refactored_trade_processor.risk import _enforce_max_risk_on_fill
from refactored_trade_processor.helpers import *
from refactored_trade_processor.destination_recovery import *
from refactored_trade_processor.routing import *
from refactored_trade_processor.sltp_repair import *
from refactored_trade_processor.notifications import *
from refactored_trade_processor.handlers_open import *
from refactored_trade_processor.handlers_pending import *
from refactored_trade_processor.handlers_modify_close import *
from refactored_trade_processor.processor import *
