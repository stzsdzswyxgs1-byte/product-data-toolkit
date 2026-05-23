# -*- coding: utf-8 -*-
"""
說明模板處理器
批量將模板內容插入到說明列 (前面或後面)
"""
import pandas as pd
from typing import Callable

LogFn = Callable[[str], None]


def apply_desc_append(df: pd.DataFrame, template_content: str,
                      color: str = '', font_family: str = '',
                      font_size: str = '', position: str = 'before',
                      log_fn: LogFn = print) -> dict:
    """
    將模板內容插入到說明列
    template_content: 模板文字內容
    color: 顏色hex (如 '#FF0000'), 空=不設顏色
    font_family: 字體名稱 (如 '微軟正黑體'), 空=不設字體
    font_size: 字號px (如 '14'), 空=不設字號
    position: 'before'=插在說明前面, 'after'=插在說明後面
    返回統計 dict
    """
    stats = {'total': 0, 'appended': 0}

    if '說明' not in df.columns:
        log_fn("[警告] 無說明欄位, 跳過說明模板")
        return stats

    if not template_content.strip():
        log_fn("[警告] 模板內容為空, 跳過")
        return stats

    # 構建 CSS 樣式
    styles = []
    if color and color.strip():
        styles.append(f'color: {color.strip()}')
    if font_family and font_family.strip():
        styles.append(f"font-family: '{font_family.strip()}'")
    if font_size and font_size.strip():
        styles.append(f'font-size: {font_size.strip()}px')

    # 構建 HTML: 換行→<br>
    html_content = template_content.replace('\n', '<br>\n')

    if styles:
        style_str = '; '.join(styles)
        html_block = f'<div style="{style_str};">\n{html_content}\n</div>'
    else:
        html_block = html_content

    pos_label = '前面' if position == 'before' else '後面'
    log_fn(f"  模板插入位置: 說明{pos_label}")
    if font_family:
        log_fn(f"  字體: {font_family}")
    if font_size:
        log_fn(f"  字號: {font_size}px")
    if color:
        log_fn(f"  顏色: {color}")

    for idx in df.index:
        existing = str(df.at[idx, '說明']) if pd.notna(df.at[idx, '說明']) else ''
        stats['total'] += 1

        if position == 'before':
            if existing.strip():
                df.at[idx, '說明'] = html_block + '\n<br><br>\n' + existing
            else:
                df.at[idx, '說明'] = html_block
        else:
            if existing.strip():
                df.at[idx, '說明'] = existing + '\n<br><br>\n' + html_block
            else:
                df.at[idx, '說明'] = html_block
        stats['appended'] += 1

    log_fn(f"說明模板追加完成: 共{stats['total']}條, 已插入{stats['appended']}條 (說明{pos_label})")
    return stats
