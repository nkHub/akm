/*
  自定义 JSON 查看组件（akm-json-viewer）
  目标：
  1) 内置 Worker 解析 JSON，减少主线程阻塞。
  2) 默认仅渲染首层，子节点在展开时懒加载，降低大数据量卡顿。
  3) 超长文本直接报错，并提供下载原始内容能力。
*/
(function () {
  var DEFAULT_MAX_TEXT_LENGTH = 600000;
  var DEFAULT_EAGER_DEPTH = 0;

  function esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function isPrimitive(v) {
    return v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';
  }

  function renderPrimitiveHtml(v) {
    if (v === null) return '<span class="bool">null</span>';
    if (typeof v === 'string') return '<span class="str">"' + esc(v) + '"</span>';
    if (typeof v === 'number') return '<span class="num">' + v + '</span>';
    if (typeof v === 'boolean') return '<span class="bool">' + v + '</span>';
    return '<span class="muted">' + esc(String(v)) + '</span>';
  }

  class AkmJsonViewer extends HTMLElement {
    constructor() {
      super();
      this._worker = null;
      this._taskSeq = 0;
      this._activeTaskId = 0;
      this._raw = '';
      this._treeData = null;
      this._maxTextLength = DEFAULT_MAX_TEXT_LENGTH;
      this._eagerDepth = DEFAULT_EAGER_DEPTH;
      this._root = this.attachShadow({ mode: 'open' });
      this._root.innerHTML = [
        '<style>',
        ':host{display:block;width:100%;min-height:20px;overflow:auto;font-size:12px;line-height:1.5;color:#d1d5db;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,Liberation Mono,Courier New,monospace;tab-size:2;}',
        '.muted{color:#9ca3af;} .num{color:#fbbf24;} .str{color:#4ade80;} .bool{color:#c084fc;} .key{color:#60a5fa;}',
        '.row{margin:2px 0;} .indent{margin-left:8px;padding-left:6px;border-left:1px solid rgba(255,255,255,.08);} .brace{color:#9ca3af;} .comma{color:#6b7280;} .node-inline{display:inline;} .node-inline>.indent{display:block;}',
        '.toggle{cursor:pointer;user-select:none;color:#9ca3af;display:inline-block;padding:0 2px;border-radius:4px;}',
        '.toggle:hover{background:rgba(255,255,255,.06);color:#e5e7eb;}',
        '.box{white-space:pre;word-break:normal;min-width:max-content;} pre{white-space:pre;word-break:normal;margin:0;tab-size:2;} .err{color:#fca5a5;} .link{color:#93c5fd;text-decoration:underline;cursor:pointer;}',
        '</style>',
        '<div id="wrap" class="box"></div>'
      ].join('');
      this._wrap = this._root.getElementById('wrap');
    }

    connectedCallback() {
      this.clear();
    }

    clear() {
      this._raw = '';
      this._treeData = null;
      this._activeTaskId = 0;
      this._wrap.innerHTML = '';
      this.scrollTop = 0;
    }

    setLoading() {
      this._wrap.innerHTML = '<div class="muted">加载中...</div>';
      this.scrollTop = 0;
    }

    setRaw(raw) {
      this._raw = raw === null || raw === undefined ? '' : String(raw);
      if (this._raw.trim() === '') {
        this._wrap.innerHTML = '<pre class="muted">null</pre>';
        this.scrollTop = 0;
        return;
      }
      if (this._raw.length > this._maxTextLength) {
        this._renderTooLarge();
        return;
      }
      this.setLoading();
      this._parseWithWorker(this._raw);
    }

    setMaxTextLength(limit) {
      var n = parseInt(limit, 10);
      if (!Number.isFinite(n) || n <= 0) return;
      this._maxTextLength = n;
    }

    setEagerDepth(depth) {
      var n = parseInt(depth, 10);
      if (!Number.isFinite(n) || n < 0) return;
      this._eagerDepth = n;
    }

    _renderTooLarge() {
      var self = this;
      this._wrap.innerHTML = '';
      var msg = document.createElement('div');
      msg.className = 'err';
      msg.textContent = '文本过长，已停止渲染（' + this._raw.length.toLocaleString() + ' 字符）。';
      var dl = document.createElement('a');
      dl.href = '#';
      dl.className = 'link';
      dl.textContent = '下载原始数据';
      dl.onclick = function (e) {
        e.preventDefault();
        self._downloadRaw();
      };
      this._wrap.appendChild(msg);
      this._wrap.appendChild(document.createTextNode(' '));
      this._wrap.appendChild(dl);
      this.scrollTop = 0;
    }

    _downloadRaw() {
      var blob = new Blob([this._raw], { type: 'text/plain;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'audit-log-' + Date.now() + '.txt';
      a.click();
      setTimeout(function () { URL.revokeObjectURL(url); }, 0);
    }

    _ensureWorker() {
      if (this._worker) return;
      try {
        this._worker = new Worker('/static/json-worker.js');
      } catch (e) {
        this._worker = null;
      }
    }

    _parseWithWorker(raw) {
      this._ensureWorker();
      var self = this;
      if (!this._worker) {
        this._parseOnMain(raw);
        return;
      }
      var id = ++this._taskSeq;
      this._activeTaskId = id;
      this._worker.onmessage = function (ev) {
        var msg = ev.data || {};
        if (msg.id !== self._activeTaskId) return;
        if (msg.ok) self._renderTree(msg.data);
        else self._renderRawFallback(raw);
      };
      this._worker.onerror = function () {
        self._worker = null;
        self._parseOnMain(raw);
      };
      try {
        this._worker.postMessage({ id: id, raw: raw });
      } catch (e) {
        this._worker = null;
        this._parseOnMain(raw);
      }
    }

    _parseOnMain(raw) {
      try {
        this._renderTree(JSON.parse(raw));
      } catch (e) {
        this._renderRawFallback(raw);
      }
    }

    _renderRawFallback(raw) {
      if (!raw || !String(raw).trim()) {
        this._wrap.innerHTML = '<pre class="muted">null</pre>';
      } else {
        this._wrap.innerHTML = '<pre>' + esc(raw) + '</pre>';
      }
      this.scrollTop = 0;
    }

    _renderTree(data) {
      this._treeData = data;
      this._wrap.innerHTML = '';
      var isArr = Array.isArray(data);
      var topOpen = document.createElement('div');
      topOpen.className = 'brace';
      topOpen.textContent = isArr ? '[' : '{';
      var topBody = document.createElement('div');
      topBody.className = 'indent';
      topBody.appendChild(this._renderNode(data, 0, true));
      var topClose = document.createElement('div');
      topClose.className = 'brace';
      topClose.textContent = isArr ? ']' : '}';
      this._wrap.appendChild(topOpen);
      this._wrap.appendChild(topBody);
      this._wrap.appendChild(topClose);
      this.scrollTop = 0;
    }

    _renderNode(value, depth, eager) {
      var node = document.createElement('div');
      node.className = 'row';
      if (isPrimitive(value)) { node.innerHTML = renderPrimitiveHtml(value); return node; }

      var isArr = Array.isArray(value);
      var keys = isArr ? value.map(function (_, i) { return i; }) : Object.keys(value || {});
      if (!keys.length) {
        node.innerHTML = isArr ? '<span class="muted">[]</span>' : '<span class="muted">{}</span>';
        return node;
      }

      // 根节点不折叠，直接展开，避免用户只看到 "{N keys}" 摘要。
      if (depth === 0) {
        var rootBody = document.createElement('div');
        rootBody.className = 'indent';
        for (var r = 0; r < keys.length; r++) {
          var rk = keys[r];
          var rr = document.createElement('div');
          rr.className = 'row';
          var rKeyHtml = isArr ? ('<span class="muted">' + rk + '</span>') : ('<span class="key">"' + esc(rk) + '"</span>');
          var rv = value[rk];
          var rComma = (r < keys.length - 1) ? '<span class="comma">,</span>' : '';
          if (isPrimitive(rv)) {
            rr.innerHTML = rKeyHtml + '<span class="muted">: </span>' + renderPrimitiveHtml(rv) + rComma;
          } else {
            rr.innerHTML = rKeyHtml + '<span class="muted">: </span>';
            var rNextEager = (depth + 1) <= this._eagerDepth;
            var rChild = this._renderNode(rv, depth + 1, rNextEager);
            rChild.classList.add('node-inline');
            rr.appendChild(rChild);
            if (r < keys.length - 1) {
              var rCommaEl = document.createElement('span');
              rCommaEl.className = 'comma';
              rCommaEl.textContent = ',';
              rr.appendChild(rCommaEl);
            }
          }
          rootBody.appendChild(rr);
        }
        node.appendChild(rootBody);
        return node;
      }

      var summary = document.createElement('span');
      summary.className = 'toggle';
      summary.textContent = isArr ? ('[ ' + keys.length + ' items ]') : ('{ ' + keys.length + ' keys }');
      var body = document.createElement('div');
      body.className = 'indent';
      body.style.display = eager ? 'block' : 'none';
      // 注意：这里必须初始化为未加载。
      // 之前若 eager=true 就先标记为已加载，会导致真正的子节点构建被跳过，展开点击看起来“无效”。
      body.dataset.loaded = '0';
      node.appendChild(summary);
      node.appendChild(body);

      var self = this;
      function loadChildrenOnce() {
        if (body.dataset.loaded === '1') return;
        body.dataset.loaded = '1';
        for (var i = 0; i < keys.length; i++) {
          var k = keys[i];
          var row = document.createElement('div');
          row.className = 'row';
          var keyHtml = isArr ? ('<span class="muted">' + k + '</span>') : ('<span class="key">"' + esc(k) + '"</span>');
          var cv = value[k];
          var comma = (i < keys.length - 1) ? '<span class="comma">,</span>' : '';
          if (isPrimitive(cv)) {
            row.innerHTML = keyHtml + '<span class="muted">: </span>' + renderPrimitiveHtml(cv) + comma;
          } else {
            row.innerHTML = keyHtml + '<span class="muted">: </span>';
            // 预渲染层级：默认展开到第 2 层（根=0）。
            // 这样比只渲染一层更易读，同时仍保留深层懒加载，避免超大数据一次性构建过重。
            var nextEager = (depth + 1) <= self._eagerDepth;
            var child = self._renderNode(cv, depth + 1, nextEager);
            child.classList.add('node-inline');
            row.appendChild(child);
            if (i < keys.length - 1) {
              var commaEl = document.createElement('span');
              commaEl.className = 'comma';
              commaEl.textContent = ',';
              row.appendChild(commaEl);
            }
          }
          body.appendChild(row);
        }
      }

      if (eager) loadChildrenOnce();
      summary.onclick = function () {
        if (body.style.display === 'none') {
          loadChildrenOnce();
          body.style.display = 'block';
        } else {
          body.style.display = 'none';
        }
      };
      return node;
    }
  }

  if (!customElements.get('akm-json-viewer')) {
    customElements.define('akm-json-viewer', AkmJsonViewer);
  }
})();
