function initializeComponentWhenParsed(element, init) {
  function run() {
    init.call(element);
    element.setAttribute('data-ready', 'true');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      run();
    }, { once: true });
    return;
  }
  setTimeout(run, 0);
}

if (!customElements.get('akm-switch')) {
  customElements.define('akm-switch', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      this.render();
      this.addEventListener('click', this._handleClick.bind(this));
      this.addEventListener('keydown', this._handleKeydown.bind(this));
      this.setChecked(this.hasAttribute('checked'));
      this.setDisabled(this.hasAttribute('disabled'));
    }

    render() {
      var label = this.getAttribute('label') || '';
      var labelClass = this.getAttribute('label-class') || 'text-xs text-gray-400';
      this.className = (this.getAttribute('host-class') || 'inline-flex items-center gap-2 cursor-pointer select-none switch-off').trim();
      this.innerHTML = '' +
        (label ? '<span class="' + labelClass + '">' + this.escape(label) + '</span>' : '') +
        '<div class="switch-track bg-gray-600 flex items-center"><div class="switch-thumb bg-white"></div></div>';
      this.setAttribute('role', 'switch');
      this.setAttribute('tabindex', this.disabled ? '-1' : '0');
    }

    escape(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    _handleKeydown(event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      this.toggle(true);
    }

    _handleClick() {
      this.toggle(true);
    }

    setChecked(checked) {
      this.checked = !!checked;
      this.setAttribute('aria-checked', this.checked ? 'true' : 'false');
      this.classList.toggle('switch-on', this.checked);
      this.classList.toggle('switch-off', !this.checked);
      var track = this.querySelector('.switch-track');
      if (track) {
        track.classList.toggle('bg-indigo-600', this.checked);
        track.classList.toggle('bg-gray-600', !this.checked);
      }
    }

    setDisabled(disabled) {
      this.disabled = !!disabled;
      this.classList.toggle('opacity-50', this.disabled);
      this.classList.toggle('cursor-not-allowed', this.disabled);
      this.classList.toggle('cursor-pointer', !this.disabled);
      this.setAttribute('tabindex', this.disabled ? '-1' : '0');
      this.setAttribute('aria-disabled', this.disabled ? 'true' : 'false');
    }

    toggle(emit) {
      if (this.disabled) return;
      this.setChecked(!this.checked);
      if (emit) {
        this.dispatchEvent(new CustomEvent('change', { detail: { checked: this.checked }, bubbles: true }));
      }
    }
  });
}

if (!customElements.get('akm-empty-state')) {
  customElements.define('akm-empty-state', class extends HTMLElement {
    connectedCallback() {
      this.textContent = this.getAttribute('message') || this.textContent || '暂无数据';
    }
  });
}

if (!customElements.get('akm-pagination')) {
  customElements.define('akm-pagination', class extends HTMLElement {
    renderPagination(config) {
      config = config || {};
      var totalPages = Math.max(0, parseInt(config.totalPages || 0, 10));
      var currentPage = Math.max(1, parseInt(config.currentPage || 1, 10));
      var onSelectName = config.onSelectName || '';
      var summary = config.summary || '';
      if (totalPages <= 1) {
        this.classList.add('hidden');
        this.innerHTML = '';
        return;
      }
      this.classList.remove('hidden');
      var disabledClass = 'text-gray-600 cursor-default';
      var activeClass = 'text-gray-400 hover:text-white hover:bg-surface-light cursor-pointer';
      var html = '';
      html += this._button('首页', 1, currentPage === 1, disabledClass, activeClass);
      html += this._button('上一页', currentPage - 1, currentPage === 1, disabledClass, activeClass);
      html += '<select data-role="page-select" class="bg-surface-light border border-border rounded px-2 py-1 text-xs text-gray-300 cursor-pointer focus:outline-none focus:border-indigo-500">';
      for (var p = 1; p <= totalPages; p++) {
        html += '<option value="' + p + '"' + (p === currentPage ? ' selected' : '') + '>第 ' + p + ' 页</option>';
      }
      html += '</select>';
      html += this._button('下一页', currentPage + 1, currentPage === totalPages, disabledClass, activeClass);
      html += this._button('末页', totalPages, currentPage === totalPages, disabledClass, activeClass);
      if (summary) html += '<span class="text-xs text-gray-500 ml-2">' + summary + '</span>';
      this.innerHTML = html;
      var self = this;
      var select = this.querySelector('[data-role="page-select"]');
      if (select) {
        select.addEventListener('change', function() { self.invoke(onSelectName, parseInt(this.value, 10)); });
      }
      this.querySelectorAll('[data-role="page-btn"]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          if (btn.disabled) return;
          self.invoke(onSelectName, parseInt(btn.getAttribute('data-page'), 10));
        });
      });
    }

    _button(label, page, disabled, disabledClass, activeClass) {
      return '<button type="button" data-role="page-btn" data-page="' + page + '" class="px-2 py-1 text-xs rounded ' + (disabled ? disabledClass : activeClass) + '"' + (disabled ? ' disabled' : '') + '>' + label + '</button>';
    }

    invoke(name, page) {
      if (name && typeof window[name] === 'function') window[name](page);
    }
  });
}

if (!customElements.get('akm-range-tabs')) {
  customElements.define('akm-range-tabs', class extends HTMLElement {
    connectedCallback() {
      this.className = this.className || 'flex items-center gap-2';
    }

    setOptions(options, currentValue, onSelectName) {
      this._options = Array.isArray(options) ? options : [];
      this._currentValue = currentValue;
      this._onSelectName = onSelectName || '';
      this.render();
    }

    render() {
      var self = this;
      this.innerHTML = (this._options || []).map(function(option) {
        var active = String(option.value) === String(self._currentValue);
        var cls = active
          ? 'bg-indigo-600 text-white text-xs px-3 py-1.5 rounded transition-colors cursor-pointer'
          : 'bg-surface-light border border-border hover:border-indigo-500 text-gray-400 hover:text-gray-200 text-xs px-3 py-1.5 rounded transition-colors cursor-pointer';
        return '<button type="button" data-role="range-tab" data-value="' + self.escape(option.value) + '" class="' + cls + '">' + self.escape(option.label) + '</button>';
      }).join('');
      this.querySelectorAll('[data-role="range-tab"]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var val = btn.getAttribute('data-value');
          self._currentValue = val;
          self.render();
          if (self._onSelectName && typeof window[self._onSelectName] === 'function') {
            window[self._onSelectName](isNaN(Number(val)) ? val : Number(val));
          }
        });
      });
    }

    escape(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
  });
}

