"use strict";

const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
};

const state = {
  token: sessionStorage.getItem("zhixu.session") || "",
  view: location.hash.replace("#", "") || "dashboard",
  search: "",
  agendaFilter: "future",
  taskFilter: "open",
  workspaceFilter: "all",
  workspaces: [],
  status: null,
  agenda: [],
  reminders: [],
  tasks: [],
  notes: [],
  identities: [],
  channels: [],
  routes: [],
  outbox: [],
  audit: [],
  llm: [],
  importantDays: [],
  importantDaysEndpoint: "",
  editorItem: null,
};

const viewTitles = {
  dashboard: "今天，一切有序",
  agenda: "日程",
  reminders: "提醒",
  tasks: "待办",
  notes: "备忘",
  "important-days": "重要日子",
  connections: "连接",
  system: "系统状态",
};

function setBusy(busy) {
  qs("#loading-bar").hidden = !busy;
}

function toast(message, kind = "success") {
  const item = node("div", `toast ${kind}`, message);
  qs("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3600);
}

function errorMessage(value) {
  if (value && value.error && value.error.message) return value.error.message;
  return "请求没有完成，请稍后重试。";
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
    credentials: "same-origin",
  });
  const value = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(errorMessage(value));
    failure.status = response.status;
    failure.payload = value;
    throw failure;
  }
  return value;
}

