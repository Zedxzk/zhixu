# Security Policy

## Reporting a vulnerability

请使用 GitHub 仓库的 **Security → Report a vulnerability** 私下提交安全报告。

不要创建公开 Issue，也不要附加真实访问令牌、个人数据、服务器地址、数据库或未脱敏日志。
复现材料应使用合成身份和无效凭据，并只包含确认问题所需的最少信息。

## Scope

当前开发版本尚未承诺稳定的安全支持周期。以下问题始终属于安全范围：

- 身份认证或权限绕过；
- 跨用户、跨通道或跨机器人身份混淆；
- LLM 或提示词绕过确定性策略；
- 日志、错误信息、备份或导出泄漏私人数据；
- 保险库密钥、秘密或一次性授权泄漏；
- 消息伪造、重复执行和出站请求越权。

收到报告后，维护者将在 GitHub Security Advisory 中协调确认、修复和披露。

公开的系统边界、威胁和安全不变量见
[威胁模型](docs/security/THREAT_MODEL.md)。该文档不包含任何实际部署信息。
