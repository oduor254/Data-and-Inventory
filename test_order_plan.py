"""Self-check for lib/order_plan.py on a made-up shop. Run: python test_order_plan.py"""
from datetime import date

import pandas as pd

from lib import order_plan as op
from lib import stock_planning as sp

today, start, end = date(2026, 11, 13), date(2026, 11, 1), date(2026, 11, 30)
# One shop, one bag in three colours: black sells fast, grey steadily, pink never.
demand = pd.DataFrame({
    "Location": ["Shop"] * 2, "Product ID": [1, 2], "SKU": ["Code 3 Black", "Code 3 Grey"],
    "Category": ["Backpacks"] * 2, "Sold 90d": [90, 30], "Revenue 90d": [270000, 90000],
    "Sold 30d": [40, 10], "Sold MTD": [10, 5], "Last Sold": [today] * 2})
position = pd.DataFrame({
    "Bucket": ["Shop", "Shop", "Shop", "Warehouse", "Production"], "Location": ["Shop"] * 3 + ["", ""],
    "Product ID": [1, 2, 3, 1, 1], "SKU": ["Code 3 Black", "Code 3 Grey", "Code 3 Pink", "Code 3 Black", "Code 3 Black"],
    "Category": ["Backpacks"] * 5, "Qty": [5, 20, 8, 10, 6]})
base = sp.shop_sku(position, demand, ["Shop"])
dates = op.dispatch_dates(today, end, {0, 2, 4})
plan = op.build(base, position, demand, units_target=100, revenue_target=0, today=today, start=start, end=end,
                dispatches=dates, cover_days=0, min_present=1)
lines, s = plan["lines"].set_index("SKU"), plan["summary"]

assert dates[0] == today and all(d.weekday() in {0, 2, 4} for d in dates)
assert s["required"] == 100 and s["sold"] == 15
assert abs(lines["Need"].sum() - 85) < 0.2                      # the target still to sell
assert lines.loc["Code 3 Black", "Standing"] == op.BEST and lines.loc["Code 3 Pink", "Standing"] == op.DORMANT
assert lines.loc["Code 3 Black", "Send"] > lines.loc["Code 3 Grey", "Send"]   # best colour gets the most
assert lines.loc["Code 3 Pink", "Send"] == 0                    # nothing more of a colour that does not sell
assert s["send"] == 61   # black needs 66 and has 5; grey needs 19 and already has 20
assert (lines[s["dates"]].sum(axis=1) == lines["Send"]).all()   # the dispatches add up to the send
assert (lines[s["dates"]] >= 0).all().all()
black = plan["production"].set_index("SKU").loc["Code 3 Black"]
assert black["From Warehouse"] == 10 and black["Already Cut"] == 6
assert black["To Make"] == black["Send"] - 16 and black["Needed By"] in dates
# a revenue target that needs more bags than the units target wins
big = op.build(base, position, demand, units_target=100, revenue_target=600000, today=today, start=start, end=end,
               dispatches=dates)["summary"]
assert round(big["avg_price"]) == 3000 and round(big["required"]) == 200
print("ok order_plan")
