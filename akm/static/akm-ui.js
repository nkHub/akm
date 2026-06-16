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
        '<div data-panel class="hidden fixed top-0 right-0 z-50 h-full w-full ' + width + ' bg-surface-light border-l border-border shadow-2xl flex flex-col" style="transform:translateX(100%); transition: transform 0.25s ease">' +
          '<div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">' +
            '<h3 data-title class="text-sm font-semibold text-white"></h3>' +
            '<button type="button" data-close class="text-gray-400 hover:text-white transition-colors cursor-pointer">' +
              '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>' +
            '</button>' +
          '</div>' +
          '<div data-body class="flex flex-col flex-1 min-h-0"></div>' +
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
