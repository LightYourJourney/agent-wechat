"""用例层：MCP 与 CLI 共用的入口策略。

为什么要有这一层：账号解析、会话消歧、limit 归一化此前在 mcp_server.py 与 cli.py
各写一遍，形态还不一致——CLI 的候选列表丢 `kind`、export 的歧义被误报成「找不到
联系人」、同一个 limit 在两个入口得到不同处理。现在每个工具一个用例函数，独占
参数归一化、账号与会话解析与结果形状；两个入口只剩「参数编组」与「渲染」。

约定：
- 成功返回类型化结果（dataclass / 既有 dict 形状），歧义返回 NeedConfirm（不是
  错误），未命中抛 errors.ContactNotFound——本层不产生任何展示文案；
- 不 import MCP SDK（与其它 core 模块一致）。

**本包内部的划分**（2026-11-21 从单文件 1804 行拆出）：

| 子模块 | 内容 |
|---|---|
| `_core` | 参数归一化、结果类型、`ChatContext`、降级收集——依赖图的根，不依赖兄弟子模块 |
| `chat` | 账号状态、联系人、时间线、导出、会话导出与总目录页 |
| `media` | 媒体导出、按消息查媒体状态、appmsg 部件解码 |
| `memory` | 按会话销毁目标、回填（媒体引用 / 部件 / 位置名片） |
| `retrieve` | 检索、上下文、向量构建与长任务 |
| `moments` | 朋友圈导出 |

**这一层是重导出面**：外部（`cli.py` / `mcp_server.py` / 测试）继续写
`usecases.xxx`，不必知道某个函数落在哪个子模块。新增用例请加在对应子模块，
再在这里补一行导入。
"""
from __future__ import annotations

from ._core import (DEGRADE_EFFECT, LIMIT_RECENT, LIMIT_SEMANTIC, LIMIT_UNBOUNDED,
                    MAX_CANDIDATES, MEDIA_KINDS, MONTH_RE, ROSTER_TTL, Candidate, ChatContext,
                    Degradation, ExportPlan, NeedConfirm, RecentMessages, clamp_limit,
                    collect_degradations, to_accounts)
from .chat import (ChatExportResult, ChatIndexResult, ExtraPart, MessagePart,
                   MessagePartsResult, account_status, chat_export, chat_index, chats,
                   contacts, export_history, export_plan, index_snapshot, message_parts,
                   recent_messages, timeline, warm_up)
from .media import (LIMIT_MEDIA, MediaExportResult, MediaRefState, MessageMediaResult,
                    media_export, message_media)
from .memory import (appmsg_rebuild_parts, media_rebuild_refs, msgextra_rebuild,
                     purge_chat_targets)
from .moments import MomentsExportResult, moments_export
from .retrieve import (LIMIT_RETRIEVAL, build_vector_index, index_ready, message_context,
                       search_history, search_messages, semantic_search, start_vector_build,
                       task_status)

# 测试会直接调这几个私有判据（它们是真身的唯一一份，测试要钉住同一份实现）。
# 对外仍算私有，只是让 `usecases._xxx` 这条路继续可用。
# （`_decode_part` / `_refresh_roster` 不在此列：只有 docstring 提到它们，没有调用点。）
from .media import _decode_extra, _state_of, _voice_keys_for  # noqa: E402
from .moments import _sniff_ext  # noqa: E402

# 这几个 core 模块对象继续挂在 `usecases` 名下：测试用 `monkeypatch.setattr(usecases.tasks,
# "start", …)` 钉住真身（不是替身），拆包后必须仍然可达，否则那些用例会静默 patch 到
# 一个不存在的属性上。它们不是本包的 API，只是沿用旧的引用路径。
#
# **不要在这里导入 `media`**：本包有同名子模块 `usecases/media.py`，这一行会把包属性
# `usecases.media` 覆写成 core 的 `agent_wechat.media`（2026-11-21 实测：`import
# agent_wechat.usecases.media as sub` 拿到的也是 core 那个），而真实子模块只在
# `sys.modules` 里。需要 core 的 media 就直接 `from agent_wechat import media`。
from .. import appmsg, export, retrieval, tasks, vector  # noqa: E402,F401
