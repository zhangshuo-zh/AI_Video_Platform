/**
 * Client half of `dsh-session-delete`: one row in a sidebar Session's "..."
 * menu that permanently deletes that Session.
 *
 * The row keeps everything inside the menu. The first click only arms the
 * confirmation (deletion cannot be undone), the second one performs it; the
 * menu stays open in between, so moving the pointer away or pressing Escape
 * disarms it. On success the Client refreshes the Session catalog, which drops
 * the row without a page reload.
 */
window.__ModuleLoader__.load({
  id: 'dsh-session-delete',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    /** The plugin context, captured for the row's click handler. */
    let pluginCtx;

    const IDLE_LABEL = '删除会话';
    const CONFIRM_LABEL = '再次点击确认删除';
    const BUSY_LABEL = '正在删除…';
    // Shown when the Host refuses because the Session is still live in memory.
    const LIVE_LABEL = '需重启 DSH 后删除';

    const STYLES = [
      '.dsd-row{display:flex;align-items:center;gap:7px;box-sizing:border-box;width:100%;padding:4px 7px;',
      'color:var(--dsw-alias-label-primary);font-family:inherit;font-size:calc(var(--dsh-content-font-size-secondary,13px) - 1px);',
      'text-align:left;background:transparent;border:none;border-radius:var(--dsw-radius-md);cursor:pointer}',
      '.dsd-row:hover{background:var(--dsw-alias-interactive-bg-hover)}',
      '.dsd-row:disabled{cursor:default;opacity:.65}',
      '.dsd-row.is-danger{color:var(--dsw-alias-label-error)}',
      '.dsd-row.is-danger:hover{background:var(--dsw-alias-interactive-bg-hover-danger)}',
      '.dsd-row svg{flex:none}',
      '.dsd-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    ].join('');

    const TRASH_ICON = h('svg', {
      viewBox: '0 0 16 16', width: 14, height: 14, fill: 'none',
      stroke: 'currentColor', strokeWidth: 1.2, strokeLinecap: 'round',
      strokeLinejoin: 'round', 'aria-hidden': true,
    },
    h('path', { d: 'M2.5 4h11' }),
    h('path', { d: 'M6 4V2.8c0-.4.4-.8.9-.8h2.2c.5 0 .9.4.9.8V4' }),
    h('path', { d: 'M4 4l.7 9.1c0 .5.5.9 1 .9h4.6c.5 0 1-.4 1-.9L12 4' }),
    h('path', { d: 'M6.6 6.6v5' }),
    h('path', { d: 'M9.4 6.6v5' }));

    /**
     * Resolve one Host route against the page base.
     *
     * A relative path (never a leading slash) keeps reverse-proxy directory
     * prefixes working, and the desktop shell forwards `dsh-app://app/…`
     * requests to the Host same-origin, which is why HTTP must ride the page
     * base rather than the injected transport origin.
     * @param path - Route without a leading slash.
     * @returns Absolute URL string.
     */
    function hostRouteUrl(path) {
      const base = typeof document !== 'undefined' && typeof document.baseURI === 'string' && document.baseURI !== ''
        ? document.baseURI
        : 'http://dsh.internal/';
      const directory = new URL(base);
      directory.search = '';
      directory.hash = '';
      if (!directory.pathname.endsWith('/')) directory.pathname += '/';
      return new URL(path.replace(/^\/+/, ''), directory.href).href;
    }

    /**
     * Ask the Host to delete one Session.
     * @param sessionId - Session to delete.
     * @returns The Host's result value.
     */
    async function requestDelete(sessionId) {
      const response = await fetch(hostRouteUrl('dsh-session-delete/delete'), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ sessionId }),
        credentials: 'same-origin',
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        throw new Error('删除失败：服务没有返回结果（HTTP ' + response.status + '）');
      }
      if (payload === null || typeof payload !== 'object' || payload.ok !== true) {
        const message = payload && payload.error && typeof payload.error.message === 'string'
          ? payload.error.message
          : '删除失败（HTTP ' + response.status + '）';
        const error = new Error(message);
        error.code = payload && payload.error && typeof payload.error.code === 'string'
          ? payload.error.code
          : undefined;
        throw error;
      }
      return payload.value;
    }

    /**
     * One "... menu" row for one Session.
     * @param props - owner share (sessionId) plus the injected menu state hook.
     * @returns The row.
     */
    function DeleteSessionMenuItem({ sessionId, useMenuOpenState }) {
      const [, setMenuOpen] = useMenuOpenState();
      const [phase, setPhase] = React.useState('idle');
      const [message, setMessage] = React.useState('');
      const [detail, setDetail] = React.useState('');
      const resetTimer = React.useRef(undefined);

      React.useEffect(() => () => {
        if (resetTimer.current !== undefined) clearTimeout(resetTimer.current);
      }, []);

      const disarm = React.useCallback(() => {
        setPhase((current) => (current === 'busy' ? current : 'idle'));
      }, []);

      const fail = (text, code) => {
        setPhase('idle');
        // A live Session explains itself at length; keep the row narrow and
        // leave the explanation in the tooltip.
        setMessage(code === 'session-live' ? LIVE_LABEL : text);
        setDetail(text);
        if (resetTimer.current !== undefined) clearTimeout(resetTimer.current);
        resetTimer.current = setTimeout(() => { setMessage(''); setDetail(''); }, 5000);
      };

      const onSelect = async () => {
        if (phase === 'busy') return;
        if (phase !== 'confirm') {
          setMessage('');
          setDetail('');
          setPhase('confirm');
          return;
        }
        setPhase('busy');
        try {
          await requestDelete(sessionId);
          // The Session is gone: close the menu now, before the catalog
          // refresh unmounts this row out from under the click.
          setMenuOpen(false);
          const sessions = pluginCtx === undefined ? undefined : pluginCtx.sessions;
          if (sessions !== undefined && typeof sessions.refresh === 'function') {
            try {
              await sessions.refresh();
            } catch {
              // The delete itself succeeded; a failed catalog refresh only
              // means the row leaves on the next natural refresh.
            }
          }
        } catch (error) {
          fail(error instanceof Error && error.message !== '' ? error.message : String(error), error && error.code);
        }
      };

      const label = message !== ''
        ? message
        : phase === 'confirm'
          ? CONFIRM_LABEL
          : phase === 'busy'
            ? BUSY_LABEL
            : IDLE_LABEL;
      const danger = phase === 'confirm' || message !== '';

      return h(React.Fragment, null,
        h('style', { key: 'styles' }, STYLES),
        h('button', {
          key: 'row',
          type: 'button',
          role: 'menuitem',
          className: danger ? 'dsd-row is-danger' : 'dsd-row',
          disabled: phase === 'busy',
          title: detail !== '' ? detail : undefined,
          onClick: onSelect,
          onMouseLeave: disarm,
          onBlur: disarm,
        },
        TRASH_ICON,
        h('span', { className: 'dsd-label' }, label)));
    }

    return {
      inject: ['slots', 'sessions'],
      apply(ctx) {
        pluginCtx = ctx;
        ctx.slots.inject('sidebar.workspaces.session.menu.item', () => ctx.slots.register({
          name: 'sidebar.workspaces.session.menu.item',
          id: 'session-delete',
          // After session-id (450): destruction is the last thing in the menu.
          order: 500,
        }, DeleteSessionMenuItem));
      },
    };
  },
});