class AkmOverlayPanelElement extends HTMLElement {
  collectNodes(splitFooter) {
    var bodyNodes = [];
    var footerNodes = [];
    Array.from(this.childNodes).forEach(function(node) {
      if (splitFooter && node.nodeType === 1 && node.hasAttribute('data-modal-footer')) footerNodes.push(node);
      else bodyNodes.push(node);
    });
    return { bodyNodes: bodyNodes, footerNodes: footerNodes };
  }

  attachEscClose(handler) {
    var self = this;
    document.addEventListener('keydown', function(event) {
      if (event.key === 'Escape' && self.isOpen()) handler.call(self, event);
    });
  }

  setTitle(text) {
    if (this.titleEl) this.titleEl.textContent = text || '';
  }
}

if (!customElements.get('akm-settings-card')) {
  customElements.define('akm-settings-card', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      var self = this;
      initializeComponentWhenParsed(this, function() { self.render(); });
    }

    render() {
      var bodyNodes = [];
      var actionNodes = [];
      Array.from(this.childNodes).forEach(function(node) {
        if (node.nodeType === 1 && node.hasAttribute('slot') && node.getAttribute('slot') === 'actions') actionNodes.push(node);
        else bodyNodes.push(node);
      });
      var align = this.getAttribute('align') || 'center';
      var bodyClass = align === 'start'
        ? 'bg-surface-light border border-border rounded-lg p-4 flex items-start justify-between'
        : 'bg-surface-light border border-border rounded-lg p-4 flex items-center justify-between';
      this.innerHTML = '<div data-card class="' + bodyClass + '"><div data-body></div><div data-actions class="shrink-0"></div></div>';
      var bodyEl = this.querySelector('[data-body]');
      var actionsEl = this.querySelector('[data-actions]');
      bodyNodes.forEach(function(node) { bodyEl.appendChild(node); });
      actionNodes.forEach(function(node) { actionsEl.appendChild(node); });
      this.style.display = 'block';
    }
  });
}

/** akm-plugin-card：插件卡片（首页插件/知识库记忆卡片共用）。
 *  支持 slot：icon（左侧图标）、desc（中部描述/指标区）、actions（右侧操作）。
 *  通过 title 属性设标题。图标与操作均为服务端拼好的子节点内联进来，
 *  组件只做骨架布局，不修改子节点内容，故长 SVG / 表格可安全透传。
 */
if (!customElements.get('akm-plugin-card')) {
  customElements.define('akm-plugin-card', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      var self = this;
      initializeComponentWhenParsed(this, function() { self.render(); });
    }

    render() {
      var iconNodes = [], descNodes = [], actionNodes = [], otherNodes = [];
      Array.from(this.childNodes).forEach(function(node) {
        if (node.nodeType !== 1) return;
        var slot = node.getAttribute && node.getAttribute('slot');
        if (slot === 'icon') iconNodes.push(node);
        else if (slot === 'desc') descNodes.push(node);
        else if (slot === 'actions') actionNodes.push(node);
        else otherNodes.push(node);
      });
      var title = this.getAttribute('title') || '';
      var titleHtml = title
        ? '<div class="text-sm font-medium text-gray-200 mb-2 truncate">' + title + '</div>'
        : '';
      this.innerHTML =
        '<div class="bg-surface-light border border-border rounded-lg p-4 flex flex-col h-full">' +
          titleHtml +
          '<div class="flex items-start gap-3 flex-1">' +
            '<div data-icon class="shrink-0 mt-0.5 w-8 h-8 flex items-center justify-center">' + '</div>' +
            '<div data-desc class="flex-1 min-w-0"></div>' +
            '<div data-actions class="shrink-0"></div>' +
          '</div>' +
        '</div>';
      var iconEl = this.querySelector('[data-icon]');
      var descEl = this.querySelector('[data-desc]');
      var actionsEl = this.querySelector('[data-actions]');
      iconNodes.forEach(function(n) { iconEl.appendChild(n); });
      descNodes.forEach(function(n) { descEl.appendChild(n); });
      actionNodes.forEach(function(n) { actionsEl.appendChild(n); });
      otherNodes.forEach(function(n) { descEl.appendChild(n); });
      this.style.display = 'block';
    }
  });
}

