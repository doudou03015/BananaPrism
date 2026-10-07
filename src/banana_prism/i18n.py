"""Chinese-first UI text catalog and future localization entry point.

UI modules refer to stable keys rather than embedding display copy.  Additional
locales can be registered without changing widget code; an installed Qt
``QTranslator`` may still translate the selected catalog templates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType

from PySide6.QtCore import QCoreApplication


DEFAULT_LOCALE = "zh_CN"
TRANSLATION_CONTEXT = "BananaPrism.UI"

_ZH_CN = {
    # Product and shared controls.
    "app.display_name": "BananaPrism — AI 图像工作室",
    "app.window_title": "{app_name} · v{version}",
    "app.brand_mark": "BANANA\nPRISM",
    "app.brand_name": "BananaPrism",
    "app.subtitle": "AI 图像生成与局部编辑工作室",
    "model.gemini_31_flash": "Nano Banana 2（Gemini 3.1 Flash Image）",
    "model.gemini_25_flash": "Nano Banana（Gemini 2.5 Flash Image）",
    "model.gemini_3_pro": "Nano Banana Pro（Gemini 3 Pro Image）",
    "model.gemini_nano_banana_21": "Nano Banana 2.1（Gemini Nano Banana 2.1）",
    "application.startup_failed.title": "BananaPrism 启动失败",
    "application.startup_failed.body": (
        "BananaPrism 无法安全启动。配置迁移或凭据存储未完成，旧配置未被删除。\n\n"
        "原因：{reason}\n\n"
        "请保留现有文件并重新启动；如问题持续，请使用新的 API Key 重新配置。"
    ),
    "application.single_instance.title": "BananaPrism 已在运行",
    "application.single_instance.body": (
        "BananaPrism 已在此用户数据目录运行。\n\n"
        "数据目录：{data_dir}\n{owner}\n\n"
        "请切换到已打开的窗口，或等待其完全退出后重试。"
    ),
    "application.single_instance.owner": (
        "锁持有者：PID {pid}，主机 {hostname}，程序 {application}"
    ),
    "application.single_instance.owner_unknown": "无法读取锁持有者信息。",
    "application.instance_lock_failed.title": "BananaPrism 无法取得实例锁",
    "application.instance_lock_failed.body": (
        "无法锁定当前用户数据目录，程序不会继续启动。\n\n"
        "数据目录：{data_dir}\n锁错误：{error}"
    ),
    "common.api_key": "API Key",
    "common.cancel": "取消",
    "common.clear": "清空",
    "common.close": "关闭",
    "common.confirm": "确认",
    "common.copy": "复制",
    "common.open": "打开",
    "common.save": "保存",
    "common.unavailable": "无法读取",
    "common.not_provided": "未提供",
    "common.not_configured": "未配置",
    "common.saved_securely": "已安全保存",

    # User-facing service error boundaries. Internal exception messages remain
    # stable developer contracts; these catalog entries own the display copy.
    "error.api.request": "API 请求参数无效，请检查模型、尺寸、比例和服务商配置。",
    "error.api.response": "API 响应无效或服务商拒绝了请求。",
    "error.api.http": "API 返回 HTTP {status}，请求未完成。",
    "error.api.network": "网络请求失败，请检查连接和服务商状态。",
    "error.api.timeout": "网络请求超时，请稍后重试。",
    "error.api.provider_http": "{provider} 返回 HTTP {status}：{detail}",
    "error.api.provider_http_empty": "{provider} 返回 HTTP {status}，请求未完成。",
    "error.api.provider_dns": (
        "{provider} DNS 解析失败，请检查网络、DNS 和服务商接口地址。{detail}"
    ),
    "error.api.provider_tls": (
        "{provider} TLS/SSL 握手失败，请检查系统时间、代理和证书链。{detail}"
    ),
    "error.api.provider_timeout": (
        "{provider} 请求超时{duration}，请稍后重试或检查代理与网络稳定性。"
    ),
    "error.api.provider_refused": (
        "{provider} 连接被拒绝，请检查代理、防火墙和服务商状态。{detail}"
    ),
    "error.api.provider_connection": (
        "{provider} 连接中断，请检查代理和网络稳定性后重试。{detail}"
    ),
    "error.api.provider_authentication": (
        "{provider} 身份验证失败，请检查 API Key 或代理认证。{detail}"
    ),
    "error.api.provider_network": (
        "{provider} 网络请求失败，请检查连接和服务商状态。{detail}"
    ),
    "error.api.provider_protocol": (
        "{provider} 未返回有效的 HTTP 响应，请检查代理和接口地址。{detail}"
    ),
    "error.api.provider_response": "{provider} 返回的响应无效或请求被拒绝。{detail}",
    "error.api.no_result": "服务商既未返回图片，也未返回文字。",
    "error.image.invalid": "图片数据无效、格式不受支持或超出安全限制。",
    "error.image.remote_unsafe": "远程图片地址不安全，已拒绝访问。",
    "error.file.save": "图片或元数据未能安全保存。",
    "error.migration": "旧版设置迁移未完成；旧文件已保留，请重试或重新配置凭据。",
    "error.settings": "应用设置无效或无法安全保存。",
    "error.secrets": "加密凭据不可用，请在当前 Windows 用户下重新配置。",
    "error.password": "密码不符合要求或无法安全保存。",
    "error.os": "文件或系统操作失败：{detail}",
    "error.input": "输入或配置无效。",
    "error.provider_detail": "服务商返回错误：{detail}",
    "error.generic": "操作未完成，请查看运行日志后重试。",

    # API preset editor.
    "api_preset.title.add": "添加 API 预设",
    "api_preset.title.edit": "编辑 API 预设",
    "api_preset.placeholder.name": "例如：个人 OpenRouter",
    "api_preset.placeholder.keep_key": "留空以保留现有密钥",
    "api_preset.placeholder.enter_key": "输入 API Key",
    "api_preset.field.name": "预设名称",
    "api_preset.field.provider": "服务商",
    "api_preset.field.endpoint": "API 基础地址",
    "api_preset.note.security": "密钥应由安全存储服务保存；此窗口不会把密钥写入普通设置或日志。",
    "api_preset.error.missing_name.title": "缺少名称",
    "api_preset.error.missing_name.body": "请输入预设名称。",
    "api_preset.error.missing_provider.title": "缺少服务商",
    "api_preset.error.missing_provider.body": "请选择 API 服务商。",
    "api_preset.error.missing_key.title": "缺少 API Key",
    "api_preset.error.missing_key.body": "新预设必须提供 API Key。",

    # Password dialog and console authentication.
    "password.title.security": "安全验证",
    "password.prompt.unlock_credentials": "请输入主密码以解锁 API 凭据。",
    "password.prompt.enter": "请输入主密码。",
    "password.placeholder.current": "当前密码",
    "password.placeholder.main": "主密码",
    "password.placeholder.again": "再次输入",
    "password.field.current": "当前密码",
    "password.field.new": "新密码",
    "password.field.password": "密码",
    "password.field.confirm": "确认密码",
    "password.error.missing.title": "缺少密码",
    "password.error.current_missing": "请输入当前密码。",
    "password.error.empty": "密码不能为空。",
    "password.error.short.title": "密码过短",
    "password.error.short.body": "密码至少需要 {minimum_length} 个字符。",
    "password.error.mismatch.title": "密码不一致",
    "password.error.mismatch.body": "两次输入的密码不一致。",

    # Request preflight.
    "preflight.title": "发送前确认",
    "preflight.heading": "请确认本次请求",
    "preflight.subheading": "确认后将使用下列精确参数调用当前 API 预设。",
    "preflight.preview.source": "原图",
    "preflight.preview.annotation": "彩色标注图",
    "preflight.preview.mask": "本地黑白选区蒙版（不上传）",
    "preflight.button.send": "确认发送",
    "preflight.button.back": "返回修改",
    "preflight.prompt": "提示词",
    "preflight.field.operation": "操作",
    "preflight.field.preset": "API 预设",
    "preflight.field.preset_id": "预设 ID",
    "preflight.field.provider": "服务商",
    "preflight.field.model": "模型",
    "preflight.field.size": "尺寸",
    "preflight.field.ratio": "比例",
    "preflight.field.pixel_dimensions": "参考像素尺寸",
    "preflight.field.output_format": "保存格式",
    "preflight.field.output_dpi": "保存 DPI",
    "preflight.field.source_format": "原文件 / 编辑输出格式",
    "preflight.field.wire_source_format": "上传源图副本格式",
    "preflight.field.source_dpi": "源 DPI",
    "preflight.field.annotation_format": "上传标注副本格式",
    "preflight.field.annotation_color": "标注颜色",
    "preflight.field.selection_mask": "选区蒙版",
    "preflight.field.wire_policy": "网络副本策略",
    "preflight.operation.generate": "生成图片",
    "preflight.operation.edit": "局部编辑",
    "preflight.annotation_format.jpeg": "JPEG 95",
    "preflight.selection_mask.local": "本地 PNG（仅生成/校验彩色标注，不上传）",
    "preflight.wire_policy.adaptive": "原图与标注图保持同尺寸；请求超过 16 MiB 时同步等比缩小",

    # Reusable panels and preview.
    "log_panel.title": "运行日志",
    "log_panel.placeholder": "请求进度、保存路径和错误会显示在这里。",
    "text_panel.toggle.expanded": "▾",
    "text_panel.toggle.collapsed": "▸",
    "text_panel.title": "模型文字结果",
    "text_panel.placeholder": "模型返回的说明会显示在这里。",
    "preview.empty": "导入图片，或从左侧开始生成",
    "preview.zoom.initial": "100%",
    "preview.zoom.out": "−",
    "preview.zoom.fit": "适配",
    "preview.zoom.in": "+",
    "preview.zoom.value": "{percent:.0f}%",
    "preview.dimensions": "{width} × {height}",

    # Settings console.
    "console.title": "BananaPrism 控制台",
    "console.tab.api": "🔑 API 配置",
    "console.tab.statistics": "📊 统计",
    "console.tab.settings": "⚙ 配置",
    "console.tab.system": "🗂 系统",
    "console.button.apply": "应用设置",
    "console.table.current": "当前",
    "console.table.name": "名称",
    "console.table.provider": "服务商",
    "console.table.credential": "凭据",
    "console.button.add": "添加",
    "console.button.edit": "编辑",
    "console.button.delete": "删除",
    "console.button.make_current": "设为当前",
    "console.password.note": "控制台密码保护 API 配置",
    "console.password.change": "修改控制台密码",
    "console.presets.note": "切换服务商和 API Key；队列任务会保留入队时的预设快照。",
    "console.api.selected_key": "所选预设的 API Key",
    "console.api.select_preset": "请选择一个 API 预设。",
    "console.api.show": "显示",
    "console.api.hide": "隐藏",
    "console.api.read_unavailable": "设置服务不支持读取 API Key。",
    "console.api.decrypt_failed": "无法解密此 API Key，请在当前 Windows 用户下重新保存。",
    "console.api.not_configured": "此预设尚未配置 API Key。",
    "console.api.loaded_masked": "API Key 已从 Windows 安全存储读取，默认隐藏。",
    "console.api.copied": "API Key 已复制到系统剪贴板，请使用后覆盖剪贴板内容。",
    "console.statistics.zero": "0",
    "console.statistics.caption": "今日成功生成 / 编辑",
    "console.statistics.refresh": "刷新统计",
    "console.statistics.note": "当前仅统计成功图片数量；服务商未统一提供可靠的 Token 费用数据，因此不估算成本。",
    "console.output.browse": "选择…",
    "console.output.character_suffix": " 字",
    "console.output.autosave_note": "自动保存始终开启；成功生成、编辑和导入后会立即写入此目录。",
    "console.output.directory": "输出目录",
    "console.output.filename_length": "文件名提示词长度",
    "console.system.user_data": "用户数据",
    "console.system.logs": "日志目录",
    "console.system.images": "图片目录",
    "console.system.note": "系统页仅显示应用自己的目录，不扫描其他位置。",
    "console.active_marker": "◆",
    "console.error.save.title": "保存失败",
    "console.settings.save_failed": "设置未保存：{error}",
    "console.settings.service_unavailable": "设置服务不可用",
    "console.preset.delete_failed": "预设未删除：{error}",
    "console.preset.activate_failed": "当前预设未切换：{error}",
    "console.delete.title": "删除预设",
    "console.delete.body": "确定删除“{name}”及其安全凭据吗？",
    "console.output.select_title": "选择输出目录",
    "console.directory.missing.title": "目录不存在",
    "console.directory.not_created": "目录尚未创建：\n{path}",
    "console.password.change_title": "修改主密码",
    "console.password.change_prompt": "修改后，现有 API 凭据应由安全存储服务重新加密。",
    "console.password.service_missing.title": "服务未连接",
    "console.password.service_missing.body": "密码服务尚未连接。",
    "console.password.change_failed.title": "修改失败",
    "console.password.current_wrong": "当前密码不正确。",
    "console.password.changed.title": "修改成功",
    "console.password.changed.body": "控制台密码已更新。",

    # Main workspace construction.
    "main.ready": "BananaPrism 已就绪。API Key 仅在发送请求时从安全存储读取。",
    "main.action.import": "导入图片…",
    "main.action.console": "控制台",
    "main.action.undo_annotation": "撤销标注",
    "main.daily_count": "今日完成 {count} 张",
    "main.status.idle": "空闲",
    "main.card.workspace": "工作区",
    "main.button.import_image": "导入图片",
    "main.button.open_save_directory": "打开保存目录",
    "main.button.copy_save_path": "复制保存路径",
    "main.card.mode": "工作模式",
    "main.mode.generate": "生成图片",
    "main.mode.edit": "局部编辑",
    "main.card.parameters": "请求参数 · 两种模式共用",
    "main.field.api_preset": "API 预设",
    "main.field.model": "模型",
    "main.field.output_size": "输出尺寸",
    "main.field.output_ratio": "输出比例",
    "main.field.output_format": "输出格式",
    "main.field.output_dpi": "输出 DPI",
    "main.parameters.estimated_pixels": "预计像素尺寸：{width} × {height} px",
    "main.parameters.estimated_pixels_model": "预计像素尺寸：以模型返回为准",
    "main.parameters.estimated_pixels_tooltip": (
        "模型按所选尺寸和比例生成；实际返回像素以保存后的图片为准。"
    ),
    "main.parameters.note": (
        "尺寸和比例会发送给模型；PNG/JPEG 与 DPI 仅用于本地自动保存，"
        "不会缩放模型返回的像素。所有选项都会记住上次设置。"
    ),
    "main.card.request": "创作请求",
    "main.button.cancel_request": "取消当前请求",
    "main.generation.placeholder": "描述要生成的画面、主体、构图、光线和风格…",
    "main.button.add_queue": "加入队列",
    "main.source.empty": "尚未导入工作图",
    "main.button.import_work_image": "导入工作图",
    "main.tool.rectangle": "矩形",
    "main.tool.brush": "画笔",
    "main.tool.eraser": "橡皮",
    "main.button.undo": "撤销",
    "main.button.clear": "清除",
    "main.field.brush_radius": "画笔半径",
    "main.field.annotation_color": "标注颜色",
    "main.annotation.color.red": "红色",
    "main.annotation.color.green": "绿色",
    "main.annotation.color.magenta": "洋红色",
    "main.annotation.color.cyan": "青色",
    "main.annotation.color.yellow": "黄色",
    "main.annotation.note": (
        "{color}标记会合成到网络标注副本；黑白蒙版仅在本地用于生成和校验标注，不会上传。"
    ),
    "main.edit.placeholder": "说明标记区域中要修改的内容…",
    "main.button.send_edit": "发送编辑请求",
    "main.card.queue": "生成队列",
    "main.queue.start": "开始队列",
    "main.queue.retry": "重试失败/已取消",
    "main.queue.stop": "停止",
    "main.queue.clear": "清理",

    # Main workspace runtime text.
    "main.settings.save_failed": "设置未保存：{error}",
    "main.preset.add_first": "请在控制台添加 API 预设",
    "main.credential.read_failed": "无法读取安全凭据：{error}",
    "main.service.not_initialized": "图片服务尚未初始化",
    "main.service.unsupported": "图片服务不支持{operation}请求",
    "main.operation.generate": "生成",
    "main.operation.edit": "编辑",
    "main.error.prompt.title": "缺少提示词",
    "main.error.prompt.generate": "请输入图片生成提示词。",
    "main.error.api.title": "未配置 API",
    "main.error.api.add_and_select": "请先在控制台添加并选择 API 预设。",
    "main.error.source.title": "无工作图",
    "main.error.source.body": "请先导入图片，或先生成一张图片。",
    "main.error.selection.title": "未选择区域",
    "main.error.selection.body": "请用矩形或画笔标出需要修改的区域。",
    "main.error.edit_prompt.title": "缺少编辑指令",
    "main.error.edit_prompt.body": "请说明标记区域要如何修改。",
    "main.error.annotation.title": "标注失败",
    "main.error.annotation.body": "无法生成无损 PNG 标注图，请重新标注。",
    "main.error.selection_mask.title": "选区蒙版失败",
    "main.error.selection_mask.body": "无法生成无损黑白 PNG 选区蒙版，请重新标注。",
    "main.job.already_running": "已有请求正在运行，请先等待或取消。",
    "main.credential.missing": "当前 API 预设没有可用密钥，请在控制台重新保存。",
    "main.job.running": "正在{operation}…",
    "main.job.started": "开始{operation} · {model} · {size} · {ratio}",
    "main.job.openrouter_images_api": "OpenRouter 4K 使用官方 Images API；该接口返回图片，但不提供模型文字说明。",
    "main.job.not_started": "请求未启动：{reason}",
    "main.service.busy": "服务忙",
    "main.result.decode_failed": "服务返回的图片无法解码",
    "main.result.type_mismatch": "服务结果类型与当前请求不匹配",
    "main.result.no_image": "服务未返回图片",
    "main.encode.failed": "输出图片编码失败：{error}；保留服务原图且不标记 DPI。",
    "main.encode.unsupported": "当前 Qt 不支持写入 {format}；保留服务原图且不标记 DPI。",
    "main.encode.buffer_failed": "无法创建图片编码缓冲区；保留服务原图且不标记 DPI。",
    "main.encode.format_failed": "{format} 编码失败；保留服务原图且不标记 DPI。",
    "main.encode.verify_failed": "{format} 编码结果无法回读；保留服务原图且不标记 DPI。",
    "main.encode.dpi_missing": "{format} 未保留请求的 {requested:g} DPI；编码回读没有有效 DPI，参数 JSON 将记录为空。",
    "main.encode.dpi_not_preserved": "{format} 未保留请求的 {requested:g} DPI；按回读值 {actual_x:g}×{actual_y:g} DPI 记录。",
    "main.error.unknown": "未知错误",
    "main.text_only.log": "模型仅返回文字：{response}",
    "main.text_only.status": "模型仅返回文字，未生成图片",
    "main.request.cancelled": "请求已取消",
    "main.request.cancelled_sentence": "请求已取消。",
    "main.progress.percent": "接收响应 {percent:.0f}%",
    "main.progress.kib": "已接收 {kib:.0f} KiB",
    "main.progress.processing": "正在处理 · {progress}",
    "main.progress.stage.upload_percent": "正在上传请求 {percent:.0f}%",
    "main.progress.stage.upload_kib": "正在上传请求 {kib:.0f} KiB",
    "main.progress.stage.generating": "请求已上传，服务商正在生成…",
    "main.progress.stage.download_percent": "正在接收结果 {percent:.0f}%",
    "main.progress.stage.download_kib": "正在接收结果 {kib:.0f} KiB",
    "main.request.done": "完成",
    "main.request.completed": "请求完成。",
    "main.request.failed_status": "请求失败 · {message}",
    "main.request.failed_log": "请求失败：{message}",
    "main.source.current": "当前工作图 · {width} × {height} · {format}",
    "main.queue.prompt_required": "请输入提示词后再加入队列。",
    "main.queue.preset_required": "请先选择 API 预设。",
    "main.queue.added": "已加入队列：{prompt}",
    "main.queue.job_running": "当前有请求运行中，暂不能启动队列。",
    "main.queue.no_pending": "队列中没有待执行任务。",
    "main.queue.completed": "队列已完成。",
    "main.queue.snapshot_missing": "队列使用的 API 预设已不存在",
    "main.queue.unsupported_size": "入队尺寸不受该模型支持，任务未发送",
    "main.queue.running_task_missing": "运行中的队列任务已丢失，队列已安全暂停",
    "main.queue.dispatch_failed": "队列任务未能启动",
    "main.queue.failure_pause": "连续失败 3 次，队列已自动暂停。",
    "main.queue.stopped": "队列已停止",
    "main.queue.remove": "移除此任务",
    "main.queue.requeue": "重新排队",
    "main.import.title": "导入工作图",
    "main.import.filter": "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)",
    "main.import.failed.title": "导入失败",
    "main.import.unrecognized": "无法识别图片格式。",
    "main.import.normalize_failed": "无法安全应用图片方向元数据。",
    "main.source.details": "{name} · {width} × {height} · {format}",
    "main.source.archived": "{details}\n已归档：{path}",
    "main.import.succeeded": "已导入工作图：{name}",
    "main.save.auto_failed": "自动保存失败：{error}",
    "main.save.succeeded": "已保存：{path}",
    "main.save.sidecar_succeeded": "参数 JSON 已同步保存：{path}",
    "main.import.archive_failed": "导入图归档失败：{error}",
    "main.import.archived": "导入图已归档：{path}",
    "main.directory.missing.title": "目录不存在",
    "main.directory.save_unavailable": "保存目录当前不可用：\n{directory}",
    "main.edit.needs_source": "局部编辑需要工作图 · 点击“导入工作图”",
    "main.edit.entered_without_source": "已进入局部编辑，请先导入工作图。",
    "main.annotation.ready": "{color}标注与本地黑白蒙版已就绪",
    "main.annotation.cleared": "标注已清除",
    "main.cancel.cancelling_ellipsis": "正在取消…",
    "main.cancel.cancelling": "正在取消",
    "main.cancel.unsupported": "当前图片服务不支持安全取消；窗口将保持打开直到请求结束。",
    "main.cancel.failed": "取消失败：{error}",
    "main.job.generating": "正在生成…",
    "main.job.editing": "正在编辑…",
    "main.job.processing": "正在处理请求",
    "main.daily.save_failed": "今日计数未保存：{error}",
    "main.console.verify_unavailable.title": "无法验证",
    "main.console.verify_unavailable.body": "控制台密码服务尚未连接，控制台保持锁定。",
    "main.console.setup.title": "设置控制台密码",
    "main.console.setup.prompt": "首次打开控制台，请设置至少 6 个字符的密码。以后每次进入都需要验证。",
    "main.console.setup_failed.title": "设置失败",
    "main.console.unlock.title": "解锁控制台",
    "main.console.verify_failed.title": "验证失败",
    "main.console.wrong_password": "密码不正确。",
    "main.close.confirm.title": "确认退出",
    "main.close.confirm.body": "仍有请求或队列任务。退出会安全取消它们，是否继续？",
}

_catalogs: dict[str, Mapping[str, str]] = {
    DEFAULT_LOCALE: MappingProxyType(_ZH_CN),
}
_locale = DEFAULT_LOCALE


def register_catalog(locale: str, messages: Mapping[str, str]) -> None:
    """Register a complete or partial locale catalog for future language packs."""

    normalized = str(locale).strip()
    if not normalized:
        raise ValueError("locale must not be empty")
    _catalogs[normalized] = MappingProxyType(dict(messages))


def set_locale(locale: str) -> None:
    """Select the active locale, falling back per key to Simplified Chinese."""

    normalized = str(locale).strip()
    if normalized not in _catalogs:
        raise ValueError(f"unregistered locale: {normalized}")
    global _locale
    _locale = normalized


def current_locale() -> str:
    return _locale


def catalog(locale: str | None = None) -> Mapping[str, str]:
    """Return a read-only view of a registered catalog."""

    selected = locale or _locale
    if selected not in _catalogs:
        raise ValueError(f"unregistered locale: {selected}")
    return _catalogs[selected]


def tr(key: str, /, **values: object) -> str:
    """Resolve ``key`` and interpolate named values after translation."""

    fallback = _catalogs[DEFAULT_LOCALE]
    selected = _catalogs[_locale]
    try:
        template = selected.get(key, fallback[key])
    except KeyError as exc:
        raise KeyError(f"unknown UI text key: {key}") from exc
    translated = QCoreApplication.translate(TRANSLATION_CONTEXT, template)
    if not values:
        return translated
    try:
        return translated.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid values for UI text key {key!r}: {exc}") from exc


_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_HTTP_STATUS = re.compile(r"(?:API returned|Remote image returned) HTTP\s+(\d{3})", re.I)
_API_ERROR_PREFIX = "BANANAPRISM_API_ERROR:"


def _provider_diagnostic_text(detail: str) -> str | None:
    if not detail.startswith(_API_ERROR_PREFIX):
        return None
    try:
        payload = json.loads(detail[len(_API_ERROR_PREFIX) :])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    provider = {
        "openrouter": "OpenRouter",
        "aihubmix": "AiHubMix",
    }.get(str(payload.get("provider", "")).casefold(), "API 服务商")
    category = str(payload.get("category", "network")).casefold()
    raw_detail = " ".join(str(payload.get("detail", "")).split())[:300]
    detail_suffix = f" 详情：{raw_detail}" if raw_detail else ""

    if category == "http":
        try:
            status = int(payload.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        if not 100 <= status <= 599:
            status = 0
        key = "error.api.provider_http" if raw_detail else "error.api.provider_http_empty"
        return tr(key, provider=provider, status=status or "未知", detail=raw_detail)
    if category == "timeout":
        try:
            timeout_ms = max(0, int(payload.get("timeout_ms", 0)))
        except (TypeError, ValueError):
            timeout_ms = 0
        duration = f"（{timeout_ms / 1000:g} 秒）" if timeout_ms else ""
        return tr("error.api.provider_timeout", provider=provider, duration=duration)

    key = {
        "dns": "error.api.provider_dns",
        "tls": "error.api.provider_tls",
        "refused": "error.api.provider_refused",
        "connection": "error.api.provider_connection",
        "authentication": "error.api.provider_authentication",
        "protocol": "error.api.provider_protocol",
        "response": "error.api.provider_response",
        "network": "error.api.provider_network",
    }.get(category, "error.api.provider_network")
    return tr(key, provider=provider, detail=detail_suffix)


def user_error_text(error: BaseException | object) -> str:
    """Convert service/internal failures to Chinese-first display text.

    Services keep stable English diagnostics for tests and logs. Every route
    that surfaces a failure in a widget passes through this function, so UI copy
    stays centralized while external provider details remain clearly labelled.
    """

    detail = str(error).strip()
    provider_diagnostic = _provider_diagnostic_text(detail)
    if provider_diagnostic is not None:
        return provider_diagnostic
    if detail and _HAN_TEXT.search(detail):
        return detail
    match = _HTTP_STATUS.search(detail)
    if match:
        return tr("error.api.http", status=match.group(1))
    lowered = detail.casefold()
    if "timed out" in lowered:
        return tr("error.api.timeout")
    if lowered.startswith("network request failed"):
        return tr("error.api.network")
    if "neither an image nor text" in lowered:
        return tr("error.api.no_result")
    if lowered.startswith("remote image") or "remote image host" in lowered:
        return tr("error.image.remote_unsafe")
    if lowered.startswith("image ") or "image payload" in lowered:
        return tr("error.image.invalid")

    type_names = {
        cls.__name__
        for cls in type(error).__mro__
    } if isinstance(error, BaseException) else set()
    categories = (
        ({"UnsafeImageUrlError"}, "error.image.remote_unsafe"),
        ({"ImageValidationError"}, "error.image.invalid"),
        ({"ApiProtocolError"}, "error.api.response"),
        ({"ApiClientError"}, "error.api.request"),
        ({"FileSaveError"}, "error.file.save"),
        ({"MigrationError", "MigrationStateError", "LegacySettingsValidationError"}, "error.migration"),
        ({"SettingsError", "SettingsValidationError", "SettingsPersistenceError", "SettingsSecretBindingError"}, "error.settings"),
        ({"SecretStoreError", "SecretProtectionError", "SecretValidationError"}, "error.secrets"),
        ({"PasswordPolicyError", "AuthPersistenceError"}, "error.password"),
    )
    for names, key in categories:
        if type_names & names:
            return tr(key)
    if isinstance(error, OSError):
        return tr("error.os", detail=detail or type(error).__name__)
    if isinstance(error, ValueError):
        return tr("error.input")
    if detail:
        # String terminal signals can contain provider-auth/billing messages.
        # Treat them as external detail rather than application display copy.
        return tr("error.provider_detail", detail=detail)
    return tr("error.generic")


__all__ = [
    "DEFAULT_LOCALE",
    "TRANSLATION_CONTEXT",
    "catalog",
    "current_locale",
    "register_catalog",
    "set_locale",
    "tr",
    "user_error_text",
]
