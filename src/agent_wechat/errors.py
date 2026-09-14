"""统一错误契约：core 只抛 AgentWeChatError 子类，由适配层（mcp_server / cli）翻译。

为什么不能靠 SDK 兜底：
- `SystemExit` 不是 `Exception` 的子类，而 MCP SDK 的兜底是 `except Exception`——
  逃出去会直接杀死常驻 stdio server；
- 即使是 `Exception`，SDK 也会统一改写成 "Error executing tool <name>" 的通用文案，
  异常自带的中文修复指引只留在服务端日志，到不了 agent。

所以 core 层一律抛本模块的类型，适配层用 to_payload / to_text 翻回对外形状。
本模块只放类型与纯翻译函数，不 import MCP SDK。
"""
from __future__ import annotations

import re

# 密钥形态：32 字节密钥/passphrase 的 hex 形态恰好 64 位。md5/chat_md5 是 32 位，不受影响。
_SECRET_HEX = re.compile(r"[0-9a-fA-F]{64}")
_MAX_LEN = 300


def redact(text: str) -> str:
    """剥掉密钥形态的连续 64 位 hex，再截断长度。

    为什么必须做：`decrypt.py` 把派生密钥拼成 `PRAGMA key = "x'<64位hex>'"` **作为
    命令行参数**交给 sqlcipher，而 sqlcipher 报语法错时会**回显参数原文并加插入符**
    （已实测，见 §测试）。C1 之前这段文字被 MCP SDK 吞掉、只留在服务端日志；C1 之后
    它会进对外信封、到 agent 手上，甚至发往外部 API——直接压在「密钥不进错误信息」
    这条硬约束上。机制已证实，触发尚未观测到，所以按「可能发生」处理。
    """
    text = _SECRET_HEX.sub("<已脱敏>", text)
    return text if len(text) <= _MAX_LEN else text[:_MAX_LEN] + "…"


class AgentWeChatError(RuntimeError):
    """全部项目自有错误的基类。

    继承 RuntimeError：适配层一个 except 就能覆盖全部，且既有
    `pytest.raises(RuntimeError)` 断言（如 decrypt 管线）继续成立。

    hint 只在有可执行修复动作时携带；消息本身已含指引就不重复。
    """

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class KeyNotFound(AgentWeChatError):
    """passphrase 不可用（未 bootstrap 或缓存缺失）。"""


class VectorError(AgentWeChatError):
    """向量层不可用（sqlite-vec 缺失 / 模型不一致 / 索引为空）。"""


class CaptureError(AgentWeChatError):
    """lldb 捕获 passphrase 失败。"""


class AccountNotFound(AgentWeChatError):
    """账号目录不存在，或无法确定当前登录账号。"""


class SearchInputError(AgentWeChatError):
    """检索入参不合法（缺 account、limit 非正）。"""


class ContactNotFound(AgentWeChatError):
    """联系人名未命中任何候选。"""


class DecryptError(AgentWeChatError):
    """快照/派生/sqlcipher 解密失败。"""


class DependencyMissing(AgentWeChatError):
    """运行期依赖缺失（如 zstandard 未安装）。"""


class DataNotFound(AgentWeChatError):
    """要读取的库/数据文件不存在。"""


class HomeUnavailable(AgentWeChatError):
    """数据目录（`$AGENT_WECHAT_HOME` / `~/.agent-wechat`）不可用：解析不了、建不出来、读不了。

    与 DataNotFound 的区别：那个是「东西不在」，这个是「**位置本身坏了**」——符号链接环让
    `mkdir`/`read_text` 抛 ELOOP，权限不足抛 EACCES，只读挂载抛 EROFS。不翻译的话裸 OSError
    会穿透适配层：CLI 那边是一整个 traceback，MCP 那边落到 `generic_payload` 兜底，agent 只
    拿到「内部错误: FileExistsError: ...」，没有 hint。本模块开篇那条契约就是为此而立的。
    """


class SchemaDrift(AgentWeChatError):
    """微信库的结构与声明不符：表没了，或列被改名/删除。

    与 DataNotFound 的边界：那个是**文件**不存在（库没导、没解密），这个是**文件在、
    结构不对**——微信升级触发后者，环境问题触发前者。两者的降级策略也不同。

    三个字段都是降级路径的输入：`capability` 让调用方查「能力 → 降级策略」表；
    `missing_columns` 决定错误消息的写法（空 = 表不存在，非空 = 列缺了）。
    """

    def __init__(self, capability: str, db: str, table: str,
                 missing_columns: tuple[str, ...] = ()):
        self.capability = capability
        self.db = db
        self.table = table
        self.missing_columns = missing_columns
        if missing_columns:
            detail = f"缺少列 {', '.join(missing_columns)}"
        else:
            detail = "表不存在"
        super().__init__(
            f"微信库结构与预期不符（{capability}）：{db} 的 {table} {detail}",
            hint="微信升级可能改变了库结构；可用 `agent-wechat inspect` 对比结构")


class TaskNotFound(AgentWeChatError):
    """查询的任务 id 不在注册表里（含重启后记录仍在、但 id 写错的情形）。"""


class PurgeRefused(AgentWeChatError):
    """purge 的拒绝或失败。hint 承载具体原因（被占用 / 符号链接 / 权限）。

    OS 层异常（PermissionError / OSError / shutil.Error）由 purge.py 捕获后重抛为本类：
    本模块的契约是 core 只抛项目异常，适配层才做翻译。
    """


def to_payload(exc: AgentWeChatError) -> dict:
    """异常 → MCP 工具错误信封。形状与旧手写的 {"error"[, "hint"]} 逐字节一致。"""
    payload = {"error": redact(str(exc))}
    if exc.hint:
        payload["hint"] = redact(exc.hint)
    return payload


def to_text(exc: AgentWeChatError) -> str:
    """异常 → 单行 CLI 文本（沿用 cmd_semantic 旧的「error（hint）」格式）。"""
    return f"{redact(str(exc))}（{redact(exc.hint)}）" if exc.hint else redact(str(exc))


def generic_payload(exc: BaseException) -> dict:
    """非项目异常（fastembed/sqlite3 等）的兜底信封。

    文案必须进信封：`_guard` 之后 SDK 再也看不到这个异常，若这里不带出类型与说明，
    它就连服务端日志也没有了，成为唯一不可观测的失败路径。
    """
    return {"error": redact(f"内部错误: {type(exc).__name__}: {exc}")}
