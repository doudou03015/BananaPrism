# 本地数据与隐私说明

BananaPrism 不提供自有账户或云同步。图像请求会发送到用户所选的 OpenRouter 或
AiHubMix；服务商如何处理请求由其各自条款决定。

## 本地保存内容

- API Key 仅保存在 Windows DPAPI CurrentUser 加密文件中，不写入普通 JSON、日志或
  图片 sidecar。
- 控制台密码只保存不可逆验证数据，首次验证旧格式后提升计算成本。
- 日志可能包含 Prompt、模型、图片路径和错误信息；写入前会执行凭据脱敏。
- 图片 sidecar 会保存完整 Prompt、编辑说明和模型文字回复，以便追溯生成过程。

使用共享电脑时，应妥善保护 Windows 账户，并定期清理不再需要的日志、图片和
sidecar。DPAPI 防止其他 Windows 用户或离线复制直接读取密钥，但无法防御已在同一
Windows 用户权限下运行的恶意程序。

