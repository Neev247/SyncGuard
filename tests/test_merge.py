from copy import deepcopy
from itertools import product

import pytest

from app.merge import three_way_merge

pytestmark = pytest.mark.local_only(reason="Calls the Python merge function, not an HTTP endpoint.")


@pytest.mark.parametrize("base,current,proposed", list(product(["a", "b", "c"], repeat=3)))
def test_complete_three_way_truth_table(base, current, proposed):
    before = {"title": base, "content": "original"}
    server = {"title": current, "content": "server content"}
    patch = {"title": proposed}
    originals = deepcopy((before, server, patch))
    result = three_way_merge(before, server, patch)
    conflict = proposed != base and current != base and proposed != current
    assert bool(result.conflicts) == conflict
    expected = current if proposed == base or conflict else proposed
    assert result.data == {"title": expected, "content": "server content"}
    assert (before, server, patch) == originals


def test_array_is_atomic_and_omitted_fields_survive():
    result = three_way_merge(
        {"tags": ["old"], "archived": False},
        {"tags": ["server"], "archived": True},
        {"tags": ["client"]},
    )
    assert list(result.conflicts) == ["tags"]
    assert result.data["archived"] is True
