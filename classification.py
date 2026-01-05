def classify_day_type(cpr, open_price, first_15_high, first_15_low, daily_atr):
    TC = cpr["TC"]; BC = cpr["BC"]
    cpr_width = abs(TC - BC)
    first_15_range = abs(first_15_high - first_15_low)

    narrow_cpr = daily_atr is not None and cpr_width < (0.6 * daily_atr)
    wide_cpr   = daily_atr is not None and cpr_width > (1.0 * daily_atr)

    if open_price > TC:
        open_location = "ABOVE_CPR"
    elif open_price < BC:
        open_location = "BELOW_CPR"
    else:
        open_location = "INSIDE_CPR"

    expanding = daily_atr is not None and first_15_range > (0.5 * daily_atr)

    if open_location in ["ABOVE_CPR", "BELOW_CPR"] and expanding and narrow_cpr:
        return "TREND_DAY"
    if open_location == "INSIDE_CPR" and wide_cpr:
        return "RANGE_DAY"
    return "NORMAL_DAY"