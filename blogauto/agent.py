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
            "使用语义化标签。\n"
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

    def extract_page_content(self, current_html: str) -> dict[str, str]:
        """从已有博客 HTML 中提取结构化内容（title/subtitle/date/content_html）。
        严格保留原文，用于风格改写时的内容迁移。
        """
        provider = create_provider(self.config)
        html_snippet = current_html[:18000]
        prompt = (
            "从以下博客 HTML 中提取结构化内容，返回 JSON。\n"
            "JSON 必须包含以下字段：\n"
            "  title: 文章标题（字符串）\n"
            "  subtitle: 文章副标题或简介（字符串，没有则填空字符串）\n"
            "  date: 发布日期（字符串，没有则填空字符串）\n"
            "  content_html: 文章正文 HTML。\n"
            "    提取规则：\n"
            "    - 只提取 <article> 或正文区域内的内容\n"
            "    - 不包含页面大标题(<h1>文章名</h1>)、副标题、日期、导航栏、页脚等页面框架元素\n"
            "    - 必须保留原文所有文字，一字不差\n"
            "    - 保留原有段落 HTML 标签（<p>、<h2>、<ul>、<code> 等）\n"
            "    - 严禁在 content_html 中包含任何 <style> 标签或 style= 内联属性\n"
            "    - 严禁在 content_html 中包含任何 <script> 标签\n"
            "严格禁止修改、增加或删除任何正文文字。\n"
            "只输出 JSON，不加任何解释和代码块标记。\n\n"
            f"HTML:\n{html_snippet}"
        )
        text = provider.chat(
            system_prompt="你是博客内容提取助手，严格保留原文内容，输出纯 JSON。",
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