async function optionalApi(paths) {
  for (const path of paths) {
    try {
      return { path, value: await api(path) };
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  }
  return { path: "", value: [] };
}

function showLogin(message = "") {
  qs("#login-view").hidden = false;
  qs("#app-view").hidden = true;
  qs("#login-error").textContent = message;
  qs("#login-password").value = "";
}

function showApp() {
  qs("#login-view").hidden = true;
  qs("#app-view").hidden = false;
  setView(state.view in viewTitles ? state.view : "dashboard", false);
}

function setView(view, updateHash = true) {
  state.view = view in viewTitles ? view : "dashboard";
  qsa("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === state.view));
  qsa("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  qs("#view-title").textContent = viewTitles[state.view];
  if (updateHash) history.replaceState(null, "", `#${state.view}`);
  qs("#global-search").value = "";
  state.search = "";
  qs(".sidebar").classList.remove("open");
  qs("#mobile-menu-button").setAttribute("aria-expanded", "false");
  renderCurrentView();
}

function renderCurrentView() {
  const renders = {
    dashboard: renderDashboard,
    agenda: renderAgenda,
    reminders: renderReminders,
    tasks: renderTasks,
    notes: renderNotes,
    "important-days": renderImportantDays,
    connections: renderConnections,
    system: renderSystem,
  };
  renders[state.view]();
}

function includesSearch(...values) {
  if (!state.search) return true;
  return values.some((value) => String(value || "").toLocaleLowerCase().includes(state.search));
}

function inSelectedWorkspace(item) {
  return state.workspaceFilter === "all" || item.workspace?.id === state.workspaceFilter;
}

function scoped(items) {
  return items.filter(inSelectedWorkspace);
}

function workspaceTag(item) {
  const workspace = item.workspace || { kind: "private", label: "私人空间" };
  return tag(workspace.label, workspace.kind === "group" ? "lime" : "");
}

function renderWorkspaceSelector() {
  const select = qs("#workspace-scope");
  const selected = state.workspaceFilter;
  const options = [{ id: "all", label: "全部空间" }, ...state.workspaces];
  select.replaceChildren(...options.map((workspace) => {
    const option = node("option", "", workspace.label);
    option.value = workspace.id;
    return option;
  }));
  state.workspaceFilter = options.some((item) => item.id === selected) ? selected : "all";
  select.value = state.workspaceFilter;
}

function localDate(value) {
  if (!value) return null;
  const dateOnly = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const result = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  return Number.isNaN(result.getTime()) ? null : result;
}

function formatDate(value, options = {}) {
  const date = value instanceof Date ? value : localDate(value);
  if (!date) return "未设置";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: options.dateOnly ? undefined : "2-digit",
    minute: options.dateOnly ? undefined : "2-digit",
    weekday: options.weekday ? "short" : undefined,
  }).format(date);
}

function sameDay(a, b = new Date()) {
  const value = localDate(a);
  return value && value.getFullYear() === b.getFullYear() && value.getMonth() === b.getMonth() && value.getDate() === b.getDate();
}

function future(a) {
  const value = localDate(a);
  return value && value.getTime() >= Date.now();
}

function empty(container, message) {
  container.replaceChildren(node("div", "empty-state", message));
}

function tag(text, className = "") {
  return node("span", `tag ${className}`.trim(), text);
}

function miniButton(label, action, className = "") {
  const button = node("button", `mini-button ${className}`.trim(), label);
  button.type = "button";
  button.addEventListener("click", action);
  return button;
}

async function loadData({ quiet = false } = {}) {
  if (!quiet) setBusy(true);
  try {
    const importantPromise = optionalApi(["/admin/important-days", "/admin/anniversaries"]);
    const [status, workspaces, agenda, reminders, tasks, notes, identities, channels, routes, outbox, audit, llm, important] = await Promise.all([
      api("/admin/status"),
      api("/admin/workspaces"),
      api("/admin/agenda"),
      api("/admin/reminders"),
      api("/admin/tasks"),
      api("/admin/notes"),
      api("/admin/identities"),
      api("/admin/channels"),
      api("/admin/channel-routes"),
      api("/admin/outbox?limit=30"),
      api("/admin/audit?limit=30"),
      api("/admin/llm-usage?limit=30"),
      importantPromise,
    ]);
    Object.assign(state, { status, workspaces, agenda, reminders, tasks, notes, identities, channels, routes, outbox, audit, llm });
    state.importantDays = Array.isArray(important.value) ? important.value : [];
    state.importantDaysEndpoint = important.path;
    renderWorkspaceSelector();
    renderAll();
  } catch (error) {
    if (error.status === 403) {
      sessionStorage.removeItem("zhixu.session");
      state.token = "";
      showLogin("会话已失效，请重新登录。");
      return;
    }
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function renderAll() {
  renderDashboard();
  renderAgenda();
  renderReminders();
  renderTasks();
  renderNotes();
  renderImportantDays();
  renderConnections();
  renderSystem();
}

function renderDashboard() {
  const now = new Date();
  const agenda = scoped(state.agenda);
  const reminders = scoped(state.reminders);
  const tasks = scoped(state.tasks);
  const notes = scoped(state.notes);
  qs("#today-day").textContent = String(now.getDate()).padStart(2, "0");
  qs("#today-weekday").textContent = new Intl.DateTimeFormat("zh-CN", { weekday: "long" }).format(now);
  qs("#today-month").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long" }).format(now);
  qs("#date-kicker").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(now);

  const todayAgenda = agenda.filter((item) => sameDay(item.start_at));
  const todayReminders = reminders.filter((item) => sameDay(item.fire_at) && item.status === "pending");
  qs("#today-summary").textContent = todayAgenda.length || todayReminders.length
    ? `今天有 ${todayAgenda.length} 项日程、${todayReminders.length} 个待触发提醒。`
    : "今天没有必须赶赴的安排，按自己的节奏来。";

  const upcoming = [
    ...agenda.map((item) => ({ ...item, when: item.start_at, kind: "日程" })),
    ...reminders.filter((item) => item.status === "pending").map((item) => ({ ...item, when: item.fire_at, kind: "提醒" })),
  ].filter((item) => future(item.when)).sort((a, b) => new Date(a.when) - new Date(b.when));
  const nextBox = qs("#next-event");
  nextBox.replaceChildren();
  if (upcoming.length) {
    const item = upcoming[0];
    nextBox.className = "next-event";
    nextBox.append(node("time", "", formatDate(item.when, { weekday: true })), node("h3", "", item.title), node("p", "", `${item.kind} · ${item.workspace?.label || "私人空间"}`));
  } else {
    nextBox.className = "next-event empty-state compact";
    nextBox.textContent = "未来暂时没有安排";
  }

  const openTasks = tasks.filter((item) => !["done", "cancelled"].includes(item.status));
  const metrics = [
    ["今日日程", todayAgenda.length, "◇", "按开始时间排列"],
    ["待触发提醒", reminders.filter((item) => item.status === "pending").length, "◷", "未来提醒"],
    ["进行中待办", openTasks.length, "✓", "尚未完成"],
    ["备忘", notes.length, "▱", state.workspaceFilter === "private" ? "私人保存" : "按空间隔离"],
  ];
  const metricGrid = qs("#metric-grid");
  metricGrid.replaceChildren(...metrics.map(([label, count, icon, hint]) => {
    const card = node("article", "metric");
    const top = node("div", "metric-top");
    top.append(node("span", "", label), node("span", "metric-icon", icon));
    card.append(top, node("strong", "", count), node("small", "", hint));
    return card;
  }));

  const timeline = qs("#today-timeline");
  const dayItems = [
    ...todayAgenda.map((item) => ({ title: item.title, when: item.start_at, detail: `${item.description || "日程"} · ${item.workspace?.label || "私人空间"}`, kind: "agenda" })),
    ...todayReminders.map((item) => ({ title: item.title, when: item.fire_at, detail: `提醒 · ${item.workspace?.label || "私人空间"}`, kind: "reminder" })),
  ].sort((a, b) => new Date(a.when) - new Date(b.when));
  if (!dayItems.length) empty(timeline, "今天没有安排");
  else timeline.replaceChildren(...dayItems.map((item) => {
    const row = node("div", `timeline-item ${item.kind}`);
    row.append(node("time", "", new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(item.when))), node("span", "timeline-marker"));
    const content = node("div", "timeline-content");
    content.append(node("strong", "", item.title), node("span", "", item.detail));
    row.append(content);
    return row;
  }));

  const focus = qs("#focus-tasks");
  const selected = openTasks.sort((a, b) => (b.priority - a.priority) || String(a.due_at).localeCompare(String(b.due_at))).slice(0, 5);
  if (!selected.length) empty(focus, "没有待办，轻松一下");
  else focus.replaceChildren(...selected.map((item) => taskStackItem(item)));
}

function taskStackItem(item) {
  const row = node("div", "stack-item");
  const complete = node("button", "check-button");
  complete.type = "button";
  complete.setAttribute("aria-label", `完成 ${item.title}`);
  complete.addEventListener("click", () => transitionTask(item, "done"));
  const main = node("div", "stack-item-main");
  main.append(node("strong", "", item.title), node("small", "", `${item.due_at ? `截止 ${formatDate(item.due_at)}` : "未设置截止时间"} · ${item.workspace?.label || "私人空间"}`));
  row.append(complete, node("span", `priority priority-${item.priority}`), main);
  return row;
}

function renderAgenda() {
  const list = qs("#agenda-list");
  const items = scoped(state.agenda)
    .filter((item) => state.agendaFilter === "all" || future(item.end_at))
    .filter((item) => includesSearch(item.title, item.description, item.recurrence_rule))
    .sort((a, b) => new Date(a.start_at) - new Date(b.start_at));
  if (!items.length) return empty(list, "没有符合条件的日程");
  list.replaceChildren(...items.map((item) => {
    const row = node("article", "resource-item");
    const time = node("div", "resource-time", formatDate(item.start_at));
    const main = node("div", "resource-main");
    main.append(node("h3", "", item.title), node("p", "", item.description || (item.recurrence_rule ? `循环 · ${item.recurrence_rule}` : "单次日程")));
    const actions = node("div", "resource-actions");
    actions.append(workspaceTag(item));
    if (item.recurrence_rule) actions.append(tag("循环", "lime"));
    actions.append(miniButton("编辑", () => openEditor("agenda", item)));
    actions.append(miniButton("删除", () => removeResource("agenda", item), "danger"));
    row.append(time, main, actions);
    return row;
  }));
}

function renderReminders() {
  const list = qs("#reminder-list");
  const items = scoped(state.reminders).filter((item) => includesSearch(item.title, item.status, item.workspace?.label)).sort((a, b) => new Date(a.fire_at) - new Date(b.fire_at));
  if (!items.length) return empty(list, "还没有提醒");
  list.replaceChildren(...items.map((item) => {
    const row = node("article", "resource-item");
    const main = node("div", "resource-main");
    main.append(node("h3", "", item.title), node("p", "", item.related_kind ? `关联 ${item.related_kind}` : "独立提醒"));
    const actions = node("div", "resource-actions");
    const statusClass = item.status === "pending" ? "lime" : "";
    actions.append(workspaceTag(item));
    actions.append(tag(reminderStatus(item.status), statusClass));
    if (item.status === "pending") actions.append(miniButton("取消", () => removeResource("reminders", item), "danger"));
    row.append(node("div", "resource-time", formatDate(item.fire_at)), main, actions);
    return row;
  }));
}

function reminderStatus(status) {
  return ({ pending: "待触发", fired: "已触发", cancelled: "已取消", completed: "已完成" })[status] || status;
}

function renderTasks() {
  const board = qs("#task-board");
  let items = scoped(state.tasks).filter((item) => includesSearch(item.title, item.description, item.status, item.workspace?.label));
  if (state.taskFilter === "open") items = items.filter((item) => !["done", "cancelled"].includes(item.status));
  if (state.taskFilter === "done") items = items.filter((item) => item.status === "done");
  const columns = [
    ["todo", "待处理"],
    ["doing", "进行中"],
    ["done", "已完成"],
  ];
  board.replaceChildren(...columns.map(([status, label]) => {
    const column = node("section", "task-column");
    const selected = items.filter((item) => status === "todo" ? !["doing", "done", "cancelled"].includes(item.status) : item.status === status);
    const header = node("div", "task-column-header");
    header.append(node("strong", "", label), node("span", "", selected.length));
    column.append(header);
    selected.forEach((item) => {
      const card = node("article", "task-card");
      card.append(node("h3", "", item.title));
      if (item.description) card.append(node("p", "", item.description));
      const meta = node("div", "task-meta");
      const taskDetails = node("div", "tags");
      taskDetails.append(workspaceTag(item), node("small", "", item.due_at ? formatDate(item.due_at) : `优先级 ${item.priority}`));
      meta.append(taskDetails);
      const actions = node("div", "resource-actions");
      actions.append(miniButton("编辑", () => openEditor("task", item)));
      if (status !== "done") actions.append(miniButton(status === "doing" ? "完成" : "开始", () => transitionTask(item, status === "doing" ? "done" : "doing")));
      actions.append(miniButton("删除", () => removeResource("tasks", item), "danger"));
      meta.append(actions);
      card.append(meta);
      column.append(card);
    });
    return column;
  }));
}

function renderNotes() {
  const grid = qs("#note-grid");
  const items = scoped(state.notes).filter((item) => includesSearch(item.title, item.body, item.workspace?.label, ...(item.tags || [])));
  if (!items.length) return empty(grid, "还没有备忘，随手记下第一条吧");
  grid.replaceChildren(...items.map((item) => {
    const card = node("article", "note-card");
    card.append(node("h3", "", item.title || "无标题"), node("p", "", item.body || "空白备忘"));
    const footer = node("footer");
    const tags = node("div", "tags");
    tags.append(workspaceTag(item));
    (item.tags || []).slice(0, 3).forEach((value) => tags.append(tag(value)));
    const actions = node("div", "resource-actions");
    actions.append(miniButton("编辑", () => openEditor("note", item)), miniButton("删除", () => removeResource("notes", item), "danger"));
    footer.append(tags, actions);
    card.append(footer);
    return card;
  }));
}

function renderImportantDays() {
  const list = qs("#important-day-list");
  const create = qs("#important-day-create");
  create.disabled = !state.importantDaysEndpoint;
  create.title = state.importantDaysEndpoint ? "添加重要日子" : "生日功能正在同步";
  const items = scoped(state.importantDays).filter((item) => includesSearch(item.title, item.name, item.kind, item.workspace?.label));
  if (!state.importantDaysEndpoint) return empty(list, "生日与重要日子的后端正在同步，UI 已预留入口。同步完成后会自动显示。");
  if (!items.length) return empty(list, "还没有重要日子");
  list.replaceChildren(...items.map((item) => {
    const card = node("article", "day-card");
    const next = item.next_occurrence || item.next_at || item.anchor_date || item.date;
    const nextDate = localDate(next);
    const remaining = nextDate ? Math.max(0, Math.ceil((nextDate.setHours(0,0,0,0) - new Date().setHours(0,0,0,0)) / 86400000)) : "—";
    card.append(node("div", "countdown", remaining), node("p", "eyebrow", remaining === 0 ? "TODAY" : "DAYS TO GO"), node("h3", "", item.title || item.name || "重要日子"));
    const recordedDate = item.calendar === "lunar"
      ? `农历 ${item.lunar_month || "—"} 月 ${item.lunar_day || "—"} 日`
      : (next ? formatDate(next, { dateOnly: true }) : "日期待定");
    const detail = [item.kind === "birthday" ? "生日" : "纪念日", recordedDate, item.calendar === "lunar" ? "农历" : "公历"].join(" · ");
    card.append(node("p", "", detail));
    if (Array.isArray(item.advance_days) && item.advance_days.length) {
      card.append(node("small", "day-leads", `提前 ${item.advance_days.join(" / ")} 天通知`));
    }
    card.append(workspaceTag(item));
    return card;
  }));
}

function renderConnections() {
  const channels = qs("#channel-list");
  if (!state.channels.length) empty(channels, "没有配置渠道");
  else channels.replaceChildren(...state.channels.map((item) => {
    const row = node("div", "stack-item");
    const main = node("div", "stack-item-main");
    main.append(node("strong", "", item.channel.toUpperCase()), node("small", "", item.mode === "conversational" ? "双向会话" : "仅出站"));
    row.append(node("span", "status-dot"), main, tag(item.capabilities?.outbound_text ? "可用" : "受限", "lime"));
    return row;
  }));

  const identities = qs("#identity-list");
  if (!state.identities.length) empty(identities, "没有绑定身份");
  else identities.replaceChildren(...state.identities.map((item) => {
    const row = node("div", "stack-item");
    const main = node("div", "stack-item-main");
    main.append(node("strong", "", `${item.channel.toUpperCase()} 身份`), node("small", "", `绑定于 ${formatDate(item.created_at, { dateOnly: true })}`));
    row.append(node("span", "nav-icon", "◎"), main, tag("已绑定", "lime"));
    return row;
  }));

  const routes = qs("#route-list");
  if (!state.routes.length) empty(routes, "尚未观察到会话");
  else routes.replaceChildren(...state.routes.filter((item) => includesSearch(item.kind, item.group_mode, item.opaque_ref)).map((item) => {
    const row = node("div", "table-row");
    const label = item.kind === "group" ? "群组" : item.kind === "private" ? "私聊" : item.kind;
    const mode = node("select");
    [["disabled", "关闭"], ["public", "公开群"], ["internal", "内部群"]].forEach(([value, text]) => {
      const option = node("option", "", text); option.value = value; option.selected = item.group_mode === value; mode.append(option);
    });
    mode.addEventListener("change", () => updateRoute(item, mode.value));
    row.append(node("strong", "", label), node("span", "", item.channel.toUpperCase()), mode, node("span", "", item.commands_enabled ? "命令已启用" : "命令已关闭"));
    const members = item.members || [];
    if (members.length) {
      const list = node("div", "member-list");
      members.forEach((member) => {
        const entry = node("div", "member-row");
        const field = node("input");
        field.type = "text";
        field.maxLength = 40;
        field.value = member.display_name || "";
        field.placeholder = "未命名成员";
        field.addEventListener("change", () => renameMember(member.id, field.value));
        entry.append(node("span", "", "创建人显示名"), field);
        list.append(entry);
      });
      row.append(list);
    }
    return row;
  }));
}

async function renameMember(memberId, displayName) {
  const name = (displayName || "").trim();
  if (!name) { toast("显示名不能为空", "error"); renderConnections(); return; }
  setBusy(true);
  try {
    await api(`/admin/members/${encodeURIComponent(memberId)}`, {
      method: "POST",
      body: { display_name: name },
    });
    toast("成员显示名已更新");
    await loadData({ quiet: true });
  } catch (error) { toast(error.message, "error"); renderConnections(); }
  finally { setBusy(false); }
}

function renderSystem() {
  const status = state.status || { health: {}, application: {} };
  const components = status.health?.components || {};
  const cards = [
    ["核心服务", status.health?.core || "unknown", "◎"],
    ["数据存储", status.application?.storage || "unknown", "▦"],
    ["模型服务", components.llm || "未配置", "✦"],
    ["敏感数据仓", components.vault || "未配置", "⌁"],
  ];
  qs("#health-grid").replaceChildren(...cards.map(([label, value, icon]) => {
    const metric = node("article", "metric");
    const top = node("div", "metric-top"); top.append(node("span", "", label), node("span", "metric-icon", icon));
    metric.append(top, node("strong", "", value === "available" || value === "ready" ? "正常" : value), node("small", "", value));
    return metric;
  }));
  renderRows(qs("#outbox-list"), state.outbox, (item) => [item.channel || "—", item.message_kind, item.status, `${item.attempts}/${item.max_attempts}`], "暂无投递记录", ["渠道", "消息类型", "状态", "尝试"]);
  renderRows(qs("#llm-list"), state.llm, (item) => [item.reason || "调用", item.model_ref || "模型", item.outcome, `${item.input_units || item.estimated_input_units || 0}（缓存 ${item.cached_input_units || 0}）→ ${item.output_units || 0}`], "暂无模型调用", ["用途", "模型", "结果", "输入（缓存）→ 输出"]);
  renderRows(qs("#audit-list"), state.audit, (item) => [item.action, item.resource_kind, item.outcome, formatDate(item.occurred_at)], "暂无审计记录", ["操作", "资源", "结果", "时间"]);
}

function renderRows(container, items, mapper, message, labels = []) {
  if (!items.length) return empty(container, message);
  container.replaceChildren(...items.map((item) => {
    const row = node("div", "table-row");
    mapper(item).forEach((value, index) => {
      const text = String(value || "—");
      const cell = node(index === 0 ? "strong" : "span", "table-cell", text);
      cell.title = text;
      if (labels[index]) {
        cell.dataset.label = labels[index];
        cell.setAttribute("aria-label", `${labels[index]}：${text}`);
      }
      row.append(cell);
    });
    return row;
  }));
}

async function transitionTask(item, status) {
  setBusy(true);
  try {
    await api(`/admin/tasks/${encodeURIComponent(item.id)}/transition`, { method: "POST", body: { expected_version: item.version, status } });
    toast(status === "done" ? "待办已完成" : "待办已开始");
    await loadData({ quiet: true });
  } catch (error) { toast(error.message, "error"); }
  finally { setBusy(false); }
}

async function updateRoute(item, groupMode) {
  setBusy(true);
  try {
    await api("/admin/channel-routes", {
      method: "PATCH",
      body: {
        channel: item.channel,
        channel_account: item.channel_account,
        opaque_ref: item.opaque_ref,
        commands_enabled: groupMode !== "disabled",
        group_mode: groupMode,
        member_user_ids: item.member_user_ids || [],
      },
    });
    toast("会话权限已更新");
    await loadData({ quiet: true });
  } catch (error) { toast(error.message, "error"); renderConnections(); }
  finally { setBusy(false); }
}

function confirmAction(title, message, { confirmLabel = "确认", dangerous = true } = {}) {
  const dialog = qs("#confirm-dialog");
  qs("#confirm-title").textContent = title;
  qs("#confirm-message").textContent = message;
  const submit = qs("#confirm-submit");
  submit.textContent = confirmLabel;
  submit.className = `button ${dangerous ? "button-danger" : "button-primary"}`;
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }));
}

