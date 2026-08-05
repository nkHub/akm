"""打包后精简 pygments：只保留常用语言 lexer，缩小 app 体积。

pygments 的 lexers 目录包含 260 个语言文件（约 15MB），但 akm 只通过
rich 的 Markdown 代码高亮用到少量语言。此脚本在 py2app 打包完成后运行，
删除除白名单外的 lexer 文件，app 体积可减少约 13MB。

白名单依据：akm agent CLI 渲染 markdown 代码块时最常用到的语言，
以及这些 lexer 顶层 import 所依赖的兄弟模块（如 html.py 依赖
javascript/jvm/css/ruby）。
"""

import glob
import os
import sys

# 需要保留的 lexer 模块（含依赖闭包）
KEEP = {
    "css.py",          # CssLexer
    "html.py",         # HtmlLexer（依赖 javascript/jvm/css/ruby）
    "javascript.py",   # JavascriptLexer
    "php.py",          # PhpLexer
    "python.py",       # PythonLexer
    "jvm.py",          # html.py 依赖 ScalaLexer
    "ruby.py",         # html.py 依赖 RubyLexer
    "__init__.py",     # 包入口（导入 _mapping）
    "_mapping.py",     # LEXERS 注册表（包入口必须导入）
    "_css_builtins.py",  # css.py 依赖
}


def main() -> int:
    # 定位 app 内 pygments/lexers 目录：支持传入 app 根路径参数，
    # 缺省从当前工作目录（build_app.sh 在项目根运行）推断
    app_root = sys.argv[1] if len(sys.argv) > 1 else "dist/AI Key Manager.app"
    lexers_dir = os.path.join(
        app_root, "Contents", "Resources", "lib", "python3.12", "pygments", "lexers"
    )
    if not os.path.isdir(lexers_dir):
        print(f"[trim_pygments] 未找到 lexers 目录：{lexers_dir}，跳过")
        return 0

    removed = []
    for path in glob.glob(os.path.join(lexers_dir, "*.py")):
        base = os.path.basename(path)
        if base not in KEEP:
            os.remove(path)
            removed.append(base)

    # 同步清理已删除模块的 .pyc 缓存（py2app 会生成 __pycache__）
    for root, dirs, files in os.walk(lexers_dir):
        for name in dirs:
            if name == "__pycache__":
                pycache = os.path.join(root, name)
                for f in os.listdir(pycache):
                    os.remove(os.path.join(pycache, f))

    print(f"[trim_pygments] 已删除 {len(removed)} 个 lexer 文件，保留 {len(KEEP)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
