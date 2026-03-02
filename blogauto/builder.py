from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

from .agent import BlogAgent
from .config import BlogConfig
from .registry import list_frameworks, list_styles


@dataclass
class BuildResult:
    generated_posts: list[str]


class BlogBuilder:
    def __init__(self, config: BlogConfig) -> None:
        self.config = config
        self.agent = BlogAgent(config)

    def build(self) -> BuildResult:
        self._ensure_dirs()
        styles = list_styles(self.config.styles_dir)
        frameworks = list_frameworks(self.config.frameworks_dir)

        if self.config.selected_style not in styles:
            raise ValueError(f"样式不存在: {self.config.selected_style}")
        if self.config.selected_framework not in frameworks:
            raise ValueError(f"框架不存在: {self.config.selected_framework}")

        generated = []
        entries = []
        for slug, post_txt, meta in self._discover_posts():
            raw_text = post_txt.read_text(encoding="utf-8")
            post = self.agent.process(raw_text)

            out_dir = self.config.output_dir / "posts" / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            style_name = meta.get("style", "__default__")
            frame_name = meta.get("framework", "__default__")
            if style_name == "__default__" or style_name not in styles:
                style_name = self.config.selected_style
            if frame_name == "__default__" or frame_name not in frameworks:
                frame_name = self.config.selected_framework

            template = frameworks[frame_name].read_text(encoding="utf-8")
            style_href = "../../styles/" + f"{style_name}.css"
            rendered = template.format(
                title=post.title,
                blog_name="AI Blog",
                subtitle=post.subtitle,
                date=post.publish_date,
                content_html=post.html,
                style_href=style_href,
            )
            (out_dir / "index.html").write_text(rendered, encoding="utf-8")
            generated.append(slug)
            entries.append((slug, post.title, post.publish_date))

        self._write_home(entries)
        self._write_manifest(generated)
        return BuildResult(generated_posts=generated)

    def _write_home(self, entries: list[tuple[str, str, str]]) -> None:
        rows = "\n".join(
            f'<li><a href="posts/{slug}/index.html">{title}</a> <small>{date}</small></li>'
            for slug, title, date in entries
        )
        home = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AI Blog 首页</title>
  <link rel="stylesheet" href="styles/{self.config.selected_style}.css" />
</head>
<body>
  <main>
    <header><h1>AI Blog</h1><p>自动维护的主页</p></header>
    <article>
      <h2>文章列表</h2>
      <ul>
        {rows}
      </ul>
    </article>
  </main>
</body>
</html>
"""
        (self.config.output_dir / "index.html").write_text(home, encoding="utf-8")

    def _write_manifest(self, generated: list[str]) -> None:
        manifest = {
            "generated_posts": generated,
        }
        (self.config.output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure_dirs(self) -> None:
        self.config.content_dir.mkdir(parents=True, exist_ok=True)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def _discover_posts(self) -> list[tuple[str, Path, dict[str, str]]]:
        root_index = self.config.workspace / "index.json"
        discovered: list[tuple[str, Path, dict[str, str]]] = []
        seen: set[Path] = set()

        if root_index.exists():
            try:
                data = json.loads(root_index.read_text(encoding="utf-8"))
                for item in data.get("posts", []):
                    slug = item.get("slug", "").strip()
                    path_text = item.get("article_file", "").strip()
                    if not slug or not path_text:
                        continue
                    path = (self.config.workspace / path_text).resolve()
                    if path.exists() and path.is_file():
                        discovered.append((slug, path, item))
                        seen.add(path)
            except Exception:
                pass

        candidates = (
            list(self.config.content_dir.glob("**/my_blog.txt"))
            + list(self.config.content_dir.glob("**/myblog.txt"))
            + list(
            self.config.content_dir.glob("**/post.txt")
            )
        )
        for path in sorted(candidates):
            resolved = path.resolve()
            if resolved in seen:
                continue
            slug = path.parent.name
            discovered.append((slug, path, {}))
            seen.add(resolved)
        return discovered