if (!customElements.get('akm-modal')) {
  customElements.define('akm-modal', class extends AkmOverlayPanelElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      var self = this;
      initializeComponentWhenParsed(this, function() {
        self.render();
        self.bindEvents();
        self.close();
      });
    }

    render() {
      var width = this.getAttribute('max-width') || 'max-w-lg';
      var bodyClass = this.getAttribute('body-class') || 'p-4';
      var panelClass = this.getAttribute('panel-class') || '';
      var title = this.getAttribute('title') || '';
      var subtitle = this.getAttribute('subtitle') || '';
      var collected = this.collectNodes(true);
      var bodyNodes = collected.bodyNodes;
      var footerNodes = collected.footerNodes;
      this.innerHTML = '' +
        '<div data-overlay class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 fade-in">' +
          '<div data-backdrop class="absolute inset-0 bg-black/60"></div>' +
          '<div class="relative bg-surface-light border border-border rounded-lg w-full ' + width + ' shadow-2xl ' + panelClass + '" style="animation: slideUp 0.2s ease">' +
            '<div class="flex items-center justify-between px-4 py-3 border-b border-border">' +
              '<div class="min-w-0">' +
                '<h3 data-title class="text-sm font-semibold text-white"></h3>' +
                '<p data-subtitle class="text-xs text-gray-500 mt-1 hidden"></p>' +
              '</div>' +
              '<button type="button" data-close class="text-gray-400 hover:text-white transition-colors cursor-pointer">' +
                '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>' +
              '</button>' +
            '</div>' +
            '<div data-body class="' + bodyClass + '"></div>' +
            '<div data-footer class="hidden"></div>' +
          '</div>' +
        '</div>';
      this.overlay = this.querySelector('[data-overlay]');
      this.backdrop = this.querySelector('[data-backdrop]');
      this.titleEl = this.querySelector('[data-title]');
      this.subtitleEl = this.querySelector('[data-subtitle]');
      this.bodyEl = this.querySelector('[data-body]');
      this.footerEl = this.querySelector('[data-footer]');
      this.setTitle(title);
      this.setSubtitle(subtitle);
      this.style.display = 'contents';
      var self = this;
      bodyNodes.forEach(function(node) { self.bodyEl.appendChild(node); });
      if (footerNodes.length) {
        this.footerEl.className = 'px-4 py-4 border-t border-border shrink-0 bg-surface-light rounded-b-lg';
        footerNodes.forEach(function(node) { self.footerEl.appendChild(node); });
      }
    }

    bindEvents() {
      var self = this;
      this.querySelector('[data-close]').addEventListener('click', function() { self.close(); });
      this.backdrop.addEventListener('click', function() { self.close(); });
      this.attachEscClose(function() { self.close(); });
    }

    isOpen() {
      return this.overlay && !this.overlay.classList.contains('hidden');
    }

    open() {
      this.overlay.classList.remove('hidden');
      document.body.classList.add('overflow-hidden');
    }

    close() {
      if (this.overlay) this.overlay.classList.add('hidden');
      document.body.classList.remove('overflow-hidden');
    }
    setSubtitle(text) {
      if (!this.subtitleEl) return;
      this.subtitleEl.textContent = text || '';
      this.subtitleEl.classList.toggle('hidden', !text);
    }
  });
}

if (!customElements.get('akm-drawer')) {
  customElements.define('akm-drawer', class extends AkmOverlayPanelElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      var self = this;
      initializeComponentWhenParsed(this, function() {
        self.render();
        self.bindEvents();
        self.close(true);
      });
    }

    render() {
      var width = this.getAttribute('max-width') || 'max-w-3xl';
      var title = this.getAttribute('title') || '';
      var bodyNodes = this.collectNodes(false).bodyNodes;
      this.innerHTML = '' +
        '<div data-overlay class="hidden fixed inset-0 z-40 bg-black/50"></div>' +
        '<div data-panel class="hidden fixed top-0 right-0 z-50 h-full w-full ' + width + ' bg-surface-light border-l border-border shadow-2xl flex flex-col overflow-hidden" style="transform:translateX(100%); transition: transform 0.25s ease">' +
          '<div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">' +
            '<h3 data-title class="text-sm font-semibold text-white"></h3>' +
            '<button type="button" data-close class="text-gray-400 hover:text-white transition-colors cursor-pointer">' +
              '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>' +
            '</button>' +
          '</div>' +
          '<div data-body class="flex flex-col flex-1 min-h-0 min-w-0 overflow-auto"></div>' +
        '</div>';
      this.overlay = this.querySelector('[data-overlay]');
      this.panel = this.querySelector('[data-panel]');
      this.titleEl = this.querySelector('[data-title]');
      this.bodyEl = this.querySelector('[data-body]');
      this.setTitle(title);
      this.style.display = 'contents';
      var self = this;
      bodyNodes.forEach(function(node) { self.bodyEl.appendChild(node); });
    }

    bindEvents() {
      var self = this;
      this.overlay.addEventListener('click', function() { self.close(); });
      this.querySelector('[data-close]').addEventListener('click', function() { self.close(); });
      this.attachEscClose(function() { self.close(); });
    }

    isOpen() {
      return this.panel && !this.panel.classList.contains('hidden');
    }

    open() {
      var self = this;
      this.overlay.classList.remove('hidden');
      this.panel.classList.remove('hidden');
      this.panel.style.transform = 'translateX(100%)';
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          self.panel.style.transform = 'translateX(0)';
        });
      });
    }

    close(immediate) {
      var self = this;
      if (!this.panel || !this.overlay) return;
      this.panel.style.transform = 'translateX(100%)';
      if (immediate) {
        this.panel.classList.add('hidden');
        this.overlay.classList.add('hidden');
        return;
      }
      setTimeout(function() {
        self.panel.classList.add('hidden');
        self.overlay.classList.add('hidden');
      }, 250);
    }
  });
}

if (!customElements.get('akm-tooltip')) {
  customElements.define('akm-tooltip', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      var self = this;
      // 行内展示，保持与宿主（如表格单元格）文本同基线，不撑高行盒
      this.style.display = 'inline-block';
      this.addEventListener('mouseenter', function() { self.show(); });
      this.addEventListener('mouseleave', function() { self.hide(); });
    }

    // 惰性创建页面级 fixed 浮层：挂在 body 下，避免被表格等容器的 overflow 裁剪
    _tip() {
      if (!this._tipEl) {
        var tip = document.createElement('div');
        tip.style.cssText =
          'position:fixed;z-index:9999;display:none;pointer-events:none;' +
          'max-width:320px;padding:8px 10px;font-size:11px;line-height:1.7;' +
          'white-space:pre-line;text-align:left;color:#d1d5db;' +
          'background:#111827;border:1px solid #374151;border-radius:6px;' +
          'box-shadow:0 8px 24px rgba(0,0,0,.4);';
        document.body.appendChild(tip);
        this._tipEl = tip;
      }
      return this._tipEl;
    }

    show() {
      var tip = this._tip();
      tip.textContent = this.getAttribute('content') || '';
      tip.style.display = 'block';
      var r = this.getBoundingClientRect();
      var tr = tip.getBoundingClientRect();
      // 默认在触发元素下方展示，超出视口下边界时翻转到上方
      var x = Math.min(r.left, window.innerWidth - tr.width - 8);
      if (x < 8) x = 8;
      var y = r.bottom + 8;
      if (y + tr.height > window.innerHeight - 8) {
        y = Math.max(8, r.top - tr.height - 8);
      }
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
    }

    hide() {
      if (this._tipEl) this._tipEl.style.display = 'none';
    }
  });
}

