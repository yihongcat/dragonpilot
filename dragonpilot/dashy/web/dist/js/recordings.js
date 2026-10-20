"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const selected = new Set();
  const PAGE_SIZE = 25;
  let data = null, page = 0, busy = false, policyDirty = false;
  const size = (bytes) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  const date = (mtime) => new Date(mtime * 1000).toLocaleString("zh-CN", { hour12: false });
  function status(text, error = false) { $("status").textContent = text; $("status").classList.toggle("error", error); }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  async function api(path, body) {
    const response = await fetch(`/api/recordings${path}`, body === undefined ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
  function filtered() {
    const query = $("search").value.trim().toLowerCase();
    return (data?.routes || []).filter(r => `${r.id} ${date(r.mtime)}`.toLowerCase().includes(query));
  }
  function render() {
    if (!data) return;
    const parked = data.isOffroad && !busy;
    $("free").textContent = size(data.storage.free);
    $("total").textContent = size(data.storage.total);
    $("recording-size").textContent = size(data.storage.recordings);
    $("recording-status").textContent = data.recordingEnabled
      ? "录制已开启，行车时会保存录像和日志。"
      : "录制已关闭，不会新增行车录像和路线日志；仍可管理已有文件。";
    const percent = 100 * data.storage.used / data.storage.total;
    $("usage").value = percent;
    $("usage-label").textContent = `已使用 ${percent.toFixed(0)}% · 录像与行车日志共 ${data.routes.length} 个行程`;
    ["auto", "keep-days", "max-gb", "save-policy", "select-old"].forEach(id => $(id).disabled = !parked);
    $("delete").disabled = !parked || !selected.size;
    $("delete").textContent = `删除所选（${selected.size}）`;
    $("route-count").textContent = data.routes.length;
    const routes = filtered();
    page = Math.max(0, Math.min(page, Math.ceil(routes.length / PAGE_SIZE) - 1));
    const visible = routes.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    $("routes").replaceChildren();
    if (!visible.length) {
      const row = node("tr"), cell = node("td", "没有找到行程", "empty");
      cell.colSpan = 5; row.append(cell); $("routes").append(row);
    }
    for (const route of visible) {
      const row = node("tr"), choice = node("td"), checkbox = node("input");
      checkbox.type = "checkbox"; checkbox.checked = selected.has(route.id); checkbox.disabled = !parked || route.locked;
      checkbox.setAttribute("aria-label", `选择行程 ${route.id}`);
      checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(route.id) : selected.delete(route.id); render(); });
      choice.append(checkbox);
      const info = node("td"); info.append(node("div", date(route.mtime)), node("div", route.id, "route-id"));
      const details = node("details"), summary = node("summary", `${route.segments.length} 段录像`), segments = node("div", undefined, "segments");
      for (const segment of route.segments) {
        const video = segment.videos.find(v => v.name === "qcamera.ts");
        if (video) {
          const preview = node("a", `预览第 ${segment.number + 1} 段 · ${size(video.bytes)}`);
          preview.href = `/api/play?file=${encodeURIComponent(video.path)}`; preview.target = "_blank"; preview.rel = "noopener";
          segments.append(preview);
        } else segments.append(node("p", `第 ${segment.number + 1} 段 · ${segment.videos.length ? "导出后查看原始录像" : "仅行车日志"}`));
      }
      details.append(summary, segments); info.append(details);
      const state = node("td"), badge = node("span", route.locked ? "记录 / 导出中" : "已完成", `badge${route.locked ? " locked" : ""}`);
      state.append(badge);
      const action = node("td"), actions = node("div", undefined, "row-actions");
      if (parked && data.wifi && !route.locked) {
        const download = node("a", "导出 ZIP", "download");
        download.href = `/api/recordings/${encodeURIComponent(route.id)}/download`;
        download.addEventListener("click", () => status("已请求导出，请在浏览器下载列表查看进度。导出完成前请保持停车和 Wi-Fi 连接。"));
        actions.append(download);
      } else actions.append(node("span", route.locked ? "等待完成" : !data.isOffroad ? "停车后导出" : "连接 Wi-Fi 导出", "note"));
      action.append(actions); row.append(choice, info, node("td", size(route.bytes)), state, action); $("routes").append(row);
    }
    const selectable = visible.filter(r => !r.locked);
    $("select-all").disabled = !parked || !selectable.length;
    $("select-all").checked = !!selectable.length && selectable.every(r => selected.has(r.id));
    $("select-all").indeterminate = selectable.some(r => selected.has(r.id)) && !$("select-all").checked;
    $("page-label").textContent = `${routes.length ? page + 1 : 0} / ${Math.ceil(routes.length / PAGE_SIZE)} 页`;
    $("previous").disabled = page === 0; $("next").disabled = (page + 1) * PAGE_SIZE >= routes.length;
  }
  async function refresh(quiet = false) {
    try {
      data = await api("");
      const available = new Set(data.routes.filter(r => !r.locked).map(r => r.id));
      for (const id of selected) if (!available.has(id)) selected.delete(id);
      if (!policyDirty) {
        $("auto").checked = data.policy.enabled; $("keep-days").value = data.policy.keep_days; $("max-gb").value = data.policy.max_gb;
      }
      if (!quiet) status(!data.isOffroad ? "设备正在行车，删除、设置和导出已暂停。" : !data.wifi ? "已读取本地录像。连接 Wi-Fi 后可以导出。" : "设备已连接 Wi-Fi，可在停车后整理和导出录像。");
      render();
    } catch (e) {
      if (data) data.isOffroad = false;
      render(); status(`无法连接设备：${e.message}`, true);
    }
  }
  ["auto", "keep-days", "max-gb"].forEach(id => $(id).addEventListener("input", () => { policyDirty = true; }));
  $("policy-form").addEventListener("submit", async e => {
    e.preventDefault();
    const policy = { enabled: $("auto").checked, keep_days: Number($("keep-days").value), max_gb: Number($("max-gb").value) };
    if (policy.enabled && !window.confirm("开启后，超出保留天数或容量上限的旧行程会自动删除，且无法恢复。确认保存？")) return;
    busy = true; render();
    try { await api("/policy", policy); policyDirty = false; await refresh(true); status("设置已保存，自动整理将在下一次检查时生效。空间不足时的清理始终开启。"); }
    catch (err) { status(`保存失败：${err.message}`, true); }
    finally { busy = false; render(); }
  });
  $("delete").addEventListener("click", async () => {
    if (selected.size > 100) { status("每次最多删除 100 个行程，请减少选择。", true); return; }
    const bytes = data.routes.filter(r => selected.has(r.id)).reduce((n, r) => n + r.bytes, 0);
    if (!window.confirm(`永久删除 ${selected.size} 个行程，释放约 ${size(bytes)}？录像和日志均会删除，请确认已导出需要保留的内容。`)) return;
    busy = true; render();
    try { const result = await api("/delete", { routes: [...selected] }); selected.clear(); await refresh(true); status(`已删除 ${result.deleted} 个行程，释放 ${size(result.freed)}。`); }
    catch (err) { status(`删除失败：${err.message}。请刷新确认当前行程列表。`, true); await refresh(true); }
    finally { busy = false; render(); }
  });
  $("select-old").addEventListener("click", () => {
    const cutoff = Date.now() / 1000 - Number($("older-days").value) * 86400;
    selected.clear(); data.routes.filter(r => !r.locked && r.mtime < cutoff).slice(-100).forEach(r => selected.add(r.id));
    render(); status(`已选中 ${selected.size} 个最旧的已完成行程，每次最多选择 100 个。`);
  });
  $("select-all").addEventListener("change", () => {
    for (const route of filtered().slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).filter(r => !r.locked)) {
      $("select-all").checked ? selected.add(route.id) : selected.delete(route.id);
    }
    render();
  });
  $("search").addEventListener("input", () => { page = 0; render(); });
  $("previous").addEventListener("click", () => { page--; render(); });
  $("next").addEventListener("click", () => { page++; render(); });
  $("refresh").addEventListener("click", () => refresh());
  refresh();
  window.setInterval(() => { if (!document.hidden && !busy) refresh(true); }, 15000);
})();
