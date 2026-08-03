/* Shared persistent header for team_dashboard.html + dashboard.html.
   Exposes window.QAHeader.init(opts) — opts shape:
     {
       page:        'team' | 'agent',
       team:        team_id string (e.g. 'member_support'),
       agent:       agent display name (agent page only),
       title:       string shown as <h1> (team page passes team title; agent
                    page passes the agent name),
       supervisors: list of supervisor names (team page only; populates the
                    <select>),
       onChange:    callback fired on every control change with
                    { days, dateFrom, dateTo, activeOnly, supervisor }
     }

   State is read from the URL on init (?days=, ?from=&to=, ?supervisor=,
   ?activeOnly=) so navigating Team↔Agent via the nav buttons carries the
   chosen window. On every change we rewrite the URL and the nav-button
   hrefs to keep that round-trip working. */

(function () {
  'use strict';

  const PRESETS = [
    { label: '1w',  days: 7   },
    { label: '1mo', days: 30  },
    { label: '3mo', days: 90  },
    { label: '6mo', days: 180 },
    { label: 'All', days: 0   },
  ];
  const DEFAULT_DAYS = 30;
  const MAX_RANGE_DAYS = 730;

  // --- URL state ----------------------------------------------------------
  function readUrlState() {
    const u = new URL(window.location.href);
    const q = u.searchParams;
    const from = q.get('from');
    const to   = q.get('to');
    const daysRaw = q.get('days');
    const days = daysRaw != null && daysRaw !== '' ? parseInt(daysRaw, 10) : null;
    return {
      dateFrom: from || null,
      dateTo:   to   || null,
      // Custom range wins when both dates present; otherwise the
      // ?days= value (if valid); otherwise the default.
      days: (from && to)
        ? null
        : (Number.isFinite(days) && days >= 0 ? days : DEFAULT_DAYS),
      activeOnly: q.get('activeOnly') !== '0',  // default true
      supervisor: q.get('supervisor') || '',
    };
  }

  function writeUrlState(state) {
    const u = new URL(window.location.href);
    const q = u.searchParams;
    if (state.dateFrom && state.dateTo) {
      q.set('from', state.dateFrom);
      q.set('to',   state.dateTo);
      q.delete('days');
    } else {
      q.delete('from');
      q.delete('to');
      if (state.days != null) q.set('days', String(state.days));
      else q.delete('days');
    }
    // Only persist supervisor/activeOnly on the team page — agent page
    // ignores them and we don't want them sticking in the URL.
    if (state.page === 'team') {
      if (state.supervisor) q.set('supervisor', state.supervisor);
      else q.delete('supervisor');
      // activeOnly defaults true; only persist the non-default.
      if (state.activeOnly === false) q.set('activeOnly', '0');
      else q.delete('activeOnly');
    }
    window.history.replaceState({}, '', u.toString());
  }

  // Rewrite nav-button hrefs to carry the current window across pages.
  function buildNavHref(base, state) {
    const u = new URL(base, window.location.origin);
    if (state.dateFrom && state.dateTo) {
      u.searchParams.set('from', state.dateFrom);
      u.searchParams.set('to',   state.dateTo);
    } else if (state.days != null && state.days !== DEFAULT_DAYS) {
      u.searchParams.set('days', String(state.days));
    }
    return u.pathname + (u.search ? u.search : '');
  }

  // --- Markup -------------------------------------------------------------
  function renderInto(host, opts) {
    const navLinks = [
      { key: 'scoring',  label: 'Batch Scoring', href: `/score/${opts.team}`,     showOn: 'both'  },
      { key: 'teamview', label: 'Team View',     href: `/dashboard/${opts.team}`, showOn: 'agent' },
      { key: 'lookup',   label: 'Lookup',        href: `/lookup/${opts.team}`,    showOn: 'both'  },
    ];
    // Batch Scoring stays exposed on both views — it's the entry point to
    // the bulk-scoring flow, which is being extended later and shouldn't
    // be reachable only from the agent page. Team View is hidden on the
    // team page itself (it IS the current page).
    const navHtml = navLinks
      .filter(n => n.showOn === 'both' || n.showOn === opts.page)
      .map(n => `<a class="qa-nav-btn" data-nav="${n.key}" href="${n.href}">${n.label}</a>`)
      .join('');

    const supervisorOpts = (opts.supervisors || [])
      .map(s => `<option value="${escapeAttr(s)}">${escapeHtml(s)}</option>`)
      .join('');

    const presetBtns = PRESETS
      .map(p => `<button type="button" data-days="${p.days}">${p.label}</button>`)
      .join('');

    host.classList.add('qa-header');
    host.innerHTML = `
      <div class="qa-header-nav">
        ${navHtml}
        <h1 id="qaHeaderTitle">${escapeHtml(opts.title || '')}</h1>
      </div>
      <div class="qa-header-controls">
        <button type="button" class="qa-toggle-btn" data-role="active-only">Active Only</button>
        <select data-role="supervisor">
          <option value="">All Supervisors</option>
          ${supervisorOpts}
        </select>
        <div class="qa-time-buttons" data-role="time-buttons">
          ${presetBtns}
          <button type="button" data-role="custom">Custom …</button>
        </div>
        <div class="qa-custom-popover" data-role="custom-popover" hidden>
          <div class="qa-custom-row">
            <label for="qaCustomFrom">From</label>
            <input type="date" id="qaCustomFrom" />
          </div>
          <div class="qa-custom-row">
            <label for="qaCustomTo">To</label>
            <input type="date" id="qaCustomTo" />
          </div>
          <div class="qa-custom-error" data-role="custom-error"></div>
          <div class="qa-custom-actions">
            <button type="button" class="qa-custom-clear">Clear</button>
            <button type="button" class="qa-custom-apply">Apply</button>
          </div>
        </div>
      </div>
    `;
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

  // --- Public API ---------------------------------------------------------
  function init(opts) {
    const host = document.getElementById('appHeader');
    if (!host) {
      console.error('QAHeader.init: <div id="appHeader"></div> not found');
      return;
    }

    const state = Object.assign(
      { page: opts.page, supervisor: '', activeOnly: true, days: DEFAULT_DAYS, dateFrom: null, dateTo: null },
      readUrlState()
    );

    renderInto(host, opts);

    const $ = sel => host.querySelector(sel);
    const $$ = sel => Array.from(host.querySelectorAll(sel));

    // Hydrate controls from URL state.
    const supervisorSel = $('select[data-role="supervisor"]');
    if (state.supervisor) supervisorSel.value = state.supervisor;

    const activeBtn = $('[data-role="active-only"]');
    activeBtn.classList.toggle('active', state.activeOnly);
    activeBtn.textContent = state.activeOnly ? 'Active Only' : 'All Agents';

    // On the agent page, Active Only / Supervisor are visually identical
    // but inert — the controls are meaningless for one agent. Keeps the
    // banner chrome consistent between views.
    if (opts.page === 'agent') {
      activeBtn.disabled = true;
      activeBtn.title = 'Not applicable on agent view';
      supervisorSel.disabled = true;
      supervisorSel.title = 'Not applicable on agent view';
    }

    markActiveChiclet(host, state);

    // Persist + propagate ----------------------------------------------------
    function emit() {
      writeUrlState(Object.assign({ page: opts.page }, state));
      // Rewrite nav-button hrefs so navigation carries the window.
      $$('.qa-nav-btn').forEach(a => {
        const base = a.getAttribute('href').split('?')[0];
        a.setAttribute('href', buildNavHref(base, state));
      });
      if (typeof opts.onChange === 'function') {
        opts.onChange({
          days:       state.days,
          dateFrom:   state.dateFrom,
          dateTo:     state.dateTo,
          activeOnly: state.activeOnly,
          supervisor: state.supervisor,
        });
      }
    }

    // Wire up controls -------------------------------------------------------
    activeBtn.addEventListener('click', () => {
      if (activeBtn.disabled) return;
      state.activeOnly = !state.activeOnly;
      activeBtn.classList.toggle('active', state.activeOnly);
      activeBtn.textContent = state.activeOnly ? 'Active Only' : 'All Agents';
      emit();
    });

    supervisorSel.addEventListener('change', () => {
      state.supervisor = supervisorSel.value;
      emit();
    });

    $$('.qa-time-buttons button[data-days]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.days = parseInt(btn.dataset.days, 10);
        state.dateFrom = null;
        state.dateTo = null;
        markActiveChiclet(host, state);
        emit();
      });
    });

    // Custom popover --------------------------------------------------------
    const popover = $('[data-role="custom-popover"]');
    const customBtn = $('[data-role="custom"]');
    const fromInput = $('#qaCustomFrom');
    const toInput   = $('#qaCustomTo');
    const errLabel  = $('[data-role="custom-error"]');
    const applyBtn  = $('.qa-custom-apply');
    const clearBtn  = $('.qa-custom-clear');

    function openPopover() {
      // Pre-fill with current range if active, otherwise leave blank.
      fromInput.value = state.dateFrom || '';
      toInput.value   = state.dateTo   || '';
      errLabel.textContent = '';
      popover.hidden = false;
    }
    function closePopover() { popover.hidden = true; }

    customBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (popover.hidden) openPopover(); else closePopover();
    });

    // Click-outside dismisses the popover (but not on the chiclet itself).
    document.addEventListener('click', e => {
      if (popover.hidden) return;
      if (popover.contains(e.target) || customBtn.contains(e.target)) return;
      closePopover();
    });

    applyBtn.addEventListener('click', () => {
      const f = fromInput.value;
      const t = toInput.value;
      if (!f || !t) { errLabel.textContent = 'Pick both dates.'; return; }
      if (t < f)    { errLabel.textContent = 'To must be on or after From.'; return; }
      // Span check matches the backend cap.
      const spanDays = Math.round(
        (new Date(t) - new Date(f)) / (1000 * 60 * 60 * 24)
      ) + 1;
      if (spanDays > MAX_RANGE_DAYS) {
        errLabel.textContent = `Range exceeds ${MAX_RANGE_DAYS}-day cap.`;
        return;
      }
      state.dateFrom = f;
      state.dateTo   = t;
      state.days     = null;
      markActiveChiclet(host, state);
      closePopover();
      emit();
    });

    clearBtn.addEventListener('click', () => {
      state.dateFrom = null;
      state.dateTo   = null;
      state.days     = DEFAULT_DAYS;
      markActiveChiclet(host, state);
      closePopover();
      emit();
    });

    // Initial fire so the page can fetch with the URL-resolved window.
    // Rewrites nav hrefs even when state matches default.
    emit();

    // Expose a tiny update API so the team page can populate supervisors
    // after fetching them async (Mails sheet is loaded after init).
    return {
      setSupervisors(list) {
        const cur = supervisorSel.value;
        supervisorSel.innerHTML =
          '<option value="">All Supervisors</option>' +
          (list || []).map(s => `<option value="${escapeAttr(s)}">${escapeHtml(s)}</option>`).join('');
        // Restore prior selection if it's still in the list.
        if (cur && [...supervisorSel.options].some(o => o.value === cur)) {
          supervisorSel.value = cur;
        }
      },
      setTitle(t) {
        const h = host.querySelector('#qaHeaderTitle');
        if (h) h.textContent = t;
      },
      getState() { return Object.assign({}, state); },
    };
  }

  function markActiveChiclet(host, state) {
    const btns = host.querySelectorAll('.qa-time-buttons button');
    btns.forEach(b => b.classList.remove('active'));
    if (state.dateFrom && state.dateTo) {
      const custom = host.querySelector('[data-role="custom"]');
      if (custom) {
        custom.classList.add('active');
        custom.textContent = `Custom: ${state.dateFrom} – ${state.dateTo}`;
      }
      return;
    }
    // Reset custom label when a preset is active.
    const custom = host.querySelector('[data-role="custom"]');
    if (custom) custom.textContent = 'Custom …';
    const match = host.querySelector(`.qa-time-buttons button[data-days="${state.days}"]`);
    if (match) match.classList.add('active');
  }

  window.QAHeader = { init };
})();
