import time
from threading import Lock

from app_state import (
    logger, PENDING_SLTP, MASTER_OPEN_LOTS, MASTER_CLOSED_LOTS,
    alert_trade_failure, alert_trade_warning, alert_trade_info,
)
from trade_executor import (copy_open_to_account, copy_pending_to_account, transition_pending_to_market)
from symbol_mapper import SymbolMapper
from .common import *
from .common import (
    _risk_mode,
    _has_valid_sl,
)
def _estimate_risk_ccy_per_1lot_from_symbol(
    symbol,
    entry_price: float,
    sl_price: float,
) -> float:
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)

        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)

        if dist <= 0:
            return 0.0

        pip_pos = getattr(
            symbol,
            "pipPosition",
            None,
        )

        digits = int(
            getattr(
                symbol,
                "digits",
                0,
            )
            or 0
        )

        if pip_pos is not None:
            tick_size = 10 ** (-int(pip_pos))

        elif digits > 0:
            tick_size = 10 ** (-digits)

        else:
            return 0.0

        if tick_size <= 0:
            return 0.0

        ticks = dist / float(tick_size)

        if ticks <= 0:
            return 0.0

        tick_value = float(
            getattr(
                symbol,
                "tickValue",
                0,
            )
            or 0.0
        )

        if tick_value <= 0:
            return 0.0

        return float(ticks) * float(tick_value)

    except Exception:
        return 0.0


def _estimate_risk_ccy_per_1lot_from_mt5(
    data: dict,
    entry_price: float,
    sl_price: float,
) -> float:
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)

        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)

        if dist <= 0:
            return 0.0

        tick_size = _first_positive_float(
            data.get("mt5_tick_size"),
            data.get("tick_size"),
            data.get("tickSize"),
            data.get("point"),
            data.get("Point"),
            data.get("trade_tick_size"),
        )

        tick_value = _first_positive_float(
            data.get("mt5_tick_value"),
            data.get("tick_value"),
            data.get("tickValue"),
            data.get("trade_tick_value"),
            data.get("tradeTickValue"),
        )

        if (
            tick_size is not None
            and tick_value is not None
            and tick_size > 0
            and tick_value > 0
        ):
            ticks = dist / float(tick_size)

            if ticks > 0:
                return float(ticks) * float(tick_value)

        mt5_contract_size = float(
            data.get("mt5_contract_size", 0)
            or 0.0
        )

        quote_to_deposit = _first_positive_float(
            data.get("quote_to_deposit_rate"),
            data.get("quote_to_account_rate"),
            data.get("conversion_rate"),
            data.get("fx_conversion_rate"),
        )

        if (
            mt5_contract_size > 0
            and quote_to_deposit is not None
            and quote_to_deposit > 0
        ):
            return (
                dist
                * mt5_contract_size
                * float(quote_to_deposit)
            )

        return 0.0

    except Exception:
        return 0.0


