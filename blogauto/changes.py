from __future__ import annotations

from datetime import datetime
from pathlib import Path
import webbrowser

from .scanner import ScanReport


def render_change_page(changes_dir: Path, report: ScanReport, open_browser: bool = True) -> Path:
    changes_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = changes_dir / f"changes-{stamp}.html"

    def render_items(items: list[str]) -> str:
        if not items:
            return "<li>无</li>"
        return "\n".join(f"<li>{item}</li>" for item in items)

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>变动目录 {stamp}</title>
</head>
<body>
  <main>
    <h1>本次提交变动目录</h1>
    <h2>新增</h2><ul>{render_items(report.added)}</ul>
    <h2>修改</h2><ul>{render_items(report.modified)}</ul>
    <h2>删除</h2><ul>{render_items(report.removed)}</ul>
  </main>
</body>
</html>
"""
    out.write_text(html, encoding="utf-8")

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out
