"""Production readiness: each check fires on the exact shape it names and stays quiet on look-alikes."""

import textwrap
from pathlib import Path

from codewright import cards, graph, stack
from codewright.intents import detect
from codewright.suggest import make_findings


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def found(tmp_path: Path, intent: str):
    return detect(graph.build(tmp_path), only={intent})


def test_n_plus_one_needs_the_loop_variable_in_the_query(tmp_path):
    write(
        tmp_path,
        {
            "app/q.py": """
            from sqlalchemy import select

            def bad(session, orders):
                for o in orders:
                    session.get(Customer, o.customer_id)

            def fine(session, orders):
                total = session.scalar(select(1))
                for o in orders:
                    print(o, total)

            def also_fine(session, ids):
                for i in ids:
                    session.execute(select(1))
            """
        },
    )
    (m,) = found(tmp_path, "n-plus-one-query")
    assert m.function.name == "bad" and m.data["orm"] == "sqlalchemy" and "in_(" in m.data["advice"]


def test_django_n_plus_one_suggests_prefetch(tmp_path):
    write(
        tmp_path,
        {
            "shop/views.py": """
            from django.db import models

            def view(orders):
                for o in orders:
                    Customer.objects.get(pk=o.customer_id)
            """
        },
    )
    (m,) = found(tmp_path, "n-plus-one-query")
    assert "select_related" in m.data["advice"]


def test_timeouts(tmp_path):
    write(
        tmp_path,
        {
            "app/h.py": """
            import urllib.request
            import httpx

            def a(u):
                return urllib.request.urlopen(u)

            def b(u):
                return urllib.request.urlopen(u, timeout=5)

            def c(u):
                return httpx.get(u, timeout=None)

            def d(u):
                return httpx.get(u)
            """
        },
    )
    got = {m.function.name: m.data["client"] for m in found(tmp_path, "http-no-timeout")}
    assert got == {"a": "urllib", "c": "httpx"}


def test_cache_on_method_and_unbounded_cache(tmp_path):
    write(
        tmp_path,
        {
            "app/c.py": """
            import functools
            from functools import cache, lru_cache

            class Repo:
                @functools.lru_cache
                def config(self):
                    return {}

                @staticmethod
                @lru_cache
                def pure(x):
                    return x

            @cache
            def per_user(user_id):
                return user_id

            @lru_cache(maxsize=256)
            def bounded(user_id):
                return user_id

            @cache
            def cold(x):
                return x

            def main():
                per_user(1)
                bounded(1)

            if __name__ == "__main__":
                main()
            """
        },
    )
    (m,) = found(tmp_path, "cache-on-method")
    assert m.function.name == "config" and m.data["no_args"] and "cached_property" in m.data["advice"]
    (u,) = found(tmp_path, "unbounded-cache")
    assert u.function.name == "per_user"  # `cold` isn't on a request or command path


def test_post_without_idempotency(tmp_path):
    write(
        tmp_path,
        {
            "app/api.py": """
            import stripe
            from fastapi import FastAPI, Header

            app = FastAPI()

            @app.post("/pay")
            def pay(session, amount: int):
                session.add(amount)
                stripe.Charge.create(amount=amount)

            @app.post("/pay2")
            def pay2(session, amount: int, idempotency_key: str = Header()):
                session.add(amount)
                stripe.Charge.create(amount=amount)

            @app.post("/note")
            def note(session, text: str):
                session.add(text)
            """
        },
    )
    (m,) = found(tmp_path, "post-without-idempotency")
    assert m.function.name == "pay" and m.data["effect"].startswith("stripe")


def test_pre_ping_autofix_skips_sqlite(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\nversion = "0"\ndependencies = ["sqlalchemy==2.0.30"]\n',
            "app/db.py": """
            from sqlalchemy import create_engine

            engine = create_engine("postgresql://db/x")
            local = create_engine("sqlite:///x.db")
            """,
        },
    )
    g = graph.build(tmp_path)
    (m,) = detect(g, only={"sqlalchemy-no-pre-ping"})
    (f,) = make_findings([m], stack.discover(tmp_path), g, cards.load())
    assert '+engine = create_engine("postgresql://db/x", pool_pre_ping=True)' in f.patch
