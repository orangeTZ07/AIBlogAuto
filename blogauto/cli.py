from __future__ import annotations

import argparse
import curses
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date_cls
from datetime import datetime
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
import json
import os
import re
import unicodedata
import threading

from .builder import BlogBuilder
from .changes import render_change_page
from .agent import BlogAgent
from .config import BlogConfig, load_config, save_config
from .prompts import write_prompt_files
from .registry import (
    BUILTIN_FRAMEWORKS,
    BUILTIN_STYLES,
    list_frameworks,
    list_styles,
    write_builtins,
)
from .scanner import DirectoryScanner
from .template_utils import render_template_placeholders
from .ai_providers import create_provider

UPSTREAM_REPO_URL = "https://github.com/orangeTZ07/AIBlogAuto.git"


def _default_editor_cmd() -> str:
    return (shutil.which("code") and "code") or "nano"


def _default_file_manager_cmd() -> str:
    if sys.platform == "darwin":
        return "open"
    if sys.platform.startswith("win"):
        return "explorer"
    return "xdg-open"


def _provider_env_var(provider: str) -> str:
    return {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "AI_API_KEY",
    }.get(provider, "AI_API_KEY")


def _secret_file_path(cfg: BlogConfig) -> Path:
    return cfg.workspace / cfg.ai_secret_file


