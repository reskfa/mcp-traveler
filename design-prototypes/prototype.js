(() => {
  const toast = document.createElement('div');
  toast.className = 'prototype-toast';
  toast.setAttribute('role', 'status');
  toast.hidden = true;
  document.body.appendChild(toast);
  let toastTimer;
  function notify(message) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  document.querySelectorAll('[data-toast]').forEach(button => {
    button.addEventListener('click', event => {
      if (button.tagName === 'A') event.preventDefault();
      notify(button.dataset.toast);
    });
  });

  document.querySelectorAll('[data-fields]').forEach(group => {
    const buttons = [...group.querySelectorAll('button')];
    const output = document.querySelector(`[data-field-count="${group.dataset.fields}"]`);
    const update = () => {
      if (output) output.textContent = `${buttons.filter(b => b.getAttribute('aria-pressed') === 'true').length} 个字段`;
    };
    buttons.forEach(button => button.addEventListener('click', () => {
      const selected = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(selected));
      button.classList.toggle('is-off', !selected);
      update();
    }));
    update();
  });

  document.querySelectorAll('[data-run]').forEach(button => {
    button.addEventListener('click', () => {
      if (button.disabled) return;
      const original = button.innerHTML;
      button.disabled = true;
      button.classList.add('is-loading');
      button.innerHTML = '<span class="spinner" aria-hidden="true"></span> 正在自动找数…';
      document.querySelectorAll('[data-query-status]').forEach(el => { el.textContent = '正在连接来源'; });
      setTimeout(() => {
        button.disabled = false;
        button.classList.remove('is-loading');
        button.innerHTML = original;
        document.querySelectorAll('[data-query-status]').forEach(el => { el.textContent = '刚刚更新 · 3 行已就绪'; });
        notify('示例结果已刷新；定稿接入后才执行真实查询');
      }, 850);
    });
  });

  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const text = button.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
        notify('已复制：' + text);
      } catch {
        notify('复制内容：' + text);
      }
    });
  });

  document.querySelectorAll('[data-tabs]').forEach(group => {
    const name = group.dataset.tabs;
    const buttons = [...group.querySelectorAll('[data-tab]')];
    const panels = [...document.querySelectorAll(`[data-panel-group="${name}"]`)];
    buttons.forEach(button => button.addEventListener('click', () => {
      buttons.forEach(item => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-selected', String(active));
      });
      panels.forEach(panel => { panel.hidden = panel.dataset.panel !== button.dataset.tab; });
    }));
  });

  document.querySelectorAll('[data-segment]').forEach(group => {
    const buttons = [...group.querySelectorAll('button')];
    buttons.forEach(button => button.addEventListener('click', () => {
      buttons.forEach(item => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
    }));
  });
})();
