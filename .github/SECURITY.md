# Security Policy

本仓库的安全模型、隐私边界与数据出口清单见 **[docs/SECURITY.md](../docs/SECURITY.md)**。

## 报告问题

请通过 GitHub 的私密漏洞报告（Security → Report a vulnerability）提交，或用仓库 owner
在 profile 上公开的联系方式直接联系作者。**不要**开公开 issue 描述可利用细节。

## 附带材料时的红线

本项目处理的是使用者**本人已登录账号**的本地数据。任何 issue、PR 或私密报告里都**不要**附带：

- 真实聊天正文、联系人备注名 / 昵称 / 群名
- 真实 `wxid`、`@chatroom` 群 id、手机号
- 数据库密钥、`key_info`、导出产物（`.db` / HTML / JSONL）
- 本机绝对路径（用户名部分）

复现请用合成夹具：仓库测试里的 `wxid_testacct01`、`/Users/you` 那套就是为此准备的。
