# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [1.0.2] - 2026-08-27

### Changed

- OpenRouter 4K 使用官方专用 Images API，保留正式模型 ID 与所选比例；1K/2K 继续使用 Chat Completions。
- 1K、2K、4K 生成结果与编辑结果统一尝试写入 300 DPI 元数据，不改变像素尺寸；
  容器不支持时记录真实回读值并提示。

### Fixed

- 修复 `google/gemini-3.1-flash-image` 经 Chat Completions 请求 4K 时被当前后端以 HTTP 400 拒绝的问题。
- 兼容 Images API 的 `data[].b64_json` 响应与两张有序编辑参考图。

## [1.0.1] - 2026-08-27

### Changed

- 主窗口启动时自动居中并最大化，保留位于当前屏幕工作区内的还原位置。
- 通过控制台密码验证后，可对所选预设的 API Key 进行掩码查看、显隐和复制。
- 生成或编辑成功后同步保存同名 JSON sidecar，并在日志中显示其路径；保留旧版核心字段。

### Fixed

- 修复 Qt 将 OpenRouter HTTP 401/402/429/5xx 响应误报为普通网络故障的问题。
- 网络错误现在区分服务商 HTTP 响应、DNS、TLS、超时、拒绝连接和连接中断，并在显示前清洗和脱敏详情。
- 兼容 OpenRouter 在 HTTP 200 响应或 SSE 中返回的嵌入式 provider error，避免误判为文字-only 结果。

## [1.0.0] - 2026-08-26

### Added

- 从 NanaBananaStudio 3.0.0 发布包恢复并重建全部已确认业务功能。
- BananaPrism 新品牌、Windows DPAPI 凭据保护、可取消网络任务和安全队列。
- Python 3.12/3.13 源码、自动化测试及 PyInstaller onedir 构建流程。

### Changed

- 更新 Gemini 图像模型至正式 GA 标识。
- 按模型能力限制输出尺寸；Gemini Native 使用大小写正确的 1K/2K/4K 参数。
- 编辑分辨率与宽高比变为可见的共享设置。
- 标注统一为黄色 PNG，撤销历史改为轻量命令。

### Fixed

- 修复文字-only 响应锁死线程、队列启动竞态和关闭活动线程的问题。
- 修复旧设置静默损坏、API Key 明文、编辑 DPI sidecar 错误与大图撤销内存暴涨。
- 修复队列行移动导致的终态错配，并通过编码回读记录真实 DPI。
- 增加单实例保护，阻止并发覆盖设置和加密凭据。
