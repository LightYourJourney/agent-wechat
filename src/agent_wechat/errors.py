"""统一错误契约：core 只抛 AgentWeChatError 子类，由适配层（mcp_server / cli）翻译。

为什么不能靠 SDK 兜底：
- 即使是 `Exception`，SDK 也会统一改写成 "Error executing tool <name>" 的通用文案，
  异常自带的中文修复指引只留在服务端日志，到不了 agent。

配套的不变量（EA-027）：**core 任何模块都不抛 `SystemExit`**——它不是 `Exception`
的子类，两个适配层的兜底（`except Exception`）都接不住，逃出去会直接杀死常驻 stdio
server。这条由源码级护栏钉住（`tests/test_errors.py::test_core_never_raises_systemexit`
按源码文本核对，全仓唯一一处抛出点在 CLI 的退出漏斗）；适配层**不**扩到 `except
BaseException`，否则会把 Ctrl-C 的 `KeyboardInterrupt` 一起吞成一条工具结果。

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

    为什么必须做：`decrypt.py` 把派生密钥拼成 `PRAGMA key = "x'<64位hex>'"` 交给
    sqlcipher，而 sqlcipher 报错时会**回显出错语句并加插入符**。两轮复审都在 /tmp 用真
    sqlcipher 实测到：**密钥行自身出错时必回显**（stdin 与 argv 两形态一样），且回显块被
    **截到 80 列**（无续行，截到哪算哪）——被截的那行只剩密钥的**前缀**（实测 62/64 位），
    而本函数的判据只认「恰好 64 位连续 hex」，对残段无效。所以 `decrypt` 侧只把 stderr 的
    **首个非空行**交出来（见 `decrypt._first_line`：残段住在回显块里），首行里的完整密钥
    由本函数换成 `<已脱敏>`。
    C1 之前这段文字被 MCP SDK 吞掉、只留在服务端日志；C1 之后它会进对外信封、到 agent
    手上，甚至发往外部 API——直接压在「密钥不进错误信息」这条硬约束上。
    （EA-053：命令已改走 stdin，密钥不再出现在 argv；回显机制与脱敏的必要性不变。）
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


class VectorIndexEmpty(VectorError):
    """向量索引为空（尚未构建，或 rebuild 进行中）。

    EA-096：与其它 VectorError 分开，好让两个适配层按**类型**追加入口专属的
    重建命令名（core 的 hint 只给中立动作，不点名任何入口的命令）。
    """


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
                 missing_columns: tuple[str, ...] = (), *,
                 detail: str | None = None, hint: str | None = None):
        """`detail` / `hint` 可覆盖（EA-105）：有一种形态**不是结构漂移**——库文件损坏
        （`file is not a database`）。复用本类型可以（降级策略相同），但默认文案会说
        「表不存在 / 微信升级」并把用户指向 `inspect`，对损坏的库是**错诊断**。"""
        self.capability = capability
        self.db = db
        self.table = table
        self.missing_columns = missing_columns
        if detail is None:
            detail = (f"缺少列 {', '.join(missing_columns)}" if missing_columns
                      else "表不存在")
        super().__init__(
            f"微信库结构与预期不符（{capability}）：{db} 的 {table} {detail}",
            hint=hint or "微信升级可能改变了库结构；可用 `agent-wechat inspect` 对比结构")


class TaskNotFound(AgentWeChatError):
    """查询的任务 id 不在注册表里（含重启后记录仍在、但 id 写错的情形）。"""


class PurgeRefused(AgentWeChatError):
    """purge 的拒绝或失败。hint 承载具体原因（被占用 / 符号链接 / 权限）。

    OS 层异常（PermissionError / OSError / shutil.Error）由 purge.py 捕获后重抛为本类：
    本模块的契约是 core 只抛项目异常，适配层才做翻译。
    """


class MediaKeyError(AgentWeChatError):
    """图片密钥推导/验证失败（kvcomm 无 code、密钥不匹配、无样本可验）。

    语义是**能力级**的：图片解不了，语音/视频不受影响——调用方（export_media）
    捕获它降级，不要让它打断整次导出。
    """


class IndexUnavailable(AgentWeChatError):
    """本地检索库（`index.db`）打不开或读不了：文件损坏、半拷贝的 WAL、权限/目录问题。

    与 SchemaDrift 的区别：那个说的是**微信的库**结构与声明不符（降级后仍有别的路可走）；
    这个是**自己的索引库**根本进不去。读路径（`message_media`）没有降级可言——没有索引就
    没有消息可判，答案只可能是一个零，而那个零会把「库坏了」谎报成「这个会话没有媒体」。
    不包装的话裸 sqlite3.Error / OSError 会穿透适配层（本模块开篇的契约）：CLI 那边是一整个
    traceback，MCP 落到 generic_payload 兜底，agent 只拿到「内部错误: …」。
    """


class ContainerWriteRefused(AgentWeChatError):
    """落点落在微信容器的可写范围（`paths.CONTAINER_DATA` 及其所有祖先，含 `/`）
    → 拒绝写入。两处触发：`--out`（四个入口）与 `AGENT_WECHAT_HOME`（`ensure_home_dirs`）。

    与 MediaOutputError 的分界：那个是**真写不动**（只读目录 / 磁盘满）；这个是
    **策略拒绝**——目标目录往往完全可写，拒绝的理由是落点选在了微信自己的容器里
    （SECURITY.md §3 的只读宣称：四个 `--out` 例外都带容器路径判据）。故 message
    已含被拒范围本身，hint 只给可执行动作（换一个容器之外的目录）。
    """


class MediaOutputError(AgentWeChatError):
    """媒体输出目录不可建或不可写（`--out` 指向只读目录、不可写 HOME、磁盘满）。

    与 HomeUnavailable 的区别：那个说的是 agent-wechat 自己的数据目录
    （`$AGENT_WECHAT_HOME`），这个是**导出目标**（`--out` 或默认的 `HOME/media`）。
    不包装的话裸 PermissionError/OSError 会穿透适配层（本模块开篇的契约）：CLI 那边
    是一整个 traceback，MCP 落到 generic_payload 兜底，agent 只拿到「内部错误: …」。
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


class SnsMediaError(AgentWeChatError):
    """朋友圈媒体下载/解密失败。消息不得含 token/key（两者都是鉴权材料）。"""
