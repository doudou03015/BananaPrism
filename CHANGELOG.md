# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

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
