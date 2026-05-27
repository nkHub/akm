/*
  会话视图组件（akm-chat-viewer）
  - 消息气泡级虚拟列表
  - 动态测量每条高度，滚动时按可视区渲染
*/
(function () {
  function esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  class AkmChatViewer extends HTMLElement {
    constructor() {
      super();
      this._items = [];
      this._heights = [];
      this._tops = [];
      this._total = 0;
      this._est = 92;
      this._overscan = 6;
      this._root = this.attachShadow({ mode: 'open' });
      this._root.innerHTML = [
        '<style>',
        ':host{display:block;height:100%;overflow:auto;}',
        '.viewport{position:relative;min-height:100%;}',
        '.item{position:absolute;left:0;right:0;padding:6px 0;}',
        '.bubble{max-width:80%;padding:10px 12px;border-radius:14px;font-size:13px;line-height:1.6;}',
        '.row-user{display:flex;justify-content:flex-end;}.row-user .bubble{background:rgba(79,70,229,.28);border:1px solid rgba(99,102,241,.45);color:#e5e7eb;}',
        '.row-assistant{display:flex;justify-content:flex-start;}.row-assistant .bubble{background:rgba(31,41,55,.65);border:1px solid rgba(99,102,241,.25);color:#d1d5db;}',
        '.row-system{display:flex;justify-content:center;}.row-system .bubble{max-width:90%;background:rgba(120,53,15,.25);border:1px solid rgba(180,83,9,.35);color:#fcd34d;font-style:italic;font-size:12px;text-align:center;}',
        '.meta{text-align:center;color:#6b7280;font-size:12px;padding-top:8px;}',
        '.md p{margin:0 0 6px 0;}.md p:last-child{margin-bottom:0;}.md pre{white-space:pre-wrap;word-break:break-word;}',
        '</style>',
        '<div id="viewport" class="viewport"></div>'
      ].join('');
      this._viewport = this._root.getElementById('viewport');
      this._onScroll = this._onScroll.bind(this);
      this._raf = 0;
    }

    connectedCallback() {
      this.addEventListener('scroll', this._onScroll, { passive: true });
    }

    disconnectedCallback() {
      this.removeEventListener('scroll', this._onScroll);
    }

    clear() {
      this._items = [];
      this._heights = [];
      this._tops = [];
      this._total = 0;
      this.scrollTop = 0;
      this._viewport.innerHTML = '';
      this._viewport.style.height = '0px';
    }

    setLoading(text) {
      this._viewport.innerHTML = '<div class="meta">' + esc(text || '加载中...') + '</div>';
      this._viewport.style.height = 'auto';
    }

    setItems(items) {
      this._items = Array.isArray(items) ? items : [];
      this._heights = this._items.map(function () { return 92; });
      this._recalc();
      this.scrollTop = 0;
      this._render();
    }

    _recalc() {
      this._tops = new Array(this._items.length);
      var t = 0;
      for (var i = 0; i < this._items.length; i++) {
        this._tops[i] = t;
        t += this._heights[i] || this._est;
      }
      this._total = t;
      this._viewport.style.height = t + 'px';
    }

    _onScroll() {
      if (this._raf) return;
      var self = this;
      this._raf = requestAnimationFrame(function () {
        self._raf = 0;
        self._render();
      });
    }

    _findStart(y) {
      var l = 0, r = this._tops.length - 1, ans = 0;
      while (l <= r) {
        var m = (l + r) >> 1;
        if (this._tops[m] <= y) { ans = m; l = m + 1; }
        else r = m - 1;
      }
      return ans;
    }

    _render() {
      var n = this._items.length;
      if (!n) {
        this._viewport.innerHTML = '<div class="meta">无对话数据</div>';
        this._viewport.style.height = 'auto';
        return;
      }
      var top = this.scrollTop;
      var vh = this.clientHeight || 600;
      var start = this._findStart(Math.max(0, top - 200));
      var endY = top + vh + 200;
      var end = start;
      while (end < n && this._tops[end] < endY) end++;
      start = Math.max(0, start - this._overscan);
      end = Math.min(n, end + this._overscan);

      this._viewport.innerHTML = '';
      var self = this;
      var changed = false;
      for (var i = start; i < end; i++) {
        var it = this._items[i];
        var el = document.createElement('div');
        el.className = 'item row-' + (it.role || 'assistant');
        el.style.top = this._tops[i] + 'px';
        var html = '<div class="bubble md">' + (it.html || '') + '</div>';
        if (it.role === 'meta') html = '<div class="meta">' + (it.html || '') + '</div>';
        el.innerHTML = html;
        this._viewport.appendChild(el);
        var h = Math.ceil(el.offsetHeight + 2);
        if (h > 0 && h !== this._heights[i]) {
          this._heights[i] = h;
          changed = true;
        }
      }
      if (changed) {
        this._recalc();
        this._render();
      }
    }
  }

  if (!customElements.get('akm-chat-viewer')) {
    customElements.define('akm-chat-viewer', AkmChatViewer);
  }
})();
