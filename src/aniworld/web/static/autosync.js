/* Auto-Sync page: status, last report and the synced series. */

(function () {
  const el = (id) => document.getElementById(id);

  const syncNowBtn = el("syncNowBtn");
  const syncedList = el("syncedList");

  // While a run is going, poll often enough to feel live
  const IDLE_POLL = 30000;
  const RUNNING_POLL = 3000;

  const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let timer = null;
  // Rebuilding the list while someone types into it would eat the input
  let editing = false;

  const STATUS_CLASS = {
    queued: "status-completed",
    "up-to-date": "status-queued",
    skipped: "status-cancelled",
    error: "status-failed"
  };

  const STATUS_LABELS = {
    queued: "Queued",
    "up-to-date": "Up to date",
    skipped: "Skipped",
    error: "Error"
  };

  function statusLabel(status) {
    const key = String(status).replace(/-/g, "_");
    return t(`autosync.status.${key}`, STATUS_LABELS[status] || status);
  }

  function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "-";
    return date.toLocaleString();
  }

  function schedule(running) {
    clearTimeout(timer);
    timer = setTimeout(load, running ? RUNNING_POLL : IDLE_POLL);
  }

  function resultDetail(row) {
    if (row.reason) return esc(row.reason);
    if (row.episodes) {
      return t("autosync.queued_episodes", "{count} episodes queued in {language}", {
        count: row.episodes,
        language: row.language || ""
      });
    }
    return esc(row.language || "");
  }

  function renderReport(report) {
    const container = el("syncReport");
    if (!report) {
      container.innerHTML = `<div class="empty-state">${t("autosync.never_ran", "Auto-Sync has not run yet.")}</div>`;
      return;
    }
    if (report.error) {
      container.innerHTML = `<div class="empty-state">${esc(report.error)}</div>`;
      return;
    }

    const rows = report.results || [];
    if (!rows.length) {
      container.innerHTML = `<div class="empty-state">${t("autosync.nothing_checked", "There was nothing to check.")}</div>`;
      return;
    }

    container.innerHTML = rows
      .map((row) => {
        const where = row.where
          ? `<span class="sync-row-where">${esc(row.where)}</span>`
          : "";
        return `
          <div class="sync-row">
            <div class="sync-row-main">
              <span class="sync-row-title">${esc(row.title)}${where}</span>
              <span class="sync-row-detail">${resultDetail(row)}</span>
            </div>
            <span class="status-pill ${STATUS_CLASS[row.status] || "status-queued"}">${esc(statusLabel(row.status))}</span>
          </div>`;
      })
      .join("");
  }

  /* ===== Synced series ===== */
  function weekdayOptions(selected) {
    return WEEKDAYS.map(
      (day, index) =>
        `<option value="${index}" ${index === selected ? "selected" : ""}>${esc(t(`settings.day_${day}`, WEEKDAY_NAMES[index]))}</option>`
    ).join("");
  }

  function cadenceOptions(selected) {
    return [
      ["global", t("autosync.cadence_global", "On the Auto-Sync schedule")],
      ["daily", t("autosync.cadence_daily", "Every day")],
      ["weekly", t("autosync.cadence_weekly", "Every week")]
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`
      )
      .join("");
  }

  function renderSeries(rows, running) {
    if (editing) return;
    if (!rows.length) {
      syncedList.innerHTML = `<div class="empty-state">${t("autosync.no_series", "No series are synced yet. Tick \"Sync this with Auto-Sync\" in the download dialog of a series.")}</div>`;
      return;
    }

    syncedList.innerHTML = rows
      .map((row) => {
        const result = row.last_result;
        const resultHtml = result
          ? `<span class="status-pill ${STATUS_CLASS[result.status] || "status-queued"}">${esc(statusLabel(result.status))}</span> ${resultDetail(result)}`
          : "-";
        return `
          <div class="synced-series" data-id="${row.id}">
            <div class="synced-series-head">
              <div>
                <div class="synced-series-title">
                  <a href="${esc(row.series_url)}" target="_blank" rel="noopener noreferrer">${esc(row.title || row.series_url)}</a>
                </div>
                <div class="synced-series-meta">${esc(row.language)} &middot; ${esc(row.where)}</div>
              </div>
              <div class="synced-series-actions">
                <button class="btn btn-ghost btn-small" data-run="${row.id}" ${running ? "disabled" : ""}>${t("autosync.check_now", "Check now")}</button>
                <button class="btn btn-danger btn-small" data-remove="${row.id}">${t("common.remove", "Remove")}</button>
              </div>
            </div>
            <div class="control-row">
              <label class="field field-compact">
                <span class="field-label">${t("index.autosync_start_season", "Start at season")}</span>
                <input type="number" min="0" step="1" data-field="start_season" value="${row.start_season}" />
              </label>
              <label class="field field-compact">
                <span class="field-label">${t("index.autosync_start_episode", "Episode")}</span>
                <input type="number" min="1" step="1" data-field="start_episode" value="${row.start_episode}" />
              </label>
              <label class="field">
                <span class="field-label">${t("index.autosync_cadence", "Check it")}</span>
                <select data-field="cadence">${cadenceOptions(row.cadence)}</select>
              </label>
              <label class="field" ${row.cadence === "weekly" ? "" : "hidden"}>
                <span class="field-label">${t("index.autosync_weekday", "On")}</span>
                <select data-field="weekday">${weekdayOptions(row.weekday)}</select>
              </label>
              <label class="field field-compact" ${row.cadence === "global" ? "hidden" : ""}>
                <span class="field-label">${t("index.autosync_time", "At")}</span>
                <input type="time" data-field="run_time" value="${esc(row.run_time)}" />
              </label>
            </div>
            <div class="synced-series-status">
              <span>${t("autosync.schedule", "Schedule")}: ${esc(row.schedule || "-")}</span>
              <span>${t("autosync.next_run", "Next run")}: ${esc(formatTime(row.next_run))}</span>
              <span>${t("autosync.last_checked", "Last checked")}: ${esc(formatTime(row.last_checked))}</span>
              <span>${resultHtml}</span>
            </div>
          </div>`;
      })
      .join("");
  }

  async function load() {
    let data;
    try {
      data = await apiFetch("/api/autosync/status");
    } catch (error) {
      schedule(false);
      return;
    }

    el("scheduleValue").textContent = data.schedule || "-";
    el("lastRun").textContent = formatTime(data.last_run);
    el("nextRun").textContent = data.running
      ? t("autosync.running", "Running...")
      : formatTime(data.next_run);

    const report = data.last_report;
    el("lastResult").textContent = report && !report.error
      ? t("autosync.result", "{queued} of {checked} queued", {
        queued: report.queued,
        checked: report.checked
      })
      : "-";

    renderReport(report);
    renderSeries(data.series || [], data.running);

    syncNowBtn.disabled = Boolean(data.running);
    syncNowBtn.textContent = data.running
      ? t("autosync.running", "Running...")
      : t("autosync.sync_now", "Sync now");

    schedule(data.running);
  }

  async function startRun(url) {
    await apiSend(url, "POST");
    showToast(t("autosync.started", "Sync started"));
    schedule(true);
    load();
  }

  syncNowBtn.addEventListener("click", async () => {
    syncNowBtn.disabled = true;
    try {
      await startRun("/api/autosync/run");
    } catch (error) {
      showToast(error.message);
      syncNowBtn.disabled = false;
    }
  });

  syncedList.addEventListener("focusin", () => {
    editing = true;
  });
  syncedList.addEventListener("focusout", (event) => {
    if (!syncedList.contains(event.relatedTarget)) editing = false;
  });

  syncedList.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-field]");
    if (!input) return;
    const card = input.closest("[data-id]");
    const field = input.dataset.field;
    const value = input.type === "number" || field === "weekday" ? Number(input.value) : input.value;

    // The day only matters weekly and the time only with a preset
    if (field === "cadence") {
      card.querySelector('[data-field="weekday"]').closest(".field").hidden = value !== "weekly";
      card.querySelector('[data-field="run_time"]').closest(".field").hidden = value === "global";
    }

    try {
      await apiSend(`/api/autosync/series/${card.dataset.id}`, "PUT", { [field]: value });
      showToast(t("settings.saved", "Saved"));
    } catch (error) {
      showToast(error.message);
    }
    editing = false;
    load();
  });

  syncedList.addEventListener("click", async (event) => {
    const run = event.target.closest("[data-run]");
    if (run) {
      run.disabled = true;
      try {
        await startRun(`/api/autosync/series/${run.dataset.run}/run`);
      } catch (error) {
        showToast(error.message);
        run.disabled = false;
      }
      return;
    }

    const remove = event.target.closest("[data-remove]");
    if (!remove) return;
    try {
      await apiSend(`/api/autosync/series/${remove.dataset.remove}`, "DELETE");
      showToast(t("index.autosync_untracked", "No longer synced by Auto-Sync"));
      editing = false;
      load();
    } catch (error) {
      showToast(error.message);
    }
  });

  load();
})();
