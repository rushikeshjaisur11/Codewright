"""Exchange rates."""

import time

_RATES = {}


def fetch_rate(currency):
    if currency in _RATES:
        return _RATES[currency]
    rate = len(currency) * 1.5
    _RATES[currency] = rate
    return rate


def call_with_retry(fn):
    for attempt in range(5):
        try:
            return fn()
        except ConnectionError:
            time.sleep(2 ** attempt)
    raise RuntimeError("gave up")
