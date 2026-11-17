import asyncio
from unittest.mock import AsyncMock

from gateway import core, exploration


def test_precomputed_field_exploration_reuses_sdk_query(monkeypatch):
    query = AsyncMock(return_value={'data': []})
    discover = AsyncMock()
    monkeypatch.setattr(core, 'query', query)
    monkeypatch.setattr(exploration, 'invoke', discover)
    request = exploration.ExplorationRequest(
        dataset='technical_indicators', symbol='600000.SH',
        date='2026-11-13', name='ts_ma_qfq_20')
    result = asyncio.run(exploration.explore(request))
    assert result['status'] == 'existing'
    assert query.call_args.args[0].fields == ['ts_ma_qfq_20']
    discover.assert_not_called()


def test_precomputed_field_exploration_cannot_change_fixed_parameters(monkeypatch):
    import pytest
    query = AsyncMock()
    monkeypatch.setattr(core, 'query', query)
    request = exploration.ExplorationRequest(
        dataset='technical_indicators', symbol='600000.SH',
        date='2026-11-13', name='ts_ma_qfq_20', parameters={'n': 7})
    with pytest.raises(ValueError):
        asyncio.run(exploration.explore(request))
    query.assert_not_called()
