"""The accuracy band, and whether a measurement can actually decide it.

The band exists because a topology only differs from a baseline in the middle
of the accuracy range. Above it every topology answers everything and collapses
into the baseline; below it the critic and the debate agents argue over noise.

Which is exactly why the band has to be decided with an interval and not a
point. The band is 25 points wide. A screen of 15 items has a 95 percent
interval about 45 points wide, so it cannot separate "in band" from "nowhere
near it" no matter what number comes out. Reporting a bare percentage against
the band invites a verdict the sample cannot support, so the verdict returned
here is UNRESOLVED whenever the interval straddles an edge.

Standard library only, Python 3.10 compatible.
"""

import math

BAND_LOW = 45.0
BAND_HIGH = 70.0

Z = 1.959963984540054


def wilson(correct, total, z=Z):
    """Wilson score interval in percent. Behaves at 0/n and n/n, unlike normal."""
    if total <= 0:
        return (0.0, 100.0)
    p = float(correct) / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denom
    half = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    return (100.0 * max(0.0, centre - half), 100.0 * min(1.0, centre + half))


def verdict(correct, total, low=BAND_LOW, high=BAND_HIGH):
    """Band membership, or UNRESOLVED when the sample cannot tell.

    Returns (verdict, point_estimate, lo, hi).
    """
    if total <= 0:
        return ("NO DATA", 0.0, 0.0, 100.0)
    point = 100.0 * correct / total
    lo, hi = wilson(correct, total)
    if lo >= low and hi <= high:
        return ("IN BAND", point, lo, hi)
    if lo > high:
        return ("ABOVE BAND, ceiling", point, lo, hi)
    if hi < low:
        return ("BELOW BAND, floor", point, lo, hi)
    return ("UNRESOLVED, n too small", point, lo, hi)


def n_for_halfwidth(halfwidth_pts, p=0.55, z=Z):
    """Items needed for a given half-width, at the least favourable p near 0.5."""
    h = halfwidth_pts / 100.0
    return int(math.ceil(z * z * p * (1.0 - p) / (h * h)))


def format_verdict(correct, total):
    """One column of text: point estimate, interval and verdict."""
    name, point, lo, hi = verdict(correct, total)
    return "%5.1f%% [%4.1f-%4.1f] (%d/%d)  %s" % (point, lo, hi, correct, total, name)


if __name__ == "__main__":
    print("band %.0f-%.0f%%, %.0f points wide" % (BAND_LOW, BAND_HIGH, BAND_HIGH - BAND_LOW))
    for n in (10, 15, 30, 80, 200, 400):
        lo, hi = wilson(int(round(0.55 * n)), n)
        print("  n=%-4d 55%% observed -> 95%% CI %4.1f-%4.1f  width %4.1f pts"
              % (n, lo, hi, hi - lo))
    for h in (12.5, 10, 5):
        print("  half-width +-%4.1f pts needs n=%d" % (h, n_for_halfwidth(h)))
