/**
 * Global Application Shell Logic (base.html & login.html)
 * Handles:
 *  - Shared HTML escaping & CLP/USD client-side currency formatters
 *  - Light / Dark theme toggle
 *  - Collapsible sidebar (icon rail on desktop, drawer on mobile)
 *  - Elevating Bootstrap modals to document.body to prevent stacking context locks
 *  - Live USD/CLP exchange rate badge with post-login count-up animation
 *  - Login password visibility toggle
 */

function escapeHtmlStr(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatClpClient(val) {
  const num = Math.round(Number(val) || 0);
  return '$' + num.toLocaleString('es-CL');
}

function formatUsdClient(val) {
  const num = Number(val) || 0;
  return 'US$ ' + num.toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

window.escapeHtmlStr = escapeHtmlStr;
window.formatClpClient = formatClpClient;
window.formatUsdClient = formatUsdClient;

/**
 * Non-blocking notification (replaces window.alert).
 * type: 'error' | 'warning' | 'success' | 'info'
 */
function notify(message, type = 'info', timeoutMs) {
  let stack = document.getElementById('toastStack');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'toastStack';
    stack.className = 'toast-stack';
    stack.setAttribute('aria-live', 'polite');
    document.body.appendChild(stack);
  }
  const icons = {
    error: 'bi-x-octagon-fill',
    warning: 'bi-exclamation-triangle-fill',
    success: 'bi-check-circle-fill',
    info: 'bi-info-circle-fill',
  };
  const toast = document.createElement('div');
  toast.className = 'app-toast app-toast-' + type;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.innerHTML =
    '<i class="bi ' + (icons[type] || icons.info) + '"></i>' +
    '<div class="app-toast-msg">' + escapeHtmlStr(message) + '</div>' +
    '<button type="button" class="btn-close" aria-label="Cerrar"></button>';
  const close = () => {
    toast.classList.add('is-leaving');
    setTimeout(() => toast.remove(), 200);
  };
  toast.querySelector('.btn-close').addEventListener('click', close);
  stack.prepend(toast);  // newest on top
  const ms = timeoutMs ?? (type === 'error' ? 8000 : 5000);
  if (ms > 0) setTimeout(close, ms);
  return toast;
}

window.notify = notify;

(function () {
  const btn = document.getElementById('themeToggleBtn');
  const icon = document.getElementById('themeToggleIcon');

  function syncIcon() {
    const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    if (icon) {
      icon.className = isDark ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
    }
  }

  syncIcon();

  if (btn) {
    btn.addEventListener('click', function () {
      const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-bs-theme', next);
      try {
        localStorage.setItem('theme', next);
      } catch (_) {}
      syncIcon();
    });
  }
})();

(function () {
  const menuBtn = document.getElementById('sidebarToggleBtn');
  const sidebar = document.getElementById('sidebarPanel');
  const scrim = document.getElementById('sidebarScrim');
  if (!menuBtn || !sidebar || !scrim) return;

  // Same breakpoint as the drawer layout in theme.css
  const mobile = window.matchMedia('(max-width: 991.98px)');
  const root = document.documentElement;

  function syncExpanded() {
    const expanded = mobile.matches
      ? sidebar.classList.contains('open')
      : root.getAttribute('data-sidebar') !== 'collapsed';
    menuBtn.setAttribute('aria-expanded', String(expanded));
  }

  function openSidebar() {
    sidebar.classList.add('open');
    scrim.classList.add('open');
    syncExpanded();
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    scrim.classList.remove('open');
    syncExpanded();
  }

  // Desktop: collapse to an icon rail and remember it; mobile: slide-in drawer
  function toggleCollapsed() {
    const collapsed = root.getAttribute('data-sidebar') !== 'collapsed';
    if (collapsed) {
      root.setAttribute('data-sidebar', 'collapsed');
    } else {
      root.removeAttribute('data-sidebar');
    }
    try {
      localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '0');
    } catch (_) {}
    syncExpanded();
  }

  menuBtn.addEventListener('click', function () {
    if (!mobile.matches) {
      toggleCollapsed();
    } else if (sidebar.classList.contains('open')) {
      closeSidebar();
    } else {
      openSidebar();
    }
  });
  mobile.addEventListener('change', closeSidebar);
  syncExpanded();
  scrim.addEventListener('click', closeSidebar);
  sidebar.querySelectorAll('a').forEach(function (a) {
    a.addEventListener('click', closeSidebar);
  });
})();