// ── 共享的页面级悬浮浮层：fixed 挂 body，避免被卡片/表格的 overflow-hidden 裁剪 ──
// 供折线图、环形图等自绘图表复用：
//   var tip = akmFloatingTip();
//   tip.show(['第一行', '第二行'], anchorX, anchorY);   // 锚点为视口坐标
//   tip.hide(); tip.destroy();
function akmFloatingTip() {
  var el = null;
  function ensure() {
    if (!el) {
      el = document.createElement('div');
      el.style.cssText =
        'position:fixed;z-index:9999;display:none;pointer-events:none;' +
        'max-width:300px;padding:8px 10px;font-size:11px;line-height:1.7;' +
        'white-space:pre-line;text-align:left;color:#d1d5db;' +
        'background:#111827;border:1px solid #374151;border-radius:6px;' +
        'box-shadow:0 8px 24px rgba(0,0,0,.4);';
      document.body.appendChild(el);
    }
    return el;
  }
  return {
    show: function(lines, anchorX, anchorY) {
      var tip = ensure();
      tip.textContent = (Array.isArray(lines) ? lines : [lines]).join('\n');
      tip.style.display = 'block';
      var rect = tip.getBoundingClientRect();
      var x = anchorX - rect.width / 2;
      if (x < 8) x = 8;
      if (x + rect.width > window.innerWidth - 8) x = window.innerWidth - rect.width - 8;
      // 优先显示在锚点上方，顶部空间不足时翻到下方
      var y = anchorY - rect.height - 12;
      if (y < 8) y = anchorY + 16;
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
      return tip;
    },
    hide: function() { if (el) el.style.display = 'none'; },
    destroy: function() {
      if (el && el.parentNode) el.parentNode.removeChild(el);
      el = null;
    }
  };
}

