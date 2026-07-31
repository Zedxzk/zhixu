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

Before consulting a model, unrecognized text is searched against the current user's
eligible notes. General questions and summaries require an explicitly configured model.
Model output is accepted only through a strict action schema, and every proposed mutation
requires code-side confirmation and authorization. L3/L4 data never enters a model prompt.

## Message retention

Inbound message bodies are processed in memory and are not stored as chat history. Only an
explicit note, task, or reminder command creates domain data. Receipts contain keyed opaque
hashes, intent/result metadata, and timestamps rather than the original message or platform
identifier.
