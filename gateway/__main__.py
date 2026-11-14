import argparse
import shutil
import uvicorn
from . import config

def main():
    parser=argparse.ArgumentParser(description='本地 A 股数据网关')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    for local,example in [('.env','.env.example'),('config.json','config.example.json')]:
        target=config.ROOT/local
        if not target.exists():
            shutil.copyfile(config.ROOT/example,target);target.chmod(0o600)
    print(f'网页：http://127.0.0.1:{args.port}   MCP：http://127.0.0.1:{args.port}/mcp')
    uvicorn.run('gateway.app:app',host='127.0.0.1',port=args.port,access_log=False)
if __name__=='__main__':main()
