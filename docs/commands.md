# Conversational commands

QQ is the first conversational channel, but these commands belong to the channel-independent
assistant. A future inbound adapter can submit the same normalized events and receive the
same deterministic behavior.

## Help

```text
/帮助
/help
/菜单
```

Help is rendered as a deterministic Markdown card with quick buttons for today's agenda,
the task list, and the reminder list. It never calls an LLM.

## Agenda, tasks, and notes

```text
/今天
/日程

/待办
/任务 Buy replacement filter
/完成 task_ID
/延期 task_ID 30分钟

/记 The spare key is in drawer seven
/搜索 spare key
/总结 project keyword
```

`/今天`, task listing and updates, note creation, and full-text search never call an LLM.
`/总结` uses deterministic search first; without a configured model it returns the matching
notes instead of failing.

## Group modes and data scope

Every observed group is disabled by default. An administrator must explicitly configure
it as either `public` or `internal`.

- A public group accepts only `/帮助` and `/问`. This remains true even when the sender has
  a bound account; public-group execution cannot read or write private or shared records.
- An internal group requires both a bound identity and membership in that group's ACL.
  Reads are limited to the current group's shared database. They never include any member's
  private records or another group's shared records.
- Creates issued in an internal group default to its shared database and retain the human
  creator separately from the group owner. Reminders target the current group.
- A private conversation reads the user's private database plus every enabled internal-group
  shared database for which that user remains a member.

An internal-group member can explicitly create a private record without exposing its contents
to group queries:

```text
/私人任务 Buy a personal item
/私人记 A private note
/私人提醒 明天下午3点提醒我处理私人事项
```

Confidential records remain unavailable in all group chats regardless of group mode.

## Reminders

```text
15分钟后提醒我关烤箱
明天上午9点提醒我提交报告
稍后提醒我检查下载

/提醒
/提醒列表
/取消提醒 reminder_ID
/提醒完成 reminder_ID
/提醒稍后 reminder_ID 15分钟
```

The reminder list shows the internal reminder identifier needed by cancellation commands.
Delivery buttons produce the same `/提醒完成` and `/提醒稍后` actions, so button callbacks
remain deterministic and idempotent. A reminder can target only a channel identity already
bound to the current internal user.

## Optional questions

```text
/问 Why does the sky appear blue?
```

Before consulting a model, unrecognized text is searched against the notes eligible in the
current scope. Public-group questions skip note search entirely. General questions and
summaries require an explicitly configured model.
Model output is accepted only through a strict action schema, and every proposed mutation
requires code-side confirmation and authorization. L3/L4 data never enters a model prompt.

## Message retention

Inbound message bodies are processed in memory and are not stored as chat history. Only an
explicit note, task, or reminder command creates domain data. Receipts contain keyed opaque
hashes, intent/result metadata, and timestamps rather than the original message or platform
identifier.
