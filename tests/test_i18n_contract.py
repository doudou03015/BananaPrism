from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from banana_prism.i18n import (
    DEFAULT_LOCALE,
    catalog,
    current_locale,
    register_catalog,
    set_locale,
    tr,
    user_error_text,
)
from banana_prism.constants import MODELS


UI_ROOT = Path(__file__).parents[1] / "src" / "banana_prism" / "ui"
HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            result.add(id(first.value))
    return result


def test_ui_modules_do_not_embed_simplified_chinese_display_copy() -> None:
    violations: list[str] = []
    for path in UI_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and HAN_TEXT.search(node.value)
            ):
                violations.append(f"{path.relative_to(UI_ROOT)}:{node.lineno}: {node.value!r}")
    assert not violations, "UI 文案必须通过 banana_prism.i18n.tr(key) 取得：\n" + "\n".join(violations)


def test_every_literal_ui_translation_key_exists_in_default_catalog() -> None:
    known = catalog(DEFAULT_LOCALE)
    missing: list[str] = []
    for path in UI_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "tr" or not node.args:
                continue
            key_node = node.args[0]
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                if key_node.value not in known:
                    missing.append(f"{path.relative_to(UI_ROOT)}:{node.lineno}: {key_node.value}")
    assert not missing, "UI 使用了不存在的文案 key：\n" + "\n".join(missing)
    assert all(model.label_key in known for model in MODELS)


def test_translation_formats_dynamic_copy_and_partial_locale_falls_back() -> None:
    assert tr("password.error.short.body", minimum_length=6) == "密码至少需要 6 个字符。"
    original = current_locale()
    register_catalog("test_partial", {"common.cancel": "Cancel"})
    try:
        set_locale("test_partial")
        assert tr("common.cancel") == "Cancel"
        assert tr("common.save") == "保存"
    finally:
        set_locale(original)


def test_unknown_translation_key_is_an_explicit_error() -> None:
    with pytest.raises(KeyError, match="unknown UI text key"):
        tr("missing.key")


def test_service_errors_are_localized_at_the_user_display_boundary() -> None:
    class ApiProtocolError(RuntimeError):
        pass

    assert user_error_text(ApiProtocolError("API response contains no JSON objects")) == (
        "API 响应无效或服务商拒绝了请求。"
    )
    assert user_error_text("Network request timed out") == "网络请求超时，请稍后重试。"
    assert user_error_text("API returned HTTP 429: quota") == "API 返回 HTTP 429，请求未完成。"
    assert user_error_text("provider-specific billing message").startswith("服务商返回错误：")
    localized = "设置未保存"
    assert user_error_text(localized) == localized
