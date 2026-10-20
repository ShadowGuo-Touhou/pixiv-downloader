const $ = (id) => document.getElementById(id);

let currentJobId = null;
let eventSource = null;
let refreshTimer = null;
let lastItemsLength = -1;
//: 计划生成中时为 true，防止连点生成出多个任务互相拖慢
let planPending = false;

// ------------------------------------------------------------------ 工具

function log(message) {
  const el = $("log");
  const time = new Date().toLocaleTimeString();
  el.textContent += `[${time}] ${message}\n`;
  el.scrollTop = el.scrollHeight;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function basename(path) {
  return String(path || "").split(/[\\/]/).pop();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = {};
  try {
    data = await response.json();
  } catch {
    /* 空响应 */
  }
  if (!response.ok) {
    throw new Error((data && data.detail) || response.statusText);
  }
  return data;
}

// ------------------------------------------------------------------ 登录

function setBadge(text, kind) {
  const badge = $("auth-badge");
  badge.textContent = text;
  badge.className = "badge" + (kind ? " " + kind : "");
}

function setLoggedIn(name) {
  setBadge(`已登录：${name}`, "ok");
  $("btn-login").hidden = true;
  $("btn-logout").hidden = false;
}

function setLoggedOut() {
  setBadge("未登录", "");
  $("btn-login").hidden = false;
  $("btn-logout").hidden = true;
}

async function refreshAuth() {
  try {
    const state = await api("/api/state");
    if (state.loggedIn) setLoggedIn(state.userName);
    else setLoggedOut();
  } catch (err) {
    log("读取状态失败：" + err.message);
  }
}

async function pollLogin() {
  let snapshot;
  try {
    snapshot = await api("/api/login/status");
  } catch {
    setTimeout(pollLogin, 2000);
    return;
  }
  if (snapshot.status === "opening" || snapshot.status === "waiting") {
    setBadge(snapshot.message || "等待登录…", "waiting");
    setTimeout(pollLogin, 1500);
    return;
  }
  if (snapshot.status === "success") {
    setLoggedIn(snapshot.userName);
    log(`登录成功：${snapshot.userName}`);
  } else if (snapshot.status === "error") {
    setBadge("登录失败", "err");
    log("登录失败：" + snapshot.message);
  } else if (snapshot.status === "cancelled") {
    setLoggedOut();
    log("已取消登录");
  }
}

$("btn-login").addEventListener("click", async () => {
  $("btn-login").disabled = true;
  try {
    // 先停掉可能还挂着的旧会话：否则关掉过一次窗口后再点会没有反应
    try {
      await api("/api/login/cancel", { method: "POST" });
    } catch {
      /* 没有会话在跑也无所谓 */
    }
    const snapshot = await api("/api/login/open", { method: "POST" });
    setBadge(snapshot.message || "正在打开浏览器…", "waiting");
    const which = snapshot.browser ? `（${snapshot.browser}）` : "";
    log(`已请求打开登录窗口${which}，请在弹出的窗口里完成登录`);
    setTimeout(pollLogin, 1200);
  } catch (err) {
    log("启动登录失败：" + err.message);
    setLoggedOut();
  } finally {
    $("btn-login").disabled = false;
  }
});

$("btn-cookie").addEventListener("click", () => {
  $("cookie-modal").hidden = false;
  $("cookie-status").textContent = "";
  $("cookie-input").focus();
});

$("cookie-cancel").addEventListener("click", () => {
  $("cookie-modal").hidden = true;
});

$("cookie-input").addEventListener("keydown", (event) => {
  if (event.key === "Escape") $("cookie-modal").hidden = true;
});

$("cookie-save").addEventListener("click", async () => {
  const cookie = $("cookie-input").value;
  if (!cookie.trim()) {
    $("cookie-status").textContent = "先把 Cookie 粘进来";
    return;
  }
  $("cookie-save").disabled = true;
  $("cookie-status").textContent = "验证中…";
  try {
    const result = await api("/api/login/cookie", {
      method: "POST",
      body: JSON.stringify({ cookie }),
    });
    setLoggedIn(result.userName);
    log(`Cookie 验证通过：${result.userName}`);
    $("cookie-modal").hidden = true;
    $("cookie-input").value = "";
  } catch (err) {
    $("cookie-status").textContent = "失败：" + err.message;
    log("Cookie 登录失败：" + err.message);
  } finally {
    $("cookie-save").disabled = false;
  }
});

$("btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  setLoggedOut();
  log("已退出");
});

// ------------------------------------------------------------------ 作者

