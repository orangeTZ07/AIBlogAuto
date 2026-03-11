from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


CONFIG_FILE = "blogauto.json"


@dataclass
class BlogConfig:
    workspace: Path
    content_dir_path: str = ""
    selected_style: str = "clean-light"
    selected_framework: str = "classic"
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    ai_provider: str = "deepseek"
    ai_model: str = "deepseek-chat"
    ai_base_url: str = "https://api.deepseek.com"
    ai_key_source: str = "env"
    ai_secret_file: str = ".blogauto-secrets.json"
    draft_structure_template: str = "{slug}"
    default_editor: str = ""
    default_file_manager: str = ""

    @property
    def content_dir(self) -> Path:
        raw = self.content_dir_path.strip()
        if raw:
            return Path(raw).expanduser()
        return self.workspace / "content"

    @property
    def output_dir(self) -> Path:
        return self.content_dir

    @property
    def index_path(self) -> Path:
        return self.content_dir / "index.json"

    @property
    def styles_dir(self) -> Path:
        return self.content_dir / "styles"

    @property
    def frameworks_dir(self) -> Path:
        return self.content_dir / "frameworks"

    @property
    def prompts_dir(self) -> Path:
        return self.workspace / "prompts"

    @property
    def changes_dir(self) -> Path:
        return self.workspace / "changes"

    @property
    def previews_dir(self) -> Path:
        return self.workspace / "previews"


def load_config(workspace: Path) -> BlogConfig:
    cfg_path = workspace / CONFIG_FILE
    if not cfg_path.exists():
        raise FileNotFoundError(f"未找到配置文件: {cfg_path}")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    return BlogConfig(
        workspace=workspace,
        content_dir_path=data.get("content_dir_path", ""),
        selected_style=data.get("selected_style", "clean-light"),
        selected_framework=data.get("selected_framework", "classic"),
        deepseek_model=data.get("deepseek_model", "deepseek-chat"),
        deepseek_base_url=data.get("deepseek_base_url", "https://api.deepseek.com"),
        ai_provider=data.get("ai_provider", "deepseek"),
        ai_model=data.get("ai_model", data.get("deepseek_model", "deepseek-chat")),
        ai_base_url=data.get("ai_base_url", data.get("deepseek_base_url", "https://api.deepseek.com")),
        ai_key_source=data.get("ai_key_source", "env"),
        ai_secret_file=data.get("ai_secret_file", ".blogauto-secrets.json"),
        draft_structure_template=data.get("draft_structure_template", "{slug}"),
        default_editor=data.get("default_editor", ""),
        default_file_manager=data.get("default_file_manager", ""),
    )


def save_config(config: BlogConfig) -> None:
    cfg_path = config.workspace / CONFIG_FILE
    cfg_path.write_text(
        json.dumps(
            {
                "selected_style": config.selected_style,
                "selected_framework": config.selected_framework,
                "content_dir_path": config.content_dir_path,
                "deepseek_model": config.deepseek_model,
                "deepseek_base_url": config.deepseek_base_url,
                "ai_provider": config.ai_provider,
                "ai_model": config.ai_model,
                "ai_base_url": config.ai_base_url,
                "ai_key_source": config.ai_key_source,
                "ai_secret_file": config.ai_secret_file,
                "draft_structure_template": config.draft_structure_template,
                "default_editor": config.default_editor,
                "default_file_manager": config.default_file_manager,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
