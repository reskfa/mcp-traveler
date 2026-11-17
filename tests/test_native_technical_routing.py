"""Native vendor profiles keep snapshot/history boundaries through public inputs."""
import pytest
from gateway.core import Query
from gateway.exploration import ExplorationRequest
from gateway.catalog import technical_profiles


def test_wind_snapshot_date_optional_but_history_required():
    q=Query(dataset='technical_indicators',symbol='600519.SH',fields=['wind_ma_20_native'])
    assert q.start_date is None
    assert technical_profiles()['wind_technical_snapshot']['temporal']=='snapshot'
    with pytest.raises(ValueError,match='历史技术指标'):
        Query(dataset='technical_indicators',symbol='600519.SH',fields=['kdj_k'])
    with pytest.raises(ValueError,match='历史技术指标'):
        Query(dataset='technical_indicators',symbol='600519.SH',fields=['wind_ma_20_native','kdj_k'])
    with pytest.raises(ValueError,match='历史技术指标'):
        Query(dataset='technical_indicators',symbol='600519.SH',fields=['wind_ma_20_native'],start_date='2026-11-13')


def test_snapshot_exploration_accepts_dictionary_id_without_dates():
    q=ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',name='wind_ma_20_native')
    assert q.start_date is None


def test_wind_history_requires_dates_and_exploration_reuses_query(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from gateway import exploration,core
    with pytest.raises(ValueError,match='历史技术指标'):
        Query(dataset='technical_indicators',symbol='600519.SH',fields=['wind_history_ma'])
    query=AsyncMock(return_value={'data':[]})
    discover=AsyncMock()
    monkeypatch.setattr(core,'query',query)
    monkeypatch.setattr(exploration,'invoke',discover)
    request=ExplorationRequest(dataset='technical_indicators',symbol='600519.SH',name='wind_history_ma',date='2026-11-13',parameters={'n':10},provider='wind')
    result=asyncio.run(exploration.explore(request))
    assert result['status']=='existing'
    assert query.call_args.args[0].fields==['wind_history_ma']
    assert query.call_args.args[0].parameters=={'n':10}
    discover.assert_not_called()