async function removeResource(kind, item) {
  const labels = { agenda: "日程", reminders: "提醒", tasks: "待办", notes: "备忘" };
  if (!await confirmAction(`删除${labels[kind]}`, `“${item.title || "无标题"}”将不再保留。`)) return;
  setBusy(true);
  try {
    const headers = kind === "reminders" ? { "X-Zhixu-Confirm": "true" } : {};
    await api(`/admin/${kind}/${encodeURIComponent(item.id)}`, { method: "DELETE", headers });
    toast(`${labels[kind]}已删除`);
    await loadData({ quiet: true });
  } catch (error) { toast(error.message, "error"); }
  finally { setBusy(false); }
}

const editorDefinitions = {
  agenda: {
    title: "新建日程", kicker: "SCHEDULE",
    fields: [
      ["title", "事项", "text", true], ["start_at", "开始时间", "datetime-local", true],
      ["end_at", "结束时间", "datetime-local", true], ["description", "说明", "textarea", false],
      ["recurrence_rule", "循环规则（可选）", "text", false], ["all_day", "全天日程", "checkbox", false],
    ],
  },
  reminder: {
    title: "新建提醒", kicker: "REMINDER",
    fields: [["title", "提醒内容", "text", true], ["fire_at", "提醒时间", "datetime-local", true], ["target_ref", "发送到", "identity", true]],
  },
  task: {
    title: "新建待办", kicker: "TASK",
    fields: [["title", "待办事项", "text", true], ["due_at", "截止时间（可选）", "datetime-local", false], ["priority", "优先级", "priority", false], ["description", "说明", "textarea", false]],
  },
  note: {
    title: "新建备忘", kicker: "NOTE",
    fields: [["title", "标题", "text", false], ["body", "内容", "textarea", true], ["tags", "标签（逗号分隔）", "text", false]],
  },
  "important-day": {
    title: "添加重要日子", kicker: "IMPORTANT DAY",
    fields: [
      ["title", "名称", "text", true], ["kind", "类型", "important-kind", true],
      ["calendar", "历法", "calendar-system", true], ["event_year", "年份（生日不知道可留空）", "number", false],
      ["event_month", "月份", "number", true], ["event_day", "日期", "number", true],
      ["advance_days", "提前通知（天，逗号分隔）", "text", false], ["lunar_leap", "农历闰月", "checkbox", false],
    ],
  },
};

