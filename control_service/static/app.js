// AD9910 控制台前端
// 状态模型：服务端 applied state 快照（host_applied，非硬件回读）。
// 交互：即时提交。方向键（↑/↓）按插入光标左侧的数字位增减并自动提交
//       （节流合并，上限约 10Hz）；手动输入数字后按回车提交。
// 数值显示最多 6 位有效数字；内部与提交保持完整浮点精度。

"use strict";

const PROFILE_COUNT = 8;
const PROFILE_KEYS = ["freq", "phase", "amp"];
const THROTTLE_MS = 100;   // 方向键提交节流窗口（对应约 10Hz 上限）

let state = null;
let currentDevice = null;

// 即时提交状态：key `${deviceId}:${idx}:${key}` -> {timer, pending, inFlight}
// 同一字段同一时刻至多一个 in-flight 请求，期间的新值合并后在下一次提交。
const submitState = new Map();

// ---------------- 按位调节（LabVIEW 式） ----------------

// 把文本插入光标（caretIndex = selectionStart）左侧那一位数字按 direction
// （+1/-1）增减，带进位/借位，保持原小数位格式。返回新字符串；
// 左侧不是数字 / 值越界无变化 / 无法解析时返回 null。
// min/max 为数值边界（clamp）；越界时按键无效（返回 null）。
function bumpAtCaret(text, caretIndex, direction, min = null, max = null) {
  if (typeof text !== "string" || text.length === 0) return null;
  if (typeof caretIndex !== "number" || caretIndex <= 0 || caretIndex > text.length) return null;
  const idx = caretIndex - 1;               // 竖线左侧字符
  if (!/[0-9]/.test(text[idx])) return null;
  if (/[eE]/.test(text)) return null;       // 不支持科学计数法
  const dotPos = text.indexOf(".");
  const decimals = dotPos >= 0 ? text.length - dotPos - 1 : 0;
  let weight;                               // 该数字位的位权
  if (dotPos < 0) {
    weight = Math.pow(10, text.length - 1 - idx);
  } else if (idx < dotPos) {
    weight = Math.pow(10, dotPos - 1 - idx);
  } else {
    weight = Math.pow(10, -(idx - dotPos));
  }
  const value = parseFloat(text);
  if (!Number.isFinite(value)) return null;
  let next = value + direction * weight;
  const scale = Math.pow(10, decimals);
  next = Math.round(next * scale) / scale;  // 消除浮点噪声
  if (min !== null && next < min) next = min;
  if (max !== null && next > max) next = max;
  if (Math.abs(next - value) < 1e-12) return null;  // 边界上无变化
  return next.toFixed(decimals);
}

const sixDigit = new Intl.NumberFormat("en-US", {
  useGrouping: false,
  maximumSignificantDigits: 6,
});

// ---------------- 数值显示 ----------------

function fmt(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "";
  return sixDigit.format(Number(value));
}

// ---------------- 暂存参数 -> 提交 payload ----------------

function deviceFieldsPayload(fields) {
  const p = {};
  if (fields.name !== undefined) p.name = fields.name;
  if (fields.active_profile !== undefined) p.active_profile = fields.active_profile;
  if (fields.enable_profile_asf !== undefined) p.enable_profile_asf = fields.enable_profile_asf;
  if (fields.enable_sine_out !== undefined) p.enable_sine_out = fields.enable_sine_out;
  return p;
}

function profilePayload(fields) {
  const profs = new Array(PROFILE_COUNT).fill(null);
  let any = false;
  for (let i = 0; i < PROFILE_COUNT; i++) {
    if (fields.profiles && fields.profiles[i]) { profs[i] = fields.profiles[i]; any = true; }
  }
  return any ? { profiles: profs } : null;
}

// 兼容 node 测试导出的全量转换（合并设备字段与 profiles）
function draftToParameters(fields) {
  const p = deviceFieldsPayload(fields);
  const profs = profilePayload(fields);
  if (profs) p.profiles = profs.profiles;
  return p;
}

// ---------------- API ----------------

let ui = null;

function showMessage(text, error = false) {
  if (!ui) return;
  ui.message.hidden = !text;
  ui.message.textContent = text || "";
  ui.message.classList.toggle("error", error);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || response.statusText);
    error.conflict = body.code === "conflict";
    error.exists = body.code === "exists";
    error.code = body.code;
    throw error;
  }
  return body;
}

// ---------------- 状态接收 ----------------

