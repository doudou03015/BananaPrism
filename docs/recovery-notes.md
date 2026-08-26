# 恢复依据与边界

BananaPrism 1.0.0 根据以下只读制品重建：

- 原发布包：`NanaBananaStudio\NanaBananaStudio.exe`
- SHA-256：`3D89A8DC286479A6F928B3C1D4AD2F07AA106344190B9C0E589DF64D61297F30`
- 打包形态：PyInstaller onedir、Python 3.13.12、PySide6。
- 已验证字节码：入口 `main` 加 24 个 `app.*` 模块，共 25 个有效 code object 容器。

恢复目标是功能和数据语义等价。注释、空白、原始高阶表达式及局部命名无法从字节码
精确还原，因此本仓库是可维护的重新实现，不是逐字节反编译结果。

原发布目录、真实用户 AppData 以及 `.nana_recovery_tmp` 取证制品不属于本仓库，
构建与测试不得修改它们。

