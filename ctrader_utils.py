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
    Convert MT5 lots to cTrader volume in *cents of units*.
    """
    mt5_units = mt5_lots * mt5_contract_size

    if lot_size_cents <= 0:
        units_per_lot_ctrader = mt5_contract_size or 1.0
    else:
        units_per_lot_ctrader = lot_size_cents / 100.0

    if units_per_lot_ctrader <= 0:
        target_lots_ctrader = mt5_lots
    else:
        target_lots_ctrader = mt5_units / units_per_lot_ctrader

    target_units = target_lots_ctrader * units_per_lot_ctrader
    target_cents = int(round(target_units * 100))

    if min_volume_cents and min_volume_cents > 0:
        target_cents = max(target_cents, min_volume_cents)
    if max_volume_cents and max_volume_cents > 0:
        target_cents = min(target_cents, max_volume_cents)

    if step_volume_cents and step_volume_cents > 0:
        base = min_volume_cents if (min_volume_cents and min_volume_cents > 0) else 0
        steps = round((target_cents - base) / step_volume_cents)
        target_cents = base + int(steps) * step_volume_cents

    return max(target_cents, min_volume_cents or 0)
