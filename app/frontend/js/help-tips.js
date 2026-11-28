/** 全站帮助提示。动态插入的问号同样支持键盘与点按。 */
export function mountHelpTips(root = document.body) {
  let active = null;
  let nextId = 0;
  function close() {
    if (!active) return;
    active.classList.remove('is-open');
    active.setAttribute('aria-expanded', 'false');
    active = null;
  }
  function prepare(tip) {
    if (tip.dataset.helpReady) return;
    tip.dataset.helpReady = '1';
    let pop = tip.querySelector('.tip-pop');
    if (!pop && tip.dataset.tip) {
      pop = document.createElement('span');
      pop.className = 'tip-pop';
      pop.textContent = tip.dataset.tip;
      tip.append(pop);
    }
    if (!pop) return;
    pop.id ||= `fv-help-${++nextId}`;
    pop.setAttribute('role', 'tooltip');
    tip.setAttribute('role', 'button');
    tip.tabIndex = 0;
    tip.setAttribute('aria-label', tip.getAttribute('aria-label') || '查看帮助');
    tip.setAttribute('aria-describedby', pop.id);
    tip.setAttribute('aria-expanded', 'false');
  }
  function scan(node) {
    if (!(node instanceof Element)) return;
    if (node.matches('.help-tip')) prepare(node);
    node.querySelectorAll('.help-tip').forEach(prepare);
  }
  function open(tip) {
    if (active === tip) return;
    close();
    active = tip;
    tip.classList.add('is-open');
    tip.setAttribute('aria-expanded', 'true');
    const pop = tip.querySelector('.tip-pop');
    pop?.style.removeProperty('--help-shift');
    if (pop && window.innerWidth > 640) {
      pop.style.setProperty('--help-shift', '0px');
      const bounds = pop.getBoundingClientRect();
      const shift = Math.max(12 - bounds.left, Math.min(0, document.documentElement.clientWidth - 12 - bounds.right));
      pop.style.setProperty('--help-shift', `${shift}px`);
    }
  }
  function target(e) {
    return e.target instanceof Element ? e.target.closest('.help-tip') : null;
  }
  function click(e) {
    const tip = target(e);
    if (!tip || !root.contains(tip)) { close(); return; }
    if (e.target.closest('.tip-pop')) return;
    e.stopPropagation();
    if (active === tip) close();
    else open(tip);
  }
  function keydown(e) {
    if (e.key === 'Escape' && active) {
      close();
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    const tip = target(e);
    if (tip && !e.target.closest('.tip-pop') && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      e.stopPropagation();
      if (active === tip) close();
      else open(tip);
    }
  }
  function pointerover(e) {
    if (e.pointerType !== 'mouse') return;
    const tip = target(e);
    if (tip && !tip.contains(e.relatedTarget)) open(tip);
  }
  function pointerout(e) {
    const tip = target(e);
    if (e.pointerType === 'mouse' && tip && tip === active && !tip.contains(e.relatedTarget)) close();
  }
  function focusout(e) {
    if (active && !active.contains(e.relatedTarget)) close();
  }
  scan(root);
  const observer = new MutationObserver((records) => {
    if (active && !active.isConnected) close();
    for (const record of records) record.addedNodes.forEach(scan);
  });
  observer.observe(root, { childList: true, subtree: true });
  document.addEventListener('click', click);
  document.addEventListener('keydown', keydown, true);
  root.addEventListener('pointerover', pointerover);
  root.addEventListener('pointerout', pointerout);
  root.addEventListener('focusout', focusout);
  return () => {
    close();
    observer.disconnect();
    document.removeEventListener('click', click);
    document.removeEventListener('keydown', keydown, true);
    root.removeEventListener('pointerover', pointerover);
    root.removeEventListener('pointerout', pointerout);
    root.removeEventListener('focusout', focusout);
  };
}
