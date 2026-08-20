(() => {
  "use strict";
  const config = window.ROBOT_APP_CONFIG || {};
  const token = String(config.token || "");
  const normalizeBase = (value) => String(value || "").trim().replace(/\/$/, "");
  const pageOrigin = (() => {
    if (!/^https?:$/.test(window.location.protocol)) return "";
    const hostname = String(window.location.hostname || "").toLowerCase();
    if (!hostname || hostname === "localhost" || hostname === "127.0.0.1") return "";
    return normalizeBase(window.location.origin);
  })();
  const configuredBases = [config.serverBase, ...(Array.isArray(config.serverBases) ? config.serverBases : [])]
    .filter((value) => value && value !== "auto")
    .map(normalizeBase);
  const endpointCandidates = [...new Set([pageOrigin, ...configuredBases].filter(Boolean))];
  const CONNECT_TIMEOUT_MS = 4500;
  const TELEMETRY_STALE_MS = 3500;
  const OFFLINE_GRACE_MS = 12000;
  const VOICE_UPLOAD_TIMEOUT_MS = 30000;
  let base = endpointCandidates[0] || "";
  const $ = (id) => document.getElementById(id);
  const state = {
    socket: null, retry: 0, reconnectTimer: null, mapImage: null, mapMeta: null,
    pose: null, target: null, videos: [], feedGrams: 20, pending: new Map(), online: false,
    program: null, programTransition: null, task: null, microphone: null, endpointIndex: 0, endpointAttempts: 0,
    lastMessageAt: 0, disconnectStartedAt: 0, connectTimer: null, offlineTimer: null,
  };
  const voiceCapture = {
    stream: null, recorder: null, chunks: [], startedAt: 0,
    maxTimer: null, releaseRequested: false, streamId: null,
    streaming: false, streamFailed: false, streamChain: Promise.resolve(),
  };

  function api(path) {
    return `${base}${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
  }

  function toast(message, duration = 2300) {
    const element = $("toast");
    element.textContent = message;
    element.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => element.classList.remove("show"), duration);
  }

  function setActivity(message) {
    const element = $("activityText");
    if (element) element.textContent = message;
  }

  function renderMapSource(status = "ready") {
    const element = $("mapSourceText");
    if (!element) return;
    element.classList.toggle("error", status === "error");
    if (status === "loading") {
      element.innerHTML = '<i class="fa-solid fa-satellite-dish" aria-hidden="true"></i> 正在读取真实地图';
      return;
    }
    if (status === "error" || !state.mapMeta) {
      element.innerHTML = '<i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> 真实地图不可用';
      return;
    }
    const live = state.online && state.mapMeta.source === "ros:/map";
    const label = live ? "ROS 实时地图" : "机器人真实地图";
    element.innerHTML = `<i class="fa-solid fa-satellite-dish" aria-hidden="true"></i> ${label}`;
  }

  function setOnline(online) {
    state.online = Boolean(online);
    const badge = $("connectionBadge");
    badge.className = `status ${state.online ? "online" : "offline"}`;
    badge.querySelector("span").textContent = state.online ? "机器人在线" : "机器人离线";
    document.body.dataset.robotOnline = String(state.online);
    if (!state.online) {
      clearPending("等待机器人上线");
      stopVoiceCapture(true);
      state.pose = null;
      state.target = null;
      state.task = null;
      $("targetPanel").classList.add("hidden");
      clearMap();
      renderPose();
      setActivity("等待机器人上线");
    }
    if (state.mapMeta) renderMapSource();
    renderProgram();
    renderTaskState();
    renderMicrophone();
  }

  function setRecovering(message = "控制通道正在恢复") {
    const badge = $("connectionBadge");
    if (badge) {
      badge.className = "status reconnecting";
      badge.querySelector("span").textContent = "正在重连";
    }
    setActivity(message);
  }

  function clearConnectionTimers() {
    clearTimeout(state.connectTimer);
    state.connectTimer = null;
    clearTimeout(state.offlineTimer);
    state.offlineTimer = null;
  }

  function markConnectionHealthy() {
    state.lastMessageAt = Date.now();
    state.disconnectStartedAt = 0;
    clearConnectionTimers();
  }

  function scheduleOfflineFallback() {
    clearTimeout(state.offlineTimer);
    const elapsed = state.disconnectStartedAt ? Date.now() - state.disconnectStartedAt : 0;
    const remaining = Math.max(0, OFFLINE_GRACE_MS - elapsed);
    state.offlineTimer = setTimeout(() => {
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN || Date.now() - state.lastMessageAt > TELEMETRY_STALE_MS) {
        setOnline(false);
      }
    }, remaining);
  }

  function renderTaskState() {
    const element = $("taskStatus");
    if (!element) return;
    const task = state.task || {};
    const active = state.online && Boolean(task.active);
    const queued = Number(task.queued || 0);
    element.className = `task-status ${active ? "busy" : "ready"}`;
    if (!state.online) {
      element.innerHTML = '<i class="fa-solid fa-link-slash"></i><span>控制通道离线</span>';
    } else if (active) {
      const taskNames = {
        navigation_goto:"正在导航", push_up:"正在进行俯卧撑计数", squat:"正在进行下蹲计数",
        pull_up:"正在进行引体向上计数", pet_map_search:"正在寻找宠物",
        pet_tracking:"正在追踪宠物", autonomous_projection:"正在寻找投影墙面",
        light_control:"正在控制灯光", feeder_control:"正在控制投食机",
        head_control:"正在调整头部", projector_control:"正在控制投影",
        move_forward:"正在前进", move_backward:"正在后退", move_left:"正在左转", move_right:"正在右转",
      };
      const procedureNames = {
        homecoming_welcome:"正在播放欢迎回家",
        push_up_companion:"正在陪练俯卧撑",
        pull_up_companion:"正在陪练引体向上",
        squat_companion:"正在陪练深蹲",
        find_pet:"正在寻找宠物",
        find_pet_at:"正在指定地点寻找宠物",
        find_pet_here:"正在当前位置寻找宠物",
        find_and_feed_doudou:"正在寻找并投喂宠物",
        meeting_projection:"正在准备会议投影",
        meeting_projection_stop:"正在关闭会议投影",
        rest_lighting:"正在调整休息环境",
        living_room_light_service:"正在处理客厅灯光",
      };
      const firstProcedure = Array.isArray(task.active_procedures) ? task.active_procedures[0] : "";
      const firstSkill = Array.isArray(task.active_skills) ? task.active_skills[0] : "";
      const name = escapeHtml(procedureNames[firstProcedure] || taskNames[firstSkill] || (task.planning ? "正在理解语音" : "机器人正在执行任务"));
      element.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i><span>${name}${queued ? ` · ${queued} 项等待中` : ""}</span>`;
    } else {
      element.innerHTML = '<i class="fa-solid fa-shield-halved"></i><span>任务调度器空闲</span>';
    }
    document.body.dataset.taskBusy = String(active);
  }

  function renderProgram() {
    const badge = $("programBadge");
    if (!badge) return;
    const rawState = state.online ? String(state.program && state.program.state || "unknown") : "offline";
    const labels = {
      running: "运行中", stopped: "已关闭", starting: "启动中", stopping: "关闭中",
      partial: "部分运行", dry_run: "测试模式", offline: "控制离线", unknown: "检测中",
    };
    badge.className = `program-badge ${rawState}`;
    badge.querySelector("span").textContent = labels[rawState] || "状态未知";
    document.body.dataset.programState = rawState;
    const startButton = $("programStart"), stopButton = $("programStop");
    const busy = rawState === "starting" || rawState === "stopping";
    startButton.disabled = !state.online || busy || rawState === "running";
    stopButton.disabled = !state.online || busy || rawState === "stopped";
    const hints = {
      running: "核心、导航、模型与语音服务正在运行。",
      stopped: "机器人程序已关闭，App 控制通道保持在线。",
      starting: "正在依次启动机器人服务，请稍候……",
      stopping: "正在安全结束任务并关闭机器人服务……",
      partial: "部分服务未正常运行，可先关闭后重新启动。",
      offline: "控制通道离线，暂时无法管理机器人程序。",
    };
    $("programHint").textContent = hints[rawState] || "正在读取机器人程序状态……";
  }

  function renderMicrophone() {
    const badge = $("microphoneBadge");
    if (!badge) return;
    const microphone = state.microphone && typeof state.microphone === "object" ? state.microphone : null;
    const enabled = microphone ? Boolean(microphone.enabled) : null;
    const accepting = Boolean(microphone && microphone.accepting_local_voice);
    const status = !state.online || enabled === null ? "unknown" : enabled ? "enabled" : "disabled";
    const labels = {enabled:"已打开", disabled:"已关闭", unknown:state.online ? "检测中" : "控制离线"};
    badge.className = `microphone-badge ${status}`;
    badge.querySelector("span").textContent = labels[status];
    $("microphoneIcon").classList.toggle("disabled", status === "disabled");
    $("microphoneIcon").innerHTML = `<i class="fa-solid ${status === "disabled" ? "fa-microphone-slash" : "fa-microphone"}" aria-hidden="true"></i>`;
    $("microphoneHint").textContent = status === "disabled"
      ? "现场说话不会再触发机器人，App 控制保持可用。"
      : status === "enabled" && accepting
        ? "正在接收机器人周围的现场语音。"
        : status === "enabled"
          ? "已设为打开，语音程序启动后自动恢复收音。"
          : "正在读取机器人麦克风状态……";
    const disable = $("microphoneDisable"), enable = $("microphoneEnable");
    disable.classList.toggle("active", status === "disabled");
    enable.classList.toggle("active", status === "enabled");
    disable.disabled = !state.online || status === "disabled";
    enable.disabled = !state.online || status === "enabled";
    document.body.dataset.microphoneEnabled = enabled === null ? "unknown" : String(enabled);
  }

  function labelFor(payload) {
    if (payload.action === "manual_move") return ({forward:"前进", backward:"后退", left:"左转", right:"右转", stop:"停止"})[payload.direction] || "点控";
    if (payload.action === "light") return payload.state === "on" ? "打开客厅灯" : "关闭客厅灯";
    if (payload.action === "feed") return `投食 ${payload.grams}g`;
    if (payload.action === "navigate") return "导航到目标点";
    if (payload.action === "stop") return "停止任务";
    if (payload.action === "program_start") return "启动机器人程序";
    if (payload.action === "program_stop") return "关闭机器人程序";
    if (payload._uiAction === "microphone_set") return payload._microphoneEnabled ? "打开麦克风" : "关闭麦克风";
    return "执行操作";
  }

  function programTransitionPhase(action) {
    return action === "program_start" ? "starting" : action === "program_stop" ? "stopping" : "";
  }

  function beginProgramTransition(action) {
    const phase = programTransitionPhase(action);
    if (!phase) return;
    state.programTransition = {action, phase, startedAt:Date.now()};
    state.program = {...(state.program || {}), state:phase};
    renderProgram();
  }

  function finishProgramTransition(action = "") {
    const transition = state.programTransition;
    if (!transition || (action && transition.action !== action)) return;
    state.programTransition = null;
  }

  function acceptProgramUpdate(program, source = "telemetry") {
    if (!program) return;
    const transition = state.programTransition;
    if (!transition) {
      state.program = program;
      return;
    }
    const incoming = String(program.state || "unknown");
    const completed = transition.action === "program_start" ? incoming === "running" : incoming === "stopped";
    const failedFinal = source === "result" && !completed;
    if (completed || failedFinal) {
      state.programTransition = null;
      state.program = program;
      return;
    }
    // Keep the lifecycle phase stable while delayed telemetry still reports the old state.
    state.program = {...program, state:transition.phase};
  }

  function command(payload, source, explicitLabel) {
    if (!state.online || !state.socket || state.socket.readyState !== WebSocket.OPEN) {
      toast("机器人尚未连接，请稍后再试");
      setActivity("指令未发送：机器人离线");
      return null;
    }
    const id = String(payload.id || crypto.randomUUID());
    const label = explicitLabel || labelFor(payload);
    if (source) source.dataset.pending = "true";
    const action = String(payload._uiAction || payload.action || "");
    beginProgramTransition(action);
    const timeoutMs = action.startsWith("program_") ? 660000
      : action === "navigate" ? 135000
      : action === "light" || action === "feed" ? 30000
      : action === "stop" ? 15000
      : 10000;
    const releaseTimer = setTimeout(() => {
      if (source) delete source.dataset.pending;
      state.pending.delete(id);
      if (action.startsWith("program_")) {
        finishProgramTransition(action);
        toast(`${label}仍在处理中，请稍后查看状态`, 3600);
        setActivity(`${label} · 仍在处理中`);
      } else {
        toast(`${label}响应超时，请确认机器人状态`, 3600);
        setActivity(`${label} · 响应超时`);
      }
    }, timeoutMs);
    state.pending.set(id, {label, source, releaseTimer, action});
    try {
      state.socket.send(JSON.stringify({id, ...payload}));
      recoverCommandResult(id);
    } catch (error) {
      releasePending(id);
      finishProgramTransition(action);
      toast(`${label}发送失败，请检查连接`, 3200);
      setActivity(`${label} · 发送失败`);
      return null;
    }
    setActivity(`${label} · 正在发送`);
    return id;
  }

  function clearPending(reason = "连接已断开") {
    state.pending.forEach((item) => {
      clearTimeout(item.releaseTimer);
      if (item.source) delete item.source.dataset.pending;
    });
    state.pending.clear();
    state.programTransition = null;
    setVoiceUi("idle");
    if (reason) setActivity(reason);
  }

  function releasePending(id) {
    const item = state.pending.get(String(id || ""));
    if (!item) return null;
    clearTimeout(item.releaseTimer);
    if (item.source) delete item.source.dataset.pending;
    state.pending.delete(String(id));
    return item;
  }

  function acknowledgePending(id) {
    const item = state.pending.get(String(id || ""));
    if (!item) return null;
    item.acknowledged = true;
    return item;
  }

  function connect() {
    clearTimeout(state.reconnectTimer);
    if (!endpointCandidates.length || !token || token.startsWith("__")) {
      setOnline(false);
      toast("App 尚未配置连接信息", 3200);
      return;
    }
    const candidateIndex = state.endpointIndex % endpointCandidates.length;
    base = endpointCandidates[candidateIndex];
    const wsBase = base.replace(/^http/, "ws");
    const socket = new WebSocket(`${wsBase}/ws/app?token=${encodeURIComponent(token)}`);
    state.socket = socket;
    state.connectTimer = setTimeout(() => {
      if (state.socket === socket && socket.readyState === WebSocket.CONNECTING) {
        setRecovering("当前线路连接较慢，正在切换备用线路");
        socket.close();
      }
    }, CONNECT_TIMEOUT_MS);
    socket.onopen = () => {
      clearTimeout(state.connectTimer);
      state.connectTimer = null;
      state.retry = 0;
      state.endpointAttempts = 0;
      setActivity("连接已建立，正在同步状态");
    };
    socket.onmessage = (event) => {
      markConnectionHealthy();
      try { handle(JSON.parse(event.data)); } catch (error) { console.warn("invalid message", error); }
    };
    socket.onclose = () => {
      if (state.socket !== socket) return;
      clearTimeout(state.connectTimer);
      state.connectTimer = null;
      state.socket = null;
      clearPending("控制连接已断开，正在重连");
      if (!state.disconnectStartedAt) state.disconnectStartedAt = Date.now();
      if (state.online && Date.now() - state.disconnectStartedAt < OFFLINE_GRACE_MS) {
        setRecovering();
        scheduleOfflineFallback();
      } else {
        setOnline(false);
      }
      state.endpointAttempts += 1;
      state.endpointIndex = (candidateIndex + 1) % endpointCandidates.length;
      const completedRound = state.endpointAttempts % endpointCandidates.length === 0;
      const delay = completedRound ? Math.min(10000, 700 * Math.pow(1.7, state.retry++)) : 120;
      state.reconnectTimer = setTimeout(connect, delay);
    };
    socket.onerror = () => socket.close();
  }

  function recoverConnection(message = "控制通道正在恢复") {
    const socket = state.socket;
    if (!state.disconnectStartedAt) state.disconnectStartedAt = Date.now();
    setRecovering(message);
    if (socket) {
      state.socket = null;
      try { socket.close(); } catch (_) {}
    }
    clearTimeout(state.reconnectTimer);
    state.endpointIndex = (state.endpointIndex + 1) % Math.max(1, endpointCandidates.length);
    state.reconnectTimer = setTimeout(connect, 80);
    scheduleOfflineFallback();
  }

  function ensureFreshConnection() {
    if (document.hidden) return;
    const socket = state.socket;
    if (!socket || socket.readyState === WebSocket.CLOSED) {
      recoverConnection();
      return;
    }
    if (socket.readyState === WebSocket.OPEN && state.lastMessageAt && Date.now() - state.lastMessageAt > TELEMETRY_STALE_MS) {
      recoverConnection("状态更新中断，正在自动重新连接");
    }
  }

  function handle(message) {
    if (message.type === "state") {
      state.mapMeta = message.map || null;
      state.videos = Array.isArray(message.videos) ? message.videos : [];
      acceptProgramUpdate(message.program || null, "state");
      state.task = message.task || null;
      state.microphone = message.microphone || (state.task && state.task.microphone) || state.microphone;
      const online = Boolean(message.robot && message.robot.online);
      state.pose = online ? (message.pose || null) : null;
      setOnline(online);
      renderPose(); renderVideos(); renderFitness(); renderProgram(); renderTaskState(); renderMicrophone();
      if (state.online && state.mapMeta) loadMap(); else clearMap();
      setActivity(state.online ? "系统就绪" : "等待机器人上线");
    } else if (message.type === "robot_status") {
      setOnline(Boolean(message.robot && message.robot.online));
      if (state.online) {
        if (state.mapMeta && !state.mapImage) loadMap();
        setActivity("系统就绪");
      }
    } else if (message.type === "telemetry") {
      state.pose = message.pose || null;
      if (message.program) acceptProgramUpdate(message.program, "telemetry");
      if (message.task) state.task = message.task;
      state.microphone = message.microphone || (state.task && state.task.microphone) || state.microphone;
      setOnline(true); renderPose(); renderProgram(); renderTaskState(); renderMicrophone();
      if (state.mapMeta && !state.mapImage) loadMap(); else drawMap();
    } else if (message.type === "program_status") {
      const incomingState = String(message.program && message.program.state || "");
      if (!state.programTransition && (incomingState === "starting" || incomingState === "stopping")) {
        state.programTransition = {
          action:incomingState === "starting" ? "program_start" : "program_stop",
          phase:incomingState,
          startedAt:Date.now(),
        };
      }
      acceptProgramUpdate(message.program || null, "program_status");
      renderProgram();
    } else if (message.type === "task_status") {
      state.task = message.task || null;
      renderTaskState();
    } else if (message.type === "map_update") {
      state.mapMeta = message.map || null;
      if (state.online && state.mapMeta) loadMap(); else clearMap();
    } else if (message.type === "video_available") {
      if (message.video) state.videos.unshift(message.video);
      renderVideos(); renderFitness();
      if (message.video && message.video.category === "fitness") {
        toast(`${exerciseLabel(message.video)}记录已送达：${Number(message.video.count || 0)} 个`, 3200);
      } else {
        playVideo(message.video); toast("宠物的新视频已送达");
      }
    } else if (message.type === "command_ack") {
      const item = message.ok ? acknowledgePending(message.id) : releasePending(message.id);
      if (!message.ok) {
        finishProgramTransition(item && item.action);
        const error = friendlyError(message.error);
        if (String(message.error || "").includes("robot_offline")) setOnline(false);
        toast(`执行失败：${error}`, 3200); setActivity(`失败 · ${error}`);
      } else if (item) {
        toast(`${item.label}已送达`); setActivity(`${item.label} · 正在执行`);
      }
    } else if (message.type === "command_result") {
      const item = releasePending(message.id);
      const label = item ? item.label : "操作";
      if (message.action === "voice_audio") setVoiceUi("idle");
      if (message.result && message.result.program) {
        acceptProgramUpdate(message.result.program, "result");
        renderProgram();
      }
      if (message.result && message.result.microphone) {
        state.microphone = message.result.microphone;
        renderMicrophone();
      }
      if (!message.ok) finishProgramTransition(item && item.action);
      if (message.ok && message.action === "voice_audio") {
        const transcript = String(message.result && message.result.transcript || "").trim();
        toast(transcript ? `已识别：${transcript}` : "语音指令已受理", 3600);
        setActivity("语音指令 · 已进入统一任务调度");
      } else if (message.ok) { toast(`${label}已完成`); setActivity(`${label} · 已完成`); }
      else { const error = friendlyError(message.error); toast(`${label}失败：${error}`, 3200); setActivity(`${label} · 失败`); }
    }
  }

  function friendlyError(error) {
    const normalized = String(error || "").includes("robot_program_not_running") ? "robot_program_not_running" : error;
    const keys = ["task_busy", "voice_agent_busy", "voice_agent_unavailable", "voice_audio_decode_failed", "invalid_voice_audio", "invalid_voice_size"];
    const matched = keys.find((key) => String(error || "").includes(key));
    return ({robot_offline:"机器人离线", robot_program_not_running:"机器人程序尚未启动", navigation_busy:"导航任务正在执行", task_busy:"机器人正在执行任务，请先停止当前任务", voice_agent_busy:"语音通道正在处理上一条指令", voice_agent_unavailable:"机器人语音服务尚未就绪", voice_audio_decode_failed:"录音格式解析失败，请重新录制", invalid_voice_audio:"录音无效，请重新录制", invalid_voice_size:"录音过长或为空", invalid_direction:"方向无效", navigation_target_out_of_range:"目标点超出地图范围", robot_link_failed:"机器人连接中断", unsupported_action:"暂不支持此操作"})[matched || normalized] || error || "未知错误";
  }

  function playVideo(item) {
    if (!item) return;
    const video = $("petVideo");
    const surface = $("videoSurface");
    video.controls = false;
    surface.classList.remove("playing", "has-started");
    video.src = api(`/api/videos/${item.id}/file`);
    surface.classList.remove("empty");
    $("videoTitle").textContent = petLabel(item.title);
    $("videoMeta").textContent = `${formatTime(item.created_at)} · ${Number(item.duration_sec || 5).toFixed(0)} 秒`;
  }

  function renderVideos() {
    const list = $("videoList");
    const videos = state.videos.filter((video) => video.category !== "fitness");
    if (!videos.length) {
      list.innerHTML = '<div class="video-item"><div><i class="fa-solid fa-paw" aria-hidden="true"></i></div><span><b>暂无视频</b><small>完成一次寻找宠物任务后会显示在这里</small></span></div>';
      return;
    }
    list.innerHTML = videos.slice(0, 8).map((video) => `<button class="video-item" data-video="${escapeHtml(String(video.id))}"><img src="${api(`/api/videos/${video.id}/thumb`)}" alt="宠物视频缩略图"><span><b>${escapeHtml(petLabel(video.title))}</b><small>${formatTime(video.created_at)} · ${Number(video.duration_sec || 5).toFixed(0)} 秒</small></span><i class="fa-solid fa-chevron-right row-arrow" aria-hidden="true"></i></button>`).join("");
    list.querySelectorAll("[data-video]").forEach((button) => {
      button.onclick = () => { playVideo(videos.find((video) => String(video.id) === button.dataset.video)); window.scrollTo({top:0, behavior:"smooth"}); };
    });
    if ($("videoSurface").classList.contains("empty")) playVideo(videos[0]);
  }

  function exerciseLabel(item) {
    return String(item && (item.exercise_label || ({push_up:"俯卧撑", squat:"下蹲", pull_up:"引体向上"})[item.exercise]) || "运动");
  }

  function identityLabel(value) {
    const text = String(value || "").trim();
    return ({zhangsan:"张三", zhenghang:"张三"})[text.toLowerCase()] || text || "已确认身份";
  }

  function renderFitness() {
    const list = $("fitnessList");
    if (!list) return;
    const records = state.videos.filter((video) => video.category === "fitness");
    if (!records.length) {
      list.innerHTML = '<div class="fitness-empty"><span><i class="fa-solid fa-person-running"></i></span><div><b>还没有运动记录</b><small>运动结束后，原始视频和计数会自动同步到这里</small></div></div>';
      resetFitnessHero();
      return;
    }
    list.innerHTML = records.slice(0, 12).map((item) => `<button type="button" class="fitness-item" data-fitness-video="${escapeHtml(String(item.id))}"><img src="${api(`/api/videos/${item.id}/thumb`)}" alt="${escapeHtml(exerciseLabel(item))}原始视频缩略图"><span class="fitness-main"><small>${formatTime(item.created_at)}</small><b>${escapeHtml(exerciseLabel(item))}</b><em>${escapeHtml(identityLabel(item.identity))} · 原始视频</em></span><span class="fitness-count"><strong>${Number(item.count || 0)}</strong><small>个</small></span><i class="fa-solid fa-play fitness-play"></i></button>`).join("");
    list.querySelectorAll("[data-fitness-video]").forEach((button) => {
      button.onclick = () => {
        const item = records.find((record) => String(record.id) === button.dataset.fitnessVideo);
        playFitnessVideo(item);
        $("fitnessVideoSurface").scrollIntoView({behavior:"smooth", block:"center"});
      };
    });
    if ($("fitnessVideoSurface").classList.contains("empty")) playFitnessVideo(records[0]);
  }

  function resetFitnessHero() {
    const surface = $("fitnessVideoSurface");
    const video = $("fitnessVideo");
    if (!surface || !video) return;
    video.removeAttribute("src");
    video.controls = false;
    video.load();
    surface.classList.add("empty");
    surface.classList.remove("playing", "has-started");
    $("fitnessVideoTitle").textContent = "等待新的运动记录";
    $("fitnessVideoMeta").textContent = "";
    $("fitnessHeroCount").querySelector("strong").textContent = "0";
  }

  function playFitnessVideo(item) {
    if (!item) return;
    const surface = $("fitnessVideoSurface");
    const video = $("fitnessVideo");
    video.controls = false;
    surface.classList.remove("playing", "has-started");
    video.src = api(`/api/videos/${item.id}/file`);
    video.poster = api(`/api/videos/${item.id}/thumb`);
    surface.classList.remove("empty");
    $("fitnessVideoTitle").textContent = exerciseLabel(item);
    $("fitnessVideoMeta").textContent = `${formatTime(item.created_at)} · ${identityLabel(item.identity)} · 原始视频`;
    $("fitnessHeroCount").querySelector("strong").textContent = String(Number(item.count || 0));
  }

  function startHeroPlayback(surfaceId, videoId, emptyMessage) {
    const surface = $(surfaceId);
    const video = $(videoId);
    if (surface.classList.contains("empty")) { toast(emptyMessage); return; }
    video.controls = true;
    surface.classList.add("has-started");
    const playRequest = video.play();
    if (playRequest && typeof playRequest.catch === "function") {
      playRequest.catch(() => toast("视频暂时无法播放，请稍后重试"));
    }
  }

  async function refreshVideoLibrary(button, successText) {
    if (button) button.dataset.pending = "true";
    try {
      const response = await fetch(api("/api/videos"));
      if (!response.ok) throw new Error(String(response.status));
      state.videos = (await response.json()).videos || [];
      renderVideos();
      renderFitness();
      toast(successText);
    } catch (_) {
      toast("刷新失败，请检查网络");
    } finally {
      if (button) delete button.dataset.pending;
    }
  }

  function formatTime(seconds) {
    if (!seconds) return "刚刚";
    return new Date(seconds * 1000).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"});
  }

  function escapeHtml(text) { const div = document.createElement("div"); div.textContent = text; return div.innerHTML; }

  function petLabel(text, fallback = "宠物找到了") {
    return String(text || fallback).replaceAll("豆豆", "宠物");
  }

  function loadMap() {
    if (!state.online || !state.mapMeta) { clearMap(); return; }
    const requestedMap = state.mapMeta;
    renderMapSource("loading");
    const image = new Image();
    image.onload = () => {
      if (!state.online || state.mapMeta !== requestedMap) return;
      state.mapImage = image; $("mapEmpty").style.display = "none"; renderMapSource(); drawMap();
    };
    image.onerror = () => {
      if (!state.online || state.mapMeta !== requestedMap) return;
      state.mapImage = null; $("mapEmpty").style.display = "grid"; renderMapSource("error");
    };
    image.src = api(`/api/map?t=${Date.now()}`);
  }

  function clearMap() { state.mapImage = null; $("mapEmpty").style.display = state.online ? "grid" : "none"; renderMapSource(state.online ? "error" : "ready"); const canvas = $("mapCanvas"); canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height); }

  function canvasGeometry() {
    const canvas = $("mapCanvas");
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    if (!state.mapImage) return null;
    const scale = Math.min(width / state.mapImage.width, height / state.mapImage.height);
    const imageWidth = state.mapImage.width * scale;
    const imageHeight = state.mapImage.height * scale;
    return {canvas, ctx:canvas.getContext("2d"), scale, width:imageWidth, height:imageHeight, left:(width-imageWidth)/2, top:(height-imageHeight)/2, ratio};
  }

  function drawMap() {
    if (!state.online) { clearMap(); return; }
    const geometry = canvasGeometry();
    if (!geometry) return;
    geometry.ctx.fillStyle = "#e7e9e7";
    geometry.ctx.fillRect(0, 0, geometry.canvas.width, geometry.canvas.height);
    geometry.ctx.imageSmoothingEnabled = false;
    geometry.ctx.drawImage(state.mapImage, geometry.left, geometry.top, geometry.width, geometry.height);
    if (state.pose) drawMarker(geometry, state.pose, "robot");
    if (state.target) drawMarker(geometry, state.target, "target");
  }

  function worldToPixel(pose) {
    const map = state.mapMeta;
    if (!map || !map.resolution || !map.origin) return null;
    const angle = Number(map.origin.yaw || 0), dx = Number(pose.x) - map.origin.x, dy = Number(pose.y) - map.origin.y;
    const c = Math.cos(angle), s = Math.sin(angle), localX = c*dx+s*dy, localY = -s*dx+c*dy;
    return {x:localX/map.resolution, y:(map.height-1)-(localY/map.resolution), yaw:Number(pose.yaw || 0)-angle};
  }

  function drawMarker(geometry, pose, type) {
    const point = worldToPixel(pose); if (!point) return;
    const x = geometry.left + point.x*geometry.scale, y = geometry.top + point.y*geometry.scale;
    const radius = (type === "robot" ? 12 : 10) * geometry.ratio;
    const styles = getComputedStyle(document.documentElement);
    geometry.ctx.save(); geometry.ctx.translate(x,y); geometry.ctx.rotate(-point.yaw);
    geometry.ctx.fillStyle = type === "robot" ? (styles.getPropertyValue("--teal").trim() || styles.getPropertyValue("--purple").trim() || "#0b766b") : (styles.getPropertyValue("--coral").trim() || "#df654f");
    geometry.ctx.beginPath(); geometry.ctx.moveTo(radius*1.35,0); geometry.ctx.lineTo(-radius*.8,radius*.8); geometry.ctx.lineTo(-radius*.5,0); geometry.ctx.lineTo(-radius*.8,-radius*.8); geometry.ctx.closePath(); geometry.ctx.fill();
    geometry.ctx.strokeStyle = "white"; geometry.ctx.lineWidth = 3*geometry.ratio; geometry.ctx.stroke(); geometry.ctx.restore();
  }

  function chooseTarget(event) {
    if (!state.online) { toast("机器人离线，地图导航暂不可用"); return; }
    const geometry = canvasGeometry(); if (!geometry || !state.mapMeta) { toast("地图尚未就绪"); return; }
    const rect = geometry.canvas.getBoundingClientRect();
    const px = (event.clientX-rect.left)*geometry.ratio, py = (event.clientY-rect.top)*geometry.ratio;
    const imageX = (px-geometry.left)/geometry.scale, imageY = (py-geometry.top)/geometry.scale;
    if (imageX<0 || imageY<0 || imageX>=state.mapMeta.width || imageY>=state.mapMeta.height) return;
    const map = state.mapMeta, localX = imageX*map.resolution, localY = (map.height-1-imageY)*map.resolution;
    const angle = Number(map.origin.yaw || 0), c = Math.cos(angle), s = Math.sin(angle);
    state.target = {x:map.origin.x+c*localX-s*localY, y:map.origin.y+s*localX+c*localY, yaw:state.pose ? Number(state.pose.yaw || 0) : 0};
    $("targetText").textContent = `x ${state.target.x.toFixed(2)} · y ${state.target.y.toFixed(2)}`;
    $("targetPanel").classList.remove("hidden"); drawMap();
  }

  function renderPose() {
    const pose = state.pose;
    $("poseText").querySelector("span").textContent = !state.online ? "机器人离线" : pose ? `${Number(pose.x).toFixed(2)}, ${Number(pose.y).toFixed(2)}` : "位置未知";
  }

  function openFeed() { if (typeof $("feedDialog").showModal === "function") $("feedDialog").showModal(); }

  function greetingForHour(hour) {
    if (hour >= 5 && hour < 11) return "早上好";
    if (hour >= 11 && hour < 14) return "中午好";
    if (hour >= 14 && hour < 18) return "下午好";
    return "晚上好";
  }

  function updateGreeting() { $("greeting").textContent = greetingForHour(new Date().getHours()); }

  function setVoiceUi(mode, hint) {
    const button = $("voiceHoldButton");
    const text = $("voiceHint");
    if (!button || !text) return;
    button.classList.toggle("recording", mode === "recording");
    button.classList.toggle("sending", mode === "sending");
    button.querySelector("b").textContent = mode === "recording" ? "松开发送" : mode === "sending" ? "正在发送" : "按住说话";
    text.textContent = hint || (state.task && state.task.active
      ? "当前任务不会被直接冲撞；新指令将按资源冲突安全排队或中断。"
      : "语音会传给机器人，并与现场唤醒语音共用同一任务调度器。");
  }

  function stopVoiceTracks() {
    if (voiceCapture.stream) voiceCapture.stream.getTracks().forEach((track) => track.stop());
    voiceCapture.stream = null;
    clearTimeout(voiceCapture.maxTimer);
    voiceCapture.maxTimer = null;
  }

  function voiceSocketOpen() {
    return Boolean(state.socket && state.socket.readyState === WebSocket.OPEN);
  }

  async function voiceChunkBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const step = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += step) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + step));
    }
    return btoa(binary);
  }

  function queueVoiceChunk(blob) {
    if (!blob || !blob.size) return;
    voiceCapture.chunks.push(blob);
    if (!voiceCapture.streaming || !voiceCapture.streamId) return;
    const streamId = voiceCapture.streamId;
    voiceCapture.streamChain = voiceCapture.streamChain.then(async () => {
      if (!voiceCapture.streaming || voiceCapture.streamFailed || !voiceSocketOpen()) {
        voiceCapture.streamFailed = true;
        return;
      }
      const data = await voiceChunkBase64(await blob.arrayBuffer());
      state.socket.send(JSON.stringify({action:"voice_stream_chunk", id:streamId, data_base64:data}));
    }).catch(() => { voiceCapture.streamFailed = true; });
  }

  function abortVoiceStream() {
    if (voiceCapture.streaming && voiceCapture.streamId && voiceSocketOpen()) {
      try { state.socket.send(JSON.stringify({action:"voice_stream_abort", id:voiceCapture.streamId})); } catch (_) {}
    }
    voiceCapture.streaming = false;
    voiceCapture.streamFailed = true;
  }

  function stopVoiceCapture(discard = false) {
    voiceCapture.releaseRequested = Boolean(discard);
    if (discard) abortVoiceStream();
    const recorder = voiceCapture.recorder;
    if (recorder && recorder.state !== "inactive") {
      if (discard) voiceCapture.chunks = [];
      try { recorder.stop(); } catch (_) { stopVoiceTracks(); }
    } else {
      stopVoiceTracks();
      voiceCapture.recorder = null;
      if (discard) setVoiceUi("idle", "录音已取消");
    }
  }

  function voiceMimeType() {
    const choices = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"];
    return choices.find((value) => window.MediaRecorder && MediaRecorder.isTypeSupported(value)) || "";
  }

  async function beginVoice(event) {
    event.preventDefault();
    if (!state.online) { toast("机器人离线，暂时不能发送语音"); return; }
    if (!state.program || state.program.state !== "running") { toast("请先启动机器人程序"); return; }
    if ([...state.pending.values()].some((item) => item.label === "语音指令")) {
      toast("上一条语音还在处理中，请稍等"); return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      toast("当前浏览器不支持录音，请使用最新版 App"); return;
    }
    if (voiceCapture.recorder) return;
    voiceCapture.releaseRequested = false;
    try {
      if (event.currentTarget.setPointerCapture && event.pointerId !== undefined) event.currentTarget.setPointerCapture(event.pointerId);
      const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true, channelCount:1}, video:false});
      if (voiceCapture.releaseRequested) { stream.getTracks().forEach((track) => track.stop()); return; }
      const mimeType = voiceMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, {mimeType, audioBitsPerSecond:64000}) : new MediaRecorder(stream);
      voiceCapture.stream = stream;
      voiceCapture.recorder = recorder;
      voiceCapture.chunks = [];
      voiceCapture.streamId = crypto.randomUUID();
      voiceCapture.streaming = voiceSocketOpen();
      voiceCapture.streamFailed = !voiceCapture.streaming;
      voiceCapture.streamChain = Promise.resolve();
      if (voiceCapture.streaming) {
        try {
          state.socket.send(JSON.stringify({
            action:"voice_stream_start", id:voiceCapture.streamId,
            mime_type:recorder.mimeType || mimeType || "audio/webm",
          }));
        } catch (_) {
          voiceCapture.streaming = false;
          voiceCapture.streamFailed = true;
        }
      }
      voiceCapture.startedAt = performance.now();
      recorder.ondataavailable = (dataEvent) => queueVoiceChunk(dataEvent.data);
      recorder.onerror = () => { toast("录音失败，请重新尝试"); stopVoiceTracks(); voiceCapture.recorder = null; setVoiceUi("idle"); };
      recorder.onstop = async () => {
        const duration = Math.round(performance.now() - voiceCapture.startedAt);
        const chunks = voiceCapture.chunks.splice(0);
        const discarded = voiceCapture.releaseRequested && chunks.length === 0;
        await voiceCapture.streamChain;
        const streamRequest = {
          id: voiceCapture.streamId,
          streamed: voiceCapture.streaming && !voiceCapture.streamFailed && voiceSocketOpen(),
        };
        stopVoiceTracks();
        voiceCapture.recorder = null;
        voiceCapture.releaseRequested = false;
        voiceCapture.streaming = false;
        voiceCapture.streamId = null;
        voiceCapture.streamFailed = false;
        voiceCapture.streamChain = Promise.resolve();
        if (discarded) { setVoiceUi("idle"); return; }
        const blob = new Blob(chunks, {type: recorder.mimeType || mimeType || "audio/webm"});
        if (duration < 450 || blob.size < 500) {
          if (streamRequest.streamed && streamRequest.id && voiceSocketOpen()) {
            try { state.socket.send(JSON.stringify({action:"voice_stream_abort", id:streamRequest.id})); } catch (_) {}
          }
          toast("说话时间太短，请按住后再说"); setVoiceUi("idle"); return;
        }
        await uploadVoice(blob, duration, streamRequest);
      };
      recorder.start(160);
      setVoiceUi("recording", "正在录音；松开后立即发送，最长二十秒。机器人会安全协调当前任务。");
      voiceCapture.maxTimer = setTimeout(() => stopVoiceCapture(false), 20000);
    } catch (error) {
      stopVoiceTracks();
      voiceCapture.recorder = null;
      setVoiceUi("idle");
      toast(error && error.name === "NotAllowedError" ? "请允许 App 使用麦克风" : "无法启动录音，请重试", 3200);
    }
  }

  function endVoice(event) {
    if (event) event.preventDefault();
    if (!voiceCapture.recorder) { voiceCapture.releaseRequested = true; return; }
    stopVoiceCapture(false);
  }

  async function uploadVoice(blob, durationMs, streamRequest = null) {
    const button = $("voiceHoldButton");
    const id = streamRequest && streamRequest.id ? streamRequest.id : crypto.randomUUID();
    setVoiceUi("sending", "录音已发送，机器人正在识别并安排任务……");
    button.dataset.pending = "true";
    const releaseTimer = setTimeout(() => {
      releasePending(id);
      toast("语音仍在处理，请稍后查看机器人状态", 3600);
      setVoiceUi("idle");
    }, 155000);
    state.pending.set(id, {label:"语音指令", source:button, releaseTimer, action:"voice_audio"});
    let forwarded = false;
    const controller = new AbortController();
    const uploadTimer = setTimeout(() => controller.abort(), VOICE_UPLOAD_TIMEOUT_MS);
    try {
      if (streamRequest && streamRequest.streamed) {
        state.socket.send(JSON.stringify({
          action:"voice_stream_end", id, duration_ms:durationMs,
          mime_type:blob.type || "audio/webm",
        }));
        forwarded = true;
        setVoiceUi("sending", "语音已送达，机器人正在识别并执行……");
        setActivity("语音指令 · 已送达机器人");
        recoverCommandResult(id);
        return;
      }
      const response = await fetch(api("/api/app/voice"), {
        method:"POST",
        headers:{"Content-Type":blob.type || "audio/webm", "X-Command-Id":id, "X-Audio-Duration-Ms":String(durationMs)},
        body:blob,
        signal:controller.signal,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) throw new Error(body.detail || body.error || `HTTP_${response.status}`);
      acknowledgePending(id);
      forwarded = true;
      setVoiceUi("sending", "语音已送达，机器人正在识别并执行…");
      setActivity("语音指令 · 已送达机器人");
      recoverCommandResult(id);
    } catch (error) {
      releasePending(id);
      const rawError = error && error.name === "AbortError" ? "voice_upload_timeout" : error && error.message;
      const message = rawError === "voice_upload_timeout" ? "上传超时，正在恢复连接，请重新录制" : friendlyError(rawError);
      toast(`语音发送失败：${message}`, 3600);
      setActivity("语音指令 · 发送失败");
      recoverConnection("语音上传连接异常，正在自动恢复");
    } finally {
      clearTimeout(uploadTimer);
      if (!forwarded) setVoiceUi("idle");
    }
  }

  async function recoverCommandResult(id) {
    const startedAt = Date.now();
    while (state.pending.has(id) && Date.now() - startedAt < 155000) {
      await new Promise((resolve) => setTimeout(resolve, 900));
      if (!state.pending.has(id)) return;
      try {
        const response = await fetch(api(`/api/commands/${encodeURIComponent(id)}`), {cache:"no-store"});
        if (!response.ok) continue;
        const body = await response.json();
        const record = body && body.command;
        if (record && record.state === "completed" && record.message) {
          handle(record.message);
          return;
        }
      } catch (_) {}
    }
  }

  document.querySelectorAll(".nav-item").forEach((button) => button.onclick = () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === button.dataset.view));
    document.body.classList.toggle("home-active", button.dataset.view === "homeView");
    window.scrollTo({top:0}); if (button.dataset.view === "mapView") setTimeout(drawMap, 30);
  });
  document.querySelectorAll("[data-command]").forEach((button) => button.onclick = () => command(JSON.parse(button.dataset.command), button, button.dataset.label));
  document.querySelectorAll("[data-direction]").forEach((button) => button.onclick = () => {
    const direction = button.dataset.direction;
    const isLinearMove = direction === "forward" || direction === "backward";
    command({
      action: "manual_move",
      direction,
      duration: isLinearMove ? 0.42 : 0.25,
      ...(isLinearMove ? {linear_speed: 0.18} : {}),
    }, button);
  });
  $("mapStage").addEventListener("click", chooseTarget);
  $("cancelTarget").onclick = () => { state.target = null; $("targetPanel").classList.add("hidden"); drawMap(); };
  $("confirmTarget").onclick = (event) => { if (state.target && command({action:"navigate", ...state.target}, event.currentTarget)) { $("targetPanel").classList.add("hidden"); } };
  $("feedButton").onclick = openFeed;
  $("programStart").onclick = (event) => command({action:"program_start"}, event.currentTarget);
  $("programStop").onclick = () => {
    if (typeof $("programStopDialog").showModal === "function") $("programStopDialog").showModal();
  };
  $("confirmProgramStop").onclick = (event) => command({action:"program_stop"}, event.currentTarget, "关闭机器人程序");
  function setMicrophone(enabled, button) {
    const compatibilityId = `mic-set-${enabled ? 1 : 0}-${crypto.randomUUID().replaceAll("-", "")}`;
    return command({
      id: compatibilityId,
      action: "request_state",
      _uiAction: "microphone_set",
      _microphoneEnabled: enabled,
    }, button, enabled ? "打开麦克风" : "关闭麦克风");
  }
  $("microphoneDisable").onclick = (event) => setMicrophone(false, event.currentTarget);
  $("microphoneEnable").onclick = (event) => setMicrophone(true, event.currentTarget);
  document.querySelectorAll(".feed-options button").forEach((button) => button.onclick = (event) => { event.preventDefault(); state.feedGrams = Number(button.value); document.querySelectorAll(".feed-options button").forEach((item) => item.classList.toggle("selected", item === button)); });
  $("confirmFeed").onclick = (event) => { if (command({action:"feed", grams:state.feedGrams}, event.currentTarget)) setActivity(`投食 ${state.feedGrams}g · 正在发送`); };
  $("refreshVideos").onclick = (event) => refreshVideoLibrary(event.currentTarget, "宠物动态已刷新");
  $("refreshFitness").onclick = (event) => refreshVideoLibrary(event.currentTarget, "运动记录已刷新");
  $("petHeroPlay").onclick = () => startHeroPlayback("videoSurface", "petVideo", "还没有宠物视频");
  $("petVideo").addEventListener("play", () => $("videoSurface").classList.add("playing", "has-started"));
  $("petVideo").addEventListener("pause", () => $("videoSurface").classList.remove("playing"));
  $("fitnessHeroPlay").onclick = () => startHeroPlayback("fitnessVideoSurface", "fitnessVideo", "还没有运动视频");
  $("fitnessVideo").addEventListener("play", () => $("fitnessVideoSurface").classList.add("playing", "has-started"));
  $("fitnessVideo").addEventListener("pause", () => $("fitnessVideoSurface").classList.remove("playing"));
  const voiceButton = $("voiceHoldButton");
  voiceButton.addEventListener("pointerdown", beginVoice);
  voiceButton.addEventListener("pointerup", endVoice);
  voiceButton.addEventListener("pointercancel", endVoice);
  voiceButton.addEventListener("lostpointercapture", (event) => { if (voiceCapture.recorder) endVoice(event); });
  voiceButton.addEventListener("contextmenu", (event) => event.preventDefault());
  window.addEventListener("resize", drawMap);
  window.addEventListener("pageshow", () => { updateGreeting(); ensureFreshConnection(); });
  window.addEventListener("online", ensureFreshConnection);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { updateGreeting(); ensureFreshConnection(); } });
  setInterval(ensureFreshConnection, 1000);
  updateGreeting();
  renderProgram();
  renderTaskState();
  renderMicrophone();
  renderFitness();
  document.body.classList.add("home-active");
  if (config.testMode) window.__ROBOT_APP_TEST__ = {handle, state, command, drawMap, greetingForHour, renderFitness, beginVoice, endVoice, uploadVoice};
  connect();
})();
