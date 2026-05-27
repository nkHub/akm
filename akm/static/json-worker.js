/* 在 Worker 中做 JSON.parse，减少主线程阻塞。 */
self.onmessage = function(ev) {
  var msg = ev.data || {};
  var id = msg.id;
  var raw = msg.raw;
  if (raw === null || raw === undefined || String(raw).trim() === '') {
    self.postMessage({ id: id, ok: true, data: null });
    return;
  }
  try {
    var data = JSON.parse(String(raw));
    self.postMessage({ id: id, ok: true, data: data });
  } catch (e) {
    self.postMessage({ id: id, ok: false, error: String(e) });
  }
};
