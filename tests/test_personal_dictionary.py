import copy
import json
import pytest
from gateway import config, dictionary
from gateway.catalog import FIELDS, DEFAULT_FIELDS
from gateway.core import Query


@pytest.fixture
def personal_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    dictionary.reload_dictionary()
    yield tmp_path
    monkeypatch.undo()
    dictionary.reload_dictionary()


def metric(label='个人指标'):
    sources={p:[] for p in dictionary.DICTIONARY['providers']}
    sources['tushare']=[{'recipe':'ts_fina_indicator','columns':['custom_column'],'source_unit':'%','note':'已验证来源口径'}]
    return {'label':label,'unit':'%','description':'个人指标说明','aliases':['personal_ratio'],'sources':sources}


def test_save_reload_query_search_delete(personal_home):
    dictionary.save_user_metric('financials','personal_ratio',metric(),{})
    assert 'personal_ratio' in FIELDS['financials']
    assert 'personal_ratio' not in DEFAULT_FIELDS['financials']
    assert Query(dataset='financials',symbol='600000.SH',period='2025-12-31',fields=['personal_ratio']).fields == ['personal_ratio']
    for needle in ['个人指标','personal_ratio','custom_column']:
        found=dictionary.search_metrics(needle,'financials')
        assert found['results'][0]['field']=='personal_ratio'
        assert found['results'][0]['origin']=='user'
    assert dictionary.load_dictionary()['datasets']['financials']['fields']['personal_ratio']['sources']['tushare'][0]['note']=='已验证来源口径'
    assert dictionary.delete_user_metric('financials','personal_ratio')['deleted']
    assert 'personal_ratio' not in FIELDS['financials']
    with pytest.raises(ValueError):
        Query(dataset='financials',symbol='600000.SH',period='2025-12-31',fields=['personal_ratio'])


def test_preserves_entries_and_builtin(personal_home):
    dictionary.save_user_metric('financials','first',metric('第一项'),{})
    dictionary.save_user_metric('financials','second',metric('第二项'),{})
    assert dictionary.search_metrics('第一项')['total']==1
    built=copy.deepcopy(dictionary.DICTIONARY['datasets']['financials']['fields']['roe'])
    modified=copy.deepcopy(built);modified['label']='我的ROE'
    dictionary.save_user_metric('financials','roe',modified,{})
    dictionary.delete_user_metric('financials','roe')
    assert dictionary.DICTIONARY['datasets']['financials']['fields']['roe']==built
    assert dictionary.search_metrics('',limit=1)['total']>1
    assert len(dictionary.search_metrics('',limit=1)['results'])==1


@pytest.mark.parametrize('change', ['private','params','binding','unit','field'])
def test_rejects_unsafe_without_writing(personal_home,change):
    definition=metric();recipes={};field='unsafe'
    if change in ['private','params']:
        recipe=copy.deepcopy(dictionary.DICTIONARY['recipes']['ts_fina_indicator'])
        if change=='private':recipe['method']='_private'
        else:recipe['params']['url']='https://evil.example'
        recipes={'unapproved':recipe}
        definition['sources']['tushare'][0]['recipe']='unapproved'
    elif change=='binding':definition['sources']['tushare'][0]['recipe']='ak_daily'
    elif change=='unit':definition['unit']=''
    else:field='../escape'
    with pytest.raises(ValueError):dictionary.save_user_metric('financials',field,definition,recipes)
    assert not dictionary.user_dictionary_path().exists()


def test_external_file_reload(personal_home):
    user={'schema_version':1,'datasets':{'financials':{'fields':{'external_metric':metric()}}},'recipes':{}}
    path=dictionary.user_dictionary_path();path.parent.mkdir(parents=True)
    path.write_text(json.dumps(user))
    assert Query(dataset='financials',symbol='600000.SH',period='2025-12-31',fields=['external_metric']).fields==['external_metric']


def test_approved_exploration_template(personal_home):
    recipe=copy.deepcopy(dictionary.DICTIONARY['recipes']['wind_financials'])
    recipe['template']=dictionary.EXPLORATION_TEMPLATE
    recipe['qualifier']=dictionary.EXPLORATION_QUALIFIER
    definition=metric()
    definition['sources']['tushare']=[]
    definition['sources']['wind']=[{'recipe':'personal_wind','columns':['个人指标'],'prompt':'个人指标'}]
    dictionary.save_user_metric('financials','explored',definition,{'personal_wind':recipe})
    recipe['params']['question']='https://evil.example'
    with pytest.raises(ValueError):
        dictionary.save_user_metric('financials','explored',definition,{'personal_wind':recipe})
    assert dictionary.DICTIONARY['recipes']['personal_wind']['params']['question']=='$question'
