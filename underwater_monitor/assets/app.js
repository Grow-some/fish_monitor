const statusBadge = document.querySelector("#status");

async function pollStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    statusBadge.className = `status ${status.healthy ? "status-ok" : "status-error"}`;
    statusBadge.textContent = status.healthy
      ? `LIVE配信中 通信状態 ${status.frame_per_second.toFixed(0)}fps`
      : `配信停止中 カメラが接続されていません`;
  } catch (error) {
    statusBadge.className = "status status-error";
    statusBadge.textContent = `配信停止中 通信エラーです。`;
  } finally {
    setTimeout(pollStatus, 2000);
  }
}

pollStatus();
