"""Isolated SDK calls: process termination enforces the configured wall timeout."""
import json
import sys
from . import config
from .providers import fetch

def main():
    request=json.load(sys.stdin);name=request['provider']
    try:
        p=config.load()['providers'][name]
        key=config.secrets().get(config.KEYS.get(name,''),'')
        result=fetch(name,request['query'],p,key)
        payload={'ok':True,'result':result}
    except Exception as exc:
        payload={'ok':False,'error':type(exc).__name__+': '+str(exc)[:500]}
    # Keep SDK diagnostic stdout separate from the machine-readable final record.
    print('\n__GATEWAY_RESULT__'+json.dumps(config.redact(payload),ensure_ascii=False,default=str,allow_nan=False))
if __name__=='__main__':main()