function inputField([name, label, type, required]) {
  const wrapper = node("label", type === "checkbox" ? "check-field" : "");
  const caption = node("span", "", label);
  let input;
  if (type === "textarea") input = node("textarea");
  else if (type === "identity") {
    input = node("select");
    state.identities.forEach((item) => { const option = node("option", "", `${item.channel.toUpperCase()} · ${item.channel_account}`); option.value = item.opaque_ref; input.append(option); });
  } else if (type === "priority") {
    input = node("select");
    [[0, "普通"], [1, "较低"], [2, "重要"], [3, "紧急"], [4, "最高"]].forEach(([value, text]) => { const option = node("option", "", text); option.value = value; input.append(option); });
  } else if (type === "important-kind") {
    input = node("select");
    [["birthday", "生日"], ["anniversary", "纪念日"]].forEach(([value, text]) => { const option = node("option", "", text); option.value = value; input.append(option); });
  } else if (type === "calendar-system") {
    input = node("select");
    [["solar", "公历"], ["lunar", "农历"]].forEach(([value, text]) => { const option = node("option", "", text); option.value = value; input.append(option); });
  } else { input = node("input"); input.type = type; }
  input.name = name;
  input.required = required;
  if (name === "event_year") { input.min = "1"; input.max = "9999"; input.placeholder = "不知道可留空"; }
  if (name === "event_month") { input.min = "1"; input.max = "12"; }
  if (name === "event_day") { input.min = "1"; input.max = "31"; }
  if (name === "advance_days") input.placeholder = "例如：7, 3, 1";
  if (type === "checkbox") wrapper.append(input, caption); else wrapper.append(caption, input);
  return wrapper;
}