def _enforce_max_risk_on_fill(
    account_name,
    client,
    config,
    account_manager,
    position,
    symbol,
    mt5_symbol=None,
    mt5_data=None,
):
    rm = _risk_mode(config)

    if rm not in (
        "FIXED_USD",
        "PERCENT_EQUITY",
    ):
        return

    entry = float(
        getattr(position, "price", 0)
        or 0.0
    )

    sl = float(
        getattr(position, "stopLoss", 0)
        or 0.0
    )

    if entry <= 0 or sl <= 0:
        return

    if not isinstance(mt5_data, dict):
        msg = (
            "Over-risk check skipped: "
            "missing mt5_data for strict MT5 risk mode"
        )

        logger.warning(
            f"[{account_name}] {msg}"
        )

        alert_trade_warning(
            account_name=account_name,
            action="overrisk_check_missing_mt5_data",
            ticket=0,
            message=msg,
            mt5_symbol=mt5_symbol,
        )

        return

    risk_per_1lot = (
        _estimate_risk_ccy_per_1lot_from_mt5(
            mt5_data,
            entry,
            sl,
        )
    )

    logger.info(
        f"[{account_name}] Over-risk calc "
        f"source=mt5_only, "
        f"symbol={mt5_symbol}, "
        f"entry={entry:.5f}, "
        f"sl={sl:.5f}, "
        f"mt5_tick_size={mt5_data.get('mt5_tick_size')}, "
        f"mt5_tick_value={mt5_data.get('mt5_tick_value')}, "
        f"quote_to_deposit_rate="
        f"{mt5_data.get('quote_to_deposit_rate')}, "
        f"perLot={float(risk_per_1lot):.2f}"
    )

    if risk_per_1lot <= 0:
        msg = (
            "Over-risk check skipped: "
            "cannot price MT5 risk strictly"
        )

        logger.warning(
            f"[{account_name}] {msg}"
        )

        alert_trade_warning(
            account_name=account_name,
            action="overrisk_check_cannot_price_risk",
            ticket=0,
            message=msg,
            mt5_symbol=mt5_symbol,
            entry=entry,
            sl=sl,
        )

        return

    if rm == "FIXED_USD":
        target_risk = float(
            getattr(
                config,
                "fixed_usd_risk",
                0,
            )
            or 0.0
        )

    else:
        pct = float(
            getattr(
                config,
                "risk_percent",
                0,
            )
            or 0.0
        )

        ref_amt = _get_account_equity_or_balance(
            account_manager,
            account_name,
            config,
        )

        target_risk = (
            (pct / 100.0) * float(ref_amt)
            if pct > 0 and ref_amt > 0
            else 0.0
        )

    if target_risk <= 0:
        return

    lot_size_cents = float(
        getattr(
            symbol,
            "lotSize",
            0,
        )
        or 0.0
    )

    follower_units = float(
        getattr(
            position.tradeData,
            "volume",
            0,
        )
        or 0.0
    )

    if (
        lot_size_cents <= 0
        or follower_units <= 0
    ):
        return

    follower_lots = (
        follower_units / lot_size_cents
    )

    actual_risk = (
        follower_lots * risk_per_1lot
    )

    if actual_risk <= target_risk:
        return

    allowed_lots = (
        target_risk / risk_per_1lot
    )

    excess_lots = (
        follower_lots - allowed_lots
    )

    if excess_lots <= 0:
        return

    excess_units = int(
        round(
            excess_lots * lot_size_cents
        )
    )

    if excess_units <= 0:
        return

    logger.info(
        f"[{account_name}] Over-risk detected "
        f"on fill: rm={rm}, "
        f"actual_risk={actual_risk:.2f} "
        f"target={target_risk:.2f}, "
        f"follower_lots={follower_lots:.4f}, "
        f"trim_lots={excess_lots:.4f}, "
        f"trim_units={excess_units}"
    )

    try:
        client.close_position(
            account_id=config.account_id,
            position_id=position.positionId,
            volume=excess_units,
            symbol_id=symbol.symbolId,
        )

        logger.info(
            f"[{account_name}] "
            f"Over-risk partial close sent: "
            f"positionId={position.positionId}, "
            f"trim_units={excess_units}"
        )

        alert_trade_warning(
            account_name=account_name,
            action="overrisk_partial_close_sent",
            ticket=0,
            message="Over-risk trim executed after fill",
            mt5_symbol=mt5_symbol,
            position_id=position.positionId,
            trim_units=excess_units,
            actual_risk=actual_risk,
            target_risk=target_risk,
        )

    except Exception as e:
        alert_trade_failure(
            account_name=account_name,
            action="overrisk_partial_close_failed",
            ticket=0,
            exc=e,
            mt5_symbol=mt5_symbol,
            position_id=getattr(
                position,
                "positionId",
                None,
            ),
            trim_units=excess_units,
            actual_risk=actual_risk,
            target_risk=target_risk,
        )


