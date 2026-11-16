"""Render the human-readable dictionary from the executable JSON (no second source)."""
import argparse
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def render():
    d=json.loads((ROOT/'gateway/data/dictionary.json').read_text())
    out=['# 项目数据字典','', '由 `gateway/data/dictionary.json` 生成。该 JSON 同时驱动实际取数；不要直接编辑本表。','',
         f"现有 {sum(len(g['fields']) for g in d['datasets'].values())} 个分类字段："+'、'.join(f"{ds} {len(g['fields'])} 项" for ds,g in d['datasets'].items())+'。跨类别同名字段分开计数。空缺项明确保留，不等于来源永远不支持。','',
         '修改 JSON 后重启服务；运行 `python scripts/render_dictionary.py` 更新本表。各来源覆盖是当前实现配置，并非所有字段均经过全市场验证。','']
    for ds,g in d['datasets'].items():
        out += ['## '+ds,'',g['date_semantics'],'','| 字段 | 名称 | 单位 | 含义与单位说明 |','| --- | --- | --- | --- |']
        for f,m in g['fields'].items():out.append(f"| `{f}` | {m['label']} | {m['unit']} | {m['description']} {m.get('unit_note','')} |")
        out+=['','| 字段 | 来源 | 取数方案 → 方法 | 返回列／问句指标 | 转换／说明 |','| --- | --- | --- | --- | --- |']
        for f,m in g['fields'].items():
            for p,bindings in m['sources'].items():
                if not bindings:
                    out.append(f"| `{f}` | {p} | — | — | {m.get('mapping_notes',{}).get(p,m.get('unavailable',{}).get(p,'尚未确认同口径取法'))} |");continue
                for b in bindings:
                    r=d['recipes'][b['recipe']]
                    desc=' / '.join(b['columns']+b.get('extra_columns',[]))
                    if b.get('prompt'):desc+='；问句：'+b['prompt']
                    if b.get('indicator'):desc+='；指标参数：'+b['indicator']
                    notes=[f"原单位：{b.get('source_unit','按返回单位')}；×{b.get('multiplier',1)}"]
                    if b.get('unit_multipliers'):notes.append('按返回单位换算：'+json.dumps(b['unit_multipliers'],ensure_ascii=False))
                    if 'round_digits' in b:notes.append(f"取 {b['round_digits']} 位小数")
                    if r.get('fallback_only'):notes.append('主上游失败才调用')
                    if b.get('note'):notes.append(b['note'])
                    out.append(f"| `{f}` | {p} | `{b['recipe']}` → `{r['method']}` | {desc} | {'；'.join(notes)} |")
        out.append('')
    out+=['## 取数方案详情','','参数以 `$` 开头时从查询上下文读取，其他值原样传递。模板仅做字符串替换，不执行代码。','']
    for rid,r in d['recipes'].items():
        out+=['### '+rid,'','```json',json.dumps(r,ensure_ascii=False,indent=2),'```','']
    return '\n'.join(out)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--check',action='store_true');args=ap.parse_args()
    dest=ROOT/'docs/data-dictionary.md';text=render()
    if args.check:
        if not dest.exists() or dest.read_text()!=text:raise SystemExit('字典文档过期，请运行 python scripts/render_dictionary.py')
    else:dest.write_text(text)