function openEditor(kind, item = null) {
  const definition = editorDefinitions[kind];
  if (!definition) return;
  state.editorItem = item;
  qs("#editor-kicker").textContent = definition.kicker;
  qs("#editor-title").textContent = item ? definition.title.replace("新建", "编辑") : definition.title;
  qs("#editor-form").dataset.kind = kind;
  qs("#editor-fields").replaceChildren(...definition.fields.map(inputField));
  if (kind === "agenda") {
    const start = qs('[name="start_at"]'); const end = qs('[name="end_at"]');
    const base = new Date(Date.now() + 3600000); base.setMinutes(Math.ceil(base.getMinutes() / 15) * 15, 0, 0);
    start.value = toLocalInput(base); end.value = toLocalInput(new Date(base.getTime() + 3600000));
  }
  if (kind === "reminder") qs('[name="fire_at"]').value = toLocalInput(new Date(Date.now() + 3600000));
  if (kind === "important-day") qs('[name="advance_days"]').value = "7, 3, 1";
  if (item) fillEditor(kind, item);
  qs("#editor-dialog").showModal();
}

function fillEditor(kind, item) {
  const values = {
    title: item.title || "",
    description: item.description || "",
    priority: item.priority ?? 0,
    body: item.body || "",
    tags: (item.tags || []).join(", "),
    recurrence_rule: item.recurrence_rule || "",
    all_day: Boolean(item.all_day),
    start_at: item.start_at ? toLocalInput(new Date(item.start_at)) : "",
    end_at: item.end_at ? toLocalInput(new Date(item.end_at)) : "",
    due_at: item.due_at ? toLocalInput(new Date(item.due_at)) : "",
  };
  Object.entries(values).forEach(([name, value]) => {
    const input = qs(`[name="${name}"]`);
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = String(value);
  });
  if (kind === "note") qs('[name="body"]').focus();
}

