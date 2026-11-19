# SPDX-License-Identifier: MIT

"""kubectl's AGE format (GK-4.6): seconds under 2 minutes, minutes and seconds under 10,
minutes, hours and minutes under 3 hours, hours, days."""

import pytest

from gluon_kube.status import age


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (-1, "0s"),
        (5, "5s"),
        (119, "119s"),
        (125, "2m5s"),
        (599, "9m59s"),
        (900, "15m"),
        (3 * 3600 - 1, "2h59m"),
        (5 * 3600, "5h"),
        (50 * 3600, "2d"),
    ],
)
def test_age(seconds: float, shown: str) -> None:
    assert age(seconds) == shown
