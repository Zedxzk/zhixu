# Contributing

感谢参与知序。

提交变更前请运行：

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/release/verify_release.sh
```

项目不使用托管 GitHub Actions；发布门禁必须在受信任的本机执行。

贡献要求：

- 测试只能使用合成身份、无效凭据和虚构消息。
- 不提交运行数据、数据库、备份、截图、内部设计文档或部署环境信息。
- 新增外部副作用必须通过明确的端口、权限检查和可靠队列。
- 新增 LLM 能力必须提供关闭模型后的确定性行为测试。
- 新增通道不得把平台外部身份作为内部用户主键。
- 安全相关细节请使用 GitHub Security Advisory，不要放入公开 Issue。