function toLocalInput(date) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

function isoOrNull(value) { return value ? new Date(value).toISOString() : null; }

async function submitEditor(event) {
  event.preventDefault();
  const submitter = event.submitter;
  if (!submitter || submitter.value !== "submit") return qs("#editor-dialog").close();
  const form = event.currentTarget;
  const kind = form.dataset.kind;
  const values = Object.fromEntries(new FormData(form));
  const editing = state.editorItem;
  let endpoint; let body;
  if (kind === "agenda") {
    endpoint = editing ? `/admin/agenda/${encodeURIComponent(editing.id)}` : "/admin/agenda";
    body = { title: values.title, start_at: isoOrNull(values.start_at), end_at: isoOrNull(values.end_at), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai", description: values.description || "", all_day: values.all_day === "on", recurrence_rule: values.recurrence_rule || null };
    if (editing) body.expected_version = editing.version;
  } else if (kind === "reminder") {
    if (!values.target_ref) return toast("请先绑定一个消息身份", "error");
    endpoint = "/admin/reminders"; body = { title: values.title, fire_at: isoOrNull(values.fire_at), target_ref: values.target_ref };
  } else if (kind === "task") {
    endpoint = editing ? `/admin/tasks/${encodeURIComponent(editing.id)}` : "/admin/tasks";
    body = { title: values.title, description: values.description || "", priority: Number(values.priority || 0), due_at: isoOrNull(values.due_at) };
    if (editing) body.expected_version = editing.version;
  } else if (kind === "important-day") {
    const year = values.event_year ? Number(values.event_year) : 1;
    const month = Number(values.event_month);
    const day = Number(values.event_day);
    if (values.kind === "anniversary" && !values.event_year) return toast("纪念日需要填写开始年份", "error");
    if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) return toast("请填写有效日期", "error");
    const padded = (value, width = 2) => String(value).padStart(width, "0");
    const leads = String(values.advance_days || "").split(/[,，]/).map((item) => Number(item.trim())).filter((item) => Number.isInteger(item));
    endpoint = state.importantDaysEndpoint || "/admin/important-days";
    body = {
      title: values.title,
      anchor_date: values.calendar === "lunar" ? `${padded(year, 4)}-01-01` : `${padded(year, 4)}-${padded(month)}-${padded(day)}`,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      kind: values.kind,
      calendar: values.calendar,
      lunar_leap: values.lunar_leap === "on",
      private: true,
    };
    if (values.calendar === "lunar") { body.lunar_month = month; body.lunar_day = day; }
    if (String(values.advance_days || "").trim()) body.advance_days = leads;
  } else {
    endpoint = editing ? `/admin/notes/${encodeURIComponent(editing.id)}` : "/admin/notes";
    body = { title: values.title || "", body: values.body, tags: String(values.tags || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean), attachments: editing?.attachments || [] };
    if (editing) body.expected_version = editing.version;
  }
  setBusy(true);
  try {
    try {
      await api(endpoint, { method: editing ? "PUT" : "POST", body });
    } catch (error) {
      const duplicateConfirmation = !editing
        && kind === "important-day"
        && error.status === 428
        && error.payload?.error?.code === "confirmation_required";
      if (!duplicateConfirmation) throw error;
      setBusy(false);
      const confirmed = await confirmAction(
        "可能重复的重要日子",
        `“${body.title}”的同类型、同日期记录已经存在。是否仍然再创建一条？`,
        { confirmLabel: "仍然创建", dangerous: true },
      );
      if (!confirmed) return;
      setBusy(true);
      await api(endpoint, {
        method: "POST",
        headers: { "X-Zhixu-Confirm": "true" },
        body,
      });
    }
    qs("#editor-dialog").close();
    state.editorItem = null;
    toast(editing ? "修改已保存" : "已保存");
    await loadData({ quiet: true });
  } catch (error) { toast(error.message, "error"); }
  finally { setBusy(false); }
}

