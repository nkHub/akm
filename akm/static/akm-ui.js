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

/* 自建浮层的无障碍支持：弹窗标题需要唯一 id 供 aria-labelledby 引用，这里用递增序号生成。
   放在文件顶部是因为 akm-modal / akm-drawer 的基类方法会引用它。 */
var AKM_DIALOG_SEQ = 0;

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

  /**
   * 打开弹窗/抽屉时的无障碍处理（P0 四项之一，作用于 akm-modal 与 akm-drawer 基类）。
   *
   * 为什么要有这些：本项目是自建遮罩（没有用原生 <dialog>），而原生 modal 由浏览器
   * 白送的三件事——dialog 语义、焦点移入并限制在浮层内、关闭后焦点归还——都得自己补。
   * 实测改造前：打开「添加 Key」后焦点仍停在 BODY，弹窗内 11 个可聚焦元素之外还有
   * 71 个，Tab 越过最后一个按钮会落回背后的侧栏链接。
   *
   * 做法对齐无障碍指南的「自建浮层」分支：给容器 role="dialog" + aria-modal，
   * 用 inert 屏蔽兄弟内容（不手写焦点陷阱），并把焦点移进浮层。
   */

  /** 记录打开前的焦点来源，关闭后归还（指南：关闭后焦点回到触发元素） */
  _captureFocusOrigin() {
    this._focusOrigin = document.activeElement;
  }

  /**
   * 给浮层加 dialog 语义并按可见标题建立可访问名。
   *
   * 弹窗容器本身就是浮层容器，role 加在它上面即可；标题用 data-title 里的元素，
   * 有标题时用 aria-labelledby 指向它（标题文案会随 setTitle 变化，用引用比复制文案稳）。
   * 没有可见标题时退回 aria-label 兜底，避免出现「有 role 但没有可访问名」。
   */
  _applyDialogSemantics(container, fallbackLabel) {
    if (!container) return;
    container.setAttribute('role', 'dialog');
    container.setAttribute('aria-modal', 'true');
    var titleEl = this.querySelector('[data-title]');
    if (titleEl) {
      // 标题需要 id 才能被 aria-labelledby 引用；同一个标题元素只生成一次
      if (!titleEl.id) {
        titleEl.id = 'akm-dialog-title-' + (++AKM_DIALOG_SEQ);
      }
      // 标题文案可能要到 open() 前后才由调用方填入（akm-drawer 由页面先 setTitle 再
      // open，也有调用方干脆没设）。空标题时**不能**留着 aria-labelledby：ARIA 里
      // labelledby 优先级高于 label，指向空元素会让可访问名整体变空（实测 getAttribute
      // 两者都在、但算出来的名字是空串），所以此时改挂 aria-label 兜底，两属性互斥。
      if (titleEl.textContent.trim()) {
        container.setAttribute('aria-labelledby', titleEl.id);
        container.removeAttribute('aria-label');
      } else {
        container.removeAttribute('aria-labelledby');
        container.setAttribute('aria-label', fallbackLabel || '对话框');
      }
    } else {
      container.removeAttribute('aria-labelledby');
      container.setAttribute('aria-label', fallbackLabel || '对话框');
    }
  }

  /**
   * 对浮层之外的同级节点设置 inert，让它们无法被键盘/指针/辅助技术访问。
   *
   * 关键点：inert 必须加在浮层的**兄弟**上，绝不能加到浮层的祖先（父级 inert 会连带
   * 把浮层本身也屏蔽掉）。本项目弹窗元素是 <body> 的直接子节点，与侧栏、内容区、
   * 折叠脚本同级，因此按「body 子节点中排除浮层自身」来界定范围。
   *
   * 同时排除挂在 body 上的功能性浮层（combobox 下拉菜单等）：它们视觉上属于浮层内部、
   * 需要保持可交互，误设 inert 会直接让下拉点不动。这类节点由组件创建时打上
   * data-akm-popup 标记，这里跳过。
   */
  _setSiblingsInert(active) {
    if (typeof document === 'undefined' || !document.body) return;
    var self = this;
    Array.prototype.forEach.call(document.body.children, function(el) {
      if (el === self || el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'LINK') return;
      if (el.hasAttribute('data-akm-popup')) return;
      if (active) {
        // 记下原本就 inert 的节点，关闭时只恢复我们自己动过的，避免误清
        if (el.hasAttribute('inert')) {
          el.setAttribute('data-akm-inert-keep', '1');
        } else {
          el.setAttribute('inert', '');
        }
      } else {
        if (el.hasAttribute('data-akm-inert-keep')) {
          el.removeAttribute('data-akm-inert-keep');
        } else {
          el.removeAttribute('inert');
        }
      }
    });
  }

  /**
   * 把焦点移进浮层。
   *
   * 优先聚焦 [autofocus] 标注的元素，其次第一个可聚焦元素；都没有时聚焦容器本身
   * （给容器临时 tabindex="-1"，保证读屏软件能落到这个 dialog 上）。
   * 延迟到下一帧：调用方通常在 open() 里刚移除 hidden，标签此时才可聚焦。
   */
  _focusIntoOverlay(container) {
    if (!container) return;
    var self = this;
    var focusables = '[autofocus],a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
    requestAnimationFrame(function() {
      var target = container.querySelector('[autofocus]') || container.querySelector(focusables);
      if (!target) {
        if (!container.hasAttribute('tabindex')) {
          container.setAttribute('tabindex', '-1');
        }
        target = container;
      }
      try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
    });
  }

  /** 关闭后清 inert 并把焦点还给打开前的元素 */
  _releaseOverlayA11y() {
    this._setSiblingsInert(false);
    var origin = this._focusOrigin;
    this._focusOrigin = null;
    if (origin && typeof origin.focus === 'function' && document.contains(origin)) {
      try { origin.focus({ preventScroll: true }); } catch (e) { origin.focus(); }
    }
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
      if (!this.overlay) return;
      // 记录触发元素，关闭后焦点归还给它（指南：关闭后焦点回到打开它的元素）
      this._captureFocusOrigin();
      this.overlay.classList.remove('hidden');
      // 语义：容器＝浮层容器，标题＝可访问名
      this._applyDialogSemantics(this.overlay, this.getAttribute('title') || '对话框');
      // 屏蔽浮层之外的兄弟节点（比手写焦点陷阱可靠，也不会有 Tab 逃逸）
      this._setSiblingsInert(true);
      this._focusIntoOverlay(this.overlay);
      // 滚动锁定：**不再**给 body 加 overflow-hidden。
      // body 本身是 h-screen + overflow-hidden（布局需要），这里再加/再删会把它原本的
      // 隐藏滚动行为一起破坏；且指南明确不要用 html/body 的 overflow 锁滚动（会移除根
      // 滚动条、导致布局横向位移）。浮层是 fixed 定位盖住页面，背后内容已 inert 且
      // 滚动手势由背景层吞掉，无需额外锁。
    }

    close() {
      var wasOpen = this.isOpen();
      if (this.overlay) this.overlay.classList.add('hidden');
      if (wasOpen) this._releaseOverlayA11y();
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
      // 与 akm-modal 一致：带 data-modal-footer 的节点拆到独立底栏，
      // 不随正文滚动。没有 footer 的既有调用方行为不变（footer 容器留空隐藏）。
      var collected = this.collectNodes(true);
      var bodyNodes = collected.bodyNodes;
      var footerNodes = collected.footerNodes;
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
          '<div data-footer class="hidden shrink-0"></div>' +
        '</div>';
      this.overlay = this.querySelector('[data-overlay]');
      this.panel = this.querySelector('[data-panel]');
      this.titleEl = this.querySelector('[data-title]');
      this.bodyEl = this.querySelector('[data-body]');
      this.footerEl = this.querySelector('[data-footer]');
      this.setTitle(title);
      this.style.display = 'contents';
      var self = this;
      bodyNodes.forEach(function(node) { self.bodyEl.appendChild(node); });
      footerNodes.forEach(function(node) { self.footerEl.appendChild(node); });
      if (footerNodes.length) this.footerEl.classList.remove('hidden');
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
      if (!this.overlay || !this.panel) return;
      // 与 akm-modal 同一套：记录焦点来源 → dialog 语义 → 屏蔽兄弟 → 焦点移入
      this._captureFocusOrigin();
      this.overlay.classList.remove('hidden');
      this.panel.classList.remove('hidden');
      this._applyDialogSemantics(this.panel, this.getAttribute('title') || '抽屉');
      this._setSiblingsInert(true);
      this._focusIntoOverlay(this.panel);
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
      // 初始化时会调用 close(true)，那时并未真正打开过，不能去释放 inert / 抢焦点
      var wasOpen = this.isOpen();
      this.panel.style.transform = 'translateX(100%)';
      var finish = function() {
        self.panel.classList.add('hidden');
        self.overlay.classList.add('hidden');
        if (wasOpen) self._releaseOverlayA11y();
      };
      if (immediate) {
        finish();
        return;
      }
      setTimeout(finish, 250);
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
        // data-akm-popup：标记这是挂在 body 上的功能性浮层，弹窗打开做 inert 时要跳过它，
        // 否则提示类浮层会被连坐屏蔽（combobox 下拉更是会直接点不动）。
        tip.setAttribute('data-akm-popup', '');
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
      el.setAttribute('data-akm-popup', '');
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
          notifyHost.setAttribute('data-akm-popup', '');
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


/* ── akm-combobox：下拉选择（shadcn Select 风格）─────────────────
 *
 * 为什么要自建：原生 <select> 的弹出层由系统绘制，深浅色、圆角、层级都不受页面
 * 控制；而供应商这类列表可能到 200+ 条，原生下拉又不能按关键字过滤。
 *
 * 交互对齐 shadcn/ui 的 Select 与 Radix Select：
 *   - **触发器是按钮，不是输入框**：选中项之后不该还能往里打字（那样会让人以为
 *     填错了内容）。点击/Enter/Space/↑↓ 展开菜单。
 *   - **占位符不在选项里**：`placeholder` 只在没有选中值时以灰字显示在触发器上，
 *     菜单里不会出现一条「选择供应商」还能被勾中的假选项。
 *   - **勾选标记固定在最右侧**：选项文案左对齐、勾在右列，且未选中项也保留同宽
 *     的占位空间，文字基线不会因有无勾而左右跳动。
 *   - 支持按前缀/包含**逐字过滤**（打开菜单后打字即进入搜索）——把 Radix Select
 *     只能按首字母跳转的限制补上，长列表才可用。
 *
 * 用法：
 *   <akm-combobox id="key-provider" placeholder="选择供应商"></akm-combobox>
 *   cb.setOptions([{ value: 'openai', label: 'OpenAI', group: '内置' }, ...]);
 *   cb.value          // 当前选中值（'' 表示未选择）
 *   cb.setValue('x')  // 程序设置
 *   cb.addEventListener('change', ...)
 *
 * 键盘：↑↓ 移动高亮（不依赖系统）、Home/End 首尾、Enter 选中、Esc 关闭、
 *       Tab 关闭。打开后输入可打印字符即过滤。
 */

/**
 * 注入组件的兜底样式。
 *
 * 组件同时用于管理台（Tailwind utilities 齐全）与插件 iframe（只有 preflight、
 * 没有生成任何 utilities）。后者里 `px-3` / `text-sm` / `truncate` 全部不存在，
 * 曾导致「选项挤成一坨、勾选标记错位」。这里用一段自洽的内联样式表把结构与
 * 关键度量定死，不依赖外部工具类。
 *
 * 配色一律用 `rgb(var(--c-*), 回退值)`：管理台有这套 token（自动跟随主题），
 * 插件页没有时用回退值，两边都不会出现无背景/无边框的白板下拉。
 */
function ensureComboboxStyles() {
  if (typeof document === 'undefined' || document.getElementById('akm-combobox-style')) return;
  var style = document.createElement('style');
  style.id = 'akm-combobox-style';
  style.textContent = [
    /* ── 触发器 ── */
    '.akm-cb__trigger{display:flex;align-items:center;justify-content:space-between;gap:8px;',
    '  width:100%;box-sizing:border-box;padding:8px 12px;font-size:13px;line-height:1.5;',
    '  text-align:left;cursor:pointer;border-radius:6px;',
    '  background:rgb(var(--c-surface,30 30 46));',
    '  border:1px solid rgb(var(--c-border,51 51 77));',
    '  color:rgb(var(--c-gray-200,229 231 235));',
    '  transition:border-color .15s}',
    '.akm-cb__trigger:hover{border-color:rgb(var(--c-border-light,64 64 96))}',
    '.akm-cb__trigger:focus-visible{outline:none;border-color:rgb(var(--c-indigo-500,99 102 241));',
    '  box-shadow:0 0 0 3px rgb(var(--c-indigo-500,99 102 241) / .18)}',
    /* 未选中时触发器显示占位符：用 class 控制颜色，避免与选中值混在一起 */
    '.akm-cb__trigger.is-placeholder .akm-cb__value{color:rgb(var(--c-gray-500,107 114 128))}',
    '.akm-cb__value{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.akm-cb__chevron{flex:0 0 auto;width:14px;height:14px;opacity:.65;transition:transform .15s}',
    '.akm-cb__trigger[aria-expanded="true"] .akm-cb__chevron{transform:rotate(180deg)}',
    /* ── 菜单 ── */
    '[data-combobox-menu]{box-sizing:border-box;z-index:1000}',
    '[data-combobox-index]{display:flex;align-items:center;gap:8px;width:100%;box-sizing:border-box;',
    '  padding:7px 12px;font-size:13px;line-height:1.5;text-align:left;border:0;',
    '  background:transparent;cursor:pointer;',
    '  color:rgb(var(--c-gray-200,229 231 235))}',
    '[data-combobox-index]:hover{background:rgb(var(--c-surface-hover,64 64 96) / .6)}',
    /* 高亮类由 JS 切换（沿用管理台类名，Tailwind 存在时也能叠加） */
    '[data-combobox-index].bg-surface-hover{background:rgb(var(--c-surface-hover,64 64 96) / .9)}',
    /* 选项文案：占满剩余宽度并截断 */
    '[data-combobox-index] > .akm-cb__label{flex:1 1 auto;min-width:0;',
    '  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    /* 勾选标记：固定在最右侧，未选中时留同宽空位，保证文字不左右跳动 */
    '[data-combobox-index] > .akm-cb__check{flex:0 0 auto;width:14px;height:14px;',
    '  display:inline-flex;align-items:center;justify-content:center;',
    '  color:rgb(var(--c-indigo-400,129 140 248))}',
    '[data-combobox-index][aria-selected="true"]{color:rgb(var(--c-indigo-300,165 180 252))}',
    /* 分组标题 */
    '[data-combobox-group]{padding:6px 12px 2px;font-size:11px;line-height:1.4;',
    '  color:rgb(var(--c-gray-500,107 114 128));user-select:none}',
    /* 空态 */
    '[data-combobox-empty]{padding:8px 12px;font-size:12px;',
    '  color:rgb(var(--c-gray-500,107 114 128))}'
  ].join('');
  document.head.appendChild(style);
}

if (!customElements.get('akm-combobox')) {
  customElements.define('akm-combobox', class extends HTMLElement {
    connectedCallback() {
      if (this.__mounted) return;
      this.__mounted = true;
      ensureComboboxStyles();
      // DOM 移动（弹窗重渲染）会走 disconnected → connected：_options / _value
      // 是调用方之前设好的，**不能清空**，否则重挂载后下拉变空、选中值丢失。
      if (!Array.isArray(this._options)) this._options = [];
      this._filtered = this._options.slice();
      this._activeIndex = -1;
      if (typeof this._value !== 'string') this._value = '';
      this._open = false;
      this._searchText = '';
      this.render();
      this._bindEvents();
      this._syncTrigger();
      if (this.hasAttribute('value')) this.setValue(this.getAttribute('value'), true);
    }

    /** 转义 HTML，避免选项文案里的尖括号破坏结构 */
    escape(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    /** 触发器：整体是一个 button（shadcn 的 SelectTrigger），不是输入框 */
    render() {
      var placeholder = this.getAttribute('placeholder') || this.getAttribute('label') || '';
      this.className = (this.getAttribute('host-class') || 'block').trim();
      this.innerHTML = '' +
        '<button type="button" data-combobox-trigger class="akm-cb__trigger" ' +
          'aria-haspopup="listbox" aria-expanded="false">' +
          '<span class="akm-cb__value" data-combobox-value>' + this.escape(placeholder) + '</span>' +
          '<svg class="akm-cb__chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24" ' +
            'aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" ' +
            'stroke-width="2" d="M19 9l-7 7-7-7"/></svg>' +
        '</button>';
    }

    _bindEvents() {
      var self = this;
      this._trigger = this.querySelector('[data-combobox-trigger]');
      this._valueEl = this.querySelector('[data-combobox-value]');

      // 菜单挂到 body：弹窗正文是可滚动容器（overflow-y-auto），absolute 菜单会被
      // 裁掉（实测 clientHeight 只有 69px，表现是「点了没有下拉列表」）。
      // 挂 body + position:fixed 完全不受父级裁剪影响。
      this._menu = document.createElement('div');
      this._menu.setAttribute('data-combobox-menu', '');
      this._menu.setAttribute('role', 'listbox');
      this._menu.className = 'hidden';
      // 层级/定位/盒模型用内联样式写死，不依赖 Tailwind（插件页没有 utilities）
      this._menu.style.position = 'fixed';
      this._menu.style.zIndex = '1000';
      this._menu.style.boxSizing = 'border-box';
      this._menu.style.overflowY = 'auto';
      this._menu.style.background = 'rgb(var(--c-surface-light,38 38 55))';
      this._menu.style.border = '1px solid rgb(var(--c-border,51 51 77))';
      this._menu.style.borderRadius = '8px';
      this._menu.style.boxShadow = '0 8px 24px rgb(0 0 0 / .28)';
      this._menu.style.padding = '4px 0';
      // 菜单挂在 body 上、不是弹窗子节点；打 data-akm-popup 让弹窗的 inert 逻辑
      // 跳过它，否则弹窗里的 combobox 一点下拉就被 inert 屏蔽、选项点不动。
      this._menu.setAttribute('data-akm-popup', '');
      document.body.appendChild(this._menu);

      this._trigger.addEventListener('mousedown', function(ev) {
        // 用 mousedown 而非 click：避免与 document 的「点击外部收起」抢顺序
        ev.preventDefault();
        if (self._open) self.closeMenu(); else { self._trigger.focus(); self.openMenu(); }
      });
      this._trigger.addEventListener('keydown', function(ev) { self._onKeydown(ev); });

      // 点击组件外部收起（捕获阶段，避免被其它 click 逻辑吞掉）
      this._onDocClick = function(ev) {
        if (!self.contains(ev.target) && self._menu && !self._menu.contains(ev.target)) {
          self.closeMenu();
        }
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
      // 菜单挂在 body 上，组件移除时必须自己清理，否则会残留孤儿下拉
      if (this._menu && this._menu.parentNode) this._menu.parentNode.removeChild(this._menu);
      this._menu = null;
      // **必须重置 __mounted**：弹窗重渲染时元素会被移出再插回，浏览器依次触发
      // disconnected → connected；不清标记的话 connectedCallback 会直接 return，
      // 菜单再也不创建 —— 表现就是「编辑 Key 时供应商点了没下拉」。
      this.__mounted = false;
      this._trigger = null;
      this._valueEl = null;
    }

    _onKeydown(ev) {
      var key = ev.key;
      if (key === 'ArrowDown' || key === 'ArrowUp') {
        ev.preventDefault();
        if (!this._open) { this.openMenu(); return; }
        var count = this._filtered.length;
        if (!count) return;
        var step = key === 'ArrowDown' ? 1 : -1;
        this._activeIndex = (this._activeIndex + step + count) % count;
        this._paintActive();
        return;
      }
      if (key === 'Home' || key === 'End') {
        if (!this._open || !this._filtered.length) return;
        ev.preventDefault();
        this._activeIndex = key === 'Home' ? 0 : this._filtered.length - 1;
        this._paintActive();
        return;
      }
      if (key === 'Enter' || key === ' ') {
        ev.preventDefault();
        // 关闭状态：Enter / Space / ↓ 都用于展开（对齐 Radix Select 的行为）
        if (!this._open) { this.openMenu(); return; }
        // 展开状态：确认当前高亮项
        if (this._filtered[this._activeIndex]) {
          this.setValue(this._filtered[this._activeIndex].value);
        }
        return;
      }
      if (key === 'Escape') {
        if (!this._open) return;
        ev.preventDefault();
        this.closeMenu();
        return;
      }
      if (key === 'Tab') {
        if (this._open) this.closeMenu();
        return;
      }
      // 打开状态下打可打印字符 → 进入搜索（补齐系统下拉「只能首字母跳转」的不足）
      if (this._open && key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        ev.preventDefault();
        this._searchText += key;
        this._applyFilter();
      }
    }

    /**
     * 设置候选项。
     *
     * 注意：**不**过滤 value 为 '' 的候选项。占位符由 placeholder 承担，
     * 但部分调用方需要「显式空值」作为真实选项（例如插件的 reranker_model 用
     * 空字符串表示「不启用 rerank」，插件可选项用空值表示「关闭」）。
     * 不希望出现空值项时由调用方自行过滤（keys.html 的供应商下拉即如此）。
     */
    setOptions(options, currentValue) {
      var raw = Array.isArray(options) ? options : [];
      this._options = raw.filter(function(item) {
        return item != null && item.value !== undefined && item.value !== null;
      }).map(function(item) {
        return { value: String(item.value), label: item.label, group: item.group };
      });
      // 第二个参数：一次性设初值（静默，不派发 change）
      if (currentValue !== undefined && currentValue !== null) {
        this._value = String(currentValue);
      }
      if (this._open) this._applyFilter(); else this._filtered = this._options.slice();
      this._syncTrigger();
    }

    /** 设置当前值；silent=true 时不派发 change（用于初始化回显） */
    setValue(value, silent) {
      var next = value == null ? '' : String(value);
      var found = null;
      for (var i = 0; i < this._options.length; i++) {
        if (String(this._options[i].value) === next) { found = this._options[i]; break; }
      }
      // 允许设置不在候选里的值（例如老 key 的私有 provider 名），触发器显示原文
      this._value = found ? String(found.value) : next;
      this._searchText = '';
      this._syncTrigger();
      this.closeMenu();
      if (!silent) {
        this.dispatchEvent(new CustomEvent('change', {
          detail: { value: this._value }, bubbles: true
        }));
      }
    }

    get value() { return this._value; }
    set value(v) { this.setValue(v, true); }

    /** 同步触发器文案与占位态 */
    _syncTrigger() {
      if (!this._valueEl) return;
      var found = null;
      for (var i = 0; i < this._options.length; i++) {
        if (String(this._options[i].value) === this._value) { found = this._options[i]; break; }
      }
      var text = found ? String(found.label || found.value) : this._value;
      var placeholder = this.getAttribute('placeholder') || '';
      var empty = !text;
      this._valueEl.textContent = empty ? placeholder : text;
      if (empty) this._valueEl.classList.add('is-placeholder');
      else this._valueEl.classList.remove('is-placeholder');
      // 未选中时整个触发器也走占位态配色
      if (this._trigger) {
        if (empty) this._trigger.classList.add('is-placeholder');
        else this._trigger.classList.remove('is-placeholder');
      }
    }

    /** 按当前搜索词过滤候选（匹配 label 与 value，忽略大小写） */
    _applyFilter() {
      var kw = String(this._searchText || '').trim().toLowerCase();
      this._filtered = !kw ? this._options.slice() : this._options.filter(function(item) {
        var label = String(item.label || '').toLowerCase();
        var value = String(item.value || '').toLowerCase();
        return label.indexOf(kw) >= 0 || value.indexOf(kw) >= 0;
      });
      // 过滤后高亮第一项，Enter 立即可选
      this._activeIndex = this._filtered.length ? 0 : -1;
      this._renderMenu();
    }

    _renderMenu() {
      if (!this._menu) return;
      if (!this._filtered.length) {
        this._menu.innerHTML = '<div data-combobox-empty>无匹配项</div>';
        return;
      }
      var html = '';
      var lastGroup = null;
      for (var i = 0; i < this._filtered.length; i++) {
        var item = this._filtered[i];
        var group = item.group || '';
        // 分组标题只做视觉分隔，不参与选中
        if (group && group !== lastGroup) {
          html += '<div data-combobox-group>' + this.escape(group) + '</div>';
          lastGroup = group;
        }
        var selected = String(item.value) === this._value;
        // 主文案取 label；desc 是「实际会被存下来的值」，两者相同则只显示一次
        var label = String(item.label == null ? item.value : item.label);
        var desc = item.desc == null ? '' : String(item.desc);
        if (desc && desc === label) desc = '';
        html += '<button type="button" role="option" data-combobox-index="' + i + '"' +
          ' aria-selected="' + (selected ? 'true' : 'false') + '"' +
          ' class="transition-colors' + (i === this._activeIndex ? ' bg-surface-hover' : '') + '">' +
          // 文案占满剩余宽度，desc 紧随其后，勾选标记固定在右端
          '<span class="akm-cb__label">' + this.escape(label) + '</span>' +
          (desc
            ? '<span class="akm-cb__desc" style="flex:0 0 auto;font-size:11px;opacity:.6;' +
              'font-family:ui-monospace,SFMono-Regular,Menlo,monospace">' + this.escape(desc) + '</span>'
            : '') +
          '<span class="akm-cb__check" aria-hidden="true">' +
            (selected
              ? '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" ' +
                'd="M5 13l4 4L19 7"/></svg>'
              : '') +
          '</span>' +
          '</button>';
      }
      this._menu.innerHTML = html;
      var self = this;
      this._menu.querySelectorAll('[data-combobox-index]').forEach(function(btn) {
        // 用 mousedown + preventDefault：否则触发器先失焦导致菜单收起
        btn.addEventListener('mousedown', function(ev) {
          ev.preventDefault();
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
        buttons[i].classList.toggle('bg-surface-hover', i === this._activeIndex);
      }
      var activeBtn = buttons[this._activeIndex];
      if (activeBtn && activeBtn.scrollIntoView) activeBtn.scrollIntoView({ block: 'nearest' });
    }

    openMenu() {
      if (!this._menu || this._open) return;
      this._open = true;
      this._searchText = '';
      this._trigger.setAttribute('aria-expanded', 'true');
      this._menu.classList.remove('hidden');
      this._placeMenu();
      this._applyFilter();
      // 打开时把高亮落到「当前选中项」；没有选中则落第一项
      var idx = -1;
      for (var i = 0; i < this._filtered.length; i++) {
        if (String(this._filtered[i].value) === this._value) { idx = i; break; }
      }
      this._activeIndex = idx >= 0 ? idx : (this._filtered.length ? 0 : -1);
      this._paintActive();

      // 菜单是 fixed 定位：页面/弹窗滚动会与触发器脱节，滚动时重新定位，
      // 触发器滚出视口则直接收起。
      var self = this;
      if (!this._onScroll) {
        this._onScroll = function() {
          if (!self._open) return;
          var r = self._trigger.getBoundingClientRect();
          if (r.bottom < 0 || r.top > window.innerHeight) { self.closeMenu(); return; }
          self._placeMenu();
        };
        window.addEventListener('scroll', this._onScroll, true);
        window.addEventListener('resize', this._onScroll);
      }
    }

    /** 决定朝下还是朝上展开，并跟随触发器宽度（不超过视口） */
    _placeMenu() {
      if (!this._menu || !this._trigger) return;
      var MENU_MIN_SPACE = 220;   // 大约放得下 6~7 个选项
      var rect = this._trigger.getBoundingClientRect();
      var spaceBelow = window.innerHeight - rect.bottom;
      var spaceAbove = rect.top;
      var flipUp = spaceBelow < MENU_MIN_SPACE && spaceAbove > spaceBelow;
      var avail = Math.max(120, Math.min(280, (flipUp ? spaceAbove : spaceBelow) - 12));
      this._menu.style.left = rect.left + 'px';
      this._menu.style.width = Math.min(rect.width, window.innerWidth - 16) + 'px';
      this._menu.style.maxHeight = avail + 'px';
      if (flipUp) {
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
      this._searchText = '';
      this._menu.classList.add('hidden');
      if (this._trigger) this._trigger.setAttribute('aria-expanded', 'false');
      if (this._onScroll) {
        window.removeEventListener('scroll', this._onScroll, true);
        window.removeEventListener('resize', this._onScroll);
        this._onScroll = null;
      }
    }
  });
}
