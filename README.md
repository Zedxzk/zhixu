# 知序 Zhixu

知序是一个隐私优先、自托管的个人智能助理。

它以确定性代码管理日程、任务、备忘录、提醒、权限和消息投递；LLM 是可选能力，只用于自然
语言理解、归纳和开放问答。即使不配置任何模型，核心功能仍然可以完整运行。

## 项目原则

- 确定性优先：明确命令、固定查询、调度和通知不调用 LLM。
- 最小权限：LLM不能直接写数据库、发送消息、运行命令或读取秘密。
- 数据分级：个人数据、机密数据和秘密采用不同的访问与输出策略。
- 通道可插拔：QQ 是首个完整通道，核心不绑定任何消息平台。
- 私有部署：不要求公网入站，不内置遥测。
- 安全默认：敏感能力未配置或未解锁时一律拒绝，不回退到明文。

## 当前状态

项目处于早期开发阶段，已实现的首条端到端链路是：

```text
QQ 消息 → 确定性解析 → 持久化提醒 → 调度 → 可靠队列 → QQ 主动推送
```

普通数据与高敏秘密使用不同的数据库、进程、操作系统账户和备份密钥。独立保险库默认保持
密封，只通过受限 Unix socket 接收经过授权的请求。QQ 网络进程也不具有普通业务数据库的
文件访问权限。

## 开发

要求 Python 3.11 或更高版本。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python scripts/privacy_scan.py
```

仓库不运行 GitHub Actions。正式发布前请在受信任的本机执行
`PATH="$PWD/.venv/bin:$PATH" bash scripts/release/verify_release.sh`。

Windows 请使用虚拟环境中对应的 `Scripts` 命令路径。

私有服务器安装见 [部署文档](docs/deployment.md)，管理与保险库接口见
[API 文档](docs/api.md)，QQ 与其他会话通道的确定性用法见
[命令文档](docs/commands.md)。

## 隐私

公开仓库只接受合成测试数据。请勿在 Issue、日志、截图、测试夹具或提交历史中加入真实账号、
联系人标识、消息正文、服务器信息、访问令牌、数据库或备份。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