function acceptSnapshot(next) {
  if (state && next.revision < state.revision) return;
  state = next;
  render();
}

// ---------------- 渲染 ----------------

function render() {
  if (!state) return;
  ui.status.textContent = state.status;
  ui.statusDot.className = `status-dot ${state.status}`;
  ui.revision.textContent = state.revision;
  renderService(ui.webEndpoint, state.services?.web);
  renderService(ui.rpycEndpoint, state.services?.rpyc);
  renderClock(state.services);
  ui.activeConfig.textContent = state.active_config || "—";
  ui.saveConfig.disabled = state.status !== "ready";

  const ids = Object.keys(state.devices || {}).sort();
  if (!currentDevice || !ids.includes(currentDevice)) currentDevice = ids[0];
  ui.deviceSelect.innerHTML = ids.map(id => {
    const dev = state.devices[id];
    const label = (dev && dev.name ? dev.name : "") + ` [${id}]`;
    return `<option value="${id}" ${id === currentDevice ? "selected" : ""}>${label}</option>`;
  }).join("");

  renderDeviceFields();
  renderProfiles();
}

function renderService(el, svc) {
  if (!svc) { el.textContent = "—"; return; }
  const wildcard = svc.host === "0.0.0.0" || svc.host === "::";
  const connectHost = wildcard ? (location.hostname || "127.0.0.1") : svc.host;
  el.textContent = `${connectHost}:${svc.port}`;
}

// 时钟：外部参考 REF_CLK + ÷2 分频器 -> 推导出的 SYSCLK（FTW 按它编码）
function renderClock(services) {
  if (!ui.sysclk) return;
  const ref = fmt(services?.refclk_mhz);
  const sys = fmt(services?.sysclk_mhz);
  if (ui.refclk) ui.refclk.textContent = ref || "—";
  ui.sysclk.textContent = sys || "—";
  const missing = !sys || !ref;
  // 显示 "—" 说明后端没上报（旧进程）或页面用了缓存的旧脚本
  const title = missing ? "后端未上报时钟配置：请重启服务，并用 Ctrl+F5 强制刷新页面"
    : `外部参考 ${ref} MHz，÷2 分频器${services?.div2 === false ? "旁路" : "使能"} → SYSCLK ${sys} MHz`;
  ui.sysclk.title = title;
  if (ui.refclk) ui.refclk.title = title;
}

function valueOf(deviceId, path) {
  let w = state && state.devices[deviceId];
  for (const k of path) w = w == null ? undefined : w[k];
  return w;
}

function renderDeviceFields() {
  if (!state || !currentDevice) return;
  const ready = state.status === "ready";
  ui.devName.value = valueOf(currentDevice, ["name"]) || "";
  ui.devProfile.value = String(valueOf(currentDevice, ["active_profile"]) ?? 0);
  ui.devAsf.checked = !!valueOf(currentDevice, ["enable_profile_asf"]);
  ui.devSine.checked = !!valueOf(currentDevice, ["enable_sine_out"]);
  // 服务未就绪时禁止编辑（避免排队期间误提交）
  ui.devName.disabled = !ready;
  ui.devProfile.disabled = !ready;
  ui.devAsf.disabled = !ready;
  ui.devSine.disabled = !ready;
}

function renderProfiles() {
  const dev = state?.devices?.[currentDevice];
  if (!dev) { ui.profileRows.innerHTML = ""; return; }
  const query = ui.search.value.trim().toLowerCase();
  const ready = state.status === "ready";

  // 记录当前聚焦的 profile 输入框（重建 DOM 会销毁原元素，需恢复焦点与光标）
  const active = document.activeElement;
  let savedFocus = null;
  if (active && active.matches && active.matches("input[data-key]")) {
    const row = active.closest("tr");
    if (row) {
      savedFocus = {
        idx: Number(row.dataset.profile),
        key: active.dataset.key,
        caret: active.selectionStart,
      };
    }
  }

  const rows = [];
  for (let i = 0; i < PROFILE_COUNT; i++) {
    const values = {};
    for (const key of PROFILE_KEYS) values[key] = valueOf(currentDevice, ["profiles", i, key]);
    const cells = [fmt(values.freq), fmt(values.phase), fmt(values.amp)];
    if (query && !cells.join(" ").toLowerCase().includes(query)) continue;

    const dis = ready ? "" : " disabled";
    rows.push(`<tr data-profile="${i}">
      <td class="profile-id"><strong>${i}</strong><small>0x${(0x0E + i).toString(16).toUpperCase()}</small></td>
      <td><input data-key="freq" type="text" inputmode="decimal" autocomplete="off" value="${cells[0]}" placeholder="—"${dis}></td>
      <td><input data-key="phase" type="text" inputmode="decimal" autocomplete="off" value="${cells[1]}" placeholder="—"${dis}></td>
      <td><input data-key="amp" type="text" inputmode="decimal" autocomplete="off" value="${cells[2]}" placeholder="—"${dis}></td>
      </tr>`);
  }
  ui.profileRows.innerHTML = rows.join("");

  ui.profileRows.querySelectorAll("input").forEach(input => {
    input.addEventListener("keydown", onProfileKeydown);
  });

  // 恢复焦点与光标位置（若对应输入框仍存在）
  if (savedFocus) {
    const target = ui.profileRows.querySelector(
      `tr[data-profile="${savedFocus.idx}"] input[data-key="${savedFocus.key}"]`);
    if (target) {
      target.focus();
      const caret = savedFocus.caret;
      if (caret != null && caret <= target.value.length) {
        target.setSelectionRange(caret, caret);
      }
    }
  }
}

