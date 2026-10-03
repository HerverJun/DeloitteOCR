"""Offline human-review report, linked to exact evidence records and maps."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any


def render(report: dict[str, Any], target: Path) -> None:
    source = report["input"]
    preview = escape(source["preview_path"], quote=True)
    cards = []
    for index, item in enumerate(report["evidence"]):
        title = escape(item["tool"])
        status = escape(item["status"])
        lines = [f"<h2>{index + 1}. {title} <small>{status}</small></h2>",
                 f"<p>证据族：{escape(item['family'])} · 版本：{escape(str(item['version']))}"
                 f" · 耗时：{escape(str(item['elapsed_ms']))} ms</p>"]
        if item["reason"]:
            lines.append(f"<p>原因：{escape(item['reason'])}</p>")
        if item["map_path"]:
            heat = escape(item["map_path"], quote=True)
            lines.append(f'<div class="pair"><img src="{preview}" alt="归正预览">'
                         f'<img src="{heat}" alt="{title} 灰度热图"></div>')
        if item["regions"]:
            lines.append(f"<p>候选框（预览像素坐标）：{escape(str(item['regions']))}</p>")
        if item["metrics"]:
            lines.append("<details><summary>原始指标</summary><pre>" +
                         escape(str(item["metrics"])) + "</pre></details>")
        for limitation in item["limitations"]:
            lines.append(f"<p class='caution'>{escape(limitation)}</p>")
        cards.append("<section>" + "\n".join(lines) + "</section>")
    html = f"""<!doctype html><html lang="zh-Hans"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>图像取证线索复核</title>
<style>
body{{font:16px/1.55 system-ui,sans-serif;background:#f4f4f1;color:#202720;margin:0 auto;padding:2rem;max-width:1100px}}
h1{{font-size:2rem;letter-spacing:-.03em}}h2{{font-size:1.25rem}}small{{font-size:.7em;color:#5a645c;font-weight:400}}
section{{background:white;border:1px solid #d8ddd5;margin:1.4rem 0;padding:1.4rem}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;align-items:start}}
img{{width:100%;height:auto;image-rendering:auto;border:1px solid #ddd}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.caution{{color:#74503d}}.notice{{background:#f8eacc;padding:1rem}}
@media(max-width:680px){{.pair{{grid-template-columns:1fr}}body{{padding:1rem}}}}
</style><h1>图像取证线索复核</h1>
<p class="notice">仅供人工核查线索，不判定真伪。不同工具的热图强度不可相加，灰度不是篡改概率。冲突或低质量时可以无法判断。</p>
<p>原文件 SHA-256：<code>{escape(source['sha256'])}</code><br>格式：{escape(str(source['source_format']))}
 · 归正尺寸：{source['width']} × {source['height']}。坐标原点为左上角。</p>
<p>先核对预览、原文件与转发链，再逐项审阅；跳过或报错的项目不构成反证。</p>
{''.join(cards)}
<p>调用顺序及所有原始字段以同目录 <code>report.json</code> 为准。没有跨模型总分或自动裁决。</p>
</html>"""
    target.write_text(html, encoding="utf-8")
