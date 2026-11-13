"""Order planner: from a month's units and revenue target to what each shop
should be sent, on which dispatch, and what production has to make.

The steps, in order:

1. Forecast. Each shop-and-colour line gets a forecast daily rate: the
   average of its 90-day rate and its 30-day rate, so a colour that is
   speeding up counts for more and one that is slowing counts for less.
2. Units required. The units target, or the units the revenue target needs
   at the average selling price of the forecast mix, whichever is higher.
3. Split. Required units go to shops by their share (sales trend, or the
   shop targets in Odoo), less what the shop has sold so far this month,
   then to bags and colours by each line's share of the shop's forecast.
   Best-moving colours therefore get the most.
4. Send. Need for the rest of the period, plus a few days of cover left at
   month end, less what the shop holds and has on the way. Every colour that
   sells keeps a minimum on the shelf so the range stays balanced; colours
   with no sales in 90 days get nothing.
5. Schedule. Each dispatch carries what the shop needs to last until the
   next one, so the units arrive through the month instead of all at once.
6. Supply. Each bag's sends are met from the warehouse first, then from bags
   already cut; the rest is the production workplan, dated by the dispatch
   that first needs it.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from lib import stock_planning as sp

BEST, STRONG, STEADY, SLOW, DORMANT = "Best mover", "Strong", "Steady", "Slow", "Dormant"
SPLIT_TREND, SPLIT_TARGETS = "Sales trend", "Shop targets in Odoo"


def dispatch_dates(first: date, last: date, weekdays: set[int]) -> list[date]:
    """Every date from first to last that falls on a dispatch weekday (Mon=0)."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if d.weekday() in weekdays] or [first]


def label(d: date) -> str:
    return f"{d:%a %d %b}"


def _round_to_total(values: pd.Series, groups: pd.Series) -> pd.Series:
    """Whole units whose total per group matches the unrounded total: round
    down, then give the leftover units to the largest fractions. Rounding each
    line on its own would drop every line under half a bag."""
    down = np.floor(values)
    spare = values.groupby(groups).transform("sum").round() - down.groupby(groups).transform("sum")
    rank = (values - down).groupby(groups).rank(method="first", ascending=False)
    return down + (rank <= spare)