// ── akm-line-chart：通用折线图壳组件（内联 SVG，不引入第三方图表库） ──
// 用法：
//   var chart = document.getElementById('xxx');
//   chart.render({
//     labels: ['00','01',...],   // X 轴刻度，与 values 等长
//     values: [0, 3, ...],       // 数值序列
//     height: 220,               // 可选，CSS px，默认 220（fill 时作为最小高度）
//     maxHeight: 420,            // 可选，fill 模式下的最大高度，默认等于 height
//     fill: true,                // 可选，垂直拉伸到父容器内容高度并夹在 [height, maxHeight]
//     color: '#f87171',          // 可选，折线/面积主色，默认 #818cf8
//     unit: '次',                // 可选，数据点悬浮提示的数值单位
//     format: fn,                // 可选，自定义悬浮提示里的数值格式（给了它就不再拼 unit）
//     emptyText: '暂无数据',      // 可选，全 0 时的空态文案
//     details: [['3× 超时','1× HTTP 429'], null, ...]  // 可选，与 values 等长的附加行
//   });
// 传了 details 的数据点改用组件自绘的悬浮浮层（首行「刻度: 数值单位」+ 附加行），
// 未传时保持 SVG <title> 原生提示。
// 约定：沿用页面浅色 DOM（不引入 Shadow DOM）；宽度自适应宿主容器，
//       宿主/父容器尺寸变化（含容器从 display:none 恢复显示）时自动重绘。
if (!customElements.get('akm-line-chart')) {
  customElements.define('akm-line-chart', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      this.style.display = 'block';
      var self = this;
      if (typeof ResizeObserver === 'function') {
        // 尺寸变化驱动重绘：容器从隐藏变为可见、窗口缩放、布局调整都会触发。
        // 用签名比对（宽度 + fill 模式下的父容器可用高度）避免自激循环。
        this._ro = new ResizeObserver(function() {
          if (!self._config) return;
          if (self._sizeSignature() === self._lastSig) return;
          self.render(self._config);
        });
        this._ro.observe(this);
      } else {
        this._onResize = function() {
          clearTimeout(self._resizeTimer);
          self._resizeTimer = setTimeout(function() {
            if (self._config && self.isConnected) self.render(self._config);
          }, 150);
        };
        window.addEventListener('resize', this._onResize);
      }
    }

    disconnectedCallback() {
      if (this._ro) { this._ro.disconnect(); this._ro = null; }
      if (this._onResize) window.removeEventListener('resize', this._onResize);
      this._hideTip();
      if (this._tip) { this._tip.destroy(); this._tip = null; }
    }

    // 父容器内容区高度（剔除 padding），供 fill 模式把图表拉伸到卡片剩余高度
    _availableHeight() {
      var parent = this.parentNode;
      if (!parent || typeof window.getComputedStyle !== 'function') return 0;
      var style = window.getComputedStyle(parent);
      var pad = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
      return Math.max(0, Math.round(parent.clientHeight - pad));
    }

    // 重绘判定签名：宽度变化总是重绘；fill 模式再叠加父容器可用高度
    _sizeSignature() {
      var width = Math.round(this.clientWidth || 0);
      var fill = !!(this._config && this._config.fill);
      return width + 'x' + (fill ? this._availableHeight() : 0);
    }

    // 惰性创建共享浮层（fixed 挂 body，避免被卡片 overflow-hidden 裁剪）
    _tip() {
      if (!this._tipHandle) this._tipHandle = akmFloatingTip();
      return this._tipHandle;
    }

    _showTip(index) {
      var geo = this._geo;
      if (!geo || !geo.points[index]) return;
      var point = geo.points[index];
      var shown = geo.format ? geo.format(point.value) : point.value + geo.unit;
      var lines = [geo.labels[index] + ': ' + shown];
      var detail = geo.details[index];
      var detailLines = detail == null ? [] : (Array.isArray(detail) ? detail : [detail]);
      for (var i = 0; i < detailLines.length; i++) {
        var line = detailLines[i];
        if (line != null && String(line) !== '') lines.push(String(line));
      }
      var host = this.getBoundingClientRect();
      this._tip().show(lines, host.left + point.x, host.top + point.y);
      this._highlight(index);
    }

    _hideTip() {
      if (this._tipHandle) this._tipHandle.hide();
      this._highlight(-1);
    }

    // 放大当前数据点的圆点（仅在画了圆点时有视觉效果）
    _highlight(index) {
      if (!this._hasDots) return;
      var dots = this.querySelectorAll('[data-dot]');
      for (var i = 0; i < dots.length; i++) {
        dots[i].setAttribute('r', i === index ? '5.5' : '3');
      }
    }

    render(config) {
      config = config || {};
      this._config = config;
      this._hideTip();
      var self = this;
      var labels = Array.isArray(config.labels) ? config.labels : [];
      var values = (Array.isArray(config.values) ? config.values : []).map(function(v) {
        var n = Number(v);
        return Number.isFinite(n) ? n : 0;
      });
      var details = Array.isArray(config.details) ? config.details : [];
      var hasDetails = details.some(function(item) { return item != null && item.length !== 0; });
      var escape = function(s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
      };
      // 高度：默认固定；fill 时拉伸到父容器可用高度，并夹在 [height, maxHeight]
      var baseHeight = Math.max(120, parseInt(config.height || 220, 10) || 220);
      var maxHeight = Math.max(baseHeight, parseInt(config.maxHeight || 0, 10) || baseHeight);
      var height = baseHeight;
      if (config.fill === true) {
        var available = this._availableHeight();
        if (available > 0) height = Math.min(maxHeight, Math.max(baseHeight, available));
        // 父容器尺寸变化时也要重绘；父容器可能晚于 connectedCallback 才挂上，这里补观察
        if (this._ro && this.parentNode && !this._parentObserved) {
          try { this._ro.observe(this.parentNode); this._parentObserved = true; } catch (e) {}
        }
      }
      var width = Math.round(this.clientWidth || (this.parentNode && this.parentNode.clientWidth) || 640);
      if (width < 240) width = 240;
      this._lastWidth = width;

      var color = config.color || '#818cf8';
      var unit = config.unit || '';
      var emptyText = config.emptyText || '暂无数据';
      var count = values.length;
      if (!count || !labels.length) {
        this._geo = null;
        this.innerHTML = '<div class="flex items-center justify-center text-xs text-gray-600" style="height:' + height + 'px">' + escape(emptyText) + '</div>';
        this._lastSig = this._sizeSignature();
        return;
      }

      var padLeft = 44, padRight = 14, padTop = 16, padBottom = 26;
      var innerW = Math.max(10, width - padLeft - padRight);
      var innerH = Math.max(10, height - padTop - padBottom);
      var maxValue = values.reduce(function(a, b) { return Math.max(a, b); }, 0);
      // Y 轴上界取“好看的整数”，保证 4 等分刻度都是整数；全 0 时给 4，避免除零。
      var niceMax = (function(v) {
        if (v <= 4) return 4;
        var exp = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
        var frac = v / exp;
        var mult = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
        return mult * exp;
      })(maxValue);
      var xAt = function(i) { return count === 1 ? padLeft + innerW / 2 : padLeft + innerW * i / (count - 1); };
      var yAt = function(v) { return padTop + innerH * (1 - v / niceMax); };
      var tickLabel = function(v) { return Number.isInteger(v) ? String(v) : v.toFixed(1); };
      var showDots = count <= 40;
      this._hasDots = showDots;
      this._geo = {
        labels: labels,
        details: details,
        unit: unit,
        format: typeof config.format === 'function' ? config.format : null,
        points: values.map(function(v, i) { return { x: xAt(i), y: yAt(v), value: v }; })
      };

      var svg = '<svg width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '" role="img" style="display:block;overflow:visible">';
      // 水平网格 + Y 轴刻度（4 等分）
      for (var t = 0; t <= 4; t++) {
        var gy = padTop + innerH * t / 4;
        var gv = niceMax * (1 - t / 4);
        svg += '<line x1="' + padLeft + '" y1="' + gy.toFixed(1) + '" x2="' + (padLeft + innerW) + '" y2="' + gy.toFixed(1) + '" stroke="#33334d" stroke-width="1"' + (t === 4 ? '' : ' stroke-dasharray="3 4"') + '/>';
        svg += '<text x="' + (padLeft - 6) + '" y="' + (gy + 3).toFixed(1) + '" text-anchor="end" font-size="10" fill="#6b7280">' + escape(tickLabel(gv)) + '</text>';
      }
      // 面积 + 折线
      var linePoints = values.map(function(v, i) { return xAt(i).toFixed(1) + ',' + yAt(v).toFixed(1); });
      var areaPoints = padLeft + ',' + (padTop + innerH) + ' ' + linePoints.join(' ') + ' ' + (padLeft + innerW) + ',' + (padTop + innerH);
      svg += '<polygon points="' + areaPoints + '" fill="' + color + '" fill-opacity="0.14" stroke="none"/>';
      svg += '<polyline points="' + linePoints.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
      // 数据点（点多时只保留折线，避免密集圆点糊成一团）
      values.forEach(function(v, i) {
        var tip = hasDetails ? '' : '<title>' + escape(labels[i]) + ': ' + v + unit + '</title>';
        if (showDots) {
          svg += '<circle data-dot="' + i + '" cx="' + xAt(i).toFixed(1) + '" cy="' + yAt(v).toFixed(1) + '" r="3" fill="#1e1e2e" stroke="' + color + '" stroke-width="2">' + tip + '</circle>';
        } else {
          svg += '<circle cx="' + xAt(i).toFixed(1) + '" cy="' + yAt(v).toFixed(1) + '" r="6" fill="transparent">' + tip + '</circle>';
        }
      });
      // 有 details 时铺一层透明命中区：相邻点中点为边界，整列可悬浮，不要求精准对准圆点
      if (hasDetails) {
        for (var h = 0; h < count; h++) {
          var left = h === 0 ? padLeft : (xAt(h - 1) + xAt(h)) / 2;
          var right = h === count - 1 ? padLeft + innerW : (xAt(h) + xAt(h + 1)) / 2;
          svg += '<rect data-pt="' + h + '" x="' + left.toFixed(1) + '" y="' + padTop + '" width="' + Math.max(1, right - left).toFixed(1) + '" height="' + innerH + '" fill="transparent" pointer-events="all"/>';
        }
      }
      // X 轴刻度：最多显示 8 个，首尾必显示
      var step = Math.max(1, Math.ceil(count / 8));
      labels.forEach(function(label, i) {
        if (i % step !== 0 && i !== count - 1) return;
        svg += '<text x="' + xAt(i).toFixed(1) + '" y="' + (padTop + innerH + 16) + '" text-anchor="middle" font-size="10" fill="#6b7280">' + escape(label) + '</text>';
      });
      // 全 0 时在绘图区中央给出空态文案
      if (maxValue <= 0) {
        svg += '<text x="' + (padLeft + innerW / 2).toFixed(1) + '" y="' + (padTop + innerH / 2 + 4).toFixed(1) + '" text-anchor="middle" font-size="11" fill="#6b7280">' + escape(emptyText) + '</text>';
      }
      svg += '</svg>';
      this.innerHTML = svg;

      if (hasDetails) {
        var svgEl = this.querySelector('svg');
        svgEl.addEventListener('mouseleave', function() { self._hideTip(); });
        this.querySelectorAll('[data-pt]').forEach(function(rect) {
          rect.addEventListener('mouseenter', function() { self._showTip(parseInt(rect.getAttribute('data-pt'), 10)); });
        });
      }
      this._lastSig = this._sizeSignature();
    }
  });
}