// ---------------- 即时提交 ----------------

// 各字段数值边界（方向键 clamp 用）
function boundsFor(key) {
  if (key === "freq") return { min: 0, max: null };
  if (key === "amp") return { min: 0, max: 1 };
  return { min: null, max: null };  // phase
}

function profilesPayload(idx, fields) {
  const profs = new Array(PROFILE_COUNT).fill(null);
  profs[idx] = fields;
  return { profiles: profs };
}

// 提交一个字段（payload 为 parameters 对象）；409 冲突时先刷新本地状态再抛错
async function submitFieldValue(deviceId, payload) {
  const body = { baseRevision: state.revision, parameters: payload };
  try {
    return await api(`/api/devices/${encodeURIComponent(deviceId)}/parameters:batch`,
      { method: "POST", body: JSON.stringify(body) });
  } catch (error) {
    if (error.conflict) {
      try { acceptSnapshot(await api("/api/state")); } catch (e) { /* 刷新失败忽略 */ }
    }
    throw error;
  }
}

// profile 输入框 keydown：↑/↓ 按位调节并自动提交；Enter 提交手动输入值
function onProfileKeydown(event) {
  const input = event.target;
  const row = input.closest("tr");
  const idx = Number(row.dataset.profile);
  const key = input.dataset.key;

  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault();
    const dir = event.key === "ArrowUp" ? 1 : -1;
    const b = boundsFor(key);
    // 必须在赋值前保存光标位置：input.value 赋值会重置 selectionStart
    const oldCaret = input.selectionStart;
    const next = bumpAtCaret(input.value, oldCaret, dir, b.min, b.max);
    if (next === null) return;  // 无可调数字位或已达边界
    const offset = next.length - input.value.length;
    input.value = next;
    const pos = oldCaret + offset;
    input.setSelectionRange(pos, pos);
    scheduleProfileSubmit(currentDevice, idx, key, next);
  } else if (event.key === "Enter") {
    event.preventDefault();
    const raw = input.value.trim();
    if (raw === "") { showMessage(`Profile ${idx} 的 ${key} 为空，未提交。`, true); return; }
    const value = Number(raw);
    if (!Number.isFinite(value)) { showMessage(`Profile ${idx} 的 ${key} 必须是有限数字。`, true); return; }
    submitImmediate(currentDevice, idx, key, value);
  }
}

// 方向键调节的节流合并：同一字段同一时刻至多一个 in-flight，
// 窗口（THROTTLE_MS）内多次按键合并为最后一次值，上限约 10Hz。
function scheduleProfileSubmit(deviceId, idx, key, value) {
  const k = `${deviceId}:${idx}:${key}`;
  let st = submitState.get(k);
  if (!st) { st = { timer: null, pending: null, inFlight: false }; submitState.set(k, st); }
  st.pending = value;
  if (st.inFlight) return;   // 等当前请求结束后提交最新值
  if (st.timer) return;      // 已有合并窗口，仅更新 pending
  st.timer = setTimeout(() => { st.timer = null; flushProfileSubmit(k); }, THROTTLE_MS);
}

