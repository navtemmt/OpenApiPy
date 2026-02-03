"""Volume Conversion Utilities

Handles conversion of MT5 lots to cTrader volume in cents of units.
"""
import logging

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

    Args:
        mt5_lots: MT5 lot size to convert
        mt5_contract_size: MT5 contract size (units per lot)
        mt5_volume_min: MT5 minimum volume
        mt5_volume_step: MT5 volume step
        lot_size_cents: cTrader lot size in cents
        min_volume_cents: cTrader minimum volume in cents
        max_volume_cents: cTrader maximum volume in cents
        step_volume_cents: cTrader volume step in cents

    Returns:
        Volume in cents of units for cTrader
    """
    # 1) Underlying units represented on MT5 side
    mt5_units = mt5_lots * mt5_contract_size

    if lot_size_cents <= 0:
        units_per_lot_ctrader = mt5_contract_size or 1.0
    else:
        units_per_lot_ctrader = lot_size_cents / 100.0

    # 2) Map MT5 units into cTrader "lots" for this symbol
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
        base = min_volume_cents if (min_volume_cents and min_volume_cents > 0) else 0
        steps = (target_cents - base) / step_volume_cents
        steps = round(steps)
        target_cents = base + int(steps) * step_volume_cents

    return max(target_cents, min_volume_cents or 0)
