"""agent-wechat：macOS 微信聊天记录读取 + MCP Server（仅限本人已登录账号，只读）。"""


def main(argv: list[str] | None = None) -> None:
    """CLI 入口（`agent-wechat`）。惰性载入 cli：MCP server 也要 import 本包，用不到 CLI。"""
    from .cli import main as _main
    _main(argv)


__all__ = ["main"]
__version__ = "0.2.0rc1"