// ── akm-donut-chart：通用环形图壳组件（内联 SVG，占比视角） ──
// 用法：
//   var donut = document.getElementById('xxx');
//   donut.render({
//     items: [{ label: 'zcode', value: 4170, details: ['请求 4170', '费用 $114.54'] }],
//     unit: '次',              // 可选，数值单位
//     format: fn,              // 可选，数值格式化（图例 / 圆心 / 浮层共用）
//     maxSlices: 6,            // 可选，最多画几片（含合并出的「其他」），默认 6
//     size: 168,               // 可选，直径 CSS px，默认 168
//     centerLabel: '总计',      // 可选，圆心默认文案
//     emptyText: '暂无数据'     // 可选，无正数项时的空态文案
//   });
// 交互：悬浮扇区或图例行都会弹出浮层（名称 / 数值 / 占比 / details 附加行），
//       同时高亮该片、其余片淡出、圆心切换为该片数值。
// 命名区：扇区按角度命中（不必压在环上，圆心附近除外），长尾小项建议用图例悬浮。
if (!customElements.get('akm-donut-chart')) {
  customElements.define('akm-donut-chart', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      this.style.display = 'block';
    }

    disconnectedCallback() {
      if (this._tipHandle) { this._tipHandle.destroy(); this._tipHandle = null; }
    }

    _tip() {
      if (!this._tipHandle) this._tipHandle = akmFloatingTip();
      return this._tipHandle;
    }

    _escape(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    render(config) {
      config = config || {};
      this._config = config;
      var self = this;
      var escape = function(s) { return self._escape(s); };
      var unit = config.unit || '';
      var format = typeof config.format === 'function'
        ? config.format
        : function(v) { return String(v); };
      var size = Math.max(120, parseInt(config.size || 168, 10) || 168);
      var emptyText = config.emptyText || '暂无数据';
      var maxSlices = Math.max(1, parseInt(config.maxSlices || 6, 10) || 6);
      var palette = config.palette || [
        '#818cf8', '#34d399', '#fbbf24', '#f87171', '#38bdf8', '#a78bfa', '#f472b6', '#2dd4bf'
      ];

      var raw = (Array.isArray(config.items) ? config.items : []).map(function(item) {
        return {
          label: String((item && item.label) || ''),
          value: Number((item && item.value) || 0),
          details: (item && item.details) || []
        };
      }).filter(function(item) { return item.value > 0; });
      raw.sort(function(a, b) { return b.value - a.value; });

      this._slices = [];
      if (!raw.length) {
        this.innerHTML = '<div class="flex items-center justify-center text-xs text-gray-600" style="height:' + size + 'px">' + escape(emptyText) + '</div>';
        return;
      }

      var total = raw.reduce(function(sum, item) { return sum + item.value; }, 0);
      // maxSlices 是「含其他」的总片数：长尾时留一片给「其他」，其余按大小取前几项
      var merged = raw.length > maxSlices ? raw.slice(maxSlices - 1) : [];
      var visible = merged.length ? raw.slice(0, maxSlices - 1) : raw;
      var slices = visible.map(function(item) {
        return { label: item.label, value: item.value, details: item.details };
      });
      if (merged.length) {
        var names = merged.map(function(item) { return item.label; }).join('、');
        slices.push({
          label: '其他',
          value: merged.reduce(function(sum, item) { return sum + item.value; }, 0),
          details: ['合并 ' + merged.length + ' 项：' + (names.length > 60 ? names.slice(0, 60) + '…' : names)]
        });
      }
      slices.forEach(function(item, i) {
        item.color = palette[i % palette.length];
        item.share = item.value / total;
      });
      this._slices = slices;

      var ring = size >= 150 ? 18 : 14;
      var radius = (size - ring) / 2 - 2;
      var cx = size / 2;
      var cy = size / 2;
      var circumference = 2 * Math.PI * radius;
      // 片间留一点缝（单片时不留），视觉上更好分辨
      var gapRatio = slices.length > 1 ? 0.004 : 0;

      var arcs = '';
      var ranges = [];
      var covered = 0;
      slices.forEach(function(item, i) {
        var start = covered;
        covered += item.share * 360;
        ranges.push({ start: start, end: covered, index: i });
        var dash = Math.max(0.5, circumference * item.share - gapRatio * circumference);
        arcs += '<circle data-arc="' + i + '" cx="' + cx + '" cy="' + cy + '" r="' + radius + '"'
          + ' fill="none" stroke="' + item.color + '" stroke-width="' + ring + '"'
          + ' stroke-dasharray="' + dash.toFixed(2) + ' ' + (circumference - dash).toFixed(2) + '"'
          + ' stroke-dashoffset="' + (-circumference * (start / 360)).toFixed(2) + '"'
          + ' transform="rotate(-90 ' + cx + ' ' + cy + ')"/>';
      });
      var centerLabel = config.centerLabel || '总计';
      var center = '<text data-center-label x="' + cx + '" y="' + (cy - 3) + '" text-anchor="middle" font-size="10" fill="#6b7280">' + escape(centerLabel) + '</text>'
        + '<text data-center-value x="' + cx + '" y="' + (cy + 14) + '" text-anchor="middle" font-size="13" font-weight="600" fill="#e5e7eb">' + escape(format(total) + unit) + '</text>';

      var legend = slices.map(function(item, i) {
        return '<div data-legend="' + i + '" class="flex items-center gap-2 px-1.5 py-1 rounded cursor-default">'
          + '<span class="w-2.5 h-2.5 rounded-sm shrink-0" style="background:' + item.color + '"></span>'
          + '<span class="flex-1 truncate text-xs text-gray-300" title="' + escape(item.label) + '">' + escape(item.label) + '</span>'
          + '<span class="text-xs text-gray-400 tabular-nums shrink-0">' + escape(format(item.value) + unit) + '</span>'
          + '<span class="text-xs text-gray-500 tabular-nums shrink-0 w-11 text-right">' + (item.share * 100).toFixed(1) + '%</span>'
          + '</div>';
      }).join('');

      this.innerHTML = '<div style="display:flex;align-items:center;gap:14px">'
        + '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" style="display:block;flex:0 0 auto" role="img">' + arcs + center + '</svg>'
        + '<div style="flex:1;min-width:0">' + legend + '</div>'
        + '</div>'
        + (merged.length
          ? '<div class="text-[10px] text-gray-600 mt-2">仅显示前 ' + maxSlices + ' 项，其余合并为「其他」</div>'
          : '');

      var svgEl = this.querySelector('svg');
      var arcEls = this.querySelectorAll('[data-arc]');
      var legendEls = this.querySelectorAll('[data-legend]');
      var centerLabelEl = this.querySelector('[data-center-label]');
      var centerValueEl = this.querySelector('[data-center-value]');

      var highlight = function(index) {
        for (var i = 0; i < arcEls.length; i++) {
          arcEls[i].setAttribute('stroke-width', String(i === index ? ring + 4 : ring));
          arcEls[i].setAttribute('opacity', index < 0 || i === index ? '1' : '0.4');
        }
        for (var j = 0; j < legendEls.length; j++) {
          legendEls[j].style.background = j === index ? 'rgba(99,102,241,.12)' : '';
        }
        var current = index >= 0 ? slices[index] : null;
        if (centerLabelEl) centerLabelEl.textContent = current ? current.label : centerLabel;
        if (centerValueEl) centerValueEl.textContent = format(current ? current.value : total) + unit;
      };

      var showTip = function(index, anchorX, anchorY) {
        var item = slices[index];
        if (!item) return;
        var lines = [
          item.label + ': ' + format(item.value) + unit,
          '占比 ' + (item.share * 100).toFixed(1) + '%'
        ];
        var details = Array.isArray(item.details) ? item.details : [item.details];
        details.forEach(function(line) {
          if (line != null && String(line) !== '') lines.push(String(line));
        });
        self._tip().show(lines, anchorX, anchorY);
        highlight(index);
      };

      var hideTip = function() {
        self._tip().hide();
        highlight(-1);
      };
      this._highlight = highlight;
      this._hideSliceTip = hideTip;

      // 扇区命中：按角度判定，整个圆盘（除圆心附近）都算命中区，比只压在环上好点
      var innerR = radius - ring / 2;
      var outerR = radius + ring / 2;
      svgEl.addEventListener('mousemove', function(event) {
        var rect = svgEl.getBoundingClientRect();
        var scale = rect.width ? size / rect.width : 1;
        var lx = (event.clientX - rect.left) * scale;
        var ly = (event.clientY - rect.top) * scale;
        var dx = lx - cx;
        var dy = ly - cy;
        var distance = Math.sqrt(dx * dx + dy * dy);
        if (distance < innerR * 0.6 || distance > outerR + 10) { hideTip(); return; }
        // 0° 指向 12 点方向，顺时针增长，与绘制顺序一致
        var deg = ((Math.atan2(dy, dx) * 180 / Math.PI) + 90 + 360) % 360;
        var hit = slices.length - 1;
        for (var i = 0; i < ranges.length; i++) {
          if (deg >= ranges[i].start && deg < ranges[i].end) { hit = i; break; }
        }
        showTip(hit, event.clientX, event.clientY);
      });
      svgEl.addEventListener('mouseleave', hideTip);
      legendEls.forEach(function(row) {
        row.addEventListener('mouseenter', function() {
          var box = row.getBoundingClientRect();
          showTip(parseInt(row.getAttribute('data-legend'), 10), box.left + box.width / 2, box.top);
        });
        row.addEventListener('mouseleave', hideTip);
      });
    }
  });
}

