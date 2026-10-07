# BananaPrism

[简体中文](#简体中文) | [English](#english)

## 简体中文

BananaPrism 是面向 Windows 的简体中文 AI 图像工作室，可通过 OpenRouter 或
AiHubMix 调用 Gemini 图像模型，完成文生图、局部标注编辑、串行任务队列和自动保存。

当前版本由 `pyproject.toml` 唯一确定，并在构建时同步写入窗口、PE 资源和发布清单。

### 功能

- 四款 Gemini 图像模型（含 Nano Banana 2.1）、六种宽高比；Nano Banana 2.1 和 Gemini 3 系列支持 1K/2K/4K，
  Gemini 2.5 Flash Image 按官方能力仅开放 1K。
- 本地 PNG/JPEG/WEBP/BMP 导入。
- 矩形、笔刷、橡皮擦与轻量撤销；标注颜色可选红、绿、洋红、青、黄，默认红色。
- 编辑请求只发送原图和彩色标注图两张 JPEG 95 网络副本；黑白选区蒙版仅在本地用于生成和校验标注，不上传。透明输入的两张副本会采用完全相同的白色背景合成策略，避免透明像素在 JPEG 化时造成位置或颜色差异。
- 两张网络副本始终使用相同像素尺寸；完整 JSON 超过 16 MiB 时自动按同一比例缩小（包括必要时将极窄边缩到 512 px 以下），避免大图触发 413/502，同时保持标注坐标一致。
- 发送前对原图、彩色标注图、本地蒙版、网络副本策略及最终请求参数进行预检。
- 串行生成队列、失败重试、连续失败三次暂停、请求取消。
- 图片、模型文字说明、日志、保存路径与每日次数面板。
- 多组 OpenRouter/AiHubMix API 预设和受密码保护的控制台；验证后可掩码查看、显隐并复制所选密钥。
- 自动保存图片及 JSON sidecar；API Key 使用 Windows DPAPI CurrentUser 加密。
- 生成和编辑可选择 PNG/JPEG 及 72/96/150/300 DPI，并显示所选 1K/2K/4K 与比例的参考像素尺寸；
  只修改保存容器和 DPI 元数据，不缩放服务商返回的像素。若容器未保留请求 DPI，日志和 JSON sidecar
  会记录真实回读值。
- Nano Banana 2.1 在 OpenRouter 的所有尺寸、旧模型的 4K 生成与编辑使用官方 Images API，
  保留所选模型和分辨率；该接口只返回图片，通常没有模型文字说明。
- AiHubMix 使用官方稳定域名；Nano Banana 2.1 使用 Gemini 原生非流式接口，解析时排除思考阶段的中间图片和文字。
- 模型、尺寸、比例、保存格式、DPI 与标注颜色会自动记住上次选择；队列冻结入队时的参数。
- 图片放大后可在查看模式按住鼠标左键拖动画布。
- OpenRouter 4K 编辑显示上传、服务商生成、结果下载三个阶段，并允许最长 15 分钟后安全超时或手动取消。
- 启动时自动居中并最大化；API 故障区分 HTTP 状态、DNS、TLS、超时和连接错误。
- 单实例运行，避免两个进程并发覆盖设置与凭据。
- 简体中文文案集中在语言目录，可注册其他 locale 并逐项回退中文。

### 使用 Nano Banana 2.1

在控制台添加 OpenRouter 或 AiHubMix 预设并填写对应平台的 API Key，然后在模型下拉框选择
“Nano Banana 2.1”。全新配置默认选择该模型；升级时保留已有模型选择和预设。

- OpenRouter 模型 ID：`google/gemini-nano-banana-2.1`。
- AiHubMix 模型 ID：`gemini-nano-banana-2.1`，应用自动转换平台所需的名称。
- 生成、标注编辑和队列共享 1K/2K/4K 设置；两张编辑参考图仍受 16 MiB 请求预算保护。

接入依据、验证范围及实机验收步骤见 [Nano Banana 2.1 适配说明](docs/nano-banana-2.1.md)。
API Key 使用 Windows 当前用户加密，换电脑或 Windows 账户后应重新在预设中填写。

### 开发运行

要求 Windows x64、Python 3.12 或 3.13。

```powershell
python -m pip install -e ".[dev]"
python -m banana_prism
```

测试时使用临时数据目录，禁止读取真实用户设置或发送真实 API 请求：

```powershell
pytest -p no:cacheprovider
```

### 构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

输出目录为 `dist\BananaPrism`。便携包未签名；构建脚本会生成 SHA-256 清单。
每次构建还会生成离屏主界面截图并验证便携包启动、版本、单实例锁和隔离设置。

生成结果以“同名结果目录”发布：目录内包含图片与同 stem JSON sidecar；两者先在
隐藏暂存目录完整写入并校验，再一次性发布，避免只留下半份结果。

### 数据位置与旧版迁移

- 新设置：`%APPDATA%\BananaPrism\settings.json`
- 加密凭据：`%APPDATA%\BananaPrism\secrets.v1.dpapi`
- 日志：`%APPDATA%\BananaPrism\logs`
- 默认图片：`%USERPROFILE%\Pictures\BananaPrism`

首次正常启动会检测 `%APPDATA%\NanaBananaStudio\settings.json`。只有在新凭据已加密、
原子写入并成功回读后，才会从旧设置中移除明文 API Key。旧图片和日志不会移动。

### 版本管理

应用遵循 Semantic Versioning：

- `PATCH`：兼容性修复；
- `MINOR`：向后兼容的新功能；
- `MAJOR`：不兼容的行为或数据格式变化。

版本唯一来源为 `pyproject.toml`，正式发布标签格式为 `vMAJOR.MINOR.PATCH`。

---

## English

BananaPrism is a Windows AI image workstation with a Simplified Chinese interface. It connects to Gemini image models through OpenRouter or AiHubMix and provides text-to-image generation, region-guided editing, a serial task queue, and automatic saving.

The application version has a single source of truth in `pyproject.toml`. The build process propagates it to the window title, Windows PE resources, and release manifest.

### Features

- Four Gemini image models, including Nano Banana 2.1, and six aspect ratios. Nano Banana 2.1 and Gemini 3 models support 1K/2K/4K; Gemini 2.5 Flash Image is limited to 1K according to its published capabilities.
- Local PNG/JPEG/WEBP/BMP import.
- Rectangle, brush, eraser, and lightweight undo tools. Annotation colors include red, green, magenta, cyan, and yellow; red is the default.
- Edit requests upload exactly two JPEG 95 network copies: the original image and a colored annotation guide. The binary selection mask remains local for generating and validating the guide and is never uploaded. Transparent inputs use the same white-background compositing policy for both copies.
- Both network copies always have identical pixel dimensions. If the complete JSON request exceeds 16 MiB, both copies are scaled down by the same factor—including below a 512-pixel narrow edge when necessary—to avoid 413/502 failures while preserving annotation coordinates.
- A preflight dialog reviews the original, colored guide, local mask, network-copy policy, and final request parameters before sending.
- Serial generation queue, retry support, automatic pause after three consecutive failures, and request cancellation.
- Image preview, model text response, runtime log, save-path, and daily-count panels.
- Multiple OpenRouter/AiHubMix API presets and a password-protected console. After authentication, the selected key can be viewed in masked form, revealed, and copied.
- Automatic image and JSON sidecar saving. API keys are encrypted with Windows DPAPI CurrentUser.
- PNG/JPEG output and 72/96/150/300 DPI choices for generation and editing, with reference pixel dimensions for the selected 1K/2K/4K size and aspect ratio. These options change only the saved container and DPI metadata; they do not rescale provider-returned pixels. If a container cannot retain the requested DPI, the actual decoded value is recorded in the log and JSON sidecar.
- Nano Banana 2.1 uses OpenRouter's official Images API at every resolution; older models retain that route for 4K. The selected model and resolution are preserved. This endpoint normally returns image data without a text explanation.
- AiHubMix uses its stable domain. Nano Banana 2.1 uses non-streaming Gemini Native requests; intermediate thinking images and text are excluded from results.
- The last model, size, ratio, output format, DPI, and annotation color are restored automatically. Queue items freeze their parameters when added.
- When an image is zoomed in, hold the left mouse button in view mode to pan the canvas.
- OpenRouter 4K editing reports upload, provider processing, and download phases, with safe cancellation or a maximum 15-minute timeout.
- The main window starts centered and maximized. API diagnostics distinguish HTTP status errors, DNS failures, TLS failures, timeouts, and connection errors.
- Single-instance operation prevents concurrent processes from overwriting settings or credentials.
- Simplified Chinese strings are centralized in the locale catalog, with registration and per-key fallback support for future languages.

### Using Nano Banana 2.1

Add an OpenRouter or AiHubMix preset with that provider's API key in the console, then select
Nano Banana 2.1. Fresh profiles select it by default; existing profiles keep their model selection.
The app uses `google/gemini-nano-banana-2.1` on OpenRouter and `gemini-nano-banana-2.1` on AiHubMix.
Generation, region-guided edits, and queued tasks support 1K/2K/4K.

See the [integration notes](docs/nano-banana-2.1.md) for protocol sources, verification scope,
and live acceptance steps. Keys are encrypted for the current Windows user and must be entered
again after moving to another computer or Windows account.

### Development

Requires Windows x64 and Python 3.12 or 3.13.

```powershell
python -m pip install -e ".[dev]"
python -m banana_prism
```

Tests use an isolated temporary data directory and must not read real user settings or send real API requests:

```powershell
pytest -p no:cacheprovider
```

### Build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

The output directory is `dist\BananaPrism`. The portable build is currently unsigned, and the build script generates a SHA-256 manifest. Each build also captures an offscreen UI screenshot and verifies packaged startup, version metadata, the single-instance lock, and isolated settings.

Each generated result is published as a same-named result directory containing the image and a JSON sidecar with the same stem. Both files are fully written and verified in a hidden staging directory before the directory is atomically published, preventing half-written result pairs.

### Data locations and legacy migration

- Settings: `%APPDATA%\BananaPrism\settings.json`
- Encrypted credentials: `%APPDATA%\BananaPrism\secrets.v1.dpapi`
- Logs: `%APPDATA%\BananaPrism\logs`
- Default image directory: `%USERPROFILE%\Pictures\BananaPrism`

On the first normal launch, BananaPrism checks `%APPDATA%\NanaBananaStudio\settings.json`. Plaintext API keys are removed from the legacy settings only after the new credentials have been encrypted, atomically written, and successfully read back. Existing images and logs are not moved.

### Versioning

BananaPrism follows Semantic Versioning:

- `PATCH`: backward-compatible fixes;
- `MINOR`: backward-compatible features;
- `MAJOR`: incompatible behavior or data-format changes.

`pyproject.toml` is the only version source. Formal release tags use `vMAJOR.MINOR.PATCH`.
