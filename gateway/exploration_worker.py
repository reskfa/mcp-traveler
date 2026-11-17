"""One isolated upstream discovery request; killed by caller on timeout."""
import json
import sys
from . import config
from .providers import Remote
from .acquisition import context,parameters

def main():
    try:
        payload=json.load(sys.stdin);name=payload['provider'];q=payload['request'];recipe=payload['recipe']
        if q['dataset']=='technical_indicators':
            from .technical import fetch,field_for,field_profile,normalized_parameters
            from .dictionary import fields
            field=field_for(q['name']);definition=fields(q['dataset'])[field]
            q['parameters']=normalized_parameters(q.get('parameters'),field_profile(field),definition.get('parameters'))
            result=fetch(Remote(name,config.load()['providers'][name],config.secrets()[config.KEYS[name]]),q,{field_for(q['name']):fields(q['dataset'])[field_for(q['name'])]['sources']['tdx'][0]},recipe)[1]
            print('__GATEWAY_RESULT__'+json.dumps({'ok':True,'result':result},ensure_ascii=False));return
        ctx=context(q,recipe,{})
        ctx['labels']=q['name'];ctx['question']=recipe['template'].format_map(ctx)
        result=Remote(name,config.load()['providers'][name],config.secrets()[config.KEYS[name]]).call(recipe['method'],parameters(recipe,ctx,q))
        answer={'ok':True,'result':result}
    except Exception as exc:answer={'ok':False,'error':config.redact(str(exc))}
    print('__GATEWAY_RESULT__'+json.dumps(answer,ensure_ascii=False))
if __name__=='__main__':main()