async function flushProfileSubmit(k) {
  const st = submitState.get(k);
  if (!st || st.inFlight || st.pending === null) return;
  const value = st.pending;
  st.pending = null;
  st.inFlight = true;
  try {
    const [deviceId, idx, key] = k.split(":");
    await submitFieldValue(deviceId, profilesPayload(Number(idx), { [key]: Number(value) }));
  } catch (error) {
    showMessage(`Profile ${k} 提交失败：${error.message}`, true);
  } finally {
    st.inFlight = false;
    if (st.pending !== null) flushProfileSubmit(k);  // 继续提交窗口内新值
  }
}

// Enter / 设备级字段：立即提交（不走节流）
async function submitImmediate(deviceId, idx, key, value) {
  const payload = idx === null ? { [key]: value } : profilesPayload(idx, { [key]: value });
  try {
    await submitFieldValue(deviceId, payload);
  } catch (error) {
    showMessage(`${key} 提交失败：${error.message}`, true);
  }
}

// ---------------- 清除输出 ----------------

async function clearOutputs() {
  if (!currentDevice) return;
  if (!confirm(`确认清除设备 ${currentDevice} 的输出（所有 profile 幅度归零）？`)) return;
  try {
    await api(`/api/devices/${encodeURIComponent(currentDevice)}/outputs:clear`, { method: "POST", body: "{}" });
    showMessage("输出已清除。");
    acceptSnapshot(await api("/api/state"));
  } catch (error) { showMessage(error.message, true); }
}

// ---------------- 配置 ----------------

async function loadConfigs(selected = null) {
  const data = await api("/api/configs");
  ui.configSelect.replaceChildren(...data.configs.map(name => new Option(name, name)));
  if (selected) ui.configSelect.value = selected;
  ui.loadConfig.disabled = !data.configs.length;
}

async function selectConfig() {
  const name = ui.configSelect.value;
  if (!name || !confirm(`加载 ${name} 将把全部设备状态恢复为配置中的值，继续？`)) return;
  try {
    await api("/api/config/select", { method: "POST", body: JSON.stringify({ name }) });
    showMessage(`已加载 ${name}。`);
    acceptSnapshot(await api("/api/state"));
    await loadConfigs(name);
  } catch (error) { showMessage(error.message, true); }
}

async function saveCurrentConfig(overwrite = false) {
  let name = ui.configFile.value.trim();
  if (!name) { showMessage("请输入配置名。", true); return; }
  if (name.toLowerCase().endsWith(".json")) name = name.slice(0, -5);
  try {
    const result = await api("/api/configs", { method: "POST", body: JSON.stringify({ name, overwrite }) });
    ui.configFile.value = result.name;
    showMessage(`当前已应用状态已保存到 ${result.name}。`);
    await loadConfigs(result.name);
  } catch (error) {
    if (error.exists && confirm(`${name} 已存在，确认覆盖吗？`)) { await saveCurrentConfig(true); return; }
    showMessage(error.message, true);
  }
}

// ---------------- WebSocket ----------------

function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (e) { return; }
    acceptSnapshot(message);
  };
  ws.onclose = () => setTimeout(connectWS, 1000);
}

// ---------------- 事件绑定 ----------------

function bindEvents() {
  ui.deviceSelect.addEventListener("change", () => { currentDevice = ui.deviceSelect.value; render(); });
  ui.devName.addEventListener("change", e => submitImmediate(currentDevice, null, "name", e.target.value));
  ui.devProfile.addEventListener("change", e => submitImmediate(currentDevice, null, "active_profile", Number(e.target.value)));
  ui.devAsf.addEventListener("change", e => submitImmediate(currentDevice, null, "enable_profile_asf", e.target.checked));
  ui.devSine.addEventListener("change", e => submitImmediate(currentDevice, null, "enable_sine_out", e.target.checked));
  ui.search.addEventListener("input", renderProfiles);
  ui.clearOutputs.addEventListener("click", clearOutputs);
  ui.loadConfig.addEventListener("click", selectConfig);
  ui.saveConfig.addEventListener("click", () => saveCurrentConfig(false));
  ui.configFile.addEventListener("keydown", e => { if (e.key === "Enter") saveCurrentConfig(false); });
}

function init() {
  ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(el => [el.id, el]));
  bindEvents();
  Promise.all([api("/api/state"), loadConfigs()])
    .then(([snapshot]) => acceptSnapshot(snapshot))
    .catch(e => showMessage(e.message, true));
  connectWS();
}

// 双环境支持：node 下仅导出纯函数供测试；浏览器下启动 UI。
if (typeof module !== "undefined" && module.exports) {
  module.exports = { fmt, draftToParameters, bumpAtCaret, PROFILE_COUNT };
}
if (typeof document !== "undefined") {
  init();
}
