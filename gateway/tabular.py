"""Shared CSV export and compact dataset `describe` summary for LLM/Agent consumers."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .parsing import norm_date, number

PREVIEW_ROW_LIMIT = 50
_SYMBOL_COL_RE = re.compile(r'^\d{6}\.(?:SH|SZ|BJ)$')


def preview_notice(shown: int, total: int, csv_path: str | None) -> str:
    loc = f'本地 CSV：{csv_path}' if csv_path else '本地结果文件'
    return f'【此处仅为前 {shown} 行预览，完整 {total} 行数据已保存在{loc}，请直接用 pandas 读取计算，切勿把预览当全量】'


def _infer_columns(rows: list[dict], columns: list[str] | None = None) -> list[str]:
    if columns:
        return list(columns)
    cols: list[str] = []
    for row in rows[:200]:
        for key in row.keys():
            if key not in cols:
                cols.append(key)
    return cols


def describe_rows(rows: list[dict], columns: list[str] | None = None, max_numeric_cols: int = 12) -> dict:
    cols = _infer_columns(rows, columns)
    row_count = len(rows)
    if not row_count:
        return {
            'row_count': 0,
            'col_count': len(cols),
            'columns': cols,
            'date_range': None,
            'unique_symbols': 0,
            'non_null_counts': {c: 0 for c in cols},
            'numeric_summary': {},
        }

    non_null = {c: 0 for c in cols}
    dates = set()
    symbols = set()
    numeric_vals: dict[str, list[float]] = {c: [] for c in cols[:max_numeric_cols * 2]}
    date_keys = {'date', 'trade_date', 'cal_date', 'end_date', '日期', '交易日期', '报告期'}
    sym_keys = {'symbol', 'ts_code', 'code', '证券代码', '股票代码', '债券代码', '代码', 'Wind代码'}

    for row in rows:
        for c in cols:
            val = row.get(c)
            if val is not None and val != '':
                non_null[c] += 1
                if c in date_keys:
                    d = norm_date(val)
                    if d:
                        dates.add(d)
                elif c in sym_keys:
                    symbols.add(str(val).strip())
                elif c in numeric_vals and not isinstance(val, (bool, list, dict)):
                    num = number(val)
                    if num is not None:
                        numeric_vals[c].append(num)

    numeric_summary = {}
    for c, vals in numeric_vals.items():
        if len(numeric_summary) >= max_numeric_cols:
            break
        if vals and len(vals) >= max(1, non_null.get(c, 0) // 2):
            numeric_summary[c] = {
                'count': len(vals),
                'min': round(min(vals), 4),
                'max': round(max(vals), 4),
                'mean': round(sum(vals) / len(vals), 4),
            }

    sorted_dates = sorted(dates)
    symbol_cols = [c for c in cols if _SYMBOL_COL_RE.fullmatch(str(c))]
    unique_syms = len(symbols) if symbols else len(symbol_cols)
    return {
        'row_count': row_count,
        'col_count': len(cols),
        'columns': cols,
        'date_range': [sorted_dates[0], sorted_dates[-1]] if sorted_dates else None,
        'unique_symbols': unique_syms,
        'non_null_counts': non_null,
        'numeric_summary': numeric_summary,
    }


def write_csv_and_describe(rows: list[dict], path: Path, columns: list[str] | None = None) -> tuple[str, dict]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = _infer_columns(rows, columns)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        if cols:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                    for k, v in row.items()
                })
    return str(path), describe_rows(rows, cols)
