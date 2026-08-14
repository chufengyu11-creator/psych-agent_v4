const state = {
  busy: false,
  memoryEnabled: false,
  sessionClosed: false,
  recording: false,
  sessions: [],
  ws: null,
  mediaStream: null,
  audioContext: null,
  audioSource: null,
  audioProcessor: null,
  silentGain: null,
  videoTimer: null,
  canvas: document.createElement("canvas"),
};

const els = {
  runtimeStatus: document.querySelector("#runtimeStatus"),
  userId: document.querySelector("#userId"),
  sessionId: document.querySelector("#sessionId"),
  memorySwitch: document.querySelector("#memorySwitch"),
  newSessionButton: document.querySelector("#newSessionButton"),
  closeSessionButton: document.querySelector("#closeSessionButton"),
  refreshSessionsButton: document.querySelector("#refreshSessionsButton"),
  refreshMemoriesButton: document.querySelector("#refreshMemoriesButton"),
  sessionList: document.querySelector("#sessionList"),
  memoryList: document.querySelector("#memoryList"),
  sessionState: document.querySelector("#sessionState"),
  stateVersion: document.querySelector("#stateVersion"),
  memoryWrites: document.querySelector("#memoryWrites"),
  messages: document.querySelector("#messages"),
  recordButton: document.querySelector("#recordButton"),
  cameraPreview: document.querySelector("#cameraPreview"),
  voiceStatus: document.querySelector("#voiceStatus"),
  transcriptText: document.querySelector("#transcriptText"),
  toast: document.querySelector("#toast"),
};

function userId() {
  return els.userId.value.trim();
}

function sessionId() {
  return els.sessionId.value.trim();
}

function setBusy(value) {
  state.busy = value;
  els.closeSessionButton.disabled = value || state.sessionClosed;
  els.memorySwitch.disabled = value || els.memorySwitch.dataset.available === "false";
  updateRecordAvailability();
}

function updateRecordAvailability() {
  els.recordButton.disabled = (state.busy && !state.recording) || state.sessionClosed;
  if (state.sessionClosed) {
    els.voiceStatus.textContent = "该会话已归档，可查看但不能继续说话";
  }
}

function setMemorySwitch(enabled) {
  state.memoryEnabled = enabled;
  els.memorySwitch.setAttribute("aria-pressed", String(enabled));
}

function setToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  window.clearTimeout(setToast.timer);
  setToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("show");
  }, 3200);
}

function clearMessages() {
  els.messages.innerHTML = "";
}

function appendMessage(role, text) {
  const item = document.createElement("article");
  item.className = `message ${role}`;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  item.append(paragraph);
  els.messages.append(item);
  els.messages.scrollTop = els.messages.scrollHeight;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    const detail = payload?.detail || `HTTP ${response.status}`;
    throw new Error(Array.isArray(detail) ? detail[0]?.msg || "请求失败" : detail);
  }
  return payload;
}

async function checkHealth() {
  try {
    const payload = await request("/health/ready");
    els.runtimeStatus.textContent = `服务已就绪 · ${payload.runtime_mode}`;
  } catch (error) {
    els.runtimeStatus.textContent = "服务未就绪";
    setToast(error.message);
  }
}

async function loadMemorySetting() {
  const id = userId();
  if (!id) {
    return;
  }
  try {
    const payload = await request(`/users/${encodeURIComponent(id)}/memory`);
    els.memorySwitch.dataset.available = "true";
    setMemorySwitch(payload.memory_enabled);
  } catch {
    els.memorySwitch.dataset.available = "false";
    els.memorySwitch.disabled = true;
    setMemorySwitch(false);
  }
}

