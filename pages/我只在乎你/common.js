"use strict";
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const timeText = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false }) : "尚未安排";
const labels = { active: "进行中", paused: "已暂停", completed: "已完成", cancelled: "已取消", chat: "聊天", create: "创作", tools: "工具行动", silent: "保持沉默", dispatched: "消息已交给平台", dispatch_attempt: "尝试发送", tool_result: "工具返回" };
Object.assign(labels, { heartbeat: "周期心跳", continuation: "短时续行", event: "事件触发", running: "执行中", deferred: "已暂缓", blocked: "条件受阻", failed: "执行失败", tool_notice: "工具状态提示", condition_check: "条件检查", service_check: "服务检查", work_saved: "作品保存回执" });
