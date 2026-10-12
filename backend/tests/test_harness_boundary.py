"""Boundary checks: 两条依赖方向上的墙。

1. **harness 不得 import app**。``evaharness`` 是可独立发布的业务无关框架，业务层的改动
   不许渗进来。
2. **机制包不得 import ``agents``**（T19 加，见 ADR-0024）。``runtime`` / ``sandbox`` /
   ``knowledge`` / ``journal`` / ``models`` 这些包是**被 agent 层依赖**的；反过来 import
   ``agents`` 会让「写一个工具」变成「认识某个 agent 实现」，并在包级造出环——T19 之前
   ``sandbox ↔ agents`` 就是靠 ``TYPE_CHECKING`` 才没炸（ADR-0020 的硬约束段自证）。

两条都用 AST 扫描，不靠 import 顺序或注释约定。
"""

import ast
from pathlib import Path

HARNESS_ROOT = Path(__file__).parent.parent / "packages" / "harness" / "evaharness"

BANNED_PREFIXES = ("app.",)

MECHANISM_PACKAGES = (
    "runtime",
    "journal",
    "models",
    "sandbox",
    "knowledge",
    "checkpoint",
    "fingerprint",
    "mcp",
    "skills",
    "config",
    "reflection",
    "logging",
)
"""不许 import ``agents`` 的那些包。

``agents`` 自己不在表里（它就是要用别人的一方），顶层模块也不在——这张表列的是**分层意义上的
机制包**，将来新增子包时应当顺手加进来。
"""

AGENTS_PREFIX = "evaharness.agents"


def _collect_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return (line_number, module_path) for every import in *filepath*."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                results.append((node.lineno, node.module))
    return results


def _relative(filepath: Path) -> Path:
    return filepath.relative_to(HARNESS_ROOT.parent.parent.parent)


def test_harness_does_not_import_app():
    violations: list[str] = []

    for py_file in sorted(HARNESS_ROOT.rglob("*.py")):
        for lineno, module in _collect_imports(py_file):
            if any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in BANNED_PREFIXES):
                violations.append(f"  {_relative(py_file)}:{lineno}  imports {module}")

    assert not violations, "Harness layer must not import from app layer:\n" + "\n".join(violations)


def test_mechanism_packages_do_not_import_agents():
    """机制包不许 import ``agents``——依赖只能从 agents 指向它们，不能反过来。

    破例的症状不是崩溃（Python 扛得住包级环，T19 之前就是这么活的），而是**包不再能单独
    存在**：想用文件工具就得连 agent 实现一起拖进来。所以这条墙靠测试守，不靠自觉。
    """
    violations: list[str] = []

    for name in MECHANISM_PACKAGES:
        package_root = HARNESS_ROOT / name
        if not package_root.is_dir():
            continue
        for py_file in sorted(package_root.rglob("*.py")):
            for lineno, module in _collect_imports(py_file):
                if module == AGENTS_PREFIX or module.startswith(AGENTS_PREFIX + "."):
                    violations.append(f"  {_relative(py_file)}:{lineno}  imports {module}")

    assert not violations, "机制包必须能脱离 agents 单独存在，但它们 import 了 agents：\n" + "\n".join(violations)