$("btn-parse").addEventListener("click", async () => {
  const url = $("user-url").value.trim();
  if (!url) return;
  $("user-info").textContent = "解析中…";
  try {
    const result = await api("/api/parse-user", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    $("user-info").textContent = `作者：${result.name || "(未取到名字)"}（id ${result.userId}）`;

    // 只看用户自己填的目录里已经有什么，不猜任何默认路径
    const chosenDir = $("out-dir").value.trim();
    if (!chosenDir) {
      log("还没选保存目录，点「选择…」挑一个再生成计划");
    } else {
      try {
        const info = await api("/api/inspect-dir", {
          method: "POST",
          body: JSON.stringify({ dir: chosenDir }),
        });
        if (!info.exists) {
          log(`保存目录还不存在，开始下载时会新建：${chosenDir}`);
        } else if (info.files === 0) {
          log(`保存目录是空的：${chosenDir}`);
        } else {
          log(
            `保存目录里已有 ${info.files} 个文件（${formatBytes(info.bytes)}），` +
              `重复的会跳过，只补缺的`
          );
        }
      } catch (err) {
        log("检查保存目录失败：" + err.message);
      }
    }
    log(`解析成功：id ${result.userId}`);
  } catch (err) {
    $("user-info").textContent = "解析失败：" + err.message;
  }
});

$("btn-pick").addEventListener("click", async () => {
  try {
    const result = await api("/api/pick-folder", { method: "POST" });
    if (result.ok && result.path) {
      $("out-dir").value = result.path;
      rememberDir(result.path);
    } else if (result.error) {
      log("选择目录失败：" + result.error);
    }
  } catch (err) {
    log("选择目录失败：" + err.message);
  }
});

// ------------------------------------------------------------------ 选项

function collectOptions() {
  return {
    outputDir: $("out-dir").value.trim(),
    includeIllust: $("inc-illust").checked,
    includeManga: $("inc-manga").checked,
    includeNovel: $("inc-novel").checked,
    includeUgoira: $("inc-ugoira").checked,
    r18: $("r18").value,
    timezone: $("tz").value,
    concurrency: Number($("concurrency").value) || 3,
    minIntervalMs: Number($("interval").value) || 600,
    dateFrom: $("date-from").value || null,
    dateTo: $("date-to").value || null,
  };
}

// ------------------------------------------------------------------ 计划

$("btn-plan").addEventListener("click", async () => {
  if (planPending) {
    log("计划正在生成中，请等它完成");
    return;
  }
  const url = $("user-url").value.trim();
  const options = collectOptions();
  if (!url) {
    log("先填作者主页链接");
    return;
  }
  if (!options.outputDir) {
    log("先选一个保存目录");
    return;
  }
  planPending = true;
  $("btn-plan").disabled = true;
  $("plan-status").textContent = "正在创建任务…";
  try {
    const result = await api("/api/plan", {
      method: "POST",
      body: JSON.stringify({ url, options }),
    });
    openJob(result.jobId);
    log("任务已创建：" + result.jobId);
  } catch (err) {
    planPending = false;
    $("btn-plan").disabled = false;
    $("plan-status").textContent = "失败：" + err.message;
    log("生成计划失败：" + err.message);
  }
});

$("btn-diag").addEventListener("click", async () => {
  const raw = $("user-url").value.trim();
  const match = raw.match(/(\d{3,})/);
  log("开始 API 诊断…");
  try {
    const report = await api("/api/diagnose", {
      method: "POST",
      body: JSON.stringify({ userId: match ? Number(match[1]) : null }),
    });
    log("诊断结果：\n" + JSON.stringify(report, null, 2));
  } catch (err) {
    log("诊断失败：" + err.message);
  }
});

// ------------------------------------------------------------------ 任务

function openJob(jobId) {
  currentJobId = jobId;
  lastItemsLength = -1;
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  const source = new EventSource(`/api/job/${jobId}/events`);
  eventSource = source;
  source.onmessage = (event) => {
    try {
      handleEvent(JSON.parse(event.data));
    } catch (err) {
      console.warn("bad event", err);
    }
  };
  source.onerror = () => {
    /* 浏览器会自己重连 */
  };
}

function handleEvent(event) {
  switch (event.type) {
    case "hello":
    case "status":
      scheduleRefresh();
      break;
    case "plan":
      if (event.phase === "pages") {
        $("plan-status").textContent = `正在抓作品分页 ${event.done}/${event.total}…`;
      } else if (event.phase === "novels") {
        $("plan-status").textContent = `正在抓小说清单（${event.works} 篇）…`;
      } else {
        $("plan-status").textContent = `正在抓作品详情（${event.works} 个）…`;
      }
      break;
    case "planned":
      planPending = false;
      $("btn-plan").disabled = false;
      $("plan-status").textContent = `计划就绪：${event.works} 个作品 / ${event.items} 个文件`;
      log(`计划就绪：${event.works} 个作品，${event.items} 个文件`);
      scheduleRefresh(true);
      break;
    case "item":
      scheduleRefresh();
      break;
    case "error":
      planPending = false;
      $("btn-plan").disabled = false;
      log("错误：" + event.message);
      break;
    case "finished":
      log("任务结束：" + event.status);
      scheduleRefresh(true);
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      break;
    default:
      break;
  }
}

function scheduleRefresh(immediate = false) {
  if (immediate) {
    if (refreshTimer) {
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    refreshJob();
    return;
  }
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refreshJob();
  }, 400);
}

