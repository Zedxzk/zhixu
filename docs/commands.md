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
the task list, and the reminder list. Public-group help uses the same Markdown rendering
without adding a dummy button. It never calls an LLM.

## Agenda, tasks, and notes

```text
/今天
/日程
/日历
/日历 2026-08

/待办
/任务 Buy replacement filter
/完成 task_ID
/延期 task_ID 30分钟

/记 The spare key is in drawer seven
/搜索 spare key
/总结 project keyword
```

`/今天` and `/日程` are one combined chronological view: ordinary agenda occurrences and
active reminders appear together. `/日历` renders the same two resource types in a monthly
preview with previous/current/next-month buttons; `/提醒` remains the management view that
shows reminder identifiers needed for complete, cancel, and snooze actions. This is a
user-facing merge only: reminder delivery state remains separate from calendar recurrence.

`/今天`, `/日历`, task listing and updates, note creation, and full-text search never call an LLM.
`/总结` uses deterministic search first; without a configured model it returns the matching
notes instead of failing.

## Group modes and data scope

Every observed group is disabled by default. The singleton project administrator can
register an internal group without copying platform identifiers or editing the database:

```text
# In the administrator's private conversation:
/登记内部群

# Within 10 minutes, in the target group:
/启用内部群 12345678
```

The eight-digit code is random and single-use; the server-side activation challenge stores
only a keyed opaque reference to it.
The group sender used for activation is linked to the project administrator. Other group
senders are enrolled into that group automatically on first use; those principals can use
only the current group's shared database and are not granted access to any private database.

Anyone may request a private identity link, but no assistant or database command is available
until they prove control of an identity inside an enabled internal group:

```text
# In the applicant's private conversation:
/申请绑定

# Within 10 minutes, from the same person in an enabled internal group:
/绑定私聊 12345678
```

Before linking, every private message receives only the binding instructions. After linking,
the private conversation can use its own private database plus shared databases for internal
groups joined by that same group identity. It never inherits project-administrator status or
access to another person's private database.

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

## Important days

```text
/纪念日
/纪念日 结婚 2020-05-20
/生日 张三 1995-08-20
/生日 奶奶 农历 7-25
/生日 李四 农历 1960-7-25
```

An anniversary keeps a running day count in the daily briefing and gains an extra line as
its yearly return approaches, 30, 15 and 7 days ahead by default, and on the day itself.

A birthday marks a date rather than accumulating a count, so it says nothing until it is
near: 7, 3 and 1 days ahead by default, then on the day, with an age when the birth year is
known. Omit the year when it is not.

```text
/重要日子 改 <标识> 名称 新名字
/重要日子 改 <标识> 类型 生日
/重要日子 改 <标识> 日期 2001-12-17
/重要日子 改 <标识> 日期 农历 闰6-15
/重要日子 改 <标识> 预告 30 15 7
/重要日子 改 <标识> 预告 关闭
/重要日子 删除 <标识>

/每日简报 改 <标识> 时间 07:30
/每日简报 改 <标识> 开关 关
/每日简报 删除 <标识>

/日程通知 改 <标识> 文本 新的提醒内容
/日程通知 改 <标识> 时间 09:00
/日程通知 改 <标识> 提前 1
/日程通知 改 <标识> 开关 关
/日程通知 删除 <标识>
```

`/纪念日` lists every important day with the identifier these edits take. Each edit names one
field and leaves the rest alone, so correcting a kind never disturbs a date. Changing the
calendar carries the new date with it, because a lunisolar date and a Gregorian one are not
interchangeable. Deleting is staged for confirmation like every other destructive action.

`农历` records the date on the Chinese lunisolar calendar, whose Gregorian day moves every
year; the conversion uses a vendored snapshot of the Hong Kong Observatory tables, which
agree with the mainland calculation published under GB/T 33661-2017. Supporting the
lunisolar calendar does not change the default: a date without `农历` is an ordinary
Gregorian one.

## Advance notification of calendar events

```text
/提前提醒 24小时 6小时 1小时 30分钟 1分钟 准点
/提前提醒 3天 2小时
```

Timed calendar events are announced ahead of time. The default is 24 hours, 6 hours, 1
hour, 30 minutes, 1 minute before, and at the moment the event starts. `/提前提醒` replaces
that default for everything the owner has; a single event can carry its own set instead,
and an empty set silences that event alone.

All-day events are deliberately excluded. They start at local midnight, so a lead time
would fire in the small hours to announce something the daily briefing already carries.

Announcements are delivered wherever the owner's daily briefing goes, so an owner without a
briefing configured receives none.

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

An explicit `/问` first asks the model for a strictly typed capability plan. The available
capabilities are trusted runtime date/time, authorized read-only Zhixu data, stable model
knowledge, and public web search.
Runtime answers are formatted from the application clock in the configured timezone; the
model cannot replace that value. Web search runs only when the plan selects it. Only the
literal question is sent: note bodies, private records, shared-group records, platform
identifiers, and conversation history are never added to the prompt. A web reply lists up to
five provider-returned public sources in a Markdown message. Likely credentials, account
numbers, email addresses,
network addresses, and other token-shaped values are rejected before egress. Operators can
disable the web capability with `ZHIXU_LLM_WEB_SEARCH=false` without disabling runtime or
stable-knowledge answers.

Before consulting a model, unrecognized text is searched against the notes eligible in the
current scope. Public-group questions skip note search entirely. Unprefixed text does not
trigger web search. General questions and summaries require an explicitly configured model.
Model output is accepted only through a strict action schema, and every proposed mutation
requires code-side confirmation and authorization. L3/L4 data never enters a model prompt.

## Message retention

Inbound message bodies are processed in memory and are not stored as chat history. Only an
explicit note, task, or reminder command creates domain data. Receipts contain keyed opaque
hashes, intent/result metadata, and timestamps rather than the original message or platform
identifier.
