"""Round 3 local config.

Registers the ``live`` marker so ``pytest tests/round_3`` runs cleanly on its own — the
project runs under ``filterwarnings = ["error"]``, and an unknown marker is a warning, which
that policy turns into an error. The parent ``tests/conftest.py`` autouse socket block is
inherited unchanged: every test in this directory is offline by construction, and the one
``@pytest.mark.live`` test is *deselected* by default (see ``pytest_collection_modifyitems``)
rather than allowed to reach the network. The live path lives in ``scripts/round3_live.py``.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: hits a real model over the network; costs money"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``@pytest.mark.live`` tests unless ``--run-live`` is passed.

    The default ``pytest tests/round_3`` run must be offline and free. A live test is not
    *deleted* — it stays collectable and, when a key is present and ``--run-live`` is given,
    it runs — it is simply skipped by default so the offline suite never depends on a
    network or a credential.
    """
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test — pass --run-live and set OPENROUTER_API_KEY")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run @pytest.mark.live tests against the real model (costs money)",
    )
