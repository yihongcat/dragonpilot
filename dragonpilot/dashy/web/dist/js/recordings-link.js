"use strict";
(() => {
  const link = document.createElement("a");
  link.href = "/pages/recordings.html";
  link.textContent = "本地录像管理 ↗";
  link.style.cssText = "position:fixed;bottom:16px;left:16px;z-index:410;padding:10px 16px;border:1px solid #45665b;border-radius:8px;background:#152b24;color:#a6e6cf;font-size:14px;display:none";
  document.body.append(link);
  async function update() {
    try {
      const response = await fetch("/api/init", { cache: "no-store" });
      const data = response.ok ? await response.json() : {};
      link.style.display = data.isOffroad ? "block" : "none";
    } catch { link.style.display = "none"; }
  }
  update();
  window.setInterval(update, 5000);
})();