function bytesFromBase64url(value) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

function base64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = ""; bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodePublicKey(value) {
  const result = { ...value, challenge: bytesFromBase64url(value.challenge) };
  if (value.user?.id) result.user = { ...value.user, id: bytesFromBase64url(value.user.id) };
  if (value.excludeCredentials) result.excludeCredentials = value.excludeCredentials.map((item) => ({ ...item, id: bytesFromBase64url(item.id) }));
  if (value.allowCredentials) result.allowCredentials = value.allowCredentials.map((item) => ({ ...item, id: bytesFromBase64url(item.id) }));
  return result;
}

function encodeCredential(credential) {
  const response = credential.response;
  const value = { id: credential.id, rawId: base64url(credential.rawId), type: credential.type, response: {} };
  ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"].forEach((key) => {
    if (response[key]) value.response[key] = base64url(response[key]);
  });
  if (response.getTransports) value.response.transports = response.getTransports();
  return value;
}

async function passkey(kind) {
  if (!window.PublicKeyCredential) return toast("当前浏览器不支持 Passkey", "error");
  setBusy(true);
  try {
    const registration = kind === "registration";
    const options = await api(`/admin/passkeys/${kind}/options`, { method: "POST", body: {} });
    const publicKey = decodePublicKey(options.publicKey);
    const credential = registration ? await navigator.credentials.create({ publicKey }) : await navigator.credentials.get({ publicKey });
    const result = await api(`/admin/passkeys/${kind}/verify`, { method: "POST", body: { credential: encodeCredential(credential) } });
    if (result.access_token) {
      state.token = result.access_token; sessionStorage.setItem("zhixu.session", state.token);
    }
    toast(registration ? "此设备已登记" : "安全验证已完成");
  } catch (error) { toast(error.message || "Passkey 操作失败", "error"); }
  finally { setBusy(false); }
}

