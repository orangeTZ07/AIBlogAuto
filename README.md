# AIBlogAuto

以 AI 为特色的模块化博客生成器（Python + DeepSeek API），现代化全屏 TUI。
![项目截图](./screenshot/AIBlogAuto.png "界面预览")

## 特性

- AI Agent: 自动生成博客
- 霓虹粉风格界面：现代分区布局 + 高亮焦点。
- 霓虹紫蓝红主题：全屏刷新、状态栏、焦点注解。
- Vim 风格：`j/k` 导航、`q` 返回上一页、`?` 查看帮助。
- 一键准备支持 AI 服务商设置：DeepSeek / OpenAI 兼容 / Anthropic / 自定义兼容接口。
- 样式/框架模块化：统一存放在工作目录并复用，不重复复制。
- Prompt 模板：自动生成 `prompts/*.prompt.txt` 供 Codex/Claude/Copilot 使用。
- 提交流程：扫描内容目录维护主页文章页，并可生成变动目录页面。

## 安装

```bash
bash scripts/install.sh
```

## 运行

```bash
source .venv/bin/activate
aiblogauto
```

可选参数：

```bash
aiblogauto --workspace ./my_blog --no-browser
```

## 键位说明

- `j/k` 或方向键：上下移动
- `Enter`：确认
- `1~9`：按编号直达对应功能
- `q`：返回上一页（主菜单下为退出）
- `?`：显示键位帮助
- `:logs`：查看动作日志
- `Ctrl+Z`：暂停程序，终端输入 `fg` 恢复
- 输入页：`i` 进入输入模式，`Esc` 退出输入模式

## 草稿文件说明

- 文章正文文件：`my_blog.txt`
- 提示词文件：`prompt.txt`
- 根目录登记：`index.json`（记录文章位置，便于后续查找与构建）

## DeepSeek 配置

先配置环境变量：

```bash
export DEEPSEEK_API_KEY="your_key_here"
```

默认模型与地址：

- `deepseek-chat`
- `https://api.deepseek.com`

可在工作目录的 `blogauto.json` 修改：

- `ai_provider`
- `ai_model`
- `ai_base_url`

安全说明：

- API Key 不会写入项目配置文件（避免提交到 GitHub）。
- 建议用环境变量管理，如 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`。
- 交互初始化时也可选择写入单独密钥文件（默认 `.blogauto-secrets.json`），程序会自动加入 `.gitignore`，但你仍需自行确保不外泄。
- 交互初始化时可选择 API Key 来源：环境变量或单独密钥文件。

预览提示：

- 如果浏览器预览调用失败，请自行将模板所在路径放入浏览器地址栏进行预览。

## Nerd Font

TUI 使用 Nerd Font 图标（如 `JetBrainsMono Nerd Font`）。

- 下载地址: <https://www.nerdfonts.com/font-downloads>
- 将终端字体切换为 Nerd Font 后可获得完整图标显示。

## 目录结构（初始化后）

- `content/**/my_blog.txt`: 文章素材（支持自定义目录结构）
- `content/**/prompt.txt`: 对 AI 助手的页面生成提示词
- `content/styles/*.css`: 样式文件
- `content/frameworks/*.html`: 页面框架模板
- `prompts/*.prompt.txt`: AI 工具提示词
- `index.json`: 草稿位置索引（程序根目录）
- `content/index.html`: 网站主页（静态站点根）
- `changes/changes-*.html`: 提交后变动目录页

部署提示（以 GitHub Pages 为例）：

- 部署网页时请将 `content/*` 放在仓库根目录。
