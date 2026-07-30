# 保险库威胁模型

本文只描述开源实现的通用边界，不包含实际服务器、账户、域名、密钥位置或运维拓扑。

## 目标与非目标

保险库保护 L3 机器秘密和人类秘密，拒绝 L4（支付口令、银行 PIN、保险库主口令）。它提供
最小元数据查询、受控 `use`、增强认证后的 `reveal`、更新、删除、导出、授权和轮换。

保险库不提供即时通信端到端加密，不声称能抵抗已经完全控制主机内核和进程内存的攻击者。

## 隔离边界

- `zhixu_vault` 是独立包、独立进程、独立 SQLite 和独立无登录服务账户。
- 只监听 Unix socket，并校验对端 UID；不监听 TCP，也不需要网络权限。
- QQ、LLM 和普通应用 repository 不导入保险库实现或 `SecretValue`。
- 普通应用只能提交短期、签名、动作与资源精确绑定的一次性 capability。

## 密钥层次

```text
用户保险库口令
  → Argon2id（独立随机 salt、明确参数）
  → unlock key
  → AES-GCM 解包每个版本的 master key
  → AES-GCM 解包每条记录的随机 data key
  → AES-GCM 解密秘密值
```

数据库和备份不保存口令、unlock key、master key、data key 或明文。轮换创建新的 master key 并
重包 data key；修改口令重新包装 master key，不需要解密并重写所有秘密正文。

## 认证与授权

- Passkey 使用 WebAuthn RP ID、HTTPS origin、随机 challenge、用户验证和签名计数校验。
- capability 使用 Ed25519 签名，绑定 issuer、subject、secret、action、audience、过期时间、
  认证强度和随机 nonce。
- nonce 在事务中只消费一次；过期、跨资源、跨动作、错误 audience 和重放全部拒绝。
- 人类秘密只能 `reveal`，机器秘密只能由注册 executor `use`；executor 返回操作结果而非秘密。
- `reveal`、更新、删除、导出、授权和轮换要求 step-up。

## 数据与日志

- 普通应用数据库、FTS、outbox、LLM prompt 和通道消息不得包含 L3/L4。
- vault 审计只保存主体、动作、不透明 secret ID、结果和原因，不保存 label 或秘密。
- 审计使用稳定派生密钥形成 HMAC 链；离线 CLI 校验链断裂或字段篡改。
- `SecretValue` 默认隐藏 repr，使用后覆盖其可变缓冲区；Python 无法保证消除所有运行时副本。

## 恢复与失效

- 空闲超时或显式锁定会覆盖内存密钥；锁定后旧 capability 也无法解密。
- 备份在原有信封加密外再使用独立 Argon2id + AES-GCM 加密，恢复不覆盖已有目标。
- break-glass 包必须使用独立、离线保存的恢复口令，并按使用即轮换的流程处理。
- 保险库口令和恢复口令同时丢失时，设计上无法恢复秘密。

## 必须持续测试的攻击

- 错口令、复制数据库、篡改密文、错误 AAD；
- capability 过期、重放、跨 secret/action/audience；
- Passkey challenge 过期或重复、错误 RP/origin、签名计数回退；
- 自动锁定后的读取、密钥与口令轮换、备份恢复；
- 日志、异常、审计、数据库和备份中的 canary 明文；
- 普通应用、QQ 和 LLM 对保险库实现的依赖越界。