// ── akm-notification：右上角通知组件（支持进度条） ──
// 用法：
//   akmNotify({ id: 'x', type: 'loading', title: '安装插件', message: '下载中...', progress: 40 })
//   akmNotify.update('x', { progress: 80, message: '解压中...' })
//   akmNotify.update('x', { type: 'success', title: '完成', message: '已生效' })  // 非 loading 自动消失
//   akmNotify.close('x')
// type: info | loading | success | error；progress 传 0-100，null/缺省则隐藏进度条；
// 同 id 重复 push 视为更新；loading 不自动关闭，其余默认 5s 关闭。
(function() {
  // 注入一次全局 keyframes（页面可能被多个模板共享）
  if (!document.getElementById('akm-notif-keyframes')) {
    var notifStyle = document.createElement('style');
    notifStyle.id = 'akm-notif-keyframes';
    notifStyle.textContent =
      '@keyframes akmNotifIn{from{opacity:0;transform:translateX(16px)}' +
      'to{opacity:1;transform:none}}';
    (document.head || document.documentElement).appendChild(notifStyle);
  }

  if (!customElements.get('akm-notification')) {
    customElements.define('akm-notification', class extends HTMLElement {
      connectedCallback() {
        if (this.__mounted) return;
        this.__mounted = true;
        this._cards = {};
        this._seq = 0;
        // 固定右上角通知堆栈；容器本身不拦截事件，卡片各自可点
        this.style.cssText =
          'position:fixed;top:16px;right:16px;z-index:999999;' +
          'display:flex;flex-direction:column;gap:8px;' +
          'max-width:340px;width:calc(100vw - 32px);' +
          'pointer-events:none;';
      }

      // 新增通知；id 已存在时改为更新该卡片
      push(cfg) {
        cfg = cfg || {};
        var id = cfg.id || 'akm-notif-' + (++this._seq);
        if (this._cards[id]) {
          this.update(id, cfg);
          return id;
        }
        var card = this._createCard(cfg);
        card.dataset.notifId = id;
        this._cards[id] = card;
        this.appendChild(card);
        this._scheduleClose(card);
        return id;
      }

      // 局部更新：type / title / message / progress
      update(id, patch) {
        var card = this._cards[id];
        if (!card) return;
        patch = patch || {};
        if (patch.type) card.dataset.type = patch.type;
        if (patch.title != null) card.querySelector('[data-notif-title]').textContent = patch.title;
        if (patch.message != null) card.querySelector('[data-notif-message]').textContent = patch.message;
        if (patch.progress != null) this._setProgress(card, patch.progress);
        this._scheduleClose(card);
      }

      // 立即关闭并移除指定通知
      close(id) {
        var card = this._cards[id];
        if (card) this._removeCard(card);
      }

      _createCard(cfg) {
        var card = document.createElement('div');
        card.className = 'akm-notif-card';
        card.style.cssText =
          'pointer-events:auto;display:flex;flex-direction:column;gap:6px;' +
          'background:#111827;border:1px solid #374151;border-radius:8px;' +
          'padding:10px 12px 11px;font-size:12px;line-height:1.6;color:#e5e7eb;' +
          'box-shadow:0 8px 24px rgba(0,0,0,.45);animation:akmNotifIn .18s ease;';
        card.dataset.type = cfg.type || 'info';

        var head = document.createElement('div');
        head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;';
        var title = document.createElement('div');
        title.setAttribute('data-notif-title', '');
        title.style.cssText = 'font-weight:600;font-size:12px;color:#f9fafb;word-break:break-all;';
        title.textContent = cfg.title || '';
        var closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.textContent = '×';
        closeBtn.style.cssText =
          'flex:none;width:18px;height:18px;display:flex;align-items:center;justify-content:center;' +
          'color:#9ca3af;background:transparent;border:none;cursor:pointer;' +
          'font-size:14px;line-height:1;padding:0;border-radius:4px;';
        closeBtn.addEventListener('click', function() { this._removeCard(card); }.bind(this));
        head.appendChild(title);
        head.appendChild(closeBtn);

        var msg = document.createElement('div');
        msg.setAttribute('data-notif-message', '');
        msg.style.cssText = 'color:#9ca3af;white-space:pre-line;word-break:break-word;';
        msg.textContent = cfg.message || '';

        // 进度条：progress 缺省时隐藏
        var bar = document.createElement('div');
        bar.setAttribute('data-notif-bar', '');
        bar.style.cssText =
          'height:3px;border-radius:9999px;background:#374151;overflow:hidden;' +
          (cfg.progress == null ? 'display:none;' : '');
        var fill = document.createElement('div');
        fill.style.cssText = 'height:100%;width:0%;transition:width .2s ease;background:#6366f1;';
        bar.appendChild(fill);

        card.appendChild(head);
        card.appendChild(msg);
        card.appendChild(bar);
        this._setProgress(card, cfg.progress);
        return card;
      }

      _setProgress(card, progress) {
        var bar = card.querySelector('[data-notif-bar]');
        if (!bar) return;
        if (progress == null) {
          bar.style.display = 'none';
          return;
        }
        bar.style.display = '';
        var pct = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
        var fill = bar.firstChild;
        if (fill) fill.style.width = pct + '%';
        // 进度条颜色随类型切换：success 绿 / error 红 / 其余 indigo
        var color = '#6366f1';
        if (card.dataset.type === 'success') color = '#34d399';
        if (card.dataset.type === 'error') color = '#f87171';
        fill.style.background = color;
      }

      // loading 类型不自动关闭；其他类型 duration（默认 5000ms）后自动收起
      _scheduleClose(card) {
        clearTimeout(card._closeTimer);
        if (card.dataset.type === 'loading') return;
        var dur = parseInt(card.getAttribute('duration') || card.dataset.duration || '5000', 10);
        var self = this;
        card._closeTimer = setTimeout(function() { self._removeCard(card); }, dur);
      }

      _removeCard(card) {
        if (!card.isConnected) return;
        delete this._cards[card.dataset.notifId];
        card.style.transition = 'opacity .25s ease, transform .25s ease';
        card.style.opacity = '0';
        card.style.transform = 'translateX(12px)';
        setTimeout(function() { if (card.isConnected) card.remove(); }, 260);
      }
    });
  }

  // 全局便捷入口（惰性创建单例容器并挂到 body）
  if (!window.akmNotify) {
    var notifyHost = null;
    function ensureNotifyHost() {
      if (!notifyHost || !notifyHost.isConnected) {
        notifyHost = document.querySelector('akm-notification');
        if (!notifyHost) {
          notifyHost = document.createElement('akm-notification');
          document.body.appendChild(notifyHost);
        }
      }
      return notifyHost;
    }
    window.akmNotify = function(cfg) { return ensureNotifyHost().push(cfg); };
    window.akmNotify.update = function(id, patch) { ensureNotifyHost().update(id, patch); };
    window.akmNotify.close = function(id) { ensureNotifyHost().close(id); };
  }
})();
