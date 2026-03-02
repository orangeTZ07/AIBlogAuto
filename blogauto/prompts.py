from __future__ import annotations

from pathlib import Path


PROMPTS = {
    "ai-style": """
你是前端设计助手，请为博客生成一个完整CSS文件。
要求：
1) 仅输出CSS代码。
2) 必须兼容基础HTML结构：body/main/header/article/footer/a。
3) 风格需求：{style_goal}
4) 颜色、间距、字体请统一。
""".strip(),
    "ai-framework": """
你是模板助手，请生成博客HTML模板。
要求：
1) 必须保留占位符：{title} {blog_name} {subtitle} {date} {content_html} {style_href}
2) 使用语义化标签。
3) 仅输出HTML。
4) 框架目标：{framework_goal}
""".strip(),
    "ai-content": """
你是写作助手，请根据素材生成博客正文。
要求：
1) 输出中文纯文本，段落清晰。
2) 事实不确定时显式标注“待核实”。
3) 目标读者：{audience}
4) 主题：{topic}
""".strip(),
}


def write_prompt_files(prompts_dir: Path) -> None:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for name, body in PROMPTS.items():
        (prompts_dir / f"{name}.prompt.txt").write_text(body + "\n", encoding="utf-8")
