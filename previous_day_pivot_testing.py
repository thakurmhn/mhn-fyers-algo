def compute_levels(prev_high, prev_low, prev_close):
    """
    Compute all structural levels (Traditional, CPR, Camarilla)
    from previous day's OHLC.
    Returns a tuple: (traditional_levels, cpr_levels, camarilla_levels)
    """

    # --- Traditional pivots ---
    pivot = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * pivot) - prev_low
    s1 = (2 * pivot) - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    traditional_levels = {
        "pivot": pivot,
        "r1": r1,
        "s1": s1,
        "r2": r2,
        "s2": s2
    }

    # --- CPR ---
    bc = (prev_high + prev_low) / 2
    tc = (pivot + bc) / 2
    cpr_levels = {
        "pivot": pivot,
        "bc": bc,
        "tc": tc
    }

    # --- Camarilla ---
    rng = prev_high - prev_low
    r3 = prev_close + (rng * 1.1 / 4)
    r4 = prev_close + (rng * 1.1 / 2)
    s3 = prev_close - (rng * 1.1 / 4)
    s4 = prev_close - (rng * 1.1 / 2)
    camarilla_levels = {
        "r3": r3,
        "r4": r4,
        "s3": s3,
        "s4": s4
    }

    return traditional_levels, cpr_levels, camarilla_levels