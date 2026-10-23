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
        '<div class="switch-track bg-switch-off flex items-center"><div class="switch-thumb bg-white"></div></div>';
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
        track.classList.toggle('bg-switch-off', !this.checked);
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
      var activeClass = 'text-gray-400 hover:text-strong hover:bg-surface-light cursor-pointer';
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
        // option.icon 为受信任的内联 SVG 字符串（本项目所有图标均为代码内联，非用户输入），
        // 提供时按钮只渲染图标，可读文本退到 title / aria-label，按钮尺寸与纯文字态保持一致。
        var icon = option.icon ? String(option.icon) : '';
        var cls = active
          ? 'bg-indigo-600 text-white text-xs px-3 py-1.5 rounded transition-colors cursor-pointer'
          : 'bg-surface-light border border-border hover:border-indigo-500 text-gray-400 hover:text-gray-200 text-xs px-3 py-1.5 rounded transition-colors cursor-pointer';
        if (icon) cls += ' inline-flex items-center justify-center';
        var text = self.escape(option.title || option.label || '');
        var attrs = icon ? ' title="' + text + '" aria-label="' + text + '"' : (option.title ? ' title="' + text + '"' : '');
        var inner = icon
          ? '<span class="pointer-events-none inline-flex items-center justify-center" aria-hidden="true">' + icon + '</span>'
          : text;
        return '<button type="button" data-role="range-tab" data-value="' + self.escape(option.value) + '"' + attrs + ' class="' + cls + '">' + inner + '</button>';
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
          '<div class="relative bg-surface-light border border-border rounded-lg w-full ' + width + ' shadow-2xl flex flex-col max-h-[calc(100vh-2rem)] ' + panelClass + '" style="animation: slideUp 0.2s ease">' +
            '<div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">' +
              '<div class="min-w-0">' +
                '<h3 data-title class="text-sm font-semibold text-strong"></h3>' +
                '<p data-subtitle class="text-xs text-gray-500 mt-1 hidden"></p>' +
              '</div>' +
              '<button type="button" data-close class="text-gray-400 hover:text-strong transition-colors cursor-pointer">' +
                '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>' +
              '</button>' +
            '</div>' +
            /* 弹窗正文必须能滚动：表单较长时（Key 弹窗 7 个字段）内容会超出视口，
               而 akm-combobox 的下拉是 absolute 定位，父级不可滚时它会被裁在弹窗底边之外，
               表现就是「点供应商输入框没有下拉列表」。给正文 max-h + overflow-y-auto 后，
               下拉可以在正文内部撑开并随滚动可见。 */
            '<div data-body class="' + bodyClass + ' overflow-y-auto min-h-0"></div>' +
            '<div data-footer class="hidden shrink-0"></div>' +
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
            '<h3 data-title class="text-sm font-semibold text-strong"></h3>' +
            '<button type="button" data-close class="text-gray-400 hover:text-strong transition-colors cursor-pointer">' +
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

    // 宿主被移除时（例如表格整体重渲染）mouseleave 不会再触发，浮层会永久留在页面上；
    // 且每个实例都会往 body 挂一个浮层节点，重建型的列表会不断堆积。
    // 这里主动收起并从 body 摘除浮层，保证浮层生命周期不超过它的宿主。
    disconnectedCallback() {
      this.hide();
      if (this._tipEl && this._tipEl.parentNode) {
        this._tipEl.parentNode.removeChild(this._tipEl);
      }
      this._tipEl = null;
    }

    // 惰性创建页面级 fixed 浮层：挂在 body 下，避免被表格等容器的 overflow 裁剪
    _tip() {
      if (!this._tipEl) {
        var tip = document.createElement('div');
        tip.style.cssText =
          'position:fixed;z-index:9999;display:none;pointer-events:none;' +
          'max-width:320px;padding:8px 10px;font-size:11px;line-height:1.7;' +
          'white-space:pre-line;text-align:left;color:rgb(var(--c-tooltip-text));' +
          'background:rgb(var(--c-tooltip-bg));border:1px solid rgb(var(--c-tooltip-border));border-radius:6px;' +
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
        'white-space:pre-line;text-align:left;color:rgb(var(--c-tooltip-text));' +
        'background:rgb(var(--c-tooltip-bg));border:1px solid rgb(var(--c-tooltip-border));border-radius:6px;' +
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
          'background:rgb(var(--c-tooltip-bg));border:1px solid rgb(var(--c-tooltip-border));border-radius:8px;' +
          'padding:10px 12px 11px;font-size:12px;line-height:1.6;color:rgb(var(--c-tooltip-text));' +
          'box-shadow:0 8px 24px rgba(0,0,0,.45);animation:akmNotifIn .18s ease;';
        card.dataset.type = cfg.type || 'info';

        var head = document.createElement('div');
        head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;';
        var title = document.createElement('div');
        title.setAttribute('data-notif-title', '');
        title.style.cssText = 'font-weight:600;font-size:12px;color:rgb(var(--c-tooltip-title));word-break:break-all;';
        title.textContent = cfg.title || '';
        var closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.textContent = '×';
        closeBtn.style.cssText =
          'flex:none;width:18px;height:18px;display:flex;align-items:center;justify-content:center;' +
          'color:rgb(var(--c-tooltip-muted));background:transparent;border:none;cursor:pointer;' +
          'font-size:14px;line-height:1;padding:0;border-radius:4px;';
        closeBtn.addEventListener('click', function() { this._removeCard(card); }.bind(this));
        head.appendChild(title);
        head.appendChild(closeBtn);

        var msg = document.createElement('div');
        msg.setAttribute('data-notif-message', '');
        msg.style.cssText = 'color:rgb(var(--c-tooltip-muted));white-space:pre-line;word-break:break-word;';
        msg.textContent = cfg.message || '';

        // 进度条：progress 缺省时隐藏
        var bar = document.createElement('div');
        bar.setAttribute('data-notif-bar', '');
        bar.style.cssText =
          'height:3px;border-radius:9999px;background:rgb(var(--c-tooltip-border));overflow:hidden;' +
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

/* ── akm-combobox：可搜索下拉 ─────────────────────────────────
 *
 * 原生 <select> 无法搜索，供应商列表达到 200+ 条时基本不可用，所以这里提供
 * 一个「输入框 + 过滤列表」的轻量组件。只依赖原生 DOM，不引入第三方库。
 *
 * 用法：
 *   <akm-combobox id="key-provider" placeholder="选择供应商"></akm-combobox>
 *   cb.setOptions([
 *     { value: '', label: '选择供应商' },                    // 空值项（可选）
 *     { value: 'openai', label: 'OpenAI', group: '内置' },
 *     { value: 'dmxapi', label: 'dmxapi', group: '自定义' },
 *   ]);
 *   cb.value          // 当前选中值
 *   cb.setValue('x')  // 程序设置
 *   cb.addEventListener('change', ...)
 *
 * 交互：输入即过滤（同时匹配 label 与 value，忽略大小写）；↑↓ 移动高亮；
 * Enter 选中；Esc 关闭；点击组件外部自动收起。
 */
if (!customElements.get('akm-combobox')) {
  customElements.define('akm-combobox', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      // DOM 移动（弹窗重渲染）会走 disconnected → connected：此时 _options / _value
      // 是调用方之前设好的，**不能清空**，否则重新挂载后下拉变空、选中值丢失。
      if (!Array.isArray(this._options)) this._options = [];
      this._filtered = this._options.slice();
      this._activeIndex = -1;
      if (typeof this._value !== 'string') this._value = '';
      this._open = false;
      this.render();
      this._bindEvents();
      // 重新挂载时把已选值回填到新渲染出来的 input 上
      this._syncInputToValue();
      // 初始值来自 value 属性
      if (this.hasAttribute('value')) this.setValue(this.getAttribute('value'), true);
    }

    /** 转义 HTML，避免选项文案里的尖括号破坏结构 */
    escape(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    render() {
      var placeholder = this.getAttribute('placeholder') || '';
      this.className = (this.getAttribute('host-class') || 'relative block').trim();
      this.innerHTML = '' +
        '<div class="relative">' +
          '<input type="text" data-combobox-input autocomplete="off" spellcheck="false"' +
            ' placeholder="' + this.escape(placeholder) + '"' +
            ' class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 cursor-text">' +
          '<button type="button" data-combobox-toggle tabindex="-1" class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 cursor-pointer">' +
            '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>' +
          '</button>' +
        '</div>';
      // 下拉菜单**挂到 body**（不是组件内部）：
      // 弹窗正文是可滚动容器（overflow-y-auto），absolute 菜单会被它裁掉 ——
      // 实测 clientHeight 只有 69px（应 256px），表现就是「点供应商没有下拉列表」。
      // 挂到 body + position:fixed 后完全不受父级裁剪影响。
    }

    _bindEvents() {
      var self = this;
      this._input = this.querySelector('[data-combobox-input]');
      // 菜单元素建一次、挂在 body 上复用；不用时只是 hidden
      this._menu = document.createElement('div');
      this._menu.setAttribute('data-combobox-menu', '');
      this._menu.className = 'hidden z-[60] overflow-y-auto bg-surface-light border border-border rounded-lg shadow-xl py-1';
      // position 用内联样式写死：只靠 Tailwind 的 `fixed` 类在没加载该类的环境
      // （独立预览页、样式尚未就绪的首帧）会退化成 static，菜单就跑到文档流里去了。
      this._menu.style.position = 'fixed';
      document.body.appendChild(this._menu);
      this._toggle = this.querySelector('[data-combobox-toggle]');

      this._input.addEventListener('focus', function() { self.openMenu(); });
      this._input.addEventListener('input', function() {
        self.openMenu();
        self.filter(self._input.value);
      });
      this._input.addEventListener('keydown', function(ev) { self._onKeydown(ev); });
      if (this._toggle) {
        this._toggle.addEventListener('mousedown', function(ev) {
          ev.preventDefault();
          if (self._open) self.closeMenu(); else { self._input.focus(); self.openMenu(); }
        });
      }
      // 点击组件外部收起（用捕获阶段，避免被其它 click 逻辑吞掉）
      this._onDocClick = function(ev) {
        if (!self.contains(ev.target)) self.closeMenu();
      };
      document.addEventListener('mousedown', this._onDocClick, true);
    }

    disconnectedCallback() {
      if (this._onDocClick) document.removeEventListener('mousedown', this._onDocClick, true);
      if (this._onScroll) {
        window.removeEventListener('scroll', this._onScroll, true);
        window.removeEventListener('resize', this._onScroll);
        this._onScroll = null;
      }
      // 菜单挂在 body 上，组件移除时必须自己清理，否则会残留一个孤儿下拉
      if (this._menu && this._menu.parentNode) this._menu.parentNode.removeChild(this._menu);
      this._menu = null;
      // **必须重置 __mounted**：弹窗重渲染时元素会被移出再插回（DOM 移动），
      // 浏览器会依次触发 disconnected → connected。若不清标记，connectedCallback
      // 会直接 return，菜单再也不会被创建 —— 表现就是「编辑 Key 时供应商点了没下拉」。
      // 这是引入 body 挂载方案后踩到的真实回归。
      this.__mounted = false;
      this._input = null;
      this._toggle = null;
    }

    _onKeydown(ev) {
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (!this._open) this.openMenu();
        var step = ev.key === 'ArrowDown' ? 1 : -1;
        var count = this._filtered.length;
        if (!count) return;
        this._activeIndex = (this._activeIndex + step + count) % count;
        this._paintActive();
        return;
      }
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (this._open && this._activeIndex >= 0 && this._filtered[this._activeIndex]) {
          this.setValue(this._filtered[this._activeIndex].value);
        }
        return;
      }
      if (ev.key === 'Escape') {
        ev.preventDefault();
        this.closeMenu();
        this._syncInputToValue();
      }
    }

    /** 设置候选项；空值项用于「未选择」占位 */
    setOptions(options) {
      this._options = Array.isArray(options) ? options.slice() : [];
      this._syncInputToValue();
      this.filter('');
    }

    /** 设置当前值；silent=true 时不派发 change（用于初始化回显） */
    setValue(value, silent) {
      var next = value == null ? '' : String(value);
      var found = null;
      for (var i = 0; i < this._options.length; i++) {
        if (String(this._options[i].value) === next) { found = this._options[i]; break; }
      }
      // 允许设置不在候选里的值（例如老 key 的私有 provider 名），回显原文
      this._value = found ? String(found.value) : next;
      this._input.value = found ? String(found.label || found.value) : next;
      this.closeMenu();
      if (this._value === '' || found) {
        // 选中后恢复显示 label，不再保留用户输入的搜索词
        this.filter('');
      }
      if (!silent) {
        this.dispatchEvent(new CustomEvent('change', {
          detail: { value: this._value }, bubbles: true
        }));
      }
    }

    get value() { return this._value; }
    set value(v) { this.setValue(v, true); }

    _syncInputToValue() {
      if (!this._input) return;
      for (var i = 0; i < this._options.length; i++) {
        if (String(this._options[i].value) === this._value) {
          this._input.value = String(this._options[i].label || this._options[i].value);
          return;
        }
      }
      this._input.value = this._value;
    }

    /** 按关键字过滤候选（匹配 label 与 value） */
    filter(keyword) {
      var kw = String(keyword || '').trim().toLowerCase();
      var self = this;
      this._filtered = this._options.filter(function(item) {
        if (String(item.value) === '') {
          // 空值项只在没有搜索词时展示，避免干扰搜索
          return kw === '';
        }
        if (!kw) return true;
        var label = String(item.label || '').toLowerCase();
        var value = String(item.value || '').toLowerCase();
        return label.indexOf(kw) >= 0 || value.indexOf(kw) >= 0;
      });
      this._activeIndex = this._filtered.length ? 0 : -1;
      this._renderMenu();
      return self._filtered;
    }

    _renderMenu() {
      if (!this._menu) return;
      if (!this._filtered.length) {
        this._menu.innerHTML = '<div class="px-3 py-2 text-xs text-gray-500">无匹配供应商</div>';
        return;
      }
      var html = '';
      var lastGroup = null;
      for (var i = 0; i < this._filtered.length; i++) {
        var item = this._filtered[i];
        var group = item.group || '';
        // 分组标题只做视觉分隔，不参与搜索匹配
        if (group && group !== lastGroup) {
          html += '<div class="px-3 pt-2 pb-1 text-[11px] text-gray-500 select-none">' + this.escape(group) + '</div>';
          lastGroup = group;
        }
        var selected = String(item.value) === this._value;
        html += '<button type="button" data-combobox-index="' + i + '"' +
          ' class="w-full text-left px-3 py-1.5 text-sm transition-colors cursor-pointer flex items-center justify-between gap-2 ' +
          (i === this._activeIndex ? 'bg-surface-hover text-indigo-300' : (selected ? 'text-indigo-300' : 'text-gray-300')) + '">' +
          '<span class="truncate">' + this.escape(item.label || item.value) + '</span>' +
          (selected ? '<svg class="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>' : '') +
          '</button>';
      }
      this._menu.innerHTML = html;
      var self = this;
      this._menu.querySelectorAll('[data-combobox-index]').forEach(function(btn) {
        btn.addEventListener('mousedown', function(ev) {
          ev.preventDefault();  // 防止 input 先失焦导致菜单收起
          var idx = parseInt(btn.getAttribute('data-combobox-index'), 10);
          var picked = self._filtered[idx];
          if (picked) self.setValue(picked.value);
        });
      });
    }

    _paintActive() {
      if (!this._menu) return;
      var buttons = this._menu.querySelectorAll('[data-combobox-index]');
      for (var i = 0; i < buttons.length; i++) {
        var active = i === this._activeIndex;
        buttons[i].classList.toggle('bg-surface-hover', active);
        buttons[i].classList.toggle('text-indigo-300', active);
      }
      var activeBtn = buttons[this._activeIndex];
      if (activeBtn && activeBtn.scrollIntoView) activeBtn.scrollIntoView({ block: 'nearest' });
    }

    openMenu() {
      if (!this._menu) return;
      this._open = true;
      this._menu.classList.remove('hidden');
      this._placeMenu();
      // 菜单是 fixed 定位，页面/弹窗一滚动就会与输入框脱节：
      // 滚动时重新定位；输入框滚出视口则直接收起，避免菜单浮在半空。
      var self = this;
      if (!this._onScroll) {
        this._onScroll = function() {
          if (!self._open) return;
          var r = self._input.getBoundingClientRect();
          if (r.bottom < 0 || r.top > window.innerHeight) { self.closeMenu(); return; }
          self._placeMenu();
        };
        window.addEventListener('scroll', this._onScroll, true);
        window.addEventListener('resize', this._onScroll);
      }
      // 输入框里显示的可能是「当前选中项的 label」而不是用户打的搜索词。
      // 直接拿它当关键字会误过滤：选中「未选择」时 value 是空串，
      // 而空值项只在 kw === '' 时才展示，结果下拉里连「未选择」自己都不见了。
      // 判定：输入内容等于当前选中项的 label（或等于 value）→ 视为没有搜索词。
      this.filter(this._searchKeyword());
    }

    /**
     * 从输入框内容推导「真实搜索词」。
     *
     * 用户没打字时输入框展示的是选中项的 label，那不算搜索词；
     * 一旦用户改动内容，就不再等于 label，按真实关键字过滤。
     */
    _searchKeyword() {
      var text = String((this._input && this._input.value) || '');
      if (!this._value) {
        // 当前没选中任何值：输入框里若正好是空值项的 label，也算「没搜索」
        for (var i = 0; i < this._options.length; i++) {
          if (String(this._options[i].value) === '' && text === String(this._options[i].label || '')) {
            return '';
          }
        }
        return text;
      }
      for (var j = 0; j < this._options.length; j++) {
        var opt = this._options[j];
        if (String(opt.value) !== this._value) continue;
        if (text === String(opt.label || '') || text === String(opt.value)) return '';
        break;
      }
      return text;
    }

    /**
     * 决定下拉朝下还是朝上展开。
     *
     * 为什么需要：下拉是 `absolute top-full`（贴在输入框下方），而弹窗正文现在
     * 是可滚动的 `overflow-y-auto` 容器。当输入框靠近弹窗底部时（Key 编辑弹窗里
     * 「供应商」正好在中上部、下方还有 5 个字段），朝下展开会被滚动容器裁掉，
     * 用户看到的就是「点了没反应、没有下拉列表」。
     *
     * 判定：下方可用空间不足 `MENU_MIN_SPACE` 且上方更宽裕 → 翻到输入框上方。
     */
    _placeMenu() {
      if (!this._menu || !this._input) return;
      var MENU_MIN_SPACE = 220;   // 大约能放下 6~7 个选项
      var rect = this._input.getBoundingClientRect();
      var spaceBelow = window.innerHeight - rect.bottom;
      var spaceAbove = rect.top;
      var flipUp = spaceBelow < MENU_MIN_SPACE && spaceAbove > spaceBelow;
      var avail = Math.max(120, Math.min(256, (flipUp ? spaceAbove : spaceBelow) - 12));
      this._menu.style.left = rect.left + 'px';
      this._menu.style.width = rect.width + 'px';
      this._menu.style.maxHeight = avail + 'px';
      if (flipUp) {
        // 朝上：菜单底边贴输入框顶边
        this._menu.style.top = 'auto';
        this._menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
      } else {
        this._menu.style.bottom = 'auto';
        this._menu.style.top = (rect.bottom + 4) + 'px';
      }
    }

    closeMenu() {
      if (!this._menu) return;
      this._open = false;
      this._menu.classList.add('hidden');
      if (this._onScroll) {
        window.removeEventListener('scroll', this._onScroll, true);
        window.removeEventListener('resize', this._onScroll);
        this._onScroll = null;
      }
    }
  });
}