def _resolve_open_volume_for_account(
    data: dict,
    config,
    *,
    account_name=None,
    client=None,
    account_manager=None,
):
    src_lots = float(
        data.get("volume", 0)
        or 0
    )

    sl = float(
        data.get("sl", 0)
        or 0
    )

    risk_mode = _risk_mode(config)

    reject_if_no_sl = bool(
        getattr(
            config,
            "reject_if_no_sl",
            False,
        )
    )

    source_volume_fallback = bool(
        getattr(
            config,
            "source_volume_fallback",
            True,
        )
    )

    if risk_mode == "FIXED_LOT":
        fixed_lot = float(
            getattr(
                config,
                "fixed_lot",
                0,
            )
            or 0
        )

        if fixed_lot > 0:
            return fixed_lot, "FIXED_LOT"

        return (
            src_lots,
            "FIXED_LOT invalid -> SOURCE_VOLUME",
        )

    if not _has_valid_sl(sl):
        if reject_if_no_sl:
            return None, "REJECT_NO_SL"

        if not source_volume_fallback:
            return (
                None,
                "REJECT_NO_SL_FALLBACK_DISABLED",
            )

        return (
            src_lots,
            "NO_SL_FALLBACK_SOURCE_VOLUME",
        )

    if risk_mode in (
        "FIXED_USD",
        "PERCENT_EQUITY",
    ):
        if not (
            account_manager
            and account_name
        ):
            return (
                None,
                f"REJECT_{risk_mode}_MISSING_CONTEXT",
            )

        mt5_symbol = data.get("symbol")

        entry_price = float(
            data.get("entry_price", 0)
            or 0.0
        )

        if entry_price <= 0:
            return (
                None,
                f"REJECT_{risk_mode}_"
                f"NO_ENTRY_PRICE_FROM_MT5",
            )

        risk_per_1lot = (
            _estimate_risk_ccy_per_1lot_from_mt5(
                data,
                float(entry_price),
                float(sl),
            )
        )

        logger.info(
            f"[{account_name}] Risk-per-lot calc: "
            f"source=mt5_only, "
            f"symbol={mt5_symbol}, "
            f"entry={float(entry_price):.5f}, "
            f"sl={float(sl):.5f}, "
            f"mt5_tick_size="
            f"{data.get('mt5_tick_size')}, "
            f"mt5_tick_value="
            f"{data.get('mt5_tick_value')}, "
            f"quote_to_deposit_rate="
            f"{data.get('quote_to_deposit_rate')}, "
            f"perLot={float(risk_per_1lot):.2f}"
        )

        if risk_per_1lot <= 0:
            return (
                None,
                f"REJECT_{risk_mode}_"
                f"CANNOT_PRICE_RISK_FROM_MT5",
            )

        if risk_mode == "FIXED_USD":
            usd_risk = float(
                getattr(
                    config,
                    "fixed_usd_risk",
                    0,
                )
                or 0
            )

            if usd_risk <= 0:
                return (
                    None,
                    "REJECT_FIXED_USD_INVALID",
                )

        else:
            pct = float(
                getattr(
                    config,
                    "risk_percent",
                    0,
                )
                or 0
            )

            ref_amt = (
                _get_account_equity_or_balance(
                    account_manager,
                    account_name,
                    config,
                )
            )

            if pct <= 0:
                return (
                    None,
                    "REJECT_PERCENT_EQUITY_INVALID_PCT",
                )

            if ref_amt <= 0:
                return None, "REJECT_NO_EQUITY"

            usd_risk = (
                pct / 100.0
            ) * float(ref_amt)

        lots = (
            float(usd_risk)
            / float(risk_per_1lot)
        )

        if lots <= 0:
            return (
                None,
                f"REJECT_{risk_mode}_"
                f"LOTS_NONPOSITIVE",
            )

        return (
            lots,
            f"{risk_mode} mt5_only "
            f"usd={usd_risk:.2f} "
            f"perLot={risk_per_1lot:.2f} "
            f"entry={float(entry_price):.5f}",
        )

    return (
        src_lots,
        f"{risk_mode}_"
        f"USING_SOURCE_VOLUME_FOR_NOW",
    )


