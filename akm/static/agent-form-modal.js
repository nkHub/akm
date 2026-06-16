if (!customElements.get('agent-form-modal')) {
  customElements.define('agent-form-modal', class extends HTMLElement {
    connectedCallback() {
      this.render();
      this.bindEvents();
      this.resetForm();
      this.close();
    }

    render() {
      this.innerHTML = '' +
        '<div data-backdrop class="fixed inset-0 z-40 hidden bg-black/60 px-4 py-6 sm:px-6">' +
          '<div class="flex min-h-full items-center justify-center">' +
            '<div data-panel class="w-full max-w-2xl rounded-xl border border-border bg-surface shadow-2xl">' +
              '<div class="flex items-start justify-between gap-4 border-b border-border px-5 py-4">' +
                '<div>' +
                  '<h4 class="text-base font-semibold text-white">添加供应商代理</h4>' +
                  '<p class="mt-1 text-xs text-gray-500">配置默认 base_url、认证头模板和协议能力，保存后 Key 管理页会自动可选。</p>' +
                '</div>' +
                '<button type="button" data-close class="text-sm text-gray-500 hover:text-gray-300 cursor-pointer">关闭</button>' +
              '</div>' +
              '<div class="px-5 py-4">' +
                '<div class="grid grid-cols-1 gap-3 md:grid-cols-2">' +
                  '<div>' +
                    '<label class="text-xs text-gray-400">名称</label>' +
                    '<input data-field="name" class="mt-1 w-full rounded border border-border bg-surface-light px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500" placeholder="dmxapi">' +
                  '</div>' +
                  '<div>' +
                    '<label class="text-xs text-gray-400">Base URL</label>' +
                    '<input data-field="url" class="mt-1 w-full rounded border border-border bg-surface-light px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500" placeholder="https://www.dmxapi.cn/v1">' +
                  '</div>' +
                  '<div>' +
                    '<label class="text-xs text-gray-400">认证头模板</label>' +
                    '<input data-field="auth" class="mt-1 w-full rounded border border-border bg-surface-light px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500">' +
                  '</div>' +
                  '<div>' +
                    '<label class="text-xs text-gray-400">协议能力</label>' +
                    '<div class="mt-1.5 flex flex-wrap gap-3">' +
                      '<label class="flex items-center gap-1 text-xs text-gray-400 cursor-pointer"><input type="checkbox" data-field="chat"> Chat</label>' +
                      '<label class="flex items-center gap-1 text-xs text-gray-400 cursor-pointer"><input type="checkbox" data-field="responses"> Responses</label>' +
                      '<label class="flex items-center gap-1 text-xs text-gray-400 cursor-pointer"><input type="checkbox" data-field="messages"> Messages</label>' +
                    '</div>' +
                  '</div>' +
                  '<div class="md:col-span-2">' +
                    '<label class="text-xs text-gray-400">Messages 路径开关</label>' +
                    '<label class="mt-1.5 flex items-center gap-2 text-xs text-gray-400 cursor-pointer">' +
                      '<input type="checkbox" data-field="anthropic-path">' +
                      '<span>将 `/v1/messages` 自动转到 `/anthropic/v1/messages`</span>' +
                    '</label>' +
                  '</div>' +
                '</div>' +
                '<p data-msg class="hidden text-xs mt-3"></p>' +
              '</div>' +
              '<div class="flex items-center justify-end gap-2 border-t border-border px-5 py-4">' +
                '<button type="button" data-cancel class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 cursor-pointer">取消</button>' +
                '<button type="button" data-submit class="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 cursor-pointer">保存</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';
      this.backdrop = this.querySelector('[data-backdrop]');
      this.msg = this.querySelector('[data-msg]');
    }

    bindEvents() {
      var self = this;
      this.querySelector('[data-close]').addEventListener('click', function(){ self.close(); });
      this.querySelector('[data-cancel]').addEventListener('click', function(){ self.close(); });
      this.querySelector('[data-submit]').addEventListener('click', function(){ self.submit(); });
      this.backdrop.addEventListener('click', function(event){
        if (event.target === self.backdrop) self.close();
      });
      document.addEventListener('keydown', function(event){
        if (event.key === 'Escape' && self.isOpen()) self.close();
      });
    }

    isOpen() {
      return !this.backdrop.classList.contains('hidden');
    }

    open() {
      this.resetForm();
      this.backdrop.classList.remove('hidden');
      document.body.classList.add('overflow-hidden');
      var nameInput = this.querySelector('[data-field="name"]');
      if (nameInput) nameInput.focus();
    }

    close() {
      this.backdrop.classList.add('hidden');
      document.body.classList.remove('overflow-hidden');
    }

    resetForm() {
      // 每次打开弹窗都回到默认值，避免保留上一次失败或成功后的脏状态。
      this.querySelector('[data-field="name"]').value = '';
      this.querySelector('[data-field="url"]').value = '';
      this.querySelector('[data-field="auth"]').value = 'Bearer {api_key}';
      this.querySelector('[data-field="chat"]').checked = true;
      this.querySelector('[data-field="responses"]').checked = false;
      this.querySelector('[data-field="messages"]').checked = false;
      this.querySelector('[data-field="anthropic-path"]').checked = false;
      this.msg.classList.add('hidden');
      this.msg.textContent = '';
    }

    submit() {
      var self = this;
      var payload = {
        name: this.querySelector('[data-field="name"]').value.trim(),
        default_base_url: this.querySelector('[data-field="url"]').value.trim(),
        default_auth_header: this.querySelector('[data-field="auth"]').value.trim() || 'Bearer {api_key}',
        supports_chat: this.querySelector('[data-field="chat"]').checked,
        supports_responses: this.querySelector('[data-field="responses"]').checked,
        supports_messages: this.querySelector('[data-field="messages"]').checked,
        messages_use_anthropic_path: this.querySelector('[data-field="anthropic-path"]').checked
      };
      // 页面负责实际接口调用，组件只负责收集表单和管理弹窗生命周期，保持边界清晰。
      window.addAgent(payload, this.msg, function(){
        setTimeout(function(){ self.close(); }, 300);
      });
    }
  });
}