def build(base: pd.DataFrame, position: pd.DataFrame, demand: pd.DataFrame, *,
          units_target: float, revenue_target: float, today: date, start: date, end: date,
          dispatches: list[date], shop_weights: pd.Series | None = None,
          cover_days: int = 7, min_present: int = 1) -> dict:
    """The plan: {"lines", "production", "by_date", "summary"}.

    base is stock_planning.shop_sku(); shop_weights overrides each shop's
    share of the target (any positive numbers, e.g. the shops' Odoo targets).
    """
    df = base.copy()
    df["Forecast Rate"] = (df["Daily Rate"] + df["Rate 30d"]) / 2
    prefix = [s[len(b):].strip() if str(s).lower().startswith(str(b).lower()) else s
              for s, b in zip(df["SKU"], df["Bag"])]
    df["Colour"] = [c or "—" for c in prefix]

    # --- colour performance within each bag at each shop
    bag_rate = df.groupby(["Location", "Bag"])["Forecast Rate"].transform("sum")
    colours = df.groupby(["Location", "Bag"])["SKU"].transform("count")
    share = np.where(bag_rate > 0, df["Forecast Rate"] / bag_rate, 0)
    rank = df.groupby(["Location", "Bag"])["Forecast Rate"].rank(method="first", ascending=False)
    df["Colour Share %"] = (share * 100).round(0)
    df["Standing"] = np.select(
        [df["Sold 90d"] <= 0, df["Sold 90d"] < sp.ACTIVE_MIN_SOLD, rank == 1, share >= 1 / colours],
        [DORMANT, SLOW, BEST, STRONG], default=STEADY)

    # --- units required
    sold = demand.groupby("Product ID")[["Revenue 90d", "Sold 90d"]].sum()
    overall_price = sold["Revenue 90d"].sum() / max(sold["Sold 90d"].sum(), 1)
    price = (sold["Revenue 90d"] / sold["Sold 90d"]).where(sold["Sold 90d"] > 0)
    df["Price"] = df["Product ID"].map(price).fillna(overall_price)
    total_rate = df["Forecast Rate"].sum()
    mix = df["Forecast Rate"] / total_rate if total_rate else 0
    avg_price = float((mix * df["Price"]).sum()) or overall_price
    future = start > today
    anchor = start if future else today
    days = (end - anchor).days + 1
    month_days = (end - start).days + 1
    units_for_revenue = revenue_target / avg_price if avg_price else 0
    required = max(units_target, units_for_revenue) or total_rate * month_days

    # --- split to shops, then to each shop's bags and colours
    shop_rate = df.groupby("Location")["Forecast Rate"].transform("sum")
    weights = df.groupby("Location")["Forecast Rate"].sum()
    if shop_weights is not None and shop_weights.sum() > 0:
        # a shop without a target keeps its sales share of what the others leave
        given = shop_weights.reindex(weights.index)
        scale = given.sum() / weights[given.notna()].sum() if weights[given.notna()].sum() else 1
        weights = given.fillna(weights * scale)
    weights = weights / weights.sum() if weights.sum() else weights
    shop_target = df["Location"].map(weights) * required
    shop_sold = 0 if future else df.groupby("Location")["Sold MTD"].transform("sum")
    df["Shop Target"] = shop_target
    remaining = (shop_target - shop_sold).clip(lower=0)
    need = remaining * np.where(shop_rate > 0, df["Forecast Rate"] / shop_rate, 0)

    # --- what to send
    # For a month not yet started, the shop opens with what it has left by then.
    opening = (df["On Hand"] - df["Forecast Rate"] * (start - today).days).clip(lower=0) if future else df["On Hand"]
    rate = need / days
    safety = rate * cover_days
    floor = np.where(df["Sold 90d"] >= sp.ACTIVE_MIN_SOLD, min_present, 0)
    wanted = np.maximum(need + safety, floor)
    send = _round_to_total((wanted - opening).clip(lower=0), df["Location"])
    df["Stock Counted In"] = opening.round(0)
    df["Need"] = need.round(1)
    df["Send"] = send

    # --- schedule: each dispatch covers the days until the next one
    cols, sent = [], 0
    for i, d in enumerate(dispatches):
        until = dispatches[i + 1] if i + 1 < len(dispatches) else end + timedelta(days=1)
        if i + 1 == len(dispatches):
            due = send
        else:
            cover = np.maximum(rate * (until - anchor).days + safety, floor) - opening
            due = np.minimum(np.round(cover.clip(lower=0)), send)
            due = np.maximum(due, sent)
        df[label(d)] = due - sent
        sent = due
        cols.append(label(d))

    # --- supply: warehouse first, then bags already cut, then make
    pos = position.groupby(["Bucket", "Product ID"])["Qty"].sum()
    stock = lambda bucket: pos[bucket].clip(lower=0) if bucket in pos.index.get_level_values(0) else pd.Series(dtype=float)
    sku = df.groupby(["Product ID", "SKU", "Bag"], as_index=False)[["Send", *cols]].sum()
    sku = sku[sku["Send"] > 0].reset_index(drop=True)
    wh = sku["Product ID"].map(stock("Warehouse")).fillna(0)
    cut = sku["Product ID"].map(stock("Production")).fillna(0)
    sku["In Warehouse"] = wh
    sku["From Warehouse"] = np.minimum(sku["Send"], wh)
    sku["Already Cut"] = np.minimum(sku["Send"] - sku["From Warehouse"], cut)
    sku["To Make"] = sku["Send"] - sku["From Warehouse"] - sku["Already Cut"]
    running, made, by_date = 0, 0, []
    needed_by = pd.Series(pd.NaT, index=sku.index, dtype="object")
    for d, c in zip(dispatches, cols):
        running = running + sku[c]
        from_production = (running - wh).clip(lower=0)
        make = (running - wh - cut).clip(lower=0)
        needed_by = needed_by.where(needed_by.notna() | (from_production <= 0), d)
        by_date.append({"Dispatch": d, "To Send": sku[c].sum(),
                        "From Warehouse": sku[c].sum() - (from_production.sum() - made),
                        "From Production": from_production.sum() - made,
                        "To Make by Then": make.sum()})
        made = from_production.sum()
    sku["Needed By"] = needed_by
    production = (sku[sku["Send"] > sku["From Warehouse"]]
                  [["Bag", "SKU", "Send", "In Warehouse", "From Warehouse", "Already Cut", "To Make", "Needed By"]]
                  .sort_values(["Needed By", "To Make"], ascending=[True, False]).reset_index(drop=True))

    # each line's share of what the warehouse can cover now
    ratio = (sku["From Warehouse"] / sku["Send"]).set_axis(sku["Product ID"])
    df["From Warehouse"] = np.floor(df["Send"] * df["Product ID"].map(ratio).fillna(0))
    df["Awaiting Production"] = df["Send"] - df["From Warehouse"]

    lines = df[["Location", "Bag", "SKU", "Colour", "Standing", "Colour Share %", "Sold 90d", "Sold 30d",
                "Trend %", "Stock Counted In", "Need", "Send", "From Warehouse", "Awaiting Production",
                *cols, "Price", "Shop Target", "Sold MTD", "On Hand"]]
    summary = {
        "required": required, "units_for_revenue": units_for_revenue, "avg_price": avg_price,
        "sold": 0 if future else df["Sold MTD"].sum(), "days": days, "future": future,
        "on_hand": opening.sum(), "send": send.sum(), "from_warehouse": sku["From Warehouse"].sum(),
        "already_cut": sku["Already Cut"].sum(), "to_make": sku["To Make"].sum(), "dates": cols,
    }
    return {"lines": lines, "production": production, "by_date": pd.DataFrame(by_date), "summary": summary}
