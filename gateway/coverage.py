"""Separate a reusable acquisition recipe from completeness of this response."""
import json
import re


def coverage(raw, data):
    text=json.dumps(raw,ensure_ascii=False)
    partial=bool(re.search(r'以下为部分数据|仅[展示返回]+前\s*\d+|部分数据展示|数据已截断',text))
    counts=[int(n) for n in re.findall(r'(?:找到|共计|共)\s*(\d+)\s*条数据',text)]
    days=sorted({row['date'] for row in data if row.get('date')})
    return {'status':'partial' if partial else 'not_verified','source_reported_rows':max(counts) if counts else None,
            'returned_rows':len(data),'returned_dates':len(days),'first_date':days[0] if days else None,'last_date':days[-1] if days else None,
            'note':'上游明确只展示部分数据；当前结果不是完整时间序列。' if partial else '未校验完整交易日覆盖；取法成功不代表数据已取全。'}
