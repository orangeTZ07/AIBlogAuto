from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
import json
import re

from .ai_providers import ProviderError, create_provider
from .config import BlogConfig


@dataclass
class AgentOutput:
    title: str
    subtitle: str
    html: str
    publish_date: str


class BlogAgent:
    """基于可切换 AI provider 的博客 agent。"""

    def __init__(self, config: BlogConfig) -> None:
        self.config = config

    def process(self, raw_text: str) -> AgentOutput:
        try:
            provider = create_provider(self.config)
        except Exception as exc:
            return self._fallback_output(raw_text, f"AI 未就绪，已降级本地生成: {exc}")

        prompt = self._build_prompt(raw_text)
        try:
            content = provider.chat(
                system_prompt="你是中文技术博客编辑。输出必须是 JSON，不要输出额外解释。",
                user_prompt=prompt,
                temperature=0.5,
            )
            parsed = self._parse_json_content(content)
            return AgentOutput(
                title=(parsed.get("title") or "未命名文章")[:80],
                subtitle=(parsed.get("subtitle") or "由 AI 生成")[:120],
                html=parsed.get("html") or self._paragraphs_to_html(raw_text),
                publish_date=date.today().isoformat(),
            )
        except Exception as exc:
            return self._fallback_output(raw_text, f"AI 调用失败，已降级生成: {exc}")

    def generate_style(self, style_goal: str) -> str:
        provider = create_provider(self.config)
        prompt = (
            "请生成一个完整 CSS 文件，只输出 CSS 代码。\n"
            "必须兼容 body/main/header/article/footer/a。\n"
            f"风格目标: {style_goal}"
        )
        text = provider.chat(
            system_prompt="你是前端设计助手。",
            user_prompt=prompt,
            temperature=0.6,
        )
        return self._strip_code_fence(text)

    def generate_framework(self, framework_goal: str) -> str:
        provider = create_provider(self.config)
        prompt = (
            "请生成博客 HTML 模板，只输出 HTML。\n"
            "必须保留占位符: {title} {blog_name} {subtitle} {date} {content_html} {style_href}\n"
            "使用语义化标签，可以根据需要加入合理的 <script> 以支持目录、高亮、阅读进度等交互。\n"
            "如果加入了 JS，请直接内联在 <script> 标签中。\n"
            f"目标: {framework_goal}"
        )
        text = provider.chat(
            system_prompt="你是网页模板助手。",
            user_prompt=prompt,
            temperature=0.5,
        )
        return self._strip_code_fence(text)

    def refine_asset(self, kind: str, current: str, feedback: str) -> str:
        provider = create_provider(self.config)
        if kind == "style":
            prompt = (
                "你在修改一个 CSS 文件。请根据反馈返回新的完整 CSS。\\n"
                "只输出 CSS，不要解释。\\n"
                f"当前版本:\\n{current}\\n\\n用户反馈:\\n{feedback}"
            )
            system = "你是前端设计助手。"
        else:
            prompt = (
                "你在修改一个博客 HTML 模板。请根据反馈返回新的完整 HTML。\\n"
                "必须保留占位符: {title} {blog_name} {subtitle} {date} {content_html} {style_href}\\n"
                "可以根据需要保留或新增合理的 <script>，用于目录、代码高亮、阅读进度等交互功能。\\n"
                "只输出 HTML，不要解释。\\n"
                f"当前版本:\\n{current}\\n\\n用户反馈:\\n{feedback}"
            )
            system = "你是网页模板助手。"
        text = provider.chat(system_prompt=system, user_prompt=prompt, temperature=0.4)
        return self._strip_code_fence(text)

    def generate_homepage(
        self,
        posts_json: str,
        directory_style: str,
        index_fields_prompt: str,
        framework_goal: str,
        style_name: str | None = None,
    ) -> str:
        provider = create_provider(self.config)
        prompt = (
            "请生成一个完整的博客首页 HTML，只输出 HTML。\\n"
            "首页必须包含“索引区块”，用于展示分类和路径。\\n"
            "你必须依据给定 JSON 数据组织结构。\\n"
            "目录样式偏好："
            + directory_style
            + "\\n框架目标："
            + framework_goal
            + "\\n\\n字段说明提示词:\\n"
            + index_fields_prompt
            + "\\n\\n索引 JSON:\\n"
            + posts_json
        )
        if style_name:
            prompt += self._build_homepage_style_prompt(style_name)
        text = provider.chat(
            system_prompt="你是网站信息架构师和前端助手，擅长生成结构清晰的首页。",
            user_prompt=prompt,
            temperature=0.5,
        )
        return self._strip_code_fence(text)

    def refine_homepage(
        self,
        posts_json: str,
        directory_style: str,
        index_fields_prompt: str,
        framework_goal: str,
        current_html: str,
        feedback: str,
        style_name: str | None = None,
    ) -> str:
        provider = create_provider(self.config)
        prompt = (
            "请基于当前主页 HTML 做修改，只输出完整 HTML。\\n"
            "你必须继续使用索引 JSON，并保持索引指向每篇文章 index.html。\\n"
            f"目录样式偏好：{directory_style}\\n"
            f"框架目标：{framework_goal}\\n"
            f"字段说明提示词:\\n{index_fields_prompt}\\n\\n"
            f"索引 JSON:\\n{posts_json}\\n\\n"
            f"当前 HTML:\\n{current_html}\\n\\n"
            f"用户修改意见:\\n{feedback}"
        )
        if style_name:
            prompt += self._build_homepage_style_prompt(style_name)
        text = provider.chat(
            system_prompt="你是网站信息架构师和前端助手，擅长增量修改首页。",
            user_prompt=prompt,
            temperature=0.4,
        )
        return self._strip_code_fence(text)

    def apply_css_to_html(self, html: str, css_content: str, style_href: str) -> str:
        """将目标 CSS 应用到 HTML：
        - 将 <link rel="stylesheet"> 的 href 设为 style_href
        - 根据 CSS 选择器调整 HTML 元素的 class/id，使样式得到更好的呈现
        - 不修改任何正文文字，不添加内联样式，不增删或重排现有框架结构标签
        - 模型可以“想象”对 CSS 做轻微美化性调整，但本函数只返回修改后的 HTML，不写回 CSS 文件
        """
        provider = create_provider(self.config)

        # 预处理：去掉已有 style/script，减少 token 占用和 AI 干扰
        clean_html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", html)
        clean_html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", clean_html)
        clean_html = re.sub(r"\n{3,}", "\n\n", clean_html).strip()

        css_snippet = css_content[:10000]
        html_snippet = clean_html[:12000]

        prompt = (
            "你的任务：作为一名网页美化大师，在保留正文文字不变的前提下，"
            "让下面这篇博客页面在给定 CSS 的加持下更精致、易读且层次清晰。\n\n"
            "【必须完成】\n"
            "1. 将 <head> 中 <link rel=\"stylesheet\"> 的 href 改为：" + style_href + "\n"
            "   （若不存在此标签，在 </head> 前插入）\n"
            "2. 仔细阅读给定 CSS，根据选择器含义（例如针对代码块、提示块、侧边栏、标题层级的样式），"
            "   为 HTML 元素添加或调整 class / id 属性，使这些样式得到更合理的应用。\n"
            "   可以适度重新分配哪些元素承担“主要内容区”“代码区”“侧栏”等角色，"
            "   但必须在现有标签骨架内完成（不能新增/删除结构标签）。\n\n"
            "【可以小幅度改写的范围】\n"
            "- 可以在理解 CSS 的基础上，假设对部分视觉细节（如间距、圆角、阴影）的规则做轻微优化，"
            "  并据此调整 HTML 的 class/id 使用方式，使整体效果更���观。\n"
            "- 但本步骤不负责修改 CSS 文件本身，实际输出只包含更新后的 HTML。\n\n"
            "【绝对禁止】\n"
            "- 修改任何文字内容（标题、正文、日期、链接文字等），一字不改\n"
            "- 增删或重排 HTML 框架结构标签（header/nav/main/article/aside/footer 等不得新增/删除/改层级）\n"
            "- 添加 <style> 标签或 style= 内联属性\n\n"
            "只输出完整 HTML，不加任何说明或代码块标记。\n\n"
            "CSS 内容（href=" + style_href + "）：\n" + css_snippet + "\n\n"
            "HTML：\n" + html_snippet
        )
        text = provider.chat(
            system_prompt=(
                "你是一名网页美化大师，擅长在不改变页面框架结构和正文文字的前提下，"
                "通过精细调整 HTML 的 class/id 与现有 CSS 的配合方式，显著提升视觉效果和可读性。"
                "你不会增删或重排结构标签，不会添加内联样式，也不会直接修改 CSS 文件内容。"
            ),
            user_prompt=prompt,
            temperature=0.4,
        )
        result = self._strip_code_fence(text)
        # 兜底：AI 返回空或明显错误时退回最简 href 替换
        if not result or len(result) < 50:
            fixed, n = re.subn(
                r'(<link\b[^>]*\brel=["\']stylesheet["\'][^>]*\bhref=["\'])[^"\']*(["\'][^>]*>)'
                r'|(<link\b[^>]*\bhref=["\'])[^"\']*(["\'][^>]*\brel=["\']stylesheet["\'][^>]*>)',
                lambda m: (
                    m.group(1) + style_href + m.group(2)
                    if m.group(1)
                    else m.group(3) + style_href + m.group(4)
                ),
                html,
                count=1,
            )
            if n == 0:
                fixed = html.replace(
                    "</head>",
                    f'  <link rel="stylesheet" href="{style_href}" />\n</head>',
                    1,
                )
            return fixed
        return result

    def extract_page_content(self, current_html: str) -> dict[str, str]:
        """从已有博客 HTML 中提取结构化内容（title/subtitle/date/content_html）。
        严格保留原文，用于风格改写时的内容迁移。
        """
        provider = create_provider(self.config)

        # 发送给 AI 前先剥掉 style/script/link，降低 token 用量并减少干扰
        clean_html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", current_html)
        clean_html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", clean_html)
        clean_html = re.sub(r"(?is)<link[^>]*/?>", "", clean_html)
        clean_html = re.sub(r"\n{3,}", "\n\n", clean_html).strip()

        original_len = len(current_html)
        html_snippet = clean_html[:14000]

        prompt = (
            "你的任务：从下面一篇已渲染的博客 HTML 页面中，只提取文章的正文内容片段。\n"
            "返回 JSON，包含以下 4 个字段：\n"
            "  title       文章标题字符串（通常是 <article> 内的 <h1> 或 <h2>）\n"
            "  subtitle    副标题或简介字符串（没有则填空字符串）\n"
            "  date        发布日期字符串（没有则填空字符串）\n"
            "  content_html 正文 HTML 片段\n"
            "\n"
            "【content_html 的严格规则】\n"
            "✓ 只能包含：<p> <h2> <h3> <h4> <ul> <ol> <li> <blockquote> <pre> <code>"
            " <table> <thead> <tbody> <tr> <th> <td> <strong> <em> <a> <br> <hr> 等正文标签\n"
            "✗ 绝对禁止出现：<html> <head> <body> <main> <header> <footer> <nav>"
            " <title> <meta> <link> <style> <script> 以及任何 style= 内联属性\n"
            "✗ 不要包含文章大标题（已单独用 title 字段存储）\n"
            "✗ 不要包含副标题行、日期行、页眉、页脚、导航栏\n"
            "\n"
            "【自检规则】\n"
            f"原始 HTML 总长度约 {original_len} 字符。"
            " 如果你的 content_html 长度超过原始 HTML 的 50%，说明你把页面框架结构也包进去了，必须重新缩小。\n"
            "\n"
            "只输出 JSON，不加任何说明、注释或代码块标记。\n\n"
            f"HTML:\n{html_snippet}"
        )
        text = provider.chat(
            system_prompt=(
                "你是博客正文提取助手。你的唯一职责是从完整 HTML 页面中找出"
                "文章正文片段并以 JSON 返回，绝不能把页面框架结构作为正文输出。"
            ),
            user_prompt=prompt,
            temperature=0.1,
        )
        try:
            return self._parse_json_content(text)
        except Exception:
            return {"title": "", "subtitle": "", "date": "", "content_html": ""}

    def _build_homepage_style_prompt(self, style_name: str) -> str:
        style_path = self.config.styles_dir / f"{style_name}.css"
        if not style_path.exists():
            return (
                f"\\n继续使用样式：styles/{style_name}.css。"
                "\\n注意：样式文件不存在时，请至少保留对应 <link> 标签。"
            )
        css = style_path.read_text(encoding="utf-8").strip()
        if len(css) > 12000:
            css = css[:12000] + "\\n/* ...truncated... */"
        return (
            f"\\n继续使用样式：styles/{style_name}.css。"
            "\\n你必须保证结构与该 CSS 选择器兼容。"
            "\\nCSS 内容如下：\\n```css\\n"
            + css
            + "\\n```"
        )

    def generate_post_summary(self, source_text: str, source_hint: str = "") -> str:
        cleaned = source_text.strip()
        if not cleaned:
            return ""
        try:
            provider = create_provider(self.config)
            hint_line = f"\n素材来源提示: {source_hint}" if source_hint else ""
            prompt = (
                "请为一篇博客文章生成简介（summary）。\n"
                "要求：\n"
                "1) 使用中文；\n"
                "2) 1-2 句话；\n"
                "3) 40-120 字；\n"
                "4) 信息准确，不要编造；\n"
                "5) 只输出简介文本，不要标题、前缀或解释。\n"
                f"{hint_line}\n\n"
                "文章素材如下：\n"
                f"{cleaned[:6000]}"
            )
            text = provider.chat(
                system_prompt="你是中文技术博客编辑，擅长提炼准确简介。",
                user_prompt=prompt,
                temperature=0.3,
            )
            return self._normalize_summary(self._strip_code_fence(text))
        except Exception:
            return self._fallback_summary(cleaned)

    def _build_prompt(self, raw_text: str) -> str:
        return (
            "请将下面素材整理为博客结构，返回 JSON。\n"
            "JSON 字段必须包含：title(字符串), subtitle(字符串), html(字符串)。\n"
            "html 字段必须是正文 HTML（<p>...</p>），不要包含 <html>/<body> 标签。\n"
            "素材如下：\n"
            f"{raw_text}"
        )

    def _parse_json_content(self, content: str) -> dict[str, str]:
        block = content.strip()
        if block.startswith("```"):
            match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", block, re.DOTALL)
            if match:
                block = match.group(1)
        return json.loads(block)

    def _strip_code_fence(self, text: str) -> str:
        block = text.strip()
        if block.startswith("```"):
            m = re.search(r"```(?:html|css)?\s*(.*?)\s*```", block, re.DOTALL)
            if m:
                return m.group(1).strip()
        return block

    def _normalize_summary(self, text: str) -> str:
        one_line = " ".join(text.strip().split())
        return one_line[:180]

    def _fallback_summary(self, source_text: str) -> str:
        plain = re.sub(r"<[^>]+>", " ", source_text)
        compact = " ".join(plain.split())
        return compact[:120]

    def _fallback_output(self, raw_text: str, subtitle_hint: str) -> AgentOutput:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        title = lines[0][:80] if lines else "未命名文章"
        return AgentOutput(
            title=title,
            subtitle=subtitle_hint,
            html=self._paragraphs_to_html(raw_text),
            publish_date=date.today().isoformat(),
        )

    def _paragraphs_to_html(self, raw_text: str) -> str:
        parts = []
        for block in raw_text.split("\n\n"):
            text = block.strip()
            if not text:
                continue
            parts.append(f"<p>{escape(text).replace(chr(10), '<br />')}</p>")
        return "\n".join(parts)
