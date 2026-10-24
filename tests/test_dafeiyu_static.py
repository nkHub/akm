"""桌宠模块的静态健全性检查。

**为什么需要这个测试**：``pet_app.py`` 里的 ``tick`` 是 NSTimer 回调，
任何 ``NameError`` 都会让定时器停止重复触发，表现为「桌宠卡住、动画不动」。
历史上真的踩过两次：

- ``math`` 未导入 → ``_stepWander`` 每次漫游都抛异常，整个 tick 被跳过；
- ``subprocess`` 未导入 → 系统通知每次静默失败。

这两类问题 ``py_compile`` 查不出来（语法合法），跑单测也查不出来
（那些函数没被调用），只能在运行日志里看到 NameError。
因此这里用 AST 做一次「函数体里引用了但模块级不存在」的检查，
把它们挡在提交前。
"""

import ast
import builtins
import sys
from pathlib import Path

import pytest

_PET_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu" / "pet"
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"

# 待检查的模块（桌宠子进程 + 插件后端）
_TARGETS = [
    _PET_DIR / "pet_app.py",
    _PET_DIR / "physics.py",
    _PET_DIR / "companion.py",
    _PLUGIN_DIR / "index.py",
]

# 允许的「看似未定义其实是闭包变量」的名字。
# 这些是嵌套函数里引用的外层局部变量，AST 静态分析看不到外层作用域，
# 属于已知误报，不是真问题。
_CLOSURE_FALSE_POSITIVES = {"self", "cls", "lock_screen", "pick"}


def _module_level_names(tree: ast.Module) -> set[str]:
    """收集模块级可见的名字：导入 + 顶层定义 + 顶层赋值 + 内建。"""
    names = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "annotations"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _function_locals(fn: ast.AST) -> set[str]:
    """收集一个函数体内所有被绑定的名字（参数 / 赋值 / for / except / 内嵌定义）。"""
    local: set[str] = set()
    args = fn.args
    for arg in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
        local.add(arg.arg)
    if args.vararg:
        local.add(args.vararg.arg)
    if args.kwarg:
        local.add(args.kwarg.arg)
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            local.add(sub.id)
        elif isinstance(sub, ast.arg):
            local.add(sub.arg)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not fn:
            local.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            local.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                local.add((alias.asname or alias.name).split(".")[0])
    return local


def _find_undefined(path: Path) -> list[tuple[str, str, int]]:
    """返回 ``[(函数名, 未定义名字, 行号)]``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_names = _module_level_names(tree)
    problems: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _function_locals(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                name = sub.id
                if name in local or name in module_names:
                    continue
                if name in _CLOSURE_FALSE_POSITIVES:
                    continue
                problems.append((node.name, name, sub.lineno))
    return problems


@pytest.mark.parametrize("path", _TARGETS, ids=lambda p: p.name)
def test_no_undefined_globals(path):
    """每个模块都不应引用「函数内没定义、模块级也没有」的名字。

    这条断言能抓住 ``NameError`` 类问题（例如忘写 ``import math``），
    而这类错误在 ``tick`` 里会直接让桌宠停摆。
    """
    assert path.exists(), f"模块不存在: {path}"
    problems = _find_undefined(path)
    if problems:
        detail = "\n".join(
            f"  {path.name}:{line} {fn}() 引用了未定义的 {name}"
            for fn, name, line in problems
        )
        pytest.fail(f"{path.name} 存在未定义的名字（会抛 NameError）：\n{detail}")


def test_pet_app_imports_math_and_subprocess():
    """回归测试：``math`` 与 ``subprocess`` 曾被误删，导致漫游与通知静默失败。"""
    src = (_PET_DIR / "pet_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
    assert "math" in imported, "pet_app.py 必须 import math（_stepWander 用到 math.copysign）"
    assert "subprocess" in imported, "pet_app.py 必须 import subprocess（系统通知用到）"


def test_physics_and_companion_are_importable():
    """两个纯逻辑模块能被独立导入（不依赖 AppKit）。"""
    for name in ("physics", "companion"):
        if str(_PET_DIR) not in sys.path:
            sys.path.insert(0, str(_PET_DIR))
        module = __import__(name)
        assert module is not None


def test_text_field_cell_cleared_after_replacement():
    """回归测试：替换 NSTextFieldCell 后必须显式清空 stringValue。

    ``NSTextFieldCell`` 的默认 stringValue 是字符串 ``"Field"``
    （``NSTextField`` 默认是空串）。为让文字垂直居中而新建
    ``CenteredTextFieldCell`` 再 ``setCell_`` 时，若不显式清空，
    输入框里就会预填一个「Field」——用户看到的就是这个。
    """
    src = (_PET_DIR / "pet_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 找到 setCell_ 调用所在的函数体，检查其前后是否有 setStringValue_("")
    found_clear = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "setCell_":
            continue
        # 在同一函数的语句序列里找 setStringValue_("")
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node not in list(ast.walk(parent)):
                continue
            for stmt in ast.walk(parent):
                if not isinstance(stmt, ast.Call):
                    continue
                f2 = stmt.func
                if not isinstance(f2, ast.Attribute) or f2.attr != "setStringValue_":
                    continue
                if stmt.args and isinstance(stmt.args[0], ast.Constant) and stmt.args[0].value == "":
                    found_clear = True
    assert found_clear, (
        "替换 NSTextFieldCell 后必须 setStringValue_(\"\")，"
        "否则 NSTextFieldCell 的默认值 \"Field\" 会出现在输入框里"
    )
