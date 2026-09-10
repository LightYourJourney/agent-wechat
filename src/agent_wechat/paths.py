"""微信数据路径解析。纯函数 + 常量，不做有副作用的 IO。

实测路径结构（WeChat 4.1.13, macOS 沙盒容器）：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
    ├── xwechat_files/<wxid>_<hex4>/          # 账号数据目录（wxid 带后缀）
    │   └── db_storage/{message,session,contact,...}/*.db
    ├── xwechat_files/all_users/login/<wxid>/ # key_info.db / key_info.dat（裸 wxid）
    └── app_data/login/<wxid>/key_info.dat    # 每次登录重写（裸 wxid）
"""
from __future__ import annotations

import os
import pathlib
import re

CONTAINER = pathlib.Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents"
XWECHAT_FILES = CONTAINER / "xwechat_files"
APP_DATA = CONTAINER / "app_data"

# 账号目录形如 wxid_testacct01_ab12（尾部 _hex4 是实例后缀）
ACCOUNT_RE = re.compile(r"^wxid_.+_[0-9a-f]{4}$")
# 消息分片形如 message_0.db；不含 message_fts/message_resource 等非分片库
SHARD_RE = re.compile(r"^message_(\d+)\.db$")

# 本项目数据目录（0700）：密钥缓存、临时明文、导出库。可用环境变量覆盖。
HOME_DIR = pathlib.Path(os.environ.get("AGENT_WECHAT_HOME", str(pathlib.Path.home() / ".agent-wechat")))


def account_dirs() -> list[str]:
    """枚举本机所有账号数据目录名（wxid_xxx_hex4）。"""
    if not XWECHAT_FILES.is_dir():
        return []
    return sorted(p.name for p in XWECHAT_FILES.iterdir() if p.is_dir() and ACCOUNT_RE.match(p.name))


def bare_wxid(account: str) -> str:
    """wxid_testacct01_ab12 -> wxid_testacct01（登录目录用裸 wxid）。"""
    return account.rsplit("_", 1)[0]


def db_storage(account: str) -> pathlib.Path:
    return XWECHAT_FILES / account / "db_storage"


def message_shards(account: str) -> list[pathlib.Path]:
    """消息分片（message_0.db..N），按编号排序。绝不包含 fts/resource/revoke/biz/media。"""
    d = db_storage(account) / "message"
    if not d.is_dir():
        return []
    shards = [(int(m.group(1)), d / m.group(0))
              for p in d.iterdir() if (m := SHARD_RE.match(p.name))]
    return [p for _, p in sorted(shards)]


def key_info_dat(account: str) -> pathlib.Path:
    """app_data/login/<裸wxid>/key_info.dat（每次登录重写的那份）。"""
    return APP_DATA / "login" / bare_wxid(account) / "key_info.dat"


def key_info_db(account: str) -> pathlib.Path:
    """xwechat_files/all_users/login/<裸wxid>/key_info.db（明文 SQLite，56 行包裹 blob）。"""
    return XWECHAT_FILES / "all_users" / "login" / bare_wxid(account) / "key_info.dat"


def all_dbs(account: str) -> list[pathlib.Path]:
    """账号下全部 .db 文件（用于密钥校验覆盖统计）。"""
    root = db_storage(account)
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*.db"))
