import math


class GreeksEngine:
    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def delta(spot: float, strike: float, time_to_expiry: float, rate: float, vol: float, option_type: str):
        if spot <= 0 or strike <= 0 or time_to_expiry <= 0 or vol <= 0:
            return 0.0

        d1 = (
            math.log(spot / strike)
            + (rate + 0.5 * vol * vol) * time_to_expiry
        ) / (vol * math.sqrt(time_to_expiry))

        if option_type.upper() == "C":
            return GreeksEngine._norm_cdf(d1)
        return GreeksEngine._norm_cdf(d1) - 1.0