async function refreshJob() {
  if (!currentJobId) return;
  try {
    const snapshot = await api(`/api/job/${currentJobId}?limit=500`);
    renderJob(snapshot);
  } catch (err) {
    console.warn(err);
  }
}

function renderJob(snapshot) {
  const counts = snapshot.counts || { total: 0, done: 0, skipped: 0, failed: 0 };
  $("st-status").textContent = snapshot.status;
  $("st-works").textContent = snapshot.works || 0;
  $("st-total").textContent = counts.total;
  $("st-done").textContent = counts.done;
  $("st-skipped").textContent = counts.skipped;
  $("st-failed").textContent = counts.failed;
  $("st-bytes").textContent = formatBytes(snapshot.bytes);

  const finished = counts.done + counts.skipped + counts.failed;
  const pct = counts.total ? (finished / counts.total) * 100 : 0;
  $("bar").style.width = pct.toFixed(1) + "%";

  $("btn-start").disabled = snapshot.status !== "ready";
  $("btn-pause").disabled = snapshot.status !== "running";
  $("btn-cancel").disabled = !["running", "paused"].includes(snapshot.status);
  $("btn-retry").disabled = counts.failed === 0;

  const items = snapshot.items || [];
  if (items.length !== lastItemsLength || snapshot.status === "done" || snapshot.status === "failed") {
    lastItemsLength = items.length;
    renderItems(items);
  }
}

function renderItems(items) {
  const body = $("items");
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="4" class="hint">还没有文件</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map((item) => {
      const cls = "s-" + (item.status || "pending");
      return `<tr>
        <td class="${cls}">${esc(item.status)}</td>
        <td>${esc(item.date)}</td>
        <td>${esc(item.title)}${item.pageCount > 1 ? ` <span class="hint">${item.pageIndex + 1}/${item.pageCount}</span>` : ""}</td>
        <td class="path">${esc(basename(item.path))}${item.error ? `<br><span class="s-failed">${esc(item.error)}</span>` : ""}</td>
      </tr>`;
    })
    .join("");
}

$("btn-start").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await api(`/api/job/${currentJobId}/start`, { method: "POST" });
    log("开始下载");
    scheduleRefresh(true);
  } catch (err) {
    log("开始失败：" + err.message);
  }
});

$("btn-pause").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await api(`/api/job/${currentJobId}/pause`, { method: "POST" });
    log("已暂停");
    scheduleRefresh(true);
  } catch (err) {
    log("暂停失败：" + err.message);
  }
});

$("btn-cancel").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await api(`/api/job/${currentJobId}/cancel`, { method: "POST" });
    log("已取消");
    scheduleRefresh(true);
  } catch (err) {
    log("取消失败：" + err.message);
  }
});

$("btn-retry").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await api(`/api/job/${currentJobId}/retry-failed`, { method: "POST" });
    log("重试失败项");
    scheduleRefresh(true);
  } catch (err) {
    log("重试失败：" + err.message);
  }
});

// ------------------------------------------------------------------ 启动

const LAST_DIR_KEY = "pixiv-dl:outdir";

function rememberDir(value) {
  try {
    if (value) localStorage.setItem(LAST_DIR_KEY, value);
  } catch {
    /* 隐私模式下 localStorage 可能不可用 */
  }
}

function restoreDir() {
  try {
    const saved = localStorage.getItem(LAST_DIR_KEY);
    if (saved) $("out-dir").value = saved;
  } catch {
    /* ignore */
  }
}

$("out-dir").addEventListener("input", () => {
  rememberDir($("out-dir").value.trim());
});

restoreDir();
refreshAuth();
log("界面就绪。先登录，然后填作者主页。");
