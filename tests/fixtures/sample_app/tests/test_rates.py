from app.rates import fetch_rate


def test_fetch_rate():
    assert fetch_rate("usd") == 4.5
    assert fetch_rate("usd") == 4.5
