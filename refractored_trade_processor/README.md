# Refactored trade event processor

All implementation files are kept below roughly 1,000 lines.

- `common.py` — basic helpers, pending SL/TP state, normalization, account/symbol helpers
- `risk.py` — risk sizing and risk enforcement
- `helpers.py` — entry/price helpers and startup recovery planning
- `destination_recovery.py` — destination-loss recovery
- `routing.py` — target-account routing and safe symbol lookup
- `sltp_repair.py` — deferred SL/TP repair queue
- `notifications.py` — position update notification hook
- `handlers_open.py` — OPEN handler
- `handlers_pending.py` — pending OPEN/MODIFY/CANCEL handlers
- `handlers_modify_close.py` — MODIFY and CLOSE handlers
- `processor.py` — main event dispatcher
- `trade_event_processor.py` — compatibility facade exporting the old public surface

Use the package facade `trade_event_processor.py` if existing imports expect one module.