async function updateMemorySetting(enabled) {
  const id = userId();
  if (!id) {
    setToast("请先填写用户 ID");
    return;
  }
  setBusy(true);
  try {
    const payload = await request(`/users/${encodeURIComponent(id)}/memory`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    });
    setMemorySwitch(payload.memory_enabled);
    setToast(payload.memory_enabled ? "长期记忆已开启" : "长期记忆已关闭");
    await loadMemories();
  } catch (error) {
    setToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function loadSessions() {
  if (!userId()) {
    return;
  }
  try {
    const payload = await request(`/sessions?user_id=${encodeURIComponent(userId())}&limit=30`);
    state.sessions = payload.sessions || [];
    renderSessions();
    const current = state.sessions.find((item) => item.session_id === sessionId());
    if (current) {
      applySessionMeta(current);
    }
  } catch (error) {
    els.sessionList.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

function renderSessions() {
  els.sessionList.innerHTML = "";
  if (!state.sessions.length) {
    els.sessionList.innerHTML = '<p class="empty">还没有历史会话</p>';
    return;
  }
  for (const item of state.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    if (item.session_id === sessionId()) {
      button.classList.add("active");
    }
    button.innerHTML = `
      <span>${escapeHtml(item.session_id)}</span>
      <small>${sessionStatusLabel(item.status)} · ${formatDate(item.started_at)}</small>
    `;
    button.addEventListener("click", () => selectSession(item));
    els.sessionList.append(button);
  }
}

async function selectSession(item) {
  els.sessionId.value = item.session_id;
  localStorage.setItem("psych-ui-session-id", item.session_id);
  applySessionMeta(item);
  renderSessions();
  await loadCurrentMessages();
}

function applySessionMeta(item) {
  state.sessionClosed = item.status === "closed";
  els.sessionState.textContent = sessionStatusLabel(item.status);
  els.stateVersion.textContent = String(item.current_state_version ?? 0);
  updateRecordAvailability();
}

async function loadCurrentMessages() {
  if (!userId() || !sessionId()) {
    return;
  }
  try {
    const payload = await request(
      `/sessions/${encodeURIComponent(sessionId())}/messages?user_id=${encodeURIComponent(userId())}&limit=200`,
    );
    clearMessages();
    if (!payload.messages?.length) {
      appendMessage("assistant", "这个会话还没有消息。你可以直接开始说话。");
      return;
    }
    for (const message of payload.messages) {
      appendMessage(message.role, message.content);
    }
  } catch {
    clearMessages();
    appendMessage("assistant", "你好，我在这里。你可以直接说现在发生了什么。");
  }
}

async function loadMemories() {
  if (!userId()) {
    return;
  }
  try {
    const payload = await request(`/users/${encodeURIComponent(userId())}/memories?status=all&limit=100`);
    renderMemories(payload.memories || []);
  } catch (error) {
    els.memoryList.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

function renderMemories(memories) {
  els.memoryList.innerHTML = "";
  if (!memories.length) {
    els.memoryList.innerHTML = '<p class="empty">还没有长期记忆</p>';
    return;
  }
  for (const memory of memories) {
    const item = document.createElement("article");
    item.className = `memory-item ${memory.status}`;
    const actions = [];
    if (memory.status === "pending_confirmation") {
      actions.push(`<button data-action="confirm" data-id="${escapeHtml(memory.id)}" type="button">确认</button>`);
    }
    if (memory.status !== "deleted") {
      actions.push(`<button data-action="delete" data-id="${escapeHtml(memory.id)}" type="button">删除</button>`);
    }
    item.innerHTML = `
      <p>${escapeHtml(memory.content)}</p>
      <small>${memoryTypeLabel(memory.memory_type)} · ${memoryStatusLabel(memory.status)} · ${Math.round(memory.confidence * 100)}%</small>
      <div class="memory-actions">${actions.join("")}</div>
    `;
    els.memoryList.append(item);
  }
}

async function mutateMemory(action, memoryId) {
  setBusy(true);
  try {
    if (action === "confirm") {
      await request(`/users/${encodeURIComponent(userId())}/memories/${encodeURIComponent(memoryId)}/confirmation`, {
        method: "POST",
        body: JSON.stringify({ confirmed: true }),
      });
      setToast("记忆已确认");
    } else if (action === "delete") {
      await request(`/users/${encodeURIComponent(userId())}/memories/${encodeURIComponent(memoryId)}`, {
        method: "DELETE",
      });
      setToast("记忆已删除");
    }
    await loadMemories();
  } catch (error) {
    setToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function closeSession() {
  if (!userId() || !sessionId()) {
    setToast("请先填写用户 ID 和会话 ID");
    return;
  }
  setBusy(true);
  try {
    const payload = await request("/sessions/close", {
      method: "POST",
      body: JSON.stringify({
        user_id: userId(),
        session_id: sessionId(),
        reason: "web-ui",
      }),
    });
    state.sessionClosed = true;
    els.sessionState.textContent = "已归档";
    els.memoryWrites.textContent = String(payload.memory_write_count);
    appendMessage(
      "system",
      `会话已归档。候选记忆 ${payload.candidate_memory_count} 条，写入 ${payload.memory_write_count} 条。`,
    );
    await loadSessions();
    await loadMemories();
  } catch (error) {
    appendMessage("system", error.message);
  } finally {
    setBusy(false);
  }
}

function newSession() {
  const next = `session-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}`;
  els.sessionId.value = next;
  state.sessionClosed = false;
  els.sessionState.textContent = "未开始";
  els.stateVersion.textContent = "0";
  els.memoryWrites.textContent = "0";
  els.transcriptText.textContent = "";
  clearMessages();
  appendMessage("assistant", "新的会话已经准备好。");
  localStorage.setItem("psych-ui-session-id", next);
  renderSessions();
  updateRecordAvailability();
}

async function startRecording() {
  if (!userId() || !sessionId()) {
    setToast("请先填写用户 ID 和会话 ID");
    return;
  }
  if (state.sessionClosed) {
    setToast("这个会话已归档，请新建会话继续聊");
    return;
  }
  localStorage.setItem("psych-ui-user-id", userId());
  localStorage.setItem("psych-ui-session-id", sessionId());
  setBusy(true);
  els.transcriptText.textContent = "";
  els.voiceStatus.textContent = "正在请求麦克风和摄像头权限";

  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        facingMode: "user",
      },
    });
    els.cameraPreview.srcObject = state.mediaStream;
    await els.cameraPreview.play();

    state.ws = new WebSocket(multimodalWsUrl());
    state.ws.addEventListener("open", () => {
      state.ws.send(JSON.stringify({
        type: "start",
        user_id: userId(),
        session_id: sessionId(),
      }));
      startAudioStreaming();
      startVideoStreaming();
      state.recording = true;
      els.recordButton.textContent = "停止";
      els.voiceStatus.textContent = "正在听你说话";
      setBusy(false);
    });
    state.ws.addEventListener("message", handleWsMessage);
    state.ws.addEventListener("close", () => {
      cleanupRecording();
      setBusy(false);
    });
    state.ws.addEventListener("error", () => {
      setToast("语音连接出错");
      cleanupRecording();
      setBusy(false);
    });
  } catch (error) {
    cleanupRecording();
    setBusy(false);
    setToast(error.message);
    els.voiceStatus.textContent = "麦克风或摄像头不可用";
  }
}

function stopRecording() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "stop" }));
    els.voiceStatus.textContent = "正在处理";
    cleanupRecording();
    els.recordButton.disabled = true;
    setBusy(true);
  } else {
    cleanupRecording();
  }
}