function togglePass() {
  const input = document.getElementById('password');
  const icon = document.getElementById('eyeIcon');
  if (!input || !icon) return;

  if (input.type === 'password') {
    input.type = 'text';
    icon.classList.replace('bi-eye', 'bi-eye-slash');
  } else {
    input.type = 'password';
    icon.classList.replace('bi-eye-slash', 'bi-eye');
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  // Ensure all Bootstrap modals are direct children of document.body
  document.querySelectorAll('.modal').forEach((modalEl) => {
    if (modalEl.parentElement !== document.body) {
      document.body.appendChild(modalEl);
    }
  });

  // Login form flag for post-login dollar count-up animation
  const loginForm = document.getElementById('loginForm');
  if (loginForm) {
    loginForm.addEventListener('submit', () => {
      try {
        sessionStorage.setItem('justLoggedIn', '1');
      } catch (_) {}
    });
  }

  // Live USD/CLP Exchange Rate Badge
  const el = document.getElementById('valor-dolar');
  if (!el) return;

  const formatClp = (num) => {
    return '$' + Number(num).toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' CLP';
  };

  const showValue = (num) => {
    // Count up only the first time the rate is shown in this browser tab, or right after logging in;
    // moving between pages shows the value directly.
    let shouldAnimate = false;
    try {
      shouldAnimate = !!sessionStorage.getItem('justLoggedIn') || !sessionStorage.getItem('usdRateAnimated');
      sessionStorage.removeItem('justLoggedIn');
      sessionStorage.setItem('usdRateAnimated', '1');
    } catch (_) {}

    if (!shouldAnimate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      el.textContent = formatClp(num);
      return;
    }

    const duration = 900;
    const start = performance.now();
    function tick(now) {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatClp(num * eased);
      if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  };

  const cached = sessionStorage.getItem('usd_clp_rate_value');
  if (cached) {
    showValue(parseFloat(cached));
    return;
  }

  try {
    const res = await fetch('https://cl.dolarapi.com/v1/cotizaciones/usd');
    if (res.ok) {
      const data = await res.json();
      const val = data.ultimoCierre || data.compra || data.venta;
      if (val) {
        showValue(val);
        sessionStorage.setItem('usd_clp_rate_value', String(val));
        return;
      }
    }
  } catch (_) {}

  try {
    const res2 = await fetch('https://open.er-api.com/v6/latest/USD');
    if (res2.ok) {
      const data2 = await res2.json();
      if (data2.rates && data2.rates.CLP) {
        showValue(data2.rates.CLP);
        sessionStorage.setItem('usd_clp_rate_value', String(data2.rates.CLP));
        return;
      }
    }
  } catch (_) {}

  el.textContent = 'N/D';
});

// Delegated page-size selector for Consultas SSR pagination bars (db_pagination_bar.html)
document.addEventListener('change', (event) => {
  const select = event.target.closest('.js-db-page-size-select');
  if (!select) return;
  const form = select.form || select.closest('form');
  if (form) {
    form.submit();
    return;
  }
  const url = new URL(window.location.href);
  url.searchParams.set('por_pagina', select.value);
  url.searchParams.set('page', '1');
  window.location.assign(url.toString());
});


/* ------------------------------------------------------------------ */
/* KPI count-up                                                         */
/* ------------------------------------------------------------------ */
/* Any element with data-countup-to counts from 0 to its value the first time it is seen
   (on page load, or when its tab is shown), then shows the exact server-formatted text.
   Optional: data-countup-decimals, data-countup-prefix, data-countup-suffix. */
(function () {
  const DURATION = 900;
  const done = new WeakSet();
  const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function animate(el) {
    if (done.has(el)) return;
    done.add(el);
    if (reduced) return;
    const target = parseFloat(el.getAttribute('data-countup-to'));
    if (!isFinite(target)) return;
    const decimals = parseInt(el.getAttribute('data-countup-decimals') || '0', 10);
    const prefix = el.getAttribute('data-countup-prefix') || '';
    const suffix = el.getAttribute('data-countup-suffix') || '';
    const finalText = el.textContent; // shown exactly once the count ends
    const format = (n) => prefix + n.toLocaleString('es-CL', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix;
    let start = null;
    function tick(now) {
      // The first frame's timestamp can predate this call; starting the clock there avoids a negative first value
      if (start === null) start = now;
      const progress = Math.min(Math.max((now - start) / DURATION, 0), 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = progress < 1 ? format(target * eased) : finalText;
      if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function countUpIn(root) {
    (root || document).querySelectorAll('[data-countup-to]').forEach((el) => {
      if (el.closest('.tab-pane:not(.active)')) return; // counts when its tab is shown
      animate(el);
    });
  }

  document.addEventListener('DOMContentLoaded', () => setTimeout(() => countUpIn(document), 0));
  document.addEventListener('shown.bs.tab', (event) => {
    const sel = event.target.getAttribute('data-bs-target') || event.target.getAttribute('href');
    const pane = sel && sel.charAt(0) === '#' ? document.querySelector(sel) : null;
    if (pane) countUpIn(pane);
  });
  window.countUpIn = countUpIn;
})();
