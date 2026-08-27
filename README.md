# BananaPrism

BananaPrism 是面向 Windows 的简体中文 AI 图像工作室，可通过 OpenRouter 或
AiHubMix 调用 Gemini 图像模型，完成文生图、局部标注编辑、串行任务队列和自动保存。

当前版本由 `pyproject.toml` 唯一确定，并在构建时同步写入窗口、PE 资源和发布清单。

## 功能

- 三档 Gemini 图像模型、六种宽高比；Gemini 3 系列支持 1K/2K/4K，
  Gemini 2.5 Flash Image 按官方能力仅开放 1K。
- 本地 PNG/JPEG/WEBP/BMP 导入。
- 矩形、笔刷、橡皮擦与轻量撤销；标注颜色可选红、绿、洋红、青、黄，默认红色。
- 编辑请求同时发送彩色标注图和同尺寸黑白选区蒙版，强化 AiHubMix/OpenRouter 对局部位置的识别。
- 发送前对原图、标注图、黑白蒙版及最终请求参数进行预检。
- 串行生成队列、失败重试、连续失败三次暂停、请求取消。
- 图片、模型文字说明、日志、保存路径与每日次数面板。
- 多组 OpenRouter/AiHubMix API 预设和受密码保护的控制台；验证后可掩码查看、显隐并复制所选密钥。
- 自动保存图片及 JSON sidecar；API Key 使用 Windows DPAPI CurrentUser 加密。
- 生成和编辑可选择 PNG/JPEG 及 72/96/150/300 DPI，并显示所选 1K/2K/4K 与比例的参考像素尺寸；
  只修改保存容器和 DPI 元数据，不缩放服务商返回的像素。若容器未保留请求 DPI，日志和 JSON sidecar
  会记录真实回读值。
- OpenRouter 的 4K 生成与编辑使用官方 Images API，不替换 Preview 模型、不降低分辨率；
  该接口只返回图片，因此 4K 结果通常没有模型文字说明。
- 模型、尺寸、比例、保存格式、DPI 与标注颜色会自动记住上次选择；队列冻结入队时的参数。
- 图片放大后可在查看模式按住鼠标左键拖动画布。
- OpenRouter 4K 编辑显示上传、服务商生成、结果下载三个阶段，并允许最长 15 分钟后安全超时或手动取消。
- 启动时自动居中并最大化；API 故障区分 HTTP 状态、DNS、TLS、超时和连接错误。
- 单实例运行，避免两个进程并发覆盖设置与凭据。
- 简体中文文案集中在语言目录，可注册其他 locale 并逐项回退中文。

## 开发运行

要求 Windows x64、Python 3.12 或 3.13。

```powershell
python -m pip install -e ".[dev]"
python -m banana_prism
```

测试时使用临时数据目录，禁止读取真实用户设置或发送真实 API 请求：

```powershell
pytest -p no:cacheprovider
```

## 构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

输出目录为 `dist\BananaPrism`。便携包未签名；构建脚本会生成 SHA-256 清单。
每次构建还会生成离屏主界面截图并验证便携包启动、版本、单实例锁和隔离设置。

生成结果以“同名结果目录”发布：目录内包含图片与同 stem JSON sidecar；两者先在
隐藏暂存目录完整写入并校验，再一次性发布，避免只留下半份结果。

## 数据位置与旧版迁移

- 新设置：`%APPDATA%\BananaPrism\settings.json`
- 加密凭据：`%APPDATA%\BananaPrism\secrets.v1.dpapi`
- 日志：`%APPDATA%\BananaPrism\logs`
- 默认图片：`%USERPROFILE%\Pictures\BananaPrism`

首次正常启动会检测 `%APPDATA%\NanaBananaStudio\settings.json`。只有在新凭据已加密、
原子写入并成功回读后，才会从旧设置中移除明文 API Key。旧图片和日志不会移动。

## 版本管理

应用遵循 Semantic Versioning：

- `PATCH`：兼容性修复；
- `MINOR`：向后兼容的新功能；
- `MAJOR`：不兼容的行为或数据格式变化。

版本唯一来源为 `pyproject.toml`，正式发布标签格式为 `vMAJOR.MINOR.PATCH`。