function startAudioStreaming() {
  state.audioContext = new AudioContext();
  state.audioSource = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.audioProcessor = state.audioContext.createScriptProcessor(4096, 1, 1);
  state.silentGain = state.audioContext.createGain();
  state.silentGain.gain.value = 0;
  state.audioProcessor.onaudioprocess = (event) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.recording) {
      return;
    }
    const input = event.inputBuffer.getChannelData(0);
    const downsampled = downsample(input, state.audioContext.sampleRate, 16000);
    const pcm = floatToPcm16(downsampled);
    state.ws.send(JSON.stringify({
      type: "audio",
      sample_rate: 16000,
      pcm16_base64: bytesToBase64(new Uint8Array(pcm.buffer)),
      timestamp_ms: Date.now(),
    }));
  };
  state.audioSource.connect(state.audioProcessor);
  state.audioProcessor.connect(state.silentGain);
  state.silentGain.connect(state.audioContext.destination);
}

function startVideoStreaming() {
  const canvas = state.canvas;
  canvas.width = 320;
  canvas.height = 240;
  const context = canvas.getContext("2d");
  state.videoTimer = window.setInterval(() => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.recording) {
      return;
    }
    context.drawImage(els.cameraPreview, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL("image/jpeg", 0.68);
    const imageBase64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
    state.ws.send(JSON.stringify({
      type: "video_frame",
      format: "jpeg",
      image_base64: imageBase64,
      timestamp_ms: Date.now(),
    }));
  }, 250);
}

function handleWsMessage(event) {
  const payload = JSON.parse(event.data);
  if (payload.type === "listening") {
    els.voiceStatus.textContent = "正在听你说话";
  } else if (payload.type === "speech_start") {
    els.voiceStatus.textContent = "检测到语音";
  } else if (payload.type === "utterance_final") {
    els.transcriptText.textContent = payload.transcript;
    appendMessage("user", payload.transcript);
    els.voiceStatus.textContent = "正在生成回复";
  } else if (payload.type === "vision_summary") {
    const frames = payload.summary?.frames_received ?? 0;
    const valid = payload.summary?.valid_face_frames ?? 0;
    els.voiceStatus.textContent = `视觉统计 ${valid}/${frames} 帧`;
  } else if (payload.type === "assistant_response") {
    appendMessage("assistant", payload.response);
    state.sessionClosed = false;
    els.sessionState.textContent = "进行中";
    els.stateVersion.textContent = String(payload.state_version);
    els.voiceStatus.textContent = "回复完成";
    cleanupRecording();
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.close();
    }
    loadSessions();
    window.setTimeout(loadMemories, 1200);
  } else if (payload.type === "stopped") {
    els.voiceStatus.textContent = "已停止";
    cleanupRecording();
  } else if (payload.type === "error") {
    appendMessage("system", payload.message);
    els.voiceStatus.textContent = "出现错误";
    cleanupRecording();
  }
}