def _save_secret_key(cfg: BlogConfig, provider: str, api_key: str) -> Path:
    path = _secret_file_path(cfg)
    data = {"providers": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data.get("providers"), dict):
                data["providers"] = {}
        except Exception:
            data = {"providers": {}}
    data["providers"][provider] = {
        "api_key": api_key,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    _ensure_gitignore(path.parent, path.name)
    return path


def _ensure_gitignore(workspace: Path, filename: str) -> None:
    gitignore = workspace / ".gitignore"
    line = filename.strip()
    if not line:
        return
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if line in {x.strip() for x in content.splitlines()}:
            return
        with gitignore.open("a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            f.write(line + "\n")
        return
    gitignore.write_text(line + "\n", encoding="utf-8")


def _run_open_command(command: str, target: Path, wait: bool = False) -> None:
    argv = shlex.split(command.strip()) + [str(target)]
    if wait:
        subprocess.run(argv, check=True)
    else:
        subprocess.Popen(argv)


def _run_git_capture(
    repo_dir: Path, args: list[str], timeout: float = 4.0
) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _detect_update_notice() -> str | None:
    if shutil.which("git") is None:
        return None
    repo_dir = Path(__file__).resolve().parents[1]
    if _run_git_capture(repo_dir, ["rev-parse", "--is-inside-work-tree"]) != "true":
        return None

    branch = _run_git_capture(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        return None

    fetch_source = UPSTREAM_REPO_URL
    origin_url = _run_git_capture(repo_dir, ["remote", "get-url", "origin"])
    if origin_url:
        fetch_source = "origin"

    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "--depth=1", fetch_source, branch],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return None

    counts = _run_git_capture(
        repo_dir, ["rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"]
    )
    if not counts:
        return None
    parts = counts.split()
    if len(parts) != 2:
        return None
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return None

    if behind <= 0:
        return None
    if ahead > 0:
        return (
            f"检测到新版本：当前分支落后 {behind} 个提交（本地超前 {ahead} 个提交）。"
        )
    return f"检测到新版本：当前分支落后 {behind} 个提交，建议执行 git pull。"


def _migrate_index_if_needed(workspace: Path, cfg: "BlogConfig") -> None:
    """将旧版 workspace/index.json 迁移到 content_dir/index.json（如有）。"""
    old_path = workspace / "index.json"
    new_path = cfg.index_path
    if old_path.exists() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(old_path.read_text(encoding="utf-8"), encoding="utf-8")
        old_path.unlink()


def _upsert_index_entry(index_path: Path, entry: dict[str, str]) -> None:
    data = {"posts": []}
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if "posts" not in data or not isinstance(data["posts"], list):
                data["posts"] = []
        except Exception:
            data = {"posts": []}

    posts = [p for p in data["posts"] if p.get("slug") != entry["slug"]]
    posts.append(entry)
    data["posts"] = sorted(posts, key=lambda x: x.get("slug", ""))
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _display_path(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _store_path(workspace: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(workspace.resolve()))
    except ValueError:
        return str(resolved)


def _validate_content_dir(path_text: str) -> Path:
    raw = path_text.strip()
    if not raw:
        raise ValueError("路径不能为空。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("content 目录必须是绝对路径。")
    normalized = path.resolve()
    if normalized.name != "content":
        raise ValueError("路径必须以 /content/ 结尾。")
    return normalized


def _infer_page_url_for_entry(
    workspace: Path, content_dir: Path, entry: dict[str, str]
) -> str:
    article_file = str(entry.get("article_file", "")).strip()
    draft_dir = str(entry.get("draft_dir", "")).strip()
    existing = str(entry.get("page_url", "")).strip()
    slug = str(entry.get("slug", "")).strip() or "custom"

    def _to_content_relative(path: Path) -> str | None:
        try:
            rel = path.relative_to(content_dir)
            if not rel.parts:
                return None
            return (rel / "index.html").as_posix()
        except Exception:
            return None

    if article_file and article_file != "Custom":
        rel = _to_content_relative((workspace / article_file).resolve().parent)
        if rel:
            return rel
    if draft_dir and draft_dir != "Custom":
        rel = _to_content_relative((workspace / draft_dir).resolve())
        if rel:
            return rel
    if existing and existing.endswith("/index.html"):
        return existing.lstrip("/")
    return f"{slug}/index.html"


def _is_usable_index_value(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and text != "Custom"


def _extract_summary_input(raw_text: str, source_name: str) -> str:
    text = raw_text.strip()
    if source_name.endswith(".html"):
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
    return " ".join(text.split())[:8000]


def _resolve_post_directory(
    workspace: Path, content_dir: Path, entry: dict
) -> Path | None:
    for key in ("article_file", "draft_dir"):
        value = str(entry.get(key, "")).strip()
        if not _is_usable_index_value(value):
            continue
        p = (workspace / value).resolve()
        target_dir = p.parent if key == "article_file" else p
        if target_dir.exists() and target_dir.is_dir():
            return target_dir

    page_url = str(entry.get("page_url", "")).strip()
    if page_url:
        page_path = (content_dir / page_url).resolve()
        if page_path.exists():
            return page_path.parent
    return None


def _find_summary_source_file(
    workspace: Path, content_dir: Path, entry: dict
) -> tuple[Path | None, str]:
    target_dir = _resolve_post_directory(workspace, content_dir, entry)
    if target_dir is None:
        return None, ""
    draft_file = target_dir / "my_blog.txt"
    if draft_file.exists():
        return draft_file, "my_blog.txt"
    page_file = target_dir / "index.html"
    if page_file.exists():
        return page_file, "index.html"
    return None, ""


def init_workspace(
    workspace: Path,
    open_preview: bool = True,
    selected_style: str | None = None,
    selected_framework: str | None = None,
    ai_provider: str | None = None,
    ai_key_source: str | None = None,
    ai_model: str | None = None,
    ai_base_url: str | None = None,
    content_dir: Path | None = None,
) -> BlogConfig:
    cfg = BlogConfig(workspace=workspace)
    if content_dir is not None:
        cfg.content_dir_path = str(content_dir.resolve())
    workspace.mkdir(parents=True, exist_ok=True)
    cfg.content_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.changes_dir.mkdir(exist_ok=True)

    write_builtins(cfg.styles_dir, cfg.frameworks_dir)
    write_prompt_files(cfg.prompts_dir)
    write_preview(cfg, open_preview=open_preview)

    style_names = list(list_styles(cfg.styles_dir).keys())
    frame_names = list(list_frameworks(cfg.frameworks_dir).keys())

    if selected_style and selected_style in style_names:
        cfg.selected_style = selected_style
    if selected_framework and selected_framework in frame_names:
        cfg.selected_framework = selected_framework

    if ai_provider:
        cfg.ai_provider = ai_provider
    if ai_key_source:
        cfg.ai_key_source = ai_key_source
    if ai_model:
        cfg.ai_model = ai_model
    if ai_base_url:
        cfg.ai_base_url = ai_base_url

    cfg.deepseek_model = cfg.ai_model
    cfg.deepseek_base_url = cfg.ai_base_url

    if not cfg.default_editor:
        cfg.default_editor = _default_editor_cmd()
    if not cfg.default_file_manager:
        cfg.default_file_manager = _default_file_manager_cmd()

    save_config(cfg)
    seed_example(cfg)
    return cfg


def write_preview(cfg: BlogConfig, open_preview: bool = True) -> None:
    cfg.previews_dir.mkdir(parents=True, exist_ok=True)
    styles = list_styles(cfg.styles_dir)
    frames = list_frameworks(cfg.frameworks_dir)
    example_framework = cfg.frameworks_dir / "example.html"
    if not example_framework.exists():
        example_framework.write_text(
            """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
  <link rel="stylesheet" href="{style_href}" />
</head>
<body>
  <main>
    <header><h1>{blog_name}</h1><p>{subtitle}</p></header>
    <article>
      <h2>{title}</h2>
      <small>{date}</small>
      <div>{content_html}</div>
    </article>
    <footer><p>preview</p></footer>
  </main>
</body>
</html>
""",
            encoding="utf-8",
        )

    cards = []
    example_tpl = example_framework.read_text(encoding="utf-8")
    for name, style_path in styles.items():
        preview_file = cfg.previews_dir / f"style-{name}.html"
        style_rel = os.path.relpath(style_path, preview_file.parent).replace("\\", "/")
        preview_file.write_text(
            render_template_placeholders(
                example_tpl,
                title=f"样式预览 {name}",
                blog_name="Style Preview",
                subtitle="示例框架 example.html + 当前样式",
                date="today",
                content_html=(
                    "<p>这是样式预览页。</p>"
                    "<p>如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。</p>"
                ),
                style_href=style_rel,
            ),
            encoding="utf-8",
        )
        cards.append(
            f'<li><a href="style-{name}.html" target="_blank">样式预览: {name}（应用 example.html）</a></li>'
        )

    for name, frame_path in frames.items():
        preview_file = cfg.previews_dir / f"framework-{name}.html"
        tpl = frame_path.read_text(encoding="utf-8")
        rendered = render_template_placeholders(
            tpl,
            title=f"框架预览 {name}",
            blog_name="Framework Preview",
            subtitle="本 html 没有使用任何样式(css)",
            date="today",
            content_html="<p>这是框架结构预览，不使用任何样式。</p>",
            style_href="",
        ).replace('<link rel="stylesheet" href="" />', "")
        rendered = rendered.replace(
            "</body>",
            (
                "<p style='padding:16px;'>本html没有使用任何样式(css)</p>"
                "<p style='padding:0 16px;'>如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。</p>"
                "</body>"
            ),
        )
        preview_file.write_text(rendered, encoding="utf-8")
        cards.append(
            f'<li><a href="framework-{name}.html" target="_blank">框架预览: {name}（无 CSS）</a></li>'
        )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AIBlogAuto 效果预览</title>
</head>
<body>
  <main>
    <h1>内置效果预览入口</h1>
    <p>可先查看样式和框架文件，再在终端中选择默认组合。</p>
    <p><strong>如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。</strong></p>
    <ul>{"".join(cards)}</ul>
  </main>
</body>
</html>
"""
    out = cfg.previews_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    if open_preview:
        webbrowser.open(out.resolve().as_uri())


def seed_example(cfg: BlogConfig) -> None:
    draft_dir = cfg.content_dir / "welcome"
    draft_dir.mkdir(parents=True, exist_ok=True)
    article = draft_dir / "my_blog.txt"
    prompt_file = draft_dir / "prompt.txt"

    if not article.exists():
        article.write_text(
            "清空本文件内容后写入你的文章的主要内容（AI会自动扩充你的文章）（你可以在这里面向AI提出要求和注意事项）。",
            encoding="utf-8",
        )
    if not prompt_file.exists():
        prompt_file.write_text(
            "请读取同目录 my_blog.txt，生成博客页面内容，保留段落结构并输出 HTML 正文。",
            encoding="utf-8",
        )
    _upsert_index_entry(
        cfg.index_path,
        {
            "slug": "welcome",
            "summary": "",
            "category": "default",
            "draft_dir": _store_path(cfg.workspace, draft_dir),
            "article_file": _store_path(cfg.workspace, article),
            "prompt_file": _store_path(cfg.workspace, prompt_file),
            "page_url": "welcome/index.html",
            "style": "__default__",
            "framework": "__default__",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    _ensure_homepage_prompts(cfg)


def _ensure_homepage_prompts(cfg: BlogConfig) -> None:
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.prompts_dir / "homepage-index-fields.prompt.txt"
    summary_line = "- summary: 文章简介（建议 1-2 句）\\n"
    if not p.exists():
        p.write_text(
            "".join(
                [
                    "你要根据以下字段构建主页索引：\\n",
                    "- slug: 博客短名\\n",
                    summary_line,
                    "- category: 分类\\n",
                    "- draft_dir: 原始草稿路径\\n",
                    "- article_file: 内容文件路径\\n",
                    "- page_url: 文章页面路径（必须精确指向每篇文章的 index.html）\\n",
                    "- style/framework: 文章选择的样式和框架（__default__ 表示使用默认）\\n",
                    "请按用户提供的目录风格偏好组织索引区块。\\n",
                ]
            ),
            encoding="utf-8",
        )
    else:
        content = p.read_text(encoding="utf-8")
        if summary_line.strip() not in content:
            marker = "- slug: 博客短名\\n"
            if marker in content:
                content = content.replace(marker, marker + summary_line, 1)
            else:
                content = content.rstrip() + "\\n" + summary_line
            p.write_text(content, encoding="utf-8")
    p_style = cfg.prompts_dir / "homepage-directory-style.prompt.txt"
    if not p_style.exists():
        p_style.write_text("按分类分组并按创建时间倒序", encoding="utf-8")
    p_framework = cfg.prompts_dir / "homepage-framework.prompt.txt"
    if not p_framework.exists():
        p_framework.write_text(
            "信息密度高，左侧分类导航，右侧文章索引", encoding="utf-8"
        )


def _ensure_homepage_stylesheet_link(html: str, style_name: str | None) -> str:
    if not style_name:
        return html
    href = f"styles/{style_name}.css"
    if re.search(rf'href=["\']{re.escape(href)}["\']', html, re.IGNORECASE):
        return html
    link = f'  <link rel="stylesheet" href="{href}" />'
    if re.search(r"</head>", html, re.IGNORECASE):
        return re.sub(
            r"</head>", link + "\n</head>", html, count=1, flags=re.IGNORECASE
        )
    return link + "\n" + html


def _rewrite_homepage_css_href_for_preview(html: str, cfg: BlogConfig) -> str:
    def _replace(match: re.Match[str]) -> str:
        quote = match.group(1)
        style_name = match.group(2)
        css_path = cfg.styles_dir / f"{style_name}.css"
        if not css_path.exists():
            return match.group(0)
        rel = os.path.relpath(css_path, cfg.previews_dir).replace("\\", "/")
        return f"href={quote}{rel}{quote}"

    return re.sub(
        r'href=(["\'])styles/([a-zA-Z0-9._-]+)\.css\1',
        _replace,
        html,
        flags=re.IGNORECASE,
    )


def cmd_build_homepage_with_ai(
    workspace: Path,
    directory_style: str,
    framework_goal: str,
    style_name: str | None = None,
    quiet: bool = False,
) -> Path:
    cfg = load_config(workspace)
    _ensure_homepage_prompts(cfg)
    index_path = cfg.index_path
    if not index_path.exists():
        raise FileNotFoundError("未找到 index.json，先创建文章草稿再生成主页。")
    posts_json = index_path.read_text(encoding="utf-8")
    fields_prompt = (cfg.prompts_dir / "homepage-index-fields.prompt.txt").read_text(
        encoding="utf-8"
    )
    agent = BlogAgent(cfg)
    homepage_html = agent.generate_homepage(
        posts_json=posts_json,
        directory_style=directory_style,
        index_fields_prompt=fields_prompt,
        framework_goal=framework_goal,
        style_name=style_name,
    )
    homepage_html = _ensure_homepage_stylesheet_link(homepage_html, style_name)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / "index.html"
    out.write_text(homepage_html, encoding="utf-8")
    if not quiet:
        print(f"主页已生成: {out}")
    return out


def cmd_build(workspace: Path, quiet: bool = False) -> dict[str, int]:
    cfg = load_config(workspace)
    report = DirectoryScanner(workspace).scan_content(cfg.content_dir)
    builder = BlogBuilder(cfg)
    result = builder.build()
    summary = {
        "generated_posts": len(result.generated_posts),
        "added": len(report.added),
        "modified": len(report.modified),
        "removed": len(report.removed),
    }
    if not quiet:
        print(f"构建完成，文章数量: {summary['generated_posts']}")
        print(
            f"扫描结果: 新增{summary['added']} 修改{summary['modified']} 删除{summary['removed']}"
        )
    return summary


def cmd_submit(
    workspace: Path, message: str, no_open: bool, quiet: bool = False
) -> Path:
    cfg = load_config(workspace)
    scanner = DirectoryScanner(workspace)
    pre_report = scanner.scan_content(cfg.content_dir)

    builder = BlogBuilder(cfg)
    builder.build()

    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=True)

    change_page = render_change_page(
        cfg.changes_dir, pre_report, open_browser=not no_open
    )
    if not quiet:
        print(f"提交完成，变动页: {change_page}")
    return change_page


def cmd_set_theme(
    workspace: Path, style: str | None, framework: str | None, quiet: bool = False
) -> None:
    cfg = load_config(workspace)
    styles = list_styles(cfg.styles_dir)
    frames = list_frameworks(cfg.frameworks_dir)

    if style:
        if style not in styles:
            raise ValueError(f"样式不存在: {style}")
        cfg.selected_style = style
    if framework:
        if framework not in frames:
            raise ValueError(f"框架不存在: {framework}")
        cfg.selected_framework = framework

    save_config(cfg)
    if not quiet:
        print("默认样式/框架已更新（只影响后续生成的页面）")


def cmd_set_open_commands(
    workspace: Path,
    editor_cmd: str | None = None,
    file_manager_cmd: str | None = None,
    reset: bool = False,
    quiet: bool = False,
) -> BlogConfig:
    cfg = load_config(workspace)
    if reset:
        cfg.default_editor = ""
        cfg.default_file_manager = ""
    if editor_cmd is not None:
        cfg.default_editor = editor_cmd.strip()
    if file_manager_cmd is not None:
        cfg.default_file_manager = file_manager_cmd.strip()
    save_config(cfg)
    if not quiet:
        print(
            "默认外部工具命令已更新: "
            f"editor={cfg.default_editor or _default_editor_cmd()} "
            f"manager={cfg.default_file_manager or _default_file_manager_cmd()}"
        )
    return cfg


def cmd_add_style(
    workspace: Path, name: str, css_file: Path, quiet: bool = False
) -> Path:
    cfg = load_config(workspace)
    target = cfg.styles_dir / f"{name}.css"
    target.write_text(css_file.read_text(encoding="utf-8"), encoding="utf-8")
    if not quiet:
        print(f"已添加 AI 定制样式: {target}")
    return target


def cmd_new_post(
    workspace: Path,
    slug: str,
    relative_path_suffix: str,
    category: str,
    style_choice: str,
    framework_choice: str,
    quiet: bool = False,
) -> dict[str, Path]:
    cfg = load_config(workspace)
    relative = relative_path_suffix.strip().strip("/")
    if not relative:
        relative = slug

    draft_dir = cfg.content_dir / relative
    article = draft_dir / "my_blog.txt"
    prompt_file = draft_dir / "prompt.txt"

    draft_dir.mkdir(parents=True, exist_ok=True)
    if not article.exists():
        article.write_text(
            "清空本文件内容后写入你的文章（你可以在这里面向AI提出要求和注意事项）。",
            encoding="utf-8",
        )
    style_path = (
        _display_path(cfg.styles_dir / f"{style_choice}.css", cfg.workspace)
        if style_choice != "__default__"
        else f"默认样式（由配置 selected_style 决定，目录: {_display_path(cfg.styles_dir, cfg.workspace)}）"
    )
    framework_path = (
        _display_path(cfg.frameworks_dir / f"{framework_choice}.html", cfg.workspace)
        if framework_choice != "__default__"
        else f"默认框架（由配置 selected_framework 决定，目录: {_display_path(cfg.frameworks_dir, cfg.workspace)}）"
    )
    prompt_file.write_text(
        (
            "你是一个专业的博客美化师，博客制作者，你擅长模仿作者语气并扩充博客。\n"
            "请阅读同目录下 my_blog.txt，并生成 index.html（博客页面）。\n"
            "请优先遵循本目录提示词，再生成内容。\n"
            f"样式来源: {style_path}\n"
            f"框架来源: {framework_path}\n"
            f"如果你无法访问这些内容，请让你的调用者确保你能够访问 {_display_path(cfg.styles_dir, cfg.workspace)} 和 {_display_path(cfg.frameworks_dir, cfg.workspace)}。"
        ),
        encoding="utf-8",
    )

    _upsert_index_entry(
        cfg.index_path,
        {
            "slug": slug,
            "summary": "",
            "category": category or "default",
            "draft_dir": _store_path(cfg.workspace, draft_dir),
            "article_file": _store_path(cfg.workspace, article),
            "prompt_file": _store_path(cfg.workspace, prompt_file),
            "page_url": f"{relative}/index.html",
            "style": style_choice,
            "framework": framework_choice,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    if not quiet:
        print(f"已准备文章草稿: {article}")

    return {
        "draft_dir": draft_dir,
        "article_file": article,
        "prompt_file": prompt_file,
    }


def list_existing_blogs(cfg: "BlogConfig") -> list[dict[str, str]]:
    _migrate_index_if_needed(cfg.workspace, cfg)
    index_path = cfg.index_path
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        posts = data.get("posts", [])
        return [p for p in posts if isinstance(p, dict)]
    except Exception:
        return []


def _extract_existing_style_href(html: str) -> str:
    """从 HTML 中提取现有 <link rel="stylesheet"> 的 href 值。"""
    m = re.search(
        r'<link[^>]+rel=["\']stylesheet["\'][^>]*\bhref=["\']([^"\']+)["\']'
        r'|<link[^>]+\bhref=["\']([^"\']+)["\'][^>]+rel=["\']stylesheet["\']',
        html,
    )
    if m:
        return m.group(1) or m.group(2) or ""
    return ""


def _replace_stylesheet_href(html: str, new_href: str) -> str:
    """替换 HTML 中 <link rel="stylesheet"> 的 href，不存在则在 </head> 前注入。"""
    pattern = (
        r'(<link\b[^>]*\brel=["\']stylesheet["\'][^>]*\bhref=["\'])[^"\']*(["\'][^>]*>)'
        r'|(<link\b[^>]*\bhref=["\'])[^"\']*(["\'][^>]*\brel=["\']stylesheet["\'][^>]*>)'
    )
    replaced, n = re.subn(
        pattern,
        lambda m: (
            m.group(1) + new_href + m.group(2)
            if m.group(1)
            else m.group(3) + new_href + m.group(4)
        ),
        html,
        count=1,
    )
    if n == 0:
        replaced = html.replace(
            "</head>",
            f'  <link rel="stylesheet" href="{new_href}" />\n</head>',
            1,
        )
    return replaced


def _restyle_one_post(
    workspace: Path,
    cfg: "BlogConfig",
    slug: str,
    style_name: str | None,
    framework_name: str | None,
) -> Path:
    """改写单篇已有博客页面的 style/framework，原文内容保持不变。
    style_name=None 表示不换样式，framework_name=None 表示不换框架。
    返回写入的输出路径。
    """
    posts = list_existing_blogs(cfg)
    post = next((p for p in posts if p.get("slug") == slug), None)
    if post is None:
        raise KeyError(f"未找到文章条目: {slug}")

    page_url = post.get("page_url", "").strip() or f"{slug}/index.html"
    out_path = (cfg.output_dir / page_url).resolve()
    if not out_path.exists():
        raise FileNotFoundError(f"页面文件不存在: {out_path.name}")

    current_html = out_path.read_text(encoding="utf-8")
    out_dir = out_path.parent

    if framework_name:
        agent = BlogAgent(cfg)
        frameworks = list_frameworks(cfg.frameworks_dir)
        tpl = frameworks[framework_name].read_text(encoding="utf-8")

        if style_name:
            style_href = os.path.relpath(
                cfg.styles_dir / f"{style_name}.css", out_dir
            ).replace("\\", "/")
            css_path: Path | None = cfg.styles_dir / f"{style_name}.css"
        else:
            style_href = _extract_existing_style_href(current_html)
            # 尝试从相对 href 还原实际 CSS 路径，以便 AI 读取
            css_path = (out_dir / style_href).resolve() if style_href else None

        # 开关关闭：占位符式精确迁移（更省 token，更可控）
        if not cfg.creative_restyle:
            extracted = agent.extract_page_content(current_html)
            title = extracted.get("title") or slug
            # 清理 AI 提取内容中可能残留的嵌入样式，防止覆盖目标 CSS
            content_html = extracted.get("content_html") or ""
            content_html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", content_html)
            content_html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", content_html)
            content_html = re.sub(r'\s+style="[^"]*"', "", content_html)
            content_html = re.sub(r"\s+style='[^']*'", "", content_html)
            new_html = render_template_placeholders(
                tpl,
                title=title,
                blog_name="AI Blog",
                subtitle=extracted.get("subtitle") or "",
                date=extracted.get("date") or _date_cls.today().isoformat(),
                content_html=content_html,
                style_href=style_href,
            )
            # AI 将目标 CSS 写入新 HTML 并调整 class/id 兼容性
            if css_path and css_path.exists():
                css_content = css_path.read_text(encoding="utf-8")
                new_html = agent.apply_css_to_html(new_html, css_content, style_href)
        else:
            # 开关开启：创作式框架迁移，让 AI 阅读完整 framework + 当前页面后重构（不改正文）
            provider = create_provider(cfg)
            tpl_snippet = tpl[:14000]
            current_snippet = current_html[:14000]
            prompt = (
                "现在有一篇已经渲染好的博客页面（旧 HTML），以及一个目标框架页面（framework HTML）。\n"
                "请在不改变文章正文文字内容的前提下，将旧页面迁移到新框架结构中，输出新的完整 HTML。\n\n"
                "【必须遵守】\n"
                "1. 保留旧页面中的正文、标题、副标题和日期文本，一字不改；如有日期缺失，可以从旧页面推断或留空。\n"
                "2. 充分利用目标框架页面的布局、组件和脚本能力（例如目录、阅读进度、代码高亮等），"
                "让整体视觉和交互更加统一、美观。\n"
                "3. 如果目标框架中已经有正文区域或代码块样式，请尽量将旧正文结构映射过去，"
                "而不是简单原样嵌入。\n"
                "4. 保留或复用框架中已有的 <script> 逻辑，必要时可以稍作调整以适配新的结构，但不要删除核心功能。\n"
                "5. 不要生成额外的解释文字，只输出最终 HTML。\n\n"
                "【旧页面 HTML】\n"
                f"{current_snippet}\n\n"
                "【目标框架 HTML】\n"
                f"{tpl_snippet}\n"
            )
            text = provider.chat(
                system_prompt=(
                    "你是博客页面重构助手，专门在不改动正文文字的前提下，"
                    "阅读现有 CSS/框架并对页面结构做创作式重构，使其在新框架下更美观、易读。"
                ),
                user_prompt=prompt,
                temperature=0.4,
            )
            new_html = agent._strip_code_fence(text)  # type: ignore[attr-defined]
            if not new_html or len(new_html) < 50:
                new_html = current_html
    else:
        # 仅换样式
        new_href = os.path.relpath(
            cfg.styles_dir / f"{style_name}.css", out_dir
        ).replace("\\", "/")
        css_path = cfg.styles_dir / f"{style_name}.css"
        if css_path.exists() and cfg.creative_restyle:
            # 创作式样式迁移：让 AI 阅读 CSS + 当前 HTML 做更大胆的 class/id 调整（不改正文）
            css_content = css_path.read_text(encoding="utf-8")
            agent = BlogAgent(cfg)
            new_html = agent.apply_css_to_html(current_html, css_content, new_href)
        else:
            # 精确模式：只替换 href，不让 AI 动结构，token 消耗更低
            new_html = _replace_stylesheet_href(current_html, new_href)

    out_path.write_text(new_html, encoding="utf-8")
    return out_path


def cmd_refresh_home_index(
    workspace: Path, force_regenerate_summary: bool = False, quiet: bool = False
) -> Path:
    cfg = load_config(workspace)
    _migrate_index_if_needed(workspace, cfg)
    index_path = cfg.index_path
    if not index_path.exists():
        raise FileNotFoundError("未找到 index.json，先创建文章草稿。")
    data = json.loads(index_path.read_text(encoding="utf-8"))
    posts = data.get("posts", [])
    agent = BlogAgent(cfg)
    for item in posts:
        slug = str(item.get("slug", "")).strip()
        if not slug:
            continue
        item["page_url"] = _infer_page_url_for_entry(
            workspace, cfg.content_dir.resolve(), item
        )
        existing_summary = str(item.get("summary", "")).strip()
        need_summary = force_regenerate_summary or not existing_summary
        if need_summary:
            source_file, source_hint = _find_summary_source_file(
                workspace, cfg.content_dir.resolve(), item
            )
            if source_file is not None:
                raw_text = source_file.read_text(encoding="utf-8", errors="ignore")
                summary_input = _extract_summary_input(raw_text, source_hint)
                if summary_input:
                    item["summary"] = agent.generate_post_summary(
                        summary_input,
                        source_hint=f"{source_hint} -> {_display_path(source_file, workspace)}",
                    )
                else:
                    item["summary"] = existing_summary
            else:
                item["summary"] = existing_summary
        else:
            item["summary"] = existing_summary
        item["category"] = item.get("category") or "default"
        item["style"] = item.get("style") or "__default__"
        item["framework"] = item.get("framework") or "__default__"
    data["posts"] = posts
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not quiet:
        print(f"主页索引已更新: {index_path}")
    return index_path


def _read_index_data(index_path: Path) -> dict:
    if not index_path.exists():
        return {"posts": []}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(data.get("posts"), list):
            data["posts"] = []
        return data
    except Exception:
        return {"posts": []}


def _changed_posts(before: dict, after: dict) -> list[dict]:
    before_map = {
        str(x.get("slug", "")): x for x in before.get("posts", []) if x.get("slug")
    }
    changed: list[dict] = []
    for item in after.get("posts", []):
        slug = str(item.get("slug", ""))
        if not slug:
            continue
        if slug not in before_map or before_map[slug] != item:
            changed.append(item)
    return changed


def _ensure_unique_slug(slug: str, used: set[str]) -> str:
    base = slug or "custom"
    if base not in used:
        used.add(base)
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def cmd_rescan_content_to_index(
    workspace: Path, quiet: bool = False
) -> tuple[Path, int, int]:
    cfg = load_config(workspace)
    _migrate_index_if_needed(workspace, cfg)
    index_path = cfg.index_path
    data = _read_index_data(index_path)

    def _norm_key(value: str) -> str:
        return value.strip().replace("\\", "/").lstrip("./")

    def _usable(value: str) -> bool:
        v = value.strip()
        return bool(v) and v != "Custom"

    raw_posts = [p for p in data.get("posts", []) if isinstance(p, dict)]
    posts: list[dict] = []
    known_article_files: set[str] = set()
    known_draft_dirs: set[str] = set()
    known_page_urls: set[str] = set()
    removed = 0

    # 先收集当前 content 目录中真实存在的草稿/页面，用于清理失效条目。
    draft_files = (
        list(cfg.content_dir.glob("**/my_blog.txt"))
        + list(cfg.content_dir.glob("**/myblog.txt"))
        + list(cfg.content_dir.glob("**/post.txt"))
    )
    existing_article_files = {_norm_key(_store_path(workspace, f)) for f in draft_files}
    existing_draft_dirs = {
        _norm_key(_store_path(workspace, f.parent)) for f in draft_files
    }
    html_candidates = list(cfg.content_dir.glob("**/index.html"))
    existing_page_urls = {
        _norm_key(str(h.relative_to(cfg.content_dir)))
        for h in html_candidates
        if h.relative_to(cfg.content_dir) != Path("index.html")
        and not any(
            part in {"styles", "frameworks", "previews"}
            for part in h.relative_to(cfg.content_dir).parts
        )
    }

    # 清理 index.json 中已存在的重复项：同 article_file/draft_dir/page_url 视为同一篇。
    for p in raw_posts:
        article_key = _norm_key(str(p.get("article_file", "")))
        draft_key = _norm_key(str(p.get("draft_dir", "")))
        page_key = _norm_key(str(p.get("page_url", "")))
        has_source = False
        source_exists = False
        if _usable(article_key):
            has_source = True
            source_exists = source_exists or article_key in existing_article_files
        if _usable(draft_key):
            has_source = True
            source_exists = source_exists or draft_key in existing_draft_dirs
        if _usable(page_key):
            has_source = True
            source_exists = source_exists or page_key in existing_page_urls
        if has_source and not source_exists:
            removed += 1
            continue
        if _usable(article_key) and article_key in known_article_files:
            continue
        if _usable(draft_key) and draft_key in known_draft_dirs:
            continue
        if _usable(page_key) and page_key in known_page_urls:
            continue
        posts.append(p)
        if _usable(article_key):
            known_article_files.add(article_key)
        if _usable(draft_key):
            known_draft_dirs.add(draft_key)
        if _usable(page_key):
            known_page_urls.add(page_key)

    used_slugs = {str(p.get("slug", "")).strip() for p in posts if p.get("slug")}
    added = 0

    def add_entry(entry: dict[str, str]) -> None:
        nonlocal added
        slug = str(entry.get("slug", "")).strip()
        if not slug:
            slug = "custom"
        article_key = _norm_key(str(entry.get("article_file", "")))
        draft_key = _norm_key(str(entry.get("draft_dir", "")))
        page_key = _norm_key(str(entry.get("page_url", "")))
        if article_key and article_key in known_article_files:
            return
        if draft_key and draft_key in known_draft_dirs:
            return
        if page_key and page_key in known_page_urls:
            return
        if any(str(p.get("slug", "")).strip() == slug for p in posts):
            return
        entry["slug"] = _ensure_unique_slug(slug, used_slugs)
        posts.append(entry)
        if _usable(article_key):
            known_article_files.add(article_key)
        if _usable(draft_key):
            known_draft_dirs.add(draft_key)
        if _usable(page_key):
            known_page_urls.add(page_key)
        added += 1

    # 1) 草稿文件扫描：my_blog/myblog/post
    for file in sorted(draft_files):
        slug_guess = file.parent.name or "custom"
        add_entry(
            {
                "slug": slug_guess,
                "summary": "",
                "category": "Custom",
                "draft_dir": _store_path(workspace, file.parent),
                "article_file": _store_path(workspace, file),
                "prompt_file": _store_path(workspace, file.parent / "prompt.txt")
                if (file.parent / "prompt.txt").exists()
                else "Custom",
                "page_url": str(
                    file.parent.relative_to(cfg.content_dir) / "index.html"
                ),
                "style": "Custom",
                "framework": "Custom",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    # 2) 已存在页面扫描：任意 index.html（排除主页和模板/预览）
    for html in sorted(html_candidates):
        rel = html.relative_to(cfg.content_dir)
        if rel == Path("index.html"):
            continue
        if any(part in {"styles", "frameworks", "previews"} for part in rel.parts):
            continue
        slug_guess = html.parent.name or "custom"
        add_entry(
            {
                "slug": slug_guess,
                "summary": "",
                "category": "Custom",
                "parent_directory": "content",
                "draft_dir": "Custom",
                "article_file": "Custom",
                "prompt_file": "Custom",
                "page_url": str(html.relative_to(cfg.content_dir)),
                "style": "Custom",
                "framework": "Custom",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    data["posts"] = sorted(posts, key=lambda x: str(x.get("slug", "")))
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not quiet:
        print(f"扫描完成: {index_path}, 新增 {added} 条, 删除 {removed} 条")
    return index_path, added, removed


@dataclass
class MenuItem:
    key: str
    title: str
    hint: str
    enabled: bool = True


class VimTUIApp:
    def __init__(
        self,
        stdscr: curses.window,
        workspace: Path,
        no_browser: bool,
        update_notice: str | None = None,
    ) -> None:
        self.stdscr = stdscr
        self.workspace = workspace
        self.no_browser = no_browser
        self.update_notice = update_notice
        self.logs: list[str] = []
        self.menu = [
            MenuItem("init", "第一次使用：一键准备", "自动完成目录与基础设置。"),
            MenuItem(
                "refresh_index",
                "一键更新主页",
                "更新网站主页文章索引，将新文章直接嵌入主页，同时让AI生成文章简介。",
            ),
            MenuItem(
                "rescan_content",
                "迁移扫描",
                "快速扫描当前 content 目录并同步到 index.json，未定义字段自动写为 Custom。",
            ),
            MenuItem(
                "build_home",
                "生成主页（使用AI）",
                "基于索引和你的要求生成主页，先预览，再决定接受或继续修改。",
            ),
            MenuItem(
                "new",
                "新建博客页",
                "创建草稿目录 + my_blog.txt + prompt.txt，并登记到 index.json。",
            ),
            MenuItem(
                "theme",
                "换一个页面风格",
                "只影响之后新生成的页面，不会改动已生成页面。",
            ),
            MenuItem(
                "openers",
                "配置编辑器/文件管理器启动命令",
                "按你的习惯设置默认命令，供新建与查看博客时复用。",
            ),
            MenuItem(
                "restyle_mode",
                "切换风格改写模式（精确 / 创作式）",
                "在占位符式精确改写与创作式重构之间切换，仅影响改写已有页面。",
            ),
            MenuItem(
                "content_dir",
                "设置 content 目录",
                "手动输入绝对路径，路径必须以 /content/ 结尾。",
            ),
            MenuItem("query_blogs", "查看已有博客", "查看已创建博客列表及其目录位置。"),
            MenuItem(
                "restyle_post",
                "改写已有页面风格（不完善，暂时不推荐使用）",
                "选择一篇或多篇已有博客，用新的样式/框架重新渲染，原文内容保持不变。",
            ),
            MenuItem(
                "check_update",
                "检查版本更新",
                "手动检查当前代码是否落后远端提交。",
            ),
            MenuItem(
                "ai_generate",
                "用内置 AI 生成样式/框架",
                "交互式输入目标后，自动生成并保存模板文件。",
            ),
            # MenuItem(
            #     "sync_pending",
            #     "一键同步样式和框架（待实现）",
            #     "预留入口：后续支持一键拉取并同步模板资源。",
            #     enabled=False,
            # ),
            MenuItem(
                "edit_template",
                "打开模板目录并编辑文件",
                "快速选择样式/框架文件并用默认编辑器打开。",
            ),
        ]
        self.selected = 0
        self.running = True
        self.c_purple = 0
        self.c_blue = 0
        self.c_red = 0
        self.c_text = 0
        self.c_focus = 0

    def run(self) -> int:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self._init_theme()
        self._splash()
        while self.running:
            self._draw_main()
            key = self.stdscr.getch()
            if key in (ord("j"), curses.KEY_DOWN):
                self.selected = (self.selected + 1) % len(self.menu)
            elif key in (ord("k"), curses.KEY_UP):
                self.selected = (self.selected - 1) % len(self.menu)
            elif key in (10, 13, curses.KEY_ENTER, ord("l")):
                self._run_menu_action(self.menu[self.selected])
            elif ord("1") <= key <= ord("9"):
                n = key - ord("1")
                if n < len(self.menu):
                    self.selected = n
                    self._run_menu_action(self.menu[self.selected])
            elif key == ord(":"):
                self._command_palette()
            elif key == ord("?"):
                self._show_help()
            elif key in (ord("q"), ord("h")):
                self.running = False
        return 0

    def _init_theme(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_MAGENTA, -1)  # purple/pink
        curses.init_pair(2, curses.COLOR_CYAN, -1)  # blue
        curses.init_pair(3, curses.COLOR_RED, -1)  # red
        curses.init_pair(4, curses.COLOR_WHITE, -1)  # text
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_MAGENTA)

        self.c_purple = curses.color_pair(1) | curses.A_BOLD
        self.c_blue = curses.color_pair(2) | curses.A_BOLD
        self.c_red = curses.color_pair(3) | curses.A_BOLD
        self.c_text = curses.color_pair(4)
        self.c_focus = curses.color_pair(5) | curses.A_BOLD

    def _splash(self) -> None:
        h, w = self.stdscr.getmaxyx()
        title = "AIBlogAuto"
        subtitle = "Neon Purple Blue Red"
        start_x = 4
        end_x = max(start_x + 10, w - 5)
        y = max(2, h // 2 - 2)

        self.stdscr.clear()
        self._safe_addstr(y - 2, max(2, (w - len(title)) // 2), title, self.c_purple)
        self._safe_addstr(
            y - 1, max(2, (w - len(subtitle)) // 2), subtitle, self.c_blue
        )

        for x in range(start_x, end_x):
            self._safe_addstr(y, x, "─", self.c_red)
            self.stdscr.refresh()
            time.sleep(0.003)

        for offset in range(0, min(8, h // 3)):
            self.stdscr.clear()
            self._safe_addstr(y + offset, start_x, "─" * (end_x - start_x), self.c_red)
            self.stdscr.refresh()
            time.sleep(0.02)

    def _draw_header(self) -> None:
        _, w = self.stdscr.getmaxyx()
        line = "━" * max(10, w - 2)
        self._safe_addstr(0, 0, line, self.c_purple)
        self._safe_addstr(1, 2, "AIBlogAuto", self.c_purple)
        self._safe_addstr(2, 2, f"项目目录: {self.workspace}", self.c_text)
        self._safe_addstr(
            3,
            2,
            "Ctrl+Z 暂停程序，终端输入 fg 恢复 | ? 键位帮助 | :logs 动作日志",
            self.c_text,
        )
        self._safe_addstr(4, 0, line, self.c_blue)

    def _draw_footer(self) -> None:
        h, _ = self.stdscr.getmaxyx()
        style, frame = self._current_theme_labels()
        self._safe_addstr(
            h - 4,
            2,
            "提示: 使用自己的agent软件来修改，由AIBlogAuto来管理，体验更佳噢",
            self.c_red,
        )
        self._safe_addstr(
            h - 3, 2, f"状态栏 | 默认样式: {style} | 默认框架: {frame}", self.c_blue
        )
        self._safe_addstr(
            h - 2,
            2,
            "导航: j/k 上下  Enter 确认  1~9 直达  q 返回/退出  : 命令",
            self.c_text,
        )

    def _current_theme_labels(self) -> tuple[str, str]:
        cfg_path = self.workspace / "blogauto.json"
        if not cfg_path.exists():
            return "-", "-"
        try:
            cfg = load_config(self.workspace)
            return cfg.selected_style, cfg.selected_framework
        except Exception:
            return "-", "-"

    def _draw_main(self) -> None:
        self.stdscr.clear()
        self._draw_header()
        base_y = 6
        if self.update_notice:
            self._safe_addstr(base_y, 2, f"版本提示: {self.update_notice}", self.c_red)
            base_y += 1

        ready = "已准备" if (self.workspace / "blogauto.json").exists() else "未准备"
        self._safe_addstr(base_y, 2, f"当前状态: {ready}", self.c_blue)
        self._safe_addstr(base_y + 1, 2, "主菜单（按数字可直接进入）", self.c_text)
        menu_top = base_y + 3

        for idx, item in enumerate(self.menu):
            prefix = "❯" if idx == self.selected else " "
            disable_tag = " [待实现]" if not item.enabled else ""
            text = f"{prefix} [{idx + 1}] {item.title}{disable_tag}"
            style = self.c_focus if idx == self.selected else self.c_text
            self._safe_addstr(menu_top + idx, 4, text, style)

        tip_top = menu_top + len(self.menu) + 1
        self._draw_box(tip_top, 2, 5, "功能注解")
        self._safe_addstr(tip_top + 2, 4, self.menu[self.selected].hint, self.c_text)

        self._draw_footer()
        self.stdscr.refresh()

    def _show_message(self, title: str, lines: list[str]) -> None:
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, title, self.c_purple)
            for i, text in enumerate(lines):
                self._safe_addstr(8 + i, 4, text, self.c_text)
            self._safe_addstr(10 + len(lines), 4, "按 q 返回", self.c_blue)
            self._draw_footer()
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key == ord("q"):
                return
            if key == ord("?"):
                self._show_help()
            if key == ord(":") and self._command_palette():
                return

    def _show_new_post_result(
        self, slug: str, draft_dir: Path, article_file: Path, prompt_file: Path
    ) -> None:
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, "博客页创建完成", self.c_purple)

            self._safe_addstr(8, 4, "已生成文章模板：", self.c_text)
            show_dir = self._fmt_path(draft_dir)
            folder_name = Path(show_dir).name
            parent = str(Path(show_dir).parent)
            base = f"{parent}/" if parent and parent != "." else ""
            line_y = 9
            self._safe_addstr(line_y, 6, base, self.c_text)
            self._safe_addstr(line_y, 6 + len(base), folder_name, self.c_red)
            self._safe_addstr(
                line_y, 6 + len(base) + len(folder_name), "/my_blog.txt", self.c_text
            )

            self._safe_addstr(
                11, 4, "提示：修改 my_blog.txt 来填入文章内容。", self.c_text
            )
            self._safe_addstr(
                12, 4, "完成后可调用 codex / claude code / copilot。", self.c_text
            )
            self._safe_addstr(
                13, 4, "建议优先使用你自备的 AI agent，体验通常更好。", self.c_text
            )
            self._safe_addstr(
                14,
                4,
                f"请让它阅读提示词：{self._fmt_path(prompt_file)}",
                self.c_blue,
            )
            self._safe_addstr(
                16,
                4,
                f"文章文件：{self._fmt_path(article_file)}",
                self.c_text,
            )
            self._safe_addstr(17, 4, "目录登记：index.json 已更新", self.c_text)
            self._safe_addstr(19, 4, "按 q 返回", self.c_blue)
            self._draw_footer()
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key == ord("q"):
                return
            if key == ord("?"):
                self._show_help()
            if key == ord(":") and self._command_palette():
                return

    def _choose_from_list(
        self, title: str, items: list[tuple[str, str]], default_idx: int = 0
    ) -> str | None:
        idx = default_idx
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, title, self.c_purple)
            self._safe_addstr(
                7, 2, "j/k 选择, Enter 确认, 1~9 直达, q 返回", self.c_text
            )
            for i, (label, _) in enumerate(items):
                prefix = "❯" if i == idx else " "
                style = self.c_focus if i == idx else self.c_text
                self._safe_addstr(9 + i, 4, f"{prefix} [{i + 1}] {label}", style)
            self._draw_footer()
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key in (ord("j"), curses.KEY_DOWN):
                idx = (idx + 1) % len(items)
            elif key in (ord("k"), curses.KEY_UP):
                idx = (idx - 1) % len(items)
            elif key in (10, 13, curses.KEY_ENTER):
                return items[idx][1]
            elif ord("1") <= key <= ord("9"):
                n = key - ord("1")
                if n < len(items):
                    return items[n][1]
            elif key == ord("q"):
                return None
            elif key == ord("?"):
                self._show_help()
            elif key == ord(":") and self._command_palette():
                return None

    def _input_line(self, title: str, prompt: str, default: str = "") -> str | None:
        buf = list(default)
        pos = len(buf)
        insert_mode = False
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, title, self.c_purple)
            self._safe_addstr(8, 2, prompt, self.c_text)
            self._safe_addstr(9, 4, "".join(buf) + " ", self.c_blue)
            mode_text = (
                "-- INSERT --" if insert_mode else "-- NORMAL -- (按 i 进入输入模式)"
            )
            self._safe_addstr(
                10, 2, mode_text, self.c_red if insert_mode else self.c_text
            )
            self._safe_addstr(
                11,
                2,
                "NORMAL: i 进入输入, Enter 确认, q 返回 | INSERT: Esc 退出输入",
                self.c_text,
            )
            self._draw_footer()
            if insert_mode:
                curses.curs_set(1)
                cursor_x = 4 + self._display_width("".join(buf[:pos]))
                self.stdscr.move(9, cursor_x)
            else:
                curses.curs_set(0)
            self.stdscr.refresh()

            try:
                key = self.stdscr.get_wch()
            except curses.error:
                continue
            if insert_mode:
                if key == "\x1b":  # ESC
                    insert_mode = False
                    continue
                if key in (curses.KEY_BACKSPACE, 127, 8, "\b", "\x7f"):
                    if pos > 0:
                        buf.pop(pos - 1)
                        pos -= 1
                    continue
                if key == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                    continue
                if key == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                    continue
                if key in (10, 13, curses.KEY_ENTER, "\n", "\r"):
                    buf.insert(pos, "\n")
                    pos += 1
                    continue
                if isinstance(key, str) and key.isprintable():
                    buf.insert(pos, key)
                    pos += 1
                continue

            if key in (ord("i"), "i"):
                insert_mode = True
                continue
            if key in (10, 13, curses.KEY_ENTER, "\n", "\r"):
                return "".join(buf).strip()
            if key in (ord("q"), "q"):
                return None
            if key in (ord("?"), "?"):
                self._show_help()
                continue
            if key in (ord(":"), ":") and not buf:
                if self._command_palette():
                    return None
                continue

    def _display_width(self, text: str) -> int:
        width = 0
        for ch in text:
            if ch == "\n":
                continue
            width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
        return width

    def _fmt_path(self, path: Path) -> str:
        return _display_path(path, self.workspace)

    def _run_menu_action(self, item: MenuItem) -> None:
        if item.key == "sync_pending":
            self._show_sync_placeholder()
            return

        if item.key == "init":
            self._action_init()
        elif item.key == "refresh_index":
            self._action_refresh_index()
        elif item.key == "rescan_content":
            self._action_rescan_content()
        elif item.key == "build_home":
            self._action_build_homepage_ai()
        elif item.key == "new":
            self._action_new_post()
        elif item.key == "theme":
            self._action_set_theme()
        elif item.key == "openers":
            self._action_config_openers()
        elif item.key == "restyle_mode":
            self._action_toggle_restyle_mode()
        elif item.key == "content_dir":
            self._action_set_content_dir()
        elif item.key == "query_blogs":
            self._action_query_blogs()
        elif item.key == "restyle_post":
            self._action_restyle_posts()
        elif item.key == "check_update":
            self._action_check_update()
        elif item.key == "ai_generate":
            self._action_ai_generate_assets()
        elif item.key == "edit_template":
            self._action_edit_template_file()

    def _action_init(self) -> None:
        styles = [(s.name, s.name) for s in BUILTIN_STYLES]
        frames = [
            (f"{f.name}（一个 HTML 布局模板等内容）", f.name)
            for f in BUILTIN_FRAMEWORKS
        ]

        style = self._choose_from_list("一键准备：先选默认样式", styles, default_idx=0)
        if style is None:
            return
        frame = self._choose_from_list("一键准备：再选默认框架", frames, default_idx=0)
        if frame is None:
            return

        provider = self._choose_from_list(
            "一键准备：选择 AI 服务商",
            [
                ("DeepSeek（默认）", "deepseek"),
                ("OpenAI 兼容接口（如 OpenAI / OpenRouter）", "openai"),
                ("Anthropic Claude", "anthropic"),
                ("自定义 OpenAI 兼容接口", "custom"),
            ],
            default_idx=0,
        )
        if provider is None:
            return

        default_base = {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com",
            "custom": "https://api.deepseek.com",
        }.get(provider, "https://api.deepseek.com")
        default_model = {
            "deepseek": "deepseek-chat",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-sonnet-latest",
            "custom": "",
        }.get(provider, "")

        model = self._input_line(
            "AI 设置", "输入模型名（可直接回车用默认）:", default=default_model
        )
        if model is None:
            return
        base_url = self._input_line(
            "AI 设置", "输入 API Base URL（可回车用默认）:", default=default_base
        )
        if base_url is None:
            return
        api_key = self._input_line(
            "AI 设置", "输入 API Key（可后续在配置里再改）:", default=""
        )
        if api_key is None:
            return

        key_source = self._choose_from_list(
            "API Key 保存方式",
            [
                ("读取环境变量（推荐）", "env"),
                ("写入单独密钥文件（请勿暴露到 GitHub）", "file"),
            ],
            default_idx=0,
        )
        if key_source is None:
            return

        if key_source == "env" and api_key:
            os.environ[_provider_env_var(provider)] = api_key

        default_content = str((self.workspace / "content").resolve())
        content_text = self._input_line(
            "Content 目录",
            "请输入 content 绝对路径（必须以 /content/ 结尾）:",
            default=default_content,
        )
        if content_text is None:
            return
        try:
            content_dir = _validate_content_dir(content_text)
        except ValueError as exc:
            self._show_message("content 目录无效", [str(exc)])
            return

        cfg = self._run_with_busy(
            "正在处理：初始化项目目录与预览文件...",
            lambda: init_workspace(
                self.workspace,
                open_preview=not self.no_browser,
                selected_style=style,
                selected_framework=frame,
                ai_provider=provider,
                ai_key_source=key_source,
                ai_model=model or None,
                ai_base_url=base_url or None,
                content_dir=content_dir,
            ),
        )
        secret_notice = ""
        if key_source == "file":
            if not api_key:
                self._show_message(
                    "需要 API Key", ["你选择了密钥文件模式，但没有输入 API Key。"]
                )
                return
            secret_path = _save_secret_key(cfg, provider, api_key)
            secret_notice = f"密钥文件：{secret_path.name}（已自动加入 .gitignore）"
        self._log(
            f"完成初始化（样式={cfg.selected_style}, 框架={cfg.selected_framework}）"
        )
        self._show_message(
            "一键准备完成",
            [
                f"目录：{cfg.workspace}",
                f"AI 服务商：{cfg.ai_provider}",
                (
                    f"API Key 环境变量：{_provider_env_var(cfg.ai_provider)}（仅当前会话，不写入项目文件）"
                    if key_source == "env"
                    else secret_notice
                ),
                f"content 目录：{self._fmt_path(cfg.content_dir)}",
                f"默认编辑器：{cfg.default_editor}",
                f"默认文件管理器：{cfg.default_file_manager}",
                f"部署提示：请确保站点可访问 {cfg.content_dir}/ 下文件。",
            ],
        )

    def _action_new_post(self) -> None:
        if not self._ensure_ready():
            return
        slug = self._input_line("新建博客页", "给文章起个英文短名（例：test2222）:")
        if slug is None or not slug:
            return

        cfg = load_config(self.workspace)
        prefix = self._fmt_path(cfg.content_dir)
        suffix = self._input_line(
            "新建博客页",
            f"当前统一博客目录：{prefix}/ 请输入后续目录（用 / 分层，留空用 {slug}）:",
            default=slug,
        )
        if suffix is None:
            return

        suffix = suffix.strip() or slug
        cfg.draft_structure_template = suffix
        save_config(cfg)

        path_parts = [p for p in suffix.split("/") if p]
        default_category = path_parts[0] if path_parts else "default"
        category = self._input_line(
            "新建博客页", "分类名（用于主页索引）:", default=default_category
        )
        if category is None or not category:
            return

        styles = list_styles(cfg.styles_dir)
        frames = list_frameworks(cfg.frameworks_dir)
        style_choice = self._choose_from_list(
            "这篇文章用什么样式？",
            [('使用默认样式（来自"换一个页面风格"）', "__default__")]
            + [(f"{n} [来源: {self._fmt_path(p)}]", n) for n, p in styles.items()],
            default_idx=0,
        )
        if style_choice is None:
            return
        frame_choice = self._choose_from_list(
            "这篇文章用什么框架？",
            [('使用默认框架（来自"换一个页面风格"）', "__default__")]
            + [
                (
                    f"{n}（一个 HTML 布局模板等内容） [来源: {self._fmt_path(p)}]",
                    n,
                )
                for n, p in frames.items()
            ],
            default_idx=0,
        )
        if frame_choice is None:
            return

        result = cmd_new_post(
            self.workspace,
            slug,
            relative_path_suffix=suffix,
            category=category,
            style_choice=style_choice,
            framework_choice=frame_choice,
            quiet=True,
        )
        draft_dir = result["draft_dir"]
        article_file = result["article_file"]
        prompt_file = result["prompt_file"]
        self._log(f"创建草稿：{slug} -> {self._fmt_path(draft_dir)}")

        action = self._choose_open_action(
            "创建完成后下一步要做什么？", include_inline_ai=True
        )
        if action is None:
            action = "none"

        if action == "inline_ai":
            self._inline_write_and_generate(slug, article_file, draft_dir)
            return
        self._open_after_create(cfg, action, article_file, draft_dir)
        self._show_new_post_result(slug, draft_dir, article_file, prompt_file)

    def _action_refresh_index(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        _ensure_homepage_prompts(cfg)
        index_file = cfg.index_path
        before = _read_index_data(index_file)
        has_existing_summary = any(
            str(x.get("summary", "")).strip() for x in before.get("posts", [])
        )
        force_regenerate_summary = False
        if has_existing_summary:
            strategy = self._choose_from_list(
                "检测到已有简介，是否重生成？",
                [
                    ("仅补全缺失简介（推荐）", "missing_only"),
                    ("强制重生成全部简介", "force_all"),
                ],
                default_idx=0,
            )
            if strategy is None:
                return
            force_regenerate_summary = strategy == "force_all"
        index_path = self._run_with_busy(
            "正在处理：更新主页索引字段...",
            lambda: cmd_refresh_home_index(
                self.workspace,
                force_regenerate_summary=force_regenerate_summary,
                quiet=True,
            ),
        )
        after = _read_index_data(index_path)
        changed = _changed_posts(before, after)
        home_path = self._run_with_busy(
            "正在处理：让 AI 将索引更新写入主页...",
            lambda: self._apply_index_updates_to_home(cfg, after, changed),
        )
        try:
            webbrowser.open(home_path.resolve().as_uri())
        except Exception:
            pass
        self._log("主页索引已更新")
        self._show_message(
            "主页索引更新完成",
            [
                f"索引文件: {self._fmt_path(index_path)}",
                "每篇文章已确保包含 page_url=<文章目录>/index.html",
                "每篇文章已补充 summary（my_blog.txt 优先，不存在则读取 index.html）",
                f"AI 已同步更新主页: {self._fmt_path(home_path)}",
                f"本次更新条目数: {len(changed)}",
                "如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。",
            ],
        )

    def _action_rescan_content(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        index_path, added, removed = self._run_with_busy(
            f"正在处理：扫描 {self._fmt_path(cfg.content_dir)} 并迁移写入 index.json...",
            lambda: cmd_rescan_content_to_index(self.workspace, quiet=True),
        )
        self._log(f"迁移扫描完成：新增 {added} 条，删除 {removed} 条")
        self._show_message(
            "迁移扫描完成",
            [
                f"索引文件: {self._fmt_path(index_path)}",
                f"扫描目录: {self._fmt_path(cfg.content_dir)}",
                f"新增条目: {added}",
                f"删除失效条目: {removed}",
                "未定义字段已自动写为 Custom。",
            ],
        )

    def _apply_index_updates_to_home(
        self, cfg: BlogConfig, after: dict, changed: list[dict]
    ) -> Path:
        agent = BlogAgent(cfg)
        fields_prompt = (
            cfg.prompts_dir / "homepage-index-fields.prompt.txt"
        ).read_text(encoding="utf-8")
        directory_style = (
            cfg.prompts_dir / "homepage-directory-style.prompt.txt"
        ).read_text(encoding="utf-8")
        framework_goal = (cfg.prompts_dir / "homepage-framework.prompt.txt").read_text(
            encoding="utf-8"
        )

        posts_json = json.dumps(after, ensure_ascii=False, indent=2)
        style_name = cfg.selected_style
        out = cfg.output_dir / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)

        if out.exists():
            current = out.read_text(encoding="utf-8")
            feedback = (
                "请把以下更新条目合并到主页索引，并保持未变更部分尽量稳定：\n"
                + json.dumps(changed, ensure_ascii=False, indent=2)
            )
            html = agent.refine_homepage(
                posts_json=posts_json,
                directory_style=directory_style,
                index_fields_prompt=fields_prompt,
                framework_goal=framework_goal,
                current_html=current,
                feedback=feedback,
                style_name=style_name,
            )
        else:
            html = agent.generate_homepage(
                posts_json=posts_json,
                directory_style=directory_style,
                index_fields_prompt=fields_prompt,
                framework_goal=framework_goal,
                style_name=style_name,
            )
        if style_name and "stylesheet" not in html:
            html = html.replace(
                "</head>",
                f'  <link rel="stylesheet" href="styles/{style_name}.css" />\n</head>',
            )
        out.write_text(html, encoding="utf-8")
        return out

    def _action_build_homepage_ai(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        _ensure_homepage_prompts(cfg)
        styles = list_styles(cfg.styles_dir)

        use_existing = self._choose_from_list(
            "主页样式：是否采用已有样式？",
            [("是，采用已有样式", "yes"), ("否，先自定义生成一个样式", "no")],
            default_idx=0,
        )
        if use_existing is None:
            return

        chosen_style: str | None = None
        if use_existing == "yes":
            style_items = [("不使用样式（纯 HTML）", "__none__")] + [
                (f"{n} [来源: {self._fmt_path(p)}]", n) for n, p in styles.items()
            ]
            picked = self._choose_from_list(
                "选择要用于主页的样式", style_items, default_idx=0
            )
            if picked is None:
                return
            chosen_style = None if picked == "__none__" else picked
        else:
            goal = self._input_line(
                "主页样式", "请描述主页样式要求（AI 将先生成样式）:"
            )
            if goal is None or not goal:
                return
            name = self._input_line(
                "主页样式", "给该样式起个文件名（不带后缀）:", default="home-style"
            )
            if name is None or not name:
                return
            agent = BlogAgent(cfg)
            css = self._run_with_busy(
                "正在处理：AI 正在生成主页样式...", lambda: agent.generate_style(goal)
            )
            target = cfg.styles_dir / f"{name}.css"
            target.write_text(css.strip() + "\n", encoding="utf-8")
            chosen_style = name
            self._log(f"主页样式已生成：{self._fmt_path(target)}")

        framework_goal = self._input_line(
            "主页框架",
            "请描述你希望的主页框架（布局结构、导航、索引区样式等）:",
            default="信息密度高，左侧分类导航，右侧文章索引",
        )
        if framework_goal is None or not framework_goal:
            return

        style_text = self._input_line(
            "建立网站主页（使用AI）",
            "请描述你希望的主页目录样式（如：按分类树+时间倒序）:",
            default="按分类分组并按创建时间倒序",
        )
        if style_text is None or not style_text:
            return
        self._run_with_busy(
            "正在处理：同步索引并补全摘要...",
            lambda: cmd_refresh_home_index(self.workspace, quiet=True),
        )
        index_path = cfg.index_path
        posts_json = index_path.read_text(encoding="utf-8")
        fields_prompt = (
            cfg.prompts_dir / "homepage-index-fields.prompt.txt"
        ).read_text(encoding="utf-8")
        agent = BlogAgent(cfg)

        html = self._run_with_busy(
            "正在处理：AI 正在构建主页索引...",
            lambda: agent.generate_homepage(
                posts_json=posts_json,
                directory_style=style_text,
                index_fields_prompt=fields_prompt,
                framework_goal=framework_goal,
                style_name=chosen_style,
            ),
        )
        html = _ensure_homepage_stylesheet_link(html, chosen_style)
        while True:
            preview = cfg.previews_dir / "homepage-candidate.html"
            cfg.previews_dir.mkdir(parents=True, exist_ok=True)
            preview_html = _rewrite_homepage_css_href_for_preview(html, cfg)
            preview.write_text(preview_html, encoding="utf-8")
            try:
                webbrowser.open(preview.resolve().as_uri())
            except Exception:
                pass

            decision = self._choose_from_list(
                "主页预览完成：是否接受？",
                [
                    ("接受并保存主页", "accept"),
                    ("继续修改一轮", "revise"),
                    ("取消，不保存", "cancel"),
                ],
                default_idx=0,
            )
            if decision is None or decision == "cancel":
                self._show_message("已取消", ["主页未保存。"])
                return
            if decision == "accept":
                break
            feedback = self._input_line("修改主页", "告诉 AI 你希望改哪些地方:")
            if feedback is None or not feedback:
                continue
            html = self._run_with_busy(
                "正在处理：AI 正在修改主页...",
                lambda: agent.refine_homepage(
                    posts_json=posts_json,
                    directory_style=style_text,
                    index_fields_prompt=fields_prompt,
                    framework_goal=framework_goal,
                    current_html=html,
                    feedback=feedback,
                    style_name=chosen_style,
                ),
            )
            html = _ensure_homepage_stylesheet_link(html, chosen_style)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        home = cfg.output_dir / "index.html"
        home.write_text(html, encoding="utf-8")
        self._log("AI 更新主页索引完成")
        self._show_message(
            "主页已更新",
            [
                f"已生成: {self._fmt_path(home)}",
                "如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。",
                f"索引字段提示词: {self._fmt_path(cfg.prompts_dir / 'homepage-index-fields.prompt.txt')}",
                (
                    f"主页样式: styles/{chosen_style}.css"
                    if chosen_style
                    else "主页样式: 未使用"
                ),
            ],
        )

    def _open_after_create(
        self, cfg: BlogConfig, action: str, article_file: Path, draft_dir: Path
    ) -> None:
        try:
            if action in {"editor", "both"}:
                self._run_external(cfg.default_editor, article_file, wait=True)
                self._log(f"打开编辑器：{article_file.name}")
            if action in {"manager", "both"}:
                self._run_external(cfg.default_file_manager, draft_dir, wait=False)
                self._log(f"打开文件管理器：{self._fmt_path(draft_dir)}")
        except Exception as exc:
            self._show_message("打开外部工具失败", [str(exc)])

    def _choose_open_action(
        self, title: str, include_inline_ai: bool = False
    ) -> str | None:
        items = [
            ("只打开编辑器", "editor"),
            ("只在文件管理器中显示", "manager"),
            ("小孩子才做选择，以上都要", "both"),
            ("先不打开", "none"),
        ]
        if include_inline_ai:
            items.insert(
                3,
                (
                    "直接在本程序中输入正文(my_blog.txt)，然后由内置AI生成页面(不太建议)",
                    "inline_ai",
                ),
            )
        return self._choose_from_list(title, items)

    def _inline_write_and_generate(
        self, slug: str, article_file: Path, draft_dir: Path
    ) -> None:
        text = self._input_line(
            "直接输入正文",
            "按 i 进入输入模式，Enter 可换行；切回 NORMAL 后按 Enter 完成。",
            default="",
        )
        if text is None or not text.strip():
            self._show_message("未写入正文", ["内容为空，已取消内置 AI 生成。"])
            return

        article_file.write_text(text.strip() + "\n", encoding="utf-8")
        summary = self._run_with_busy(
            "正在处理：调用内置 AI 生成页面...",
            lambda: cmd_build(self.workspace, quiet=True),
        )
        page = draft_dir / "index.html"
        if page.exists() and not self.no_browser:
            try:
                webbrowser.open(page.resolve().as_uri())
            except Exception:
                pass
        self._log(f"内置 AI 生成页面：{slug}")
        self._show_message(
            "页面生成完成",
            [
                f"正文文件: {self._fmt_path(article_file)}",
                (
                    f"页面文件: {self._fmt_path(page)}"
                    if page.exists()
                    else "页面文件未落在当前目录，可检查 index.json 中 page_url 配置。"
                ),
                f"本次构建文章数: {summary.get('generated_posts', 0)}",
            ],
        )

    def _run_external(self, command: str, target: Path, wait: bool) -> None:
        cmd = command.strip() or (
            _default_editor_cmd() if wait else _default_file_manager_cmd()
        )
        curses.def_prog_mode()
        curses.endwin()
        try:
            _run_open_command(cmd, target, wait=wait)
            if wait:
                input("\n已从编辑器返回，按回车继续...")
        finally:
            curses.reset_prog_mode()
            self.stdscr.refresh()

    def _action_submit(self) -> None:
        if not self._ensure_ready():
            return
        message = self._input_line(
            "保存更新", "写一句本次更新说明:", default="更新博客内容"
        )
        if message is None or not message:
            return
        change_page = self._run_with_busy(
            "正在处理：生成网页并提交更新...",
            lambda: cmd_submit(
                self.workspace, message, no_open=self.no_browser, quiet=True
            ),
        )
        self._log(f"保存并提交更新：{message}")
        self._log(f"更新摘要页：{change_page.name}")
        self._show_message("保存完成", [f"更新摘要页：{change_page}"])

    def _action_set_theme(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        styles = list_styles(cfg.styles_dir)
        frames = list_frameworks(cfg.frameworks_dir)

        style_items = [
            (f"{name}  [来源: {self._fmt_path(path)}]", name)
            for name, path in styles.items()
        ]
        frame_items = [
            (
                f"{name}（一个 HTML 布局模板等内容） [来源: {self._fmt_path(path)}]",
                name,
            )
            for name, path in frames.items()
        ]
        style_default = (
            [v for _, v in style_items].index(cfg.selected_style) if style_items else 0
        )
        frame_default = (
            [v for _, v in frame_items].index(cfg.selected_framework)
            if frame_items
            else 0
        )

        style = self._choose_from_list(
            "换一个页面风格：选择样式（只影响后续页面）", style_items, style_default
        )
        if style is None:
            return
        frame = self._choose_from_list(
            "换一个页面风格：选择框架（只影响后续页面）", frame_items, frame_default
        )
        if frame is None:
            return

        cmd_set_theme(self.workspace, style=style, framework=frame, quiet=True)
        self._log(f"更新默认样式/框架：{style} / {frame}")
        self._show_message(
            "默认风格已更新",
            ["只影响之后新生成的页面。", f"样式：{style}", f"框架：{frame}"],
        )

    def _action_add_style(self) -> None:
        if not self._ensure_ready():
            return
        name = self._input_line("导入样式", "给新样式起个名字:")
        if name is None or not name:
            return
        css_path = self._input_line("导入样式", "输入 .css 文件路径:")
        if css_path is None or not css_path:
            return

        source = Path(css_path).expanduser().resolve()
        if not source.exists():
            self._show_message("文件不存在", [str(source)])
            return
        target = cmd_add_style(self.workspace, name, source, quiet=True)
        self._log(f"导入样式：{name}")
        self._show_message("导入完成", [self._fmt_path(target)])

    def _action_query_blogs(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        posts = list_existing_blogs(cfg)
        if not posts:
            self._show_message("还没有已登记博客", ['先用"新建博客页"创建内容。'])
            return

        items = []
        for idx, p in enumerate(posts):
            slug = str(p.get("slug", "-"))
            category = str(p.get("category", "-"))
            draft = str(p.get("draft_dir", "-"))
            items.append((f"[{category}] {slug} -> {draft}", str(idx)))
        picked = self._choose_from_list(
            "查看已有博客：按 vim 模式选择一篇", items, default_idx=0
        )
        if picked is None:
            return
        post = posts[int(picked)]
        article_path, folder_path = self._resolve_post_paths(post, cfg)
        if article_path is None and folder_path is None:
            self._show_message("无法打开", ["该条目未记录可用路径。"])
            return

        action = self._choose_open_action(
            "选中后要怎么打开？（编辑 my_blog.txt / 文件管理器 / 都要）",
            include_inline_ai=False,
        )
        if action is None or action == "none":
            return
        try:
            if action in {"editor", "both"}:
                if article_path is None:
                    self._show_message("未找到 my_blog.txt", ["该条目不支持编辑正文。"])
                    return
                self._run_external(cfg.default_editor, article_path, wait=True)
            if action in {"manager", "both"}:
                if folder_path is None:
                    self._show_message("未找到目录", ["该条目不支持打开文件管理器。"])
                    return
                self._run_external(cfg.default_file_manager, folder_path, wait=False)
            self._log(f"查看博客：{post.get('slug', '-')}")
        except Exception as exc:
            self._show_message("打开外部工具失败", [str(exc)])

    # ── 多选列表组件 ───────────────────────────────────────────────────────────

    def _multi_choose_from_list(
        self,
        title: str,
        items: list[tuple[str, str, bool, str]],
        # (display_label, value, default_selected, skip_reason)
        categories: list[str] | None = None,
    ) -> list[str] | None:
        """多选列表。返回被选中的 value 列表，None 表示用户取消。
        键位: j/k 移动  Space 切换  a 强制全选  A 取消全选  c 按分类全选  Enter 确认  q 取消
        """
        selected: dict[str, bool] = {item[1]: item[2] for item in items}
        idx = 0
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, title, self.c_purple)
            self._safe_addstr(
                7,
                2,
                "j/k 移动  Space 切换  a 强制全选  A 取消全选  c 按分类全选  Enter 确认  q 取消",
                self.c_text,
            )
            h, _ = self.stdscr.getmaxyx()
            start_row = 9
            max_visible = max(1, h - start_row - 4)

            if idx < 0:
                idx = 0
            if idx >= len(items):
                idx = len(items) - 1
            offset = max(0, idx - max_visible + 1)

            for i in range(offset, min(len(items), offset + max_visible)):
                label, value, _, skip_reason = items[i]
                is_sel = selected.get(value, False)
                mark = "✓" if is_sel else " "
                prefix = "❯" if i == idx else " "
                row_style = self.c_focus if i == idx else self.c_text
                skip_note = f"  ⚠ {skip_reason}" if skip_reason else ""
                row_y = start_row + (i - offset)
                self._safe_addstr(
                    row_y, 2, f"{prefix} [{mark}] {label}{skip_note}", row_style
                )

            sel_count = sum(1 for v in selected.values() if v)
            footer_row = start_row + min(len(items), max_visible) + 1
            self._safe_addstr(
                footer_row,
                2,
                f"已选 {sel_count}/{len(items)} 篇  按 Enter 确认",
                self.c_blue,
            )
            self._draw_footer()
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key in (ord("j"), curses.KEY_DOWN):
                idx = min(len(items) - 1, idx + 1)
            elif key in (ord("k"), curses.KEY_UP):
                idx = max(0, idx - 1)
            elif key == ord(" "):
                val = items[idx][1]
                selected[val] = not selected.get(val, False)
            elif key == ord("a"):
                for _, val, _, _ in items:
                    selected[val] = True
            elif key == ord("A"):
                for _, val, _, _ in items:
                    selected[val] = False
            elif key == ord("c"):
                if categories:
                    cat_items = [(c, c) for c in categories]
                    chosen_cat = self._choose_from_list(
                        "按分类全选：选择分类", cat_items
                    )
                    if chosen_cat:
                        cat_map = {item[1]: item for item in items}
                        for label, val, _, _ in items:
                            if f"[{chosen_cat}]" in label:
                                selected[val] = True
            elif key in (10, 13, curses.KEY_ENTER):
                return [val for val, v in selected.items() if v]
            elif key == ord("q"):
                return None
            elif key == ord("?"):
                self._show_help()

    # ── 并发批处理进度 ─────────────────────────────────────────────────────────

    def _run_batch_with_progress(
        self,
        title: str,
        tasks: list[tuple[str, "callable"]],
        max_workers: int = 3,
    ) -> list[tuple[str, object, Exception | None]]:
        """并发执行 tasks，期间显示进度条。
        tasks: [(key, fn), ...] 其中 fn 无参数。
        返回: [(key, result, exc_or_None), ...]
        """
        total = len(tasks)
        if total == 0:
            return []

        done_count = [0]
        lock = threading.Lock()
        results_list: list[tuple[str, object, Exception | None]] = []

        def _wrapped(key: str, fn) -> None:
            try:
                res = fn()
                with lock:
                    results_list.append((key, res, None))
            except Exception as exc:
                with lock:
                    results_list.append((key, None, exc))
            finally:
                with lock:
                    done_count[0] += 1

        width = 42
        started = time.time()
        with ThreadPoolExecutor(max_workers=min(max_workers, total)) as executor:
            for key, fn in tasks:
                executor.submit(_wrapped, key, fn)
            while done_count[0] < total:
                self.stdscr.clear()
                self._draw_header()
                self._safe_addstr(8, 4, title, self.c_purple)
                done = done_count[0]
                filled = int(width * done / total)
                bar = "█" * filled + "·" * (width - filled)
                self._safe_addstr(10, 4, f"[{bar}]", self.c_blue)
                self._safe_addstr(
                    12, 4, f"正在处理... ({done}/{total} 完成)", self.c_text
                )
                if time.time() - started >= 60:
                    self._safe_addstr(
                        13, 4, "AI处理速度可能较慢，请耐心等待", self.c_red
                    )
                self._draw_footer()
                self.stdscr.refresh()
                time.sleep(0.1)

        # 最终状态
        self.stdscr.clear()
        self._draw_header()
        self._safe_addstr(8, 4, title, self.c_purple)
        bar = "█" * width
        self._safe_addstr(10, 4, f"[{bar}]", self.c_blue)
        self._safe_addstr(12, 4, f"处理完成 ({total}/{total})", self.c_text)
        self._draw_footer()
        self.stdscr.refresh()
        time.sleep(0.3)
        return results_list

    # ── 改写已有页面风格 ────────────────────────────────────────────────────────

    def _action_restyle_posts(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        posts = list_existing_blogs(cfg)
        if not posts:
            self._show_message("还没有已登记博客", ['先用"新建博客页"创建内容。'])
            return

        # ① 选择改写范围
        scope = self._choose_from_list(
            "改写风格：选择要改哪些（建议先换框架，再单独换样式）",
            [
                ("仅换样式（CSS）", "style"),
                ("仅换框架（HTML）", "framework"),
            ],
            default_idx=0,
        )
        if scope is None:
            return
        change_style = scope == "style"
        change_framework = scope == "framework"

        # ② 选目标 style
        target_style: str | None = None
        if change_style:
            styles = list_styles(cfg.styles_dir)
            style_items = [(name, name) for name in styles]
            if not style_items:
                self._show_message(
                    "没有可用样式", ["请先在 styles/ 目录中添加 CSS 文件。"]
                )
                return
            target_style = self._choose_from_list(
                "选择目标样式（CSS）", style_items, default_idx=0
            )
            if target_style is None:
                return

        # ③ 选目标 framework
        target_framework: str | None = None
        if change_framework:
            frameworks = list_frameworks(cfg.frameworks_dir)
            frame_items = [(name, name) for name in frameworks]
            if not frame_items:
                self._show_message(
                    "没有可用框架", ["请先在 frameworks/ 目录中添加 HTML 模板文件。"]
                )
                return
            target_framework = self._choose_from_list(
                "选择目标框架（HTML 模板）", frame_items, default_idx=0
            )
            if target_framework is None:
                return

        # ④ 构建多选列表并做"已实现"检测
        all_categories: list[str] = []
        multi_items: list[tuple[str, str, bool, str]] = []
        for post in posts:
            slug = post.get("slug", "")
            if not slug:
                continue
            category = post.get("category", "-")
            if category not in all_categories:
                all_categories.append(category)
            cur_style = post.get("style", "__default__")
            cur_framework = post.get("framework", "__default__")
            page_url = post.get("page_url", "").strip()

            label = f"[{category}] {slug}  (当前: {cur_style} / {cur_framework})"

            # 页面文件不存在 → 不可选
            out_path = cfg.output_dir / page_url if page_url else None
            if not out_path or not (cfg.output_dir / page_url).exists():
                multi_items.append((label, slug, False, "页面文件不存在"))
                continue

            # 检测是否已是目标风格
            style_match = change_style and (cur_style == target_style)
            frame_match = change_framework and (cur_framework == target_framework)
            if change_style and change_framework and style_match and frame_match:
                skip_reason = "已是目标风格，自动跳过"
            elif change_style and not change_framework and style_match:
                skip_reason = "样式已是目标，自动跳过"
            elif change_framework and not change_style and frame_match:
                skip_reason = "框架已是目标，自动跳过"
            else:
                skip_reason = ""

            multi_items.append((label, slug, not bool(skip_reason), skip_reason))

        if not multi_items:
            self._show_message("没有可改写的文章", ["index.json 中没有有效条目。"])
            return

        selected_slugs = self._multi_choose_from_list(
            "选择要改写的文章（Space 切换，a 强制全选，A 取消全选，c 按分类）",
            multi_items,
            categories=all_categories,
        )
        if selected_slugs is None:
            return
        if not selected_slugs:
            self._show_message("未选择任何文章", ["请至少选中一篇文章再确认。"])
            return

        # ⑤ 确认
        scope_desc_map = {
            "style": f"样式→{target_style}",
            "framework": f"框架→{target_framework}",
        }
        scope_desc = scope_desc_map[scope]
        confirm = self._choose_from_list(
            f"即将改写 {len(selected_slugs)} 篇文章",
            [
                (f"确认（{scope_desc}）", "yes"),
                ("取消", "no"),
            ],
            default_idx=0,
        )
        if confirm != "yes":
            return

        # ⑥ 并发处理
        tasks = [
            (
                slug,
                lambda s=slug: _restyle_one_post(
                    self.workspace, cfg, s, target_style, target_framework
                ),
            )
            for slug in selected_slugs
        ]
        batch_results = self._run_batch_with_progress(
            f"正在改写 {len(selected_slugs)} 篇文章...",
            tasks,
            max_workers=3,
        )

        # ⑦ 更新 index.json（仅成功项）
        successes = [key for key, _, exc in batch_results if exc is None]
        failures = [(key, str(exc)) for key, _, exc in batch_results if exc is not None]
        if successes:
            index_path = cfg.index_path
            data = _read_index_data(index_path)
            for p in data.get("posts", []):
                if p.get("slug") in successes:
                    if target_style:
                        p["style"] = target_style
                    if target_framework:
                        p["framework"] = target_framework
            index_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        self._log(f"改写风格完成：成功 {len(successes)} 篇，失败 {len(failures)} 篇")

        # ⑧ 结果摘要 + 可选预览
        result_lines: list[str] = [
            f"✓ 成功改写: {len(successes)} 篇",
            f"✗ 失败: {len(failures)} 篇",
        ]
        if failures:
            result_lines.append("")
            result_lines.append("失败详情:")
            for slug, msg in failures:
                result_lines.append(f"  {slug}: {msg[:60]}")
        if successes:
            result_lines += ["", "按 p 在浏览器预览第一篇成功改写的页面，q 关闭。"]

        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(6, 2, "改写风格完成", self.c_purple)
            for i, line in enumerate(result_lines):
                if line.startswith("✓"):
                    col = self.c_blue
                elif line.startswith("✗") or (line.startswith("  ") and ": " in line):
                    col = self.c_red
                else:
                    col = self.c_text
                self._safe_addstr(8 + i, 4, line, col)
            self._draw_footer()
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key == ord("q"):
                return
            if key == ord("p") and successes:
                first_slug = successes[0]
                post_map_now = {
                    p["slug"]: p for p in list_existing_blogs(cfg) if p.get("slug")
                }
                first_post = post_map_now.get(first_slug)
                if first_post:
                    pu = first_post.get("page_url", "")
                    op = cfg.output_dir / pu if pu else None
                    if op and op.exists():
                        try:
                            webbrowser.open(op.resolve().as_uri())
                        except Exception:
                            pass
            if key == ord("?"):
                self._show_help()

    def _action_check_update(self) -> None:
        notice = self._run_with_busy("正在检查版本更新...", _detect_update_notice)
        self.update_notice = notice
        if notice:
            self._log("版本检查：检测到当前版本落后")
            self._show_message("发现可用更新", [notice])
            return
        self._log("版本检查：当前已是最新或暂时无法获取远端信息")
        self._show_message("检查完成", ["当前未检测到落后提交。"])

    def _resolve_post_paths(
        self, post: dict[str, str], cfg: "BlogConfig | None" = None
    ) -> tuple[Path | None, Path | None]:
        article_text = str(post.get("article_file", "")).strip()
        draft_text = str(post.get("draft_dir", "")).strip()
        article_path: Path | None = None
        folder_path: Path | None = None

        if article_text and article_text != "Custom":
            cand = (self.workspace / article_text).resolve()
            if cand.exists() and cand.is_file():
                article_path = cand
                folder_path = cand.parent
        if draft_text and draft_text != "Custom":
            cand = (self.workspace / draft_text).resolve()
            if cand.exists() and cand.is_dir():
                folder_path = cand
                if article_path is None:
                    txt = cand / "my_blog.txt"
                    if txt.exists() and txt.is_file():
                        article_path = txt

        # Custom 类型：通过 page_url 定位页面目录
        if article_path is None and folder_path is None:
            page_url = str(post.get("page_url", "")).strip()
            if page_url:
                if cfg is None:
                    try:
                        cfg = load_config(self.workspace)
                    except Exception:
                        cfg = None
                if cfg is not None:
                    page_file = (cfg.content_dir / page_url).resolve()
                    if page_file.exists() and page_file.is_file():
                        folder_path = page_file.parent

        return article_path, folder_path

    def _action_config_openers(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        current_editor = cfg.default_editor.strip() or _default_editor_cmd()
        current_manager = (
            cfg.default_file_manager.strip() or _default_file_manager_cmd()
        )
        choice = self._choose_from_list(
            "配置默认启动命令",
            [
                (f"设置默认编辑器命令（当前: {current_editor}）", "editor"),
                (f"设置默认文件管理器命令（当前: {current_manager}）", "manager"),
                ("恢复自动默认命令（按系统自动识别）", "reset"),
            ],
            default_idx=0,
        )
        if choice is None:
            return

        if choice == "editor":
            cmd = self._input_line(
                "默认编辑器命令",
                "输入命令（示例: code --wait / nvim / nano）:",
                default=current_editor,
            )
            if cmd is None:
                return
            cfg = cmd_set_open_commands(
                self.workspace, editor_cmd=cmd.strip(), quiet=True
            )
        elif choice == "manager":
            cmd = self._input_line(
                "默认文件管理器命令",
                "输入命令（示例: xdg-open / open / explorer）:",
                default=current_manager,
            )
            if cmd is None:
                return
            cfg = cmd_set_open_commands(
                self.workspace, file_manager_cmd=cmd.strip(), quiet=True
            )
        else:
            cfg = cmd_set_open_commands(self.workspace, reset=True, quiet=True)
        self._log("更新默认编辑器/文件管理器命令")
        self._show_message(
            "配置已保存",
            [
                f"默认编辑器: {cfg.default_editor.strip() or _default_editor_cmd()}",
                f"默认文件管理器: {cfg.default_file_manager.strip() or _default_file_manager_cmd()}",
            ],
        )

    def _action_toggle_restyle_mode(self) -> None:
        """在占位符式精确改写与创作式改写之间切换。"""
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        current = bool(getattr(cfg, "creative_restyle", False))
        mode_label = (
            "创作式改写（更美观，消耗略多 token）"
            if current
            else "精确占位符改写（更可控，较朴素）"
        )

        choice = self._choose_from_list(
            "选择风格改写模式",
            [
                (
                    "精确占位符改写：基于 {content_html} 等占位符重新渲染，结构较固定。",
                    "precise",
                ),
                (
                    "创作式改写：让 AI 阅读 CSS/框架 + 旧页面后重构结构（不改正文）。",
                    "creative",
                ),
            ],
            default_idx=1 if current else 0,
        )
        if choice is None:
            return

        cfg.creative_restyle = choice == "creative"
        save_config(cfg)
        self._log(
            f"已切换风格改写模式为: {'creative' if cfg.creative_restyle else 'precise'}"
        )
        self._show_message(
            "风格改写模式已更新",
            [
                "当前模式："
                + (
                    "创作式改写（不改文章内容，结构/样式更灵活）"
                    if cfg.creative_restyle
                    else "精确占位符改写（结构更可控，视觉相对保守）"
                ),
                "提示：此设置仅影响“改写已有页面风格”菜单，不影响新页面生成。",
            ],
        )

    def _action_set_content_dir(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        current = str(cfg.content_dir.resolve())
        text = self._input_line(
            "设置 content 目录",
            "请输入 content 绝对路径（必须以 /content/ 结尾）:",
            default=current,
        )
        if text is None:
            return
        try:
            content_dir = _validate_content_dir(text)
        except ValueError as exc:
            self._show_message("content 目录无效", [str(exc)])
            return

        cfg.content_dir_path = str(content_dir)
        cfg.content_dir.mkdir(parents=True, exist_ok=True)
        cfg.styles_dir.mkdir(parents=True, exist_ok=True)
        cfg.frameworks_dir.mkdir(parents=True, exist_ok=True)
        write_builtins(cfg.styles_dir, cfg.frameworks_dir)
        write_preview(cfg, open_preview=False)
        save_config(cfg)
        self._log(f"已更新 content 目录：{self._fmt_path(cfg.content_dir)}")
        self._show_message(
            "content 目录已更新",
            [
                f"新目录: {self._fmt_path(cfg.content_dir)}",
                "后续新建、扫描、构建都会使用该目录。",
                "旧目录中的内容不会自动迁移，请按需手动移动。",
            ],
        )

    def _action_ai_generate_assets(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)
        kind = self._choose_from_list(
            "AI 生成：选择类型",
            [("生成样式（CSS）", "style"), ("生成框架（HTML 模板）", "framework")],
            default_idx=0,
        )
        if kind is None:
            return

        goal = self._input_line("AI 生成", "描述你想要的风格/布局目标:")
        if goal is None or not goal:
            return
        name = self._input_line("AI 生成", "给生成结果起个文件名（不带后缀）:")
        if name is None or not name:
            return

        agent = BlogAgent(cfg)
        try:
            if kind == "style":
                content = self._run_with_busy(
                    "正在调用 AI 生成样式...", lambda: agent.generate_style(goal)
                )
                target = cfg.styles_dir / f"{name}.css"
            else:
                content = self._run_with_busy(
                    "正在调用 AI 生成框架...", lambda: agent.generate_framework(goal)
                )
                target = cfg.frameworks_dir / f"{name}.html"

            while True:
                preview_path = self._write_temp_preview(cfg, kind, name, content)
                try:
                    webbrowser.open(preview_path.resolve().as_uri())
                except Exception:
                    pass
                decision = self._choose_from_list(
                    "生成完成：你想怎么做？",
                    [
                        ("满意，保存", "save"),
                        ("不满意，继续修改一轮", "revise"),
                        ("放弃本次生成", "drop"),
                    ],
                    default_idx=0,
                )
                if decision is None or decision == "drop":
                    self._show_message("已取消", ["本次生成未保存。"])
                    return
                if decision == "save":
                    break
                feedback = self._input_line("继续修改", "告诉 AI 你希望改哪里:")
                if feedback is None or not feedback:
                    continue
                content = self._run_with_busy(
                    "正在根据反馈重新生成...",
                    lambda: agent.refine_asset(kind, content, feedback),
                )

            target.write_text(content.strip() + "\n", encoding="utf-8")
            self._log(f"AI 生成{kind}: {self._fmt_path(target)}")
            self._open_generated_preview(cfg, kind, target)
            self._show_message(
                "生成完成",
                [
                    f"已写入: {self._fmt_path(target)}",
                    "如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。",
                ],
            )
        except Exception as exc:
            self._show_message("生成失败", [str(exc)])

    def _action_edit_template_file(self) -> None:
        if not self._ensure_ready():
            return
        cfg = load_config(self.workspace)

        which = self._choose_from_list(
            "打开模板目录并编辑：选择类型",
            [
                (
                    f"样式文件（来源目录: {self._fmt_path(cfg.styles_dir)}）",
                    "style",
                ),
                (
                    f"框架文件（一个 HTML 布局模板等内容，来源目录: {self._fmt_path(cfg.frameworks_dir)}）",
                    "framework",
                ),
            ],
        )
        if which is None:
            return

        if which == "style":
            entries = list_styles(cfg.styles_dir)
        else:
            entries = list_frameworks(cfg.frameworks_dir)

        items = [
            (f"{name} [来源: {self._fmt_path(path)}]", str(path))
            for name, path in entries.items()
        ]
        picked = self._choose_from_list("选择要修改的文件", items)
        if picked is None:
            return

        target = Path(picked)
        self._run_external(cfg.default_editor, target, wait=True)
        self._log(f"编辑模板文件：{self._fmt_path(target)}")
        self._show_message("已打开编辑器", [self._fmt_path(target)])

    def _show_sync_placeholder(self) -> None:
        choice = self._choose_from_list(
            "选择要同步的内容（待实现）",
            [
                ("1. 样式", "style"),
                ("2. 框架", "framework"),
                ("3. 我都要", "both"),
            ],
        )
        if choice is None:
            return
        self._show_message(
            "待实现",
            [f"你选择了：{choice}", "该功能已保留入口，后续再接入真正同步逻辑。"],
        )

    def _run_with_busy(self, title: str, fn):
        state = {"done": False, "result": None, "error": None}
        started = time.time()

        def _target():
            try:
                state["result"] = fn()
            except Exception as exc:
                state["error"] = exc
            finally:
                state["done"] = True

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        step = 0
        while not state["done"]:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(8, 4, title, self.c_purple)
            width = 42
            filled = step % (width + 1)
            bar = "█" * filled + "·" * (width - filled)
            self._safe_addstr(10, 4, f"[{bar}]", self.c_blue)
            self._safe_addstr(12, 4, "正在处理...请稍候", self.c_text)
            if time.time() - started >= 60:
                self._safe_addstr(13, 4, "AI生成速度可能较慢，请耐心等待", self.c_red)
            self._draw_footer()
            self.stdscr.refresh()
            time.sleep(0.06)
            step += 1
        t.join()
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    def _open_generated_preview(self, cfg: BlogConfig, kind: str, target: Path) -> None:
        cfg.previews_dir.mkdir(parents=True, exist_ok=True)
        if kind == "style":
            example = cfg.frameworks_dir / "example.html"
            if not example.exists():
                write_preview(cfg, open_preview=False)
            tpl = (cfg.frameworks_dir / "example.html").read_text(encoding="utf-8")
            preview = cfg.previews_dir / f"generated-style-{target.stem}.html"
            style_href = os.path.relpath(target, preview.parent).replace("\\", "/")
            preview.write_text(
                render_template_placeholders(
                    tpl,
                    title=f"样式预览 {target.stem}",
                    blog_name="Generated Style",
                    subtitle="AI 生成样式预览",
                    date="today",
                    content_html=(
                        "<p>这是自动预览页面。</p>"
                        "<p>如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。</p>"
                    ),
                    style_href=style_href,
                ),
                encoding="utf-8",
            )
            webbrowser.open(preview.resolve().as_uri())
            return

        tpl = target.read_text(encoding="utf-8")
        preview = cfg.previews_dir / f"generated-framework-{target.stem}.html"
        rendered = render_template_placeholders(
            tpl,
            title=f"框架预览 {target.stem}",
            blog_name="Generated Framework",
            subtitle="本html没有使用任何样式(css)",
            date="today",
            content_html="<p>这是自动预览页面。</p>",
            style_href="",
        ).replace('<link rel="stylesheet" href="" />', "")
        rendered = rendered.replace(
            "</body>", "<p style='padding:16px;'>本html没有使用任何样式(css)</p></body>"
        )
        preview.write_text(rendered, encoding="utf-8")
        webbrowser.open(preview.resolve().as_uri())

    def _write_temp_preview(
        self, cfg: BlogConfig, kind: str, name: str, content: str
    ) -> Path:
        cfg.previews_dir.mkdir(parents=True, exist_ok=True)
        if kind == "style":
            css_path = cfg.previews_dir / f"tmp-{name}.css"
            css_path.write_text(content.strip() + "\n", encoding="utf-8")
            example = cfg.frameworks_dir / "example.html"
            if not example.exists():
                write_preview(cfg, open_preview=False)
            tpl = example.read_text(encoding="utf-8")
            preview = cfg.previews_dir / f"tmp-style-{name}.html"
            preview.write_text(
                render_template_placeholders(
                    tpl,
                    title=f"临时样式预览 {name}",
                    blog_name="Preview",
                    subtitle="未保存版本",
                    date="today",
                    content_html=(
                        "<p>这是未保存预览。</p>"
                        "<p>如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。</p>"
                    ),
                    style_href=css_path.name,
                ),
                encoding="utf-8",
            )
            return preview

        preview = cfg.previews_dir / f"tmp-framework-{name}.html"
        rendered = content.replace('<link rel="stylesheet" href="{style_href}" />', "")
        rendered = rendered.replace(
            "</body>", "<p style='padding:16px;'>本html没有使用任何样式(css)</p></body>"
        )
        preview.write_text(
            render_template_placeholders(
                rendered,
                title=f"临时框架预览 {name}",
                blog_name="Preview",
                subtitle="未保存版本",
                date="today",
                content_html="<p>这是未保存预览。</p>",
                style_href="",
            ),
            encoding="utf-8",
        )
        return preview

    def _show_logs(self) -> None:
        if not self.logs:
            self._show_message(
                "动作日志", ["暂无记录", "执行一次操作后可在此查看概要。"]
            )
            return

        pos = 0
        while True:
            self.stdscr.clear()
            self._draw_header()
            self._safe_addstr(
                6, 2, "动作日志（只展示做了什么，不展示文件细节）", self.c_purple
            )

            h, _ = self.stdscr.getmaxyx()
            visible = max(4, h - 13)
            window = self.logs[
                max(0, len(self.logs) - visible - pos) : len(self.logs) - pos
            ]
            for i, line in enumerate(window):
                self._safe_addstr(8 + i, 4, f"- {line}", self.c_text)

            self._safe_addstr(h - 5, 2, "j/k 滚动, q 返回", self.c_text)
            self._draw_footer()
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key == ord("q"):
                return
            if key in (ord("j"), curses.KEY_DOWN):
                pos = min(len(self.logs), pos + 1)
            if key in (ord("k"), curses.KEY_UP):
                pos = max(0, pos - 1)
            if key == ord("?"):
                self._show_help()
            if key == ord(":") and self._command_palette():
                return

    def _command_palette(self) -> bool:
        cmd = self._input_line("命令模式", "输入命令（:logs / :q）:", default="")
        if cmd is None:
            return False
        command = cmd.strip().lstrip(":")
        if command == "logs":
            self._show_logs()
            return True
        if command in {"q", "quit", "exit"}:
            self.running = False
            return True
        self._show_message("未知命令", [f":{command}", "可用命令: :logs, :q"])
        return False

    def _show_help(self) -> None:
        self._show_message(
            "键位帮助",
            [
                "j/k 或 ↑/↓: 在列表中移动",
                "Enter: 确认当前操作",
                "1~9: 直达对应编号菜单",
                "q: 返回上一页（主菜单下 q 退出）",
                "?: 打开帮助",
                ":logs: 打开动作日志",
                "Ctrl+Z: 暂停程序，fg 恢复运行",
            ],
        )

    def _ensure_ready(self) -> bool:
        if (self.workspace / "blogauto.json").exists():
            return True
        self._show_message(
            "还没完成一键准备", ["请先执行菜单 [1] 第一次使用：一键准备"]
        )
        return False

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")
        if len(self.logs) > 300:
            self.logs = self.logs[-300:]

    def _draw_box(self, y: int, x: int, h: int, title: str) -> None:
        _, w = self.stdscr.getmaxyx()
        width = max(20, w - x - 3)
        if y + h >= self.stdscr.getmaxyx()[0]:
            return
        self._safe_addstr(y, x, "╭" + "─" * (width - 2) + "╮", self.c_blue)
        self._safe_addstr(
            y + 1, x, "│ " + title[: width - 4].ljust(width - 4) + " │", self.c_purple
        )
        for i in range(2, h - 1):
            self._safe_addstr(y + i, x, "│" + " " * (width - 2) + "│", self.c_blue)
        self._safe_addstr(y + h - 1, x, "╰" + "─" * (width - 2) + "╯", self.c_blue)

    def _safe_addstr(self, y: int, x: int, text: str, style: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h:
            return
        if x < 0:
            x = 0
        room = w - x - 1
        if room <= 0:
            return
        try:
            self.stdscr.addstr(y, x, text[:room], style)
        except curses.error:
            pass


def run_tui(workspace: Path, no_browser: bool, update_notice: str | None = None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("错误: 交互式 TUI 需要在终端中运行")
        return 2

    def _wrapped(stdscr: curses.window) -> int:
        app = VimTUIApp(stdscr, workspace, no_browser, update_notice=update_notice)
        return app.run()

    return curses.wrapper(_wrapped)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIBlogAuto（Vim 风格全屏 TUI）")
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd() / "my_blog"),
        help="工作目录，默认 ./my_blog",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="禁用自动打开浏览器预览",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()

    if shutil.which("git") is None:
        print("警告: 未检测到 git，提交功能将不可用")

    try:
        return run_tui(workspace, no_browser=args.no_browser)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
