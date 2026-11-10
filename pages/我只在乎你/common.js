"use strict";
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const timeText = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false }) : "尚未安排";
const timeShort = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "尚未安排";
const soonText = value => {
  if (!value) return "";
  const diff = value - Date.now() / 1000;
  if (diff <= 0) return "已到点";
  const minutes = Math.round(diff / 60);
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return minutes + " 分钟后";
  const hours = Math.round(diff / 360) / 10;
  if (hours < 24) return hours + " 小时后";
  return Math.round(diff / 86400) + " 天后";
};
const clipText = (value, limit) => {
  const text = String(value ?? "");
  return text.length > limit ? text.slice(0, limit) + "…" : text;
};
const agoText = value => {
  if (!value) return "";
  const minutes = Math.round((Date.now() / 1000 - value) / 60);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return minutes + " 分钟前";
  const hours = Math.round(minutes / 6) / 10;
  if (hours < 24) return hours + " 小时前";
  return Math.round(minutes / 1440) + " 天前";
};
const labels = { active: "进行中", paused: "已暂停", completed: "已完成", cancelled: "已取消", chat: "聊天", create: "创作", tools: "工具行动", silent: "保持沉默", dispatched: "消息已交给平台", dispatch_attempt: "尝试发送", tool_result: "工具返回" };
Object.assign(labels, { heartbeat: "周期心跳", continuation: "短时续行", event: "事件触发", speak: "自己约定的开口", running: "执行中", deferred: "已暂缓", blocked: "条件受阻", failed: "执行失败", tool_notice: "工具状态提示", condition_check: "条件检查", service_check: "服务检查", work_saved: "作品保存回执", speak_scheduled: "约定开口时间", speak_due: "到点开口", speak_cancelled: "取消开口约定", speak_dropped: "开口约定已丢弃", speak_ignored: "开口约定已忽略", wish_kept: "记下想说的话", wish_spoken: "这句话说出来了", wish_cleared: "放下想说的话", wish_faded: "念头淡了" });
Object.assign(labels, { morning_report: "早上讲一遍昨晚", quiet_draft_sent: "安静时段的草稿发出去了", quiet_draft_dropped: "安静时段的草稿没发", quiet_draft_expired: "安静时段的草稿过期了" });