function cleanupRecording() {
  state.recording = false;
  window.clearInterval(state.videoTimer);
  state.videoTimer = null;
  if (state.audioProcessor) {
    state.audioProcessor.disconnect();
  }
  if (state.audioSource) {
    state.audioSource.disconnect();
  }
  if (state.silentGain) {
    state.silentGain.disconnect();
  }
  if (state.audioContext) {
    state.audioContext.close();
  }
  if (state.mediaStream) {
    for (const track of state.mediaStream.getTracks()) {
      track.stop();
    }
  }
  state.mediaStream = null;
  state.audioContext = null;
  state.audioSource = null;
  state.audioProcessor = null;
  state.silentGain = null;
  els.cameraPreview.srcObject = null;
  els.recordButton.textContent = "开始说话";
  updateRecordAvailability();
}

function multimodalWsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/multimodal/ws`;
}

function downsample(buffer, inputRate, outputRate) {
  if (outputRate === inputRate) {
    return buffer;
  }
  const ratio = inputRate / outputRate;
  const length = Math.floor(buffer.length / ratio);
  const result = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.floor((i + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let j = start; j < end && j < buffer.length; j += 1) {
      sum += buffer[j];
      count += 1;
    }
    result[i] = count ? sum / count : 0;
  }
  return result;
}

function floatToPcm16(buffer) {
  const output = new Int16Array(buffer.length);
  for (let i = 0; i < buffer.length; i += 1) {
    const value = Math.max(-1, Math.min(1, buffer[i]));
    output[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
  }
  return output;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function sessionStatusLabel(status) {
  if (status === "closed") {
    return "已归档";
  }
  if (status === "cancelled") {
    return "已取消";
  }
  return "进行中";
}

function memoryStatusLabel(status) {
  const labels = {
    active: "已启用",
    pending_confirmation: "待确认",
    superseded: "已替换",
    conflicted: "有冲突",
    expired: "已过期",
    deleted: "已删除",
  };
  return labels[status] || status;
}

function memoryTypeLabel(type) {
  const labels = {
    interaction_preference: "互动偏好",
    active_goal: "当前目标",
    unfinished_topic: "未完成话题",
    semantic_memory: "长期事实",
    episodic_memory: "事件记忆",
    strategy_outcome: "策略反馈",
  };
  return labels[type] || type;
}

function formatDate(value) {
  if (!value) {
    return "";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[char]);
}

function restoreLocalFields() {
  els.userId.value = localStorage.getItem("psych-ui-user-id") || els.userId.value;
  els.sessionId.value = localStorage.getItem("psych-ui-session-id") || els.sessionId.value;
}

function bindEvents() {
  els.recordButton.addEventListener("click", () => {
    if (state.recording) {
      stopRecording();
      return;
    }
    startRecording();
  });

  els.memorySwitch.addEventListener("click", () => {
    if (!state.busy) {
      updateMemorySetting(!state.memoryEnabled);
    }
  });

  els.closeSessionButton.addEventListener("click", () => {
    if (!state.busy && !state.sessionClosed) {
      closeSession();
    }
  });

  els.newSessionButton.addEventListener("click", newSession);
  els.refreshSessionsButton.addEventListener("click", loadSessions);
  els.refreshMemoriesButton.addEventListener("click", loadMemories);
  els.memoryList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || state.busy) {
      return;
    }
    mutateMemory(button.dataset.action, button.dataset.id);
  });
  els.userId.addEventListener("change", async () => {
    localStorage.setItem("psych-ui-user-id", userId());
    await loadMemorySetting();
    await loadSessions();
    await loadMemories();
  });
  els.sessionId.addEventListener("change", async () => {
    localStorage.setItem("psych-ui-session-id", sessionId());
    const current = state.sessions.find((item) => item.session_id === sessionId());
    if (current) {
      applySessionMeta(current);
    } else {
      state.sessionClosed = false;
      els.sessionState.textContent = "未开始";
      updateRecordAvailability();
    }
    renderSessions();
    await loadCurrentMessages();
  });
}

restoreLocalFields();
bindEvents();
checkHealth();
loadMemorySetting();
loadSessions();
loadMemories();
loadCurrentMessages();