async function login(event) {
  event.preventDefault();
  const user = qs("#login-user").value.trim();
  const password = qs("#login-password").value;
  qs("#login-error").textContent = "";
  setBusy(true);
  try {
    const result = await api("/admin/session", { method: "POST", body: { user_id: user, password } });
    state.token = result.access_token;
    sessionStorage.setItem("zhixu.session", state.token);
    qs("#login-password").value = "";
    showApp();
    await loadData({ quiet: true });
  } catch (error) { qs("#login-error").textContent = error.message; }
  finally { setBusy(false); }
}

async function logout() {
  try { if (state.token) await api("/admin/session", { method: "DELETE" }); } catch (_) { /* local logout still proceeds */ }
  state.token = ""; sessionStorage.removeItem("zhixu.session"); showLogin();
}

function bindEvents() {
  qs("#login-form").addEventListener("submit", login);
  qs("#logout-button").addEventListener("click", logout);
  qs("#editor-form").addEventListener("submit", submitEditor);
  qsa("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  qsa("[data-view-jump]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.viewJump)));
  qsa("[data-nav]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); setView(link.dataset.nav); }));
  qsa("[data-create]").forEach((button) => button.addEventListener("click", () => openEditor(button.dataset.create)));
  qsa("[data-agenda-filter]").forEach((button) => button.addEventListener("click", () => { state.agendaFilter = button.dataset.agendaFilter; qsa("[data-agenda-filter]").forEach((item) => item.classList.toggle("active", item === button)); renderAgenda(); }));
  qsa("[data-task-filter]").forEach((button) => button.addEventListener("click", () => { state.taskFilter = button.dataset.taskFilter; qsa("[data-task-filter]").forEach((item) => item.classList.toggle("active", item === button)); renderTasks(); }));
  qs("#global-search").addEventListener("input", (event) => { state.search = event.target.value.trim().toLocaleLowerCase(); renderCurrentView(); });
  qs("#workspace-scope").addEventListener("change", (event) => { state.workspaceFilter = event.target.value; renderAll(); });
  qs("#theme-button").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme; localStorage.setItem("zhixu.theme", theme); });
  qs("#mobile-menu-button").addEventListener("click", () => { const sidebar = qs(".sidebar"); const open = sidebar.classList.toggle("open"); qs("#mobile-menu-button").setAttribute("aria-expanded", String(open)); });
  qs("#passkey-register").addEventListener("click", () => passkey("registration"));
  qs("#passkey-verify").addEventListener("click", () => passkey("authentication"));
  window.addEventListener("hashchange", () => setView(location.hash.replace("#", "") || "dashboard", false));
}

async function bootstrap() {
  document.documentElement.dataset.theme = localStorage.getItem("zhixu.theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  bindEvents();
  if (!state.token) return showLogin();
  showApp();
  await loadData();
}

bootstrap();
