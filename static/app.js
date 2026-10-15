"use strict";

(() => {
  const state = {
    csrf: "",
    studentId: "",
    isAdmin: false,
    status: null,
    adminControl: null,
    activeKind: "daily",
    entries: [],
    page: 0,
    pageSize: 50,
    etags: new Map(),
    leaderboardCache: new Map(),
    replayFrames: [],
    replayData: null,
    replayIndex: 0,
    playTimer: null,
    replays: [],
  };

  const byId = (id) => document.getElementById(id);
  const ui = {
    loginOpen: byId("login-open"),
    logout: byId("logout-button"),
    loginDialog: byId("login-dialog"),
    loginForm: byId("login-form"),
    loginError: byId("login-error"),
    uploadForm: byId("upload-form"),
    fileInput: byId("bot-file"),
    dropZone: byId("drop-zone"),
    submitButton: byId("submit-button"),
    selectedFile: byId("selected-file"),
    submitFeedback: byId("submit-feedback"),
    submissionMeta: byId("submission-meta"),
    leaderboardRows: byId("leaderboard-rows"),
    leaderboardSearch: byId("leaderboard-search"),
    leaderboardError: byId("leaderboard-error"),
    replayList: byId("replay-list"),
    replayBoard: byId("replay-board"),
    slider: byId("replay-slider"),
    playToggle: byId("play-toggle"),
    toast: byId("toast"),
    adminTools: byId("admin-tools"),
    adminFeedback: byId("admin-feedback"),
    adminLockToggle: byId("admin-lock-toggle"),
    adminScore: byId("admin-score"),
    adminPublish: byId("admin-publish"),
    adminInviteForm: byId("admin-invite-form"),
    adminInviteButton: byId("admin-invite-button"),
    adminInviteFeedback: byId("admin-invite-feedback"),
    adminNavLink: byId("admin-nav-link"),
    adminAccessMessage: byId("admin-access-message"),
    copyBotSample: byId("copy-bot-sample"),
  };

  function cookieValue(name) {
    const prefix = `${name}=`;
    const item = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : "";
  }

  function escapeText(value) {
    return String(value ?? "");
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.csrf) {
      state.csrf = state.csrf || cookieValue("ottv2_csrf");
      headers.set("X-CSRF-Token", state.csrf);
    }
    const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
    if (response.status === 304) return { notModified: true, response };
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const error = new Error(data?.error?.message || `Yêu cầu lỗi (${response.status})`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return { data, response };
  }

  function showToast(message, isError = false) {
    ui.toast.textContent = message;
    ui.toast.classList.toggle("is-error", isError);
    ui.toast.classList.add("is-visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => ui.toast.classList.remove("is-visible"), 3200);
  }

  function setFeedback(element, message, isError = false) {
    element.textContent = message;
    element.classList.remove("hidden");
    element.classList.toggle("is-error", isError);
  }

  function statusLabel(status) {
    return ({ OPEN: "Đang mở", LOCKED: "Đã khóa", RUNNING: "Đang chấm", PUBLISHED: "Đã công bố" })[status] || "Đang kết nối";
  }

  async function loadStatus() {
    try {
      const { data } = await api("/api/status");
      state.status = data;
      const statusText = statusLabel(data.status);
      const statusLabelElement = byId("status-label");
      const statusLight = byId("status-light");
      if (statusLabelElement) statusLabelElement.textContent = statusText;
      if (statusLight) statusLight.className = `status-light ${data.status === "OPEN" || data.status === "PUBLISHED" ? "is-open" : data.status === "RUNNING" ? "is-running" : "is-locked"}`;
      const eventDay = byId("event-day");
      if (eventDay) eventDay.textContent = `Ngày ${data.day_no} · Vòng ${data.round_no}`;
      const progressDay = byId("progress-day");
      if (progressDay) progressDay.textContent = `Ngày ${data.day_no} / 35`;
      const progressRound = byId("progress-round");
      if (progressRound) progressRound.textContent = `Vòng ${data.round_no} / 5`;
      const roundNumber = byId("tab-round-number");
      if (roundNumber) roundNumber.textContent = data.round_no;
      const seasonProgress = byId("season-progress");
      if (seasonProgress) seasonProgress.style.width = `${Math.min(100, (data.day_no / 35) * 100)}%`;
      updateSubmitLock();
    } catch (error) {
      const statusLabelElement = byId("status-label");
      const statusLight = byId("status-light");
      if (statusLabelElement) statusLabelElement.textContent = "Mất kết nối";
      if (statusLight) statusLight.className = "status-light is-locked";
    }
  }

  function updateSubmitLock() {
    if (!ui.submitButton) return;
    const locked = !state.status || ["LOCKED", "RUNNING"].includes(state.status.status);
    const loggedIn = Boolean(state.studentId);
    const cannotSubmit = state.isAdmin;
    ui.submitButton.disabled = locked || !loggedIn || cannotSubmit || !ui.fileInput.files.length;
    const statusElement = byId("submission-status");
    if (statusElement) {
      statusElement.textContent = cannotSubmit
        ? "ADMIN KHÔNG NỘP BOT"
        : !loggedIn
          ? "CẦN ĐĂNG NHẬP"
          : locked
            ? "ĐANG KHÓA"
            : "SẴN SÀNG";
      statusElement.style.color = cannotSubmit || locked ? "var(--coral)" : loggedIn ? "var(--jade)" : "var(--muted)";
    }
  }

  function renderLoadingRows() {
    ui.leaderboardRows.replaceChildren();
    for (let index = 0; index < 5; index += 1) {
      const row = document.createElement("div");
      row.className = "loading-row";
      for (let cell = 0; cell < 3; cell += 1) row.append(document.createElement("span"));
      ui.leaderboardRows.append(row);
    }
    ui.leaderboardRows.setAttribute("aria-busy", "true");
  }

  function leaderboardPath() {
    if (state.activeKind === "round") return `/api/leaderboard/round?round_no=${state.status?.round_no || 1}`;
    return `/api/leaderboard/${state.activeKind}`;
  }

  async function loadLeaderboard(force = false) {
    if (!ui.leaderboardRows) return;
    const path = leaderboardPath();
    const cacheKey = path;
    const headers = {};
    const etag = state.etags.get(cacheKey);
    if (!force && etag) headers["If-None-Match"] = etag;
    ui.leaderboardError.classList.add("hidden");
    renderLoadingRows();
    try {
      const { data, response, notModified } = await api(path, { headers });
      if (notModified) {
        state.entries = state.leaderboardCache.get(cacheKey) || [];
      } else {
        state.entries = Array.isArray(data?.entries) ? data.entries : [];
        state.leaderboardCache.set(cacheKey, state.entries);
        const responseEtag = response.headers.get("etag");
        if (responseEtag) state.etags.set(cacheKey, responseEtag);
        const generated = data?.meta?.generated_at;
        const updatedLabel = byId("leaderboard-updated");
        if (updatedLabel) updatedLabel.textContent = generated ? `Cập nhật ${formatTime(generated)}` : "Bảng xếp hạng mới nhất";
      }
      state.page = 0;
      renderLeaderboard();
    } catch (error) {
      if (state.leaderboardCache.has(cacheKey)) {
        state.entries = state.leaderboardCache.get(cacheKey);
        renderLeaderboard();
      } else {
        ui.leaderboardRows.replaceChildren();
        ui.leaderboardError.textContent = error.message;
        ui.leaderboardError.classList.remove("hidden");
        ui.leaderboardRows.setAttribute("aria-busy", "false");
      }
    }
  }

  function renderLeaderboard() {
    if (!ui.leaderboardRows || !ui.leaderboardSearch) return;
    const query = ui.leaderboardSearch.value.trim().toLowerCase();
    const filtered = state.entries.filter((entry) => escapeText(entry.student_id).toLowerCase().includes(query));
    const pageCount = Math.max(1, Math.ceil(filtered.length / state.pageSize));
    state.page = Math.min(state.page, pageCount - 1);
    const visible = filtered.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
    ui.leaderboardRows.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("p");
      empty.className = "muted-text";
      empty.textContent = query ? "Không tìm thấy thí sinh phù hợp." : "Chưa có dữ liệu xếp hạng.";
      ui.leaderboardRows.append(empty);
    }
    visible.forEach((entry, index) => {
      const row = document.createElement("div");
      row.className = "table-row leader-row";
      row.setAttribute("role", "row");
      row.style.animationDelay = `${Math.min(index, 8) * 18}ms`;
      const isSelf = Boolean(state.studentId && entry.student_id === state.studentId);
      if (isSelf) row.classList.add("is-self");

      const rank = document.createElement("span");
      rank.className = "rank-cell";
      rank.setAttribute("role", "cell");
      if (Number(entry.rank) <= 3) {
        const medal = document.createElement("span");
        medal.className = "rank-medal";
        medal.textContent = Number(entry.rank) === 1 ? "✳" : Number(entry.rank) === 2 ? "✦" : "✧";
        rank.append(medal);
      }
      rank.append(document.createTextNode(String(entry.rank ?? "—")));

      const student = document.createElement("span");
      student.className = "student-cell";
      student.setAttribute("role", "cell");
      const avatar = document.createElement("span");
      avatar.className = "avatar-mark";
      avatar.textContent = escapeText(entry.student_id).slice(0, 1).toUpperCase();
      const identifier = document.createElement("span");
      identifier.className = "student-id";
      identifier.textContent = escapeText(entry.student_id);
      student.append(avatar, identifier);
      if (isSelf) {
        const you = document.createElement("span");
        you.className = "you-mark";
        you.textContent = "BẠN";
        student.append(you);
      }

      const score = document.createElement("span");
      score.className = "numeric-cell";
      score.setAttribute("role", "cell");
      score.textContent = state.activeKind === "daily" ? String(entry.latest_rank ?? entry.rank) : Number(entry.average_rank ?? entry.rank).toFixed(1);
      const days = document.createElement("span");
      days.className = "numeric-cell";
      days.setAttribute("role", "cell");
      days.textContent = String(entry.days_played ?? "—");
      const trend = document.createElement("span");
      trend.className = "trend-cell";
      trend.setAttribute("role", "cell");
      trend.textContent = entry.absent ? "Vắng" : ({ up: "▲", down: "▼", flat: "·" })[entry.trend] || "—";
      trend.setAttribute("aria-label", entry.absent ? "Vắng" : ({ up: "Tăng hạng", down: "Giảm hạng", flat: "Không đổi hạng" })[entry.trend] || "Chưa có xu hướng");
      row.append(rank, student, score, days, trend);
      ui.leaderboardRows.append(row);
    });
    ui.leaderboardRows.setAttribute("aria-busy", "false");
    byId("leaderboard-count").textContent = `${filtered.length.toLocaleString("vi-VN")} thí sinh`;
    byId("page-label").textContent = `Trang ${state.page + 1} / ${pageCount}`;
    byId("page-prev").disabled = state.page === 0;
    byId("page-next").disabled = state.page >= pageCount - 1;
  }

  function formatTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "vừa xong" : new Intl.DateTimeFormat("vi-VN", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }).format(date);
  }

  function readCsrf() {
    state.csrf = state.csrf || cookieValue("ottv2_csrf");
    return state.csrf;
  }

  function setLoggedIn(studentId, isAdmin = false) {
    state.studentId = studentId;
    state.isAdmin = Boolean(isAdmin);
    try {
      sessionStorage.setItem("ottv2-user", JSON.stringify({
        student_id: studentId,
        is_admin: state.isAdmin,
      }));
    } catch (error) {
      showToast("Không thể lưu trạng thái đăng nhập trên trình duyệt.", true);
    }
    ui.loginOpen?.classList.add("hidden");
    ui.logout?.classList.remove("hidden");
    ui.adminTools?.classList.toggle("hidden", !state.isAdmin);
    ui.adminNavLink?.classList.toggle("hidden", !state.isAdmin);
    ui.adminAccessMessage?.classList.toggle("hidden", state.isAdmin);
    updateSubmitLock();
    loadPrivateData();
    if (state.isAdmin && ui.adminTools) loadAdminControl();
    renderLeaderboard();
  }

  function setLoggedOut() {
    state.studentId = "";
    state.isAdmin = false;
    state.csrf = "";
    try {
      sessionStorage.removeItem("ottv2-user");
    } catch (error) {
      showToast("Không thể xóa trạng thái đăng nhập khỏi trình duyệt.", true);
    }
    ui.loginOpen?.classList.remove("hidden");
    ui.logout?.classList.add("hidden");
    ui.adminTools?.classList.add("hidden");
    ui.adminNavLink?.classList.add("hidden");
    ui.adminAccessMessage?.classList.remove("hidden");
    if (ui.adminInviteFeedback) {
      ui.adminInviteFeedback.textContent = "";
      ui.adminInviteFeedback.classList.remove("is-error");
    }
    state.adminControl = null;
    ui.submissionMeta?.classList.add("hidden");
    const personalDays = byId("personal-days");
    if (personalDays) personalDays.textContent = "Đăng nhập để xem kết quả";
    const personalRank = byId("personal-rank");
    if (personalRank) personalRank.textContent = "—";
    byId("rank-chart")?.replaceChildren();
    const recentGames = byId("recent-games-list");
    if (recentGames) {
      recentGames.replaceChildren();
      recentGames.append(makeText("p", "muted-text", "Đăng nhập để xem ván đấu."));
    }
    const replayQuota = byId("replay-quota");
    if (replayQuota) replayQuota.textContent = "0 / 0";
    ui.replayList?.replaceChildren(makeText("p", "muted-text", "Đăng nhập để xem replay."));
    state.replayFrames = [];
    state.replayData = null;
    drawBoard(null);
    updateSubmitLock();
    renderLeaderboard();
  }

  async function loadPrivateData() {
    if (!state.studentId) return;
    const loaders = [];
    if (ui.submissionMeta && !state.isAdmin) loaders.push(loadSubmission());
    if (byId("personal-rank")) loaders.push(loadPersonalResults());
    if (ui.replayList) loaders.push(loadReplays());
    await Promise.allSettled(loaders);
  }

  async function loadSubmission() {
    if (!ui.submissionMeta) return;
    try {
      const { data } = await api("/api/my/submission");
      ui.submissionMeta.replaceChildren();
      const version = makeText("span", "", `Phiên bản ${data.version} · ${formatTime(data.submitted_at)}`);
      const digest = makeText("span", "", `SHA-256 · ${escapeText(data.sha256).slice(0, 16)}…`);
      const download = document.createElement("a");
      download.href = data.download_url;
      download.textContent = "Tải bài của bạn ↓";
      ui.submissionMeta.append(version, digest, download);
      ui.submissionMeta.classList.remove("hidden");
      const status = byId("submission-status");
      if (status) status.textContent = "ĐÃ CÓ BÀI";
    } catch (error) {
      if (error.status !== 404) showToast(error.message, true);
    }
  }

  async function loadPersonalResults() {
    if (!byId("personal-rank")) return;
    try {
      const { data } = await api("/api/my/results");
      state.isAdmin = Boolean(data.is_admin);
      ui.adminTools?.classList.toggle("hidden", !state.isAdmin);
      ui.adminNavLink?.classList.toggle("hidden", !state.isAdmin);
      if (state.isAdmin && ui.adminTools) loadAdminControl();
      renderRankChart(data.results || []);
      const played = (data.results || []).filter((result) => !result.absent);
      const latest = played.at(-1);
      byId("personal-rank").textContent = latest ? `#${latest.rank}` : "—";
      byId("personal-days").textContent = `${played.length} ngày đã đấu`;
      const recent = [...(data.results || [])].reverse().slice(0, 3);
      const list = byId("recent-games-list");
      list.replaceChildren();
      if (!recent.length) list.append(makeText("p", "muted-text", "Chưa có kết quả được công bố."));
      recent.forEach((result) => {
        const row = document.createElement("div");
        row.className = "recent-game";
        row.append(makeText("span", "", `Ngày ${result.day_no}${result.absent ? " · Vắng" : ""}`));
        row.append(makeText("strong", "", result.absent ? "—" : `#${result.rank}`));
        list.append(row);
      });
    } catch (error) {
      if (error.status !== 401) showToast(error.message, true);
    }
  }

  async function loadAdminControl() {
    if (!state.isAdmin || !ui.adminTools) return;
    try {
      const { data } = await api("/admin/control");
      state.adminControl = data;
      byId("admin-day-label").textContent = `Ngày ${data.day_no} · Vòng ${data.round_no}`;
      byId("admin-run-status").textContent = data.status === "IDLE" ? "CHƯA CÓ LƯỢT CHẤM" : data.status;
      byId("admin-run-status").classList.toggle("is-running", data.status === "RUNNING");
      ui.adminLockToggle.textContent = data.locked ? "Mở nhận bài" : "Khóa nhận bài";
      ui.adminLockToggle.classList.toggle("is-locked", data.locked);
      ui.adminScore.disabled = !data.locked || data.status === "RUNNING";
      ui.adminPublish.disabled = !["COMPLETED", "PARTIAL"].includes(data.status) || data.published;
      ui.adminFeedback.textContent = data.locked
        ? data.lock_reason || "Đang khóa nhận bài."
        : "Đang nhận bài. Khóa thủ công trước khi chấm.";
    } catch (error) {
      ui.adminFeedback.textContent = error.message;
    }
  }

  async function handleAdminAction(path, body, successMessage) {
    const options = { method: "POST", csrf: true };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    ui.adminFeedback.textContent = "Đang xử lý…";
    try {
      const { data } = await api(path, options);
      ui.adminFeedback.textContent = successMessage;
      await loadStatus();
      await loadAdminControl();
      if (data.run_id) showToast(`Đã tạo lượt chấm ${data.run_id.slice(0, 8)}.`);
      else showToast(successMessage);
    } catch (error) {
      ui.adminFeedback.textContent = error.message;
      showToast(error.message, true);
    }
  }

  async function handleInviteParticipant(event) {
    event.preventDefault();
    const form = new FormData(ui.adminInviteForm);
    ui.adminInviteButton.disabled = true;
    ui.adminInviteFeedback.classList.remove("is-error");
    ui.adminInviteFeedback.textContent = "Đang tạo tài khoản…";
    try {
      const { data } = await api("/admin/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          student_id: form.get("student_id"),
          password: form.get("password"),
        }),
        csrf: true,
      });
      ui.adminInviteFeedback.textContent = `Đã tạo tài khoản ${data.student_id}. Bạn có thể gửi thông tin đăng nhập đã đặt cho thí sinh.`;
      ui.adminInviteForm.reset();
    } catch (error) {
      ui.adminInviteFeedback.textContent = error.message;
      ui.adminInviteFeedback.classList.add("is-error");
    } finally {
      ui.adminInviteButton.disabled = false;
    }
  }

  function renderRankChart(results) {
    const container = byId("rank-chart");
    if (!container) return;
    container.replaceChildren();
    const values = results.filter((result) => Number.isFinite(Number(result.rank))).slice(-12);
    if (!values.length) {
      container.append(makeText("p", "muted-text", "Biểu đồ sẽ xuất hiện khi có rank ngày."));
      return;
    }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 640 125");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Biểu đồ rank theo ngày, rank thấp hơn là tốt hơn");
    const min = Math.min(...values.map((item) => Number(item.rank)));
    const max = Math.max(...values.map((item) => Number(item.rank)));
    const span = Math.max(1, max - min);
    [25, 60, 95].forEach((y) => {
      const line = document.createElementNS(svg.namespaceURI, "line");
      line.setAttribute("x1", "28"); line.setAttribute("x2", "630"); line.setAttribute("y1", String(y)); line.setAttribute("y2", String(y)); line.setAttribute("class", "chart-gridline");
      svg.append(line);
    });
    const points = values.map((item, index) => {
      const x = values.length === 1 ? 320 : 32 + index * (592 / (values.length - 1));
      const y = 98 - ((Number(item.rank) - min) / span) * 66;
      return { x, y, item };
    });
    const path = document.createElementNS(svg.namespaceURI, "polyline");
    path.setAttribute("class", "chart-line");
    path.setAttribute("points", points.map((point) => `${point.x},${point.y}`).join(" "));
    svg.append(path);
    points.forEach((point) => {
      const dot = document.createElementNS(svg.namespaceURI, "circle");
      dot.setAttribute("class", "chart-dot"); dot.setAttribute("cx", String(point.x)); dot.setAttribute("cy", String(point.y)); dot.setAttribute("r", "4");
      const title = document.createElementNS(svg.namespaceURI, "title");
      title.textContent = `Ngày ${point.item.day_no}: hạng ${point.item.rank}`;
      dot.append(title); svg.append(dot);
      if (point.item.day_no === values[0].day_no || point.item.day_no === values.at(-1).day_no) {
        const label = document.createElementNS(svg.namespaceURI, "text");
        label.setAttribute("class", "chart-label"); label.setAttribute("x", String(point.x)); label.setAttribute("y", "119"); label.setAttribute("text-anchor", "middle"); label.textContent = `N${point.item.day_no}`;
        svg.append(label);
      }
    });
    container.append(svg);
  }

  async function loadReplays() {
    if (!state.studentId || !ui.replayList) return;
    const day = state.status?.day_no || 1;
    try {
      const { data } = await api(`/api/my/replays?day=${encodeURIComponent(day)}`);
      state.replays = data.replays || [];
      byId("replay-quota").textContent = `${data.selected ?? state.replays.length} / ${data.n_games ?? state.replays.length}`;
      renderReplayList();
    } catch (error) {
      ui.replayList.replaceChildren(makeText("p", "muted-text", error.message));
    }
  }

  function renderReplayList() {
    ui.replayList.replaceChildren();
    if (!state.replays.length) {
      ui.replayList.append(makeText("p", "muted-text", "Chưa có replay được cấp cho ngày này."));
      return;
    }
    state.replays.forEach((replay) => {
      const button = document.createElement("button");
      button.className = "replay-item";
      button.type = "button";
      button.dataset.gameId = replay.game_id;
      button.setAttribute("aria-label", `${replay.stage}, ${replay.result}, ${replay.plies} nước`);
      button.append(makeText("span", "replay-item-title", replay.stage));
      button.append(makeText("span", "replay-item-meta", `${replay.opponent} · ${replay.plies} ply`));
      button.append(makeText("span", "replay-item-outcome", replay.result));
      button.addEventListener("click", () => loadReplay(replay.game_id, button));
      ui.replayList.append(button);
    });
  }

  async function loadReplay(gameId, button) {
    stopReplay();
    try {
      const { data } = await api(`/api/replay/${encodeURIComponent(gameId)}`);
      state.replayData = data;
      state.replayFrames = data.frames || [];
      state.replayIndex = 0;
      ui.replayList.querySelectorAll(".replay-item").forEach((item) => item.classList.toggle("is-selected", item === button));
      const candidate = state.replays.find((item) => item.game_id === gameId);
      byId("replay-stage-label").textContent = candidate?.stage || "REPLAY";
      byId("replay-match-title").textContent = `${candidate?.opponent || "Ván đấu"} · Ngày ${candidate?.day_no || "—"}`;
      byId("replay-result").textContent = candidate?.result?.toLocaleUpperCase("vi-VN") || (data.result === "DRAW" ? "HÒA" : data.result);
      byId("win-reason").textContent = data.win_reason || "Ván đấu đã kết thúc.";
      byId("captured-label").textContent = `Đã ăn: P1 ${data.captured_p1} · P2 ${data.captured_p2}`;
      ui.slider.max = String(Math.max(0, state.replayFrames.length - 1));
      ui.slider.value = "0";
      ui.slider.disabled = state.replayFrames.length < 2;
      ui.playToggle.disabled = state.replayFrames.length < 2;
      byId("step-back").disabled = true;
      byId("step-forward").disabled = state.replayFrames.length < 2;
      drawCurrentFrame();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function drawCurrentFrame() {
    if (!ui.replayBoard) return;
    if (!state.replayFrames.length) return drawBoard(null);
    const frame = state.replayFrames[state.replayIndex];
    drawBoard(frame);
    ui.slider.value = String(state.replayIndex);
    byId("ply-label").textContent = `${frame.ply} / ${state.replayFrames.length - 1}`;
    byId("step-back").disabled = state.replayIndex === 0;
    byId("step-forward").disabled = state.replayIndex >= state.replayFrames.length - 1;
    ui.playToggle.textContent = state.playTimer ? "Ⅱ" : "▶";
    ui.playToggle.setAttribute("aria-label", state.playTimer ? "Tạm dừng replay" : "Phát replay");
  }

  function drawBoard(frame) {
    if (!ui.replayBoard) return;
    const board = frame?.board || Array.from({ length: 9 }, () => Array(9).fill(null));
    const move = frame?.move;
    const from = move?.[0];
    const to = move?.[1];
    const columns = byId("board-columns");
    const rows = byId("board-rows");
    columns.replaceChildren(); rows.replaceChildren(); ui.replayBoard.replaceChildren();
    "abcdefghi".split("").forEach((letter) => columns.append(makeText("span", "", letter)));
    for (let row = 0; row < 9; row += 1) {
      rows.append(makeText("span", "", String(row + 1)));
      for (let col = 0; col < 9; col += 1) {
        const cell = document.createElement("div");
        cell.className = "board-cell";
        cell.setAttribute("role", "gridcell");
        cell.setAttribute("aria-label", `Ô ${"abcdefghi"[col]}${row + 1}${board[row][col] ? `, quân ${board[row][col]}` : " trống"}`);
        if (row === 0 && col === 0) cell.classList.add("home-one");
        if (row === 8 && col === 8) cell.classList.add("home-two");
        if (from && from[0] === row && from[1] === col) cell.classList.add("is-last-from");
        if (to && to[0] === row && to[1] === col) cell.classList.add("is-last-to");
        const piece = board[row][col];
        if (piece) {
          const token = document.createElement("span");
          token.className = `piece ${piece[0] === "1" ? "player-one" : "player-two"}`;
          token.textContent = piece[1];
          token.setAttribute("aria-hidden", "true");
          cell.append(token);
        }
        ui.replayBoard.append(cell);
      }
    }
    byId("ply-label").textContent = `${frame?.ply ?? 0} / ${state.replayFrames.length ? state.replayFrames.length - 1 : 0}`;
  }

  function stepReplay(delta) {
    if (!state.replayFrames.length) return;
    state.replayIndex = Math.max(0, Math.min(state.replayFrames.length - 1, state.replayIndex + delta));
    drawCurrentFrame();
    if (state.replayIndex === state.replayFrames.length - 1) stopReplay();
  }

  function stopReplay() {
    if (state.playTimer) window.clearInterval(state.playTimer);
    state.playTimer = null;
    if (state.replayFrames.length) drawCurrentFrame();
  }

  function toggleReplay() {
    if (state.playTimer) return stopReplay();
    if (state.replayIndex >= state.replayFrames.length - 1) state.replayIndex = 0;
    state.playTimer = window.setInterval(() => {
      if (state.replayIndex >= state.replayFrames.length - 1) return stopReplay();
      state.replayIndex += 1;
      drawCurrentFrame();
    }, Number(byId("play-speed").value));
    drawCurrentFrame();
  }

  function makeText(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = escapeText(text);
    return element;
  }

  function selectFile(file) {
    if (!file || !ui.fileInput || !ui.selectedFile || !ui.submitButton || !ui.dropZone) return;
    const isPython = file.name.toLowerCase().endsWith(".py");
    const sizeOk = file.size <= 100 * 1024;
    ui.fileInput.setCustomValidity(!isPython ? "Chỉ chọn tệp .py" : !sizeOk ? "Tệp vượt quá 100 KB" : "");
    ui.selectedFile.textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
    ui.selectedFile.classList.remove("hidden");
    ui.submitButton.disabled = !isPython || !sizeOk || !state.studentId || !state.status || ["LOCKED", "RUNNING"].includes(state.status.status);
    ui.dropZone.classList.toggle("has-error", !isPython || !sizeOk);
  }

  async function handleLogin(event) {
    event.preventDefault();
    ui.loginError.classList.add("hidden");
    const form = new FormData(ui.loginForm);
    try {
      const { data } = await api("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: form.get("student_id"), password: form.get("password") }),
      });
      state.csrf = data.csrf_token;
      ui.loginDialog.close();
      ui.loginForm.reset();
      setLoggedIn(data.student_id, data.is_admin);
      showToast(`Xin chào, ${data.student_id}`);
    } catch (error) {
      ui.loginError.textContent = error.message;
      ui.loginError.classList.remove("hidden");
    }
  }

  async function handleLogout() {
    try {
      await api("/api/auth/logout", { method: "POST", csrf: true });
      setLoggedOut();
      showToast("Bạn đã đăng xuất.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function handleUpload(event) {
    event.preventDefault();
    if (state.isAdmin) {
      showToast("Tài khoản quản trị không thể nộp bot.", true);
      return;
    }
    const file = ui.fileInput.files[0];
    if (!file) return;
    if (!readCsrf()) return showToast("Hãy đăng nhập lại để nộp bài.", true);
    ui.submitButton.disabled = true;
    ui.submitButton.querySelector("span").textContent = "Đang kiểm tra…";
    ui.submitFeedback.classList.add("hidden");
    const form = new FormData();
    form.append("file", file);
    try {
      const { data } = await api("/api/submit", { method: "POST", body: form, csrf: true });
      setFeedback(ui.submitFeedback, `✓ Bài hợp lệ · phiên bản ${data.version} · SHA ${data.sha256.slice(0, 12)}`);
      await loadSubmission();
      ui.uploadForm.reset();
      ui.selectedFile.classList.add("hidden");
      showToast("Bài nộp đã được ghi nhận.");
    } catch (error) {
      const details = error.data?.error?.details;
      const suffix = Array.isArray(details) ? `: ${details.join(" · ")}` : "";
      setFeedback(ui.submitFeedback, `✕ ${error.message}${suffix}`, true);
    } finally {
      ui.submitButton.querySelector("span").textContent = "Nộp bài";
      updateSubmitLock();
    }
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("ottv2-theme", theme);
    const themeToggle = byId("theme-toggle");
    if (themeToggle) {
      themeToggle.setAttribute("aria-label", theme === "dark" ? "Bật giao diện sáng" : "Bật giao diện tối");
      themeToggle.textContent = theme === "dark" ? "☼" : "◐";
    }
  }

  function bindEvents() {
    const on = (element, eventName, handler) => element?.addEventListener(eventName, handler);
    on(ui.loginOpen, "click", () => ui.loginDialog.showModal());
    on(byId("login-close"), "click", () => ui.loginDialog.close());
    on(ui.loginDialog, "click", (event) => { if (event.target === ui.loginDialog) ui.loginDialog.close(); });
    on(ui.loginForm, "submit", handleLogin);
    on(ui.logout, "click", handleLogout);
    on(ui.uploadForm, "submit", handleUpload);
    on(ui.fileInput, "change", () => selectFile(ui.fileInput.files[0]));
    ["dragenter", "dragover"].forEach((name) => on(ui.dropZone, name, (event) => { event.preventDefault(); ui.dropZone.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach((name) => on(ui.dropZone, name, (event) => { event.preventDefault(); ui.dropZone.classList.remove("is-dragging"); }));
    on(ui.dropZone, "drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      ui.fileInput.files = transfer.files;
      selectFile(file);
    });
    document.querySelectorAll(".tab-button").forEach((button) => button.addEventListener("click", () => {
      state.activeKind = button.dataset.kind;
      state.page = 0;
      document.querySelectorAll(".tab-button").forEach((item) => {
        const active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", String(active));
      });
      loadLeaderboard();
    }));
    on(ui.leaderboardSearch, "input", () => { state.page = 0; renderLeaderboard(); });
    on(byId("page-prev"), "click", () => { state.page = Math.max(0, state.page - 1); renderLeaderboard(); });
    on(byId("page-next"), "click", () => { state.page += 1; renderLeaderboard(); });
    on(byId("reload-personal"), "click", loadPrivateData);
    on(ui.adminLockToggle, "click", () => {
      const locked = !state.adminControl?.locked;
      handleAdminAction(
        "/admin/submissions/lock",
        { locked },
        locked ? "Đã khóa nhận bài." : "Đã mở nhận bài.",
      );
    });
    on(ui.adminScore, "click", () => {
      const day = state.adminControl?.day_no || state.status?.day_no || 1;
      handleAdminAction(`/admin/score?day=${day}`, undefined, "Lượt chấm đã được xếp hàng.");
    });
    on(ui.adminPublish, "click", () => {
      const day = state.adminControl?.day_no || state.status?.day_no || 1;
      handleAdminAction(`/admin/publish?day=${day}`, undefined, "Bảng xếp hạng đã được công bố.");
    });
    on(ui.adminInviteForm, "submit", handleInviteParticipant);
    on(byId("step-back"), "click", () => stepReplay(-1));
    on(byId("step-forward"), "click", () => stepReplay(1));
    on(ui.playToggle, "click", toggleReplay);
    on(ui.slider, "input", () => { state.replayIndex = Number(ui.slider.value); drawCurrentFrame(); });
    on(byId("play-speed"), "change", () => { if (state.playTimer) { stopReplay(); toggleReplay(); } });
    on(byId("theme-toggle"), "click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
    on(ui.copyBotSample, "click", async () => {
      const source = byId("bot-sample").innerText;
      try {
        await navigator.clipboard.writeText(source);
        ui.copyBotSample.textContent = "Đã sao chép";
        window.setTimeout(() => { ui.copyBotSample.textContent = "Sao chép mã"; }, 1800);
      } catch (error) {
        showToast("Trình duyệt không cho phép sao chép tự động.", true);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.target.matches("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === "/" && ui.leaderboardSearch && !ui.loginDialog?.open) { event.preventDefault(); ui.leaderboardSearch.focus(); }
      if (event.key === "ArrowLeft") stepReplay(-1);
      if (event.key === "ArrowRight") stepReplay(1);
      if (event.code === "Space" && state.replayFrames.length) { event.preventDefault(); toggleReplay(); }
    });
  }

  async function initialize() {
    setTheme(localStorage.getItem("ottv2-theme") || "light");
    bindEvents();
    state.csrf = cookieValue("ottv2_csrf");
    await loadStatus();
    if (ui.leaderboardRows) await loadLeaderboard();
    window.setInterval(loadStatus, 30000);
    let cachedUser = null;
    try {
      cachedUser = JSON.parse(sessionStorage.getItem("ottv2-user") || "null");
    } catch (error) {
      showToast("Không đọc được trạng thái đăng nhập đã lưu.", true);
    }
    if (
      cachedUser &&
      typeof cachedUser.student_id === "string" &&
      typeof cachedUser.is_admin === "boolean"
    ) {
      setLoggedIn(cachedUser.student_id, cachedUser.is_admin);
    }
    try {
      const { data } = await api("/api/my/results");
      setLoggedIn(data.student_id, data.is_admin);
    } catch (error) {
      if (error.status === 401) {
        setLoggedOut();
      } else if (!cachedUser) {
        showToast("Chưa kiểm tra được phiên đăng nhập. Kiểm tra kết nối rồi tải lại.", true);
      } else {
        showToast("Đang giữ phiên đăng nhập; chưa kết nối được máy chủ.", true);
      }
    }
  }

  initialize();
})();