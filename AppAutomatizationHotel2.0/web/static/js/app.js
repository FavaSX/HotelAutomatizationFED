/**
 * Global Application Shell Logic (base.html & login.html)
 * Handles:
 *  - Light / Dark theme toggle
 *  - Mobile responsive sidebar drawer
 *  - Elevating Bootstrap modals to document.body to prevent stacking context locks
 *  - Live USD/CLP exchange rate badge with post-login count-up animation
 *  - Login password visibility toggle
 */

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
  const menuBtn = document.getElementById('mobileMenuBtn');
  const sidebar = document.getElementById('sidebarPanel');
  const scrim = document.getElementById('sidebarScrim');
  if (!menuBtn || !sidebar || !scrim) return;

  function openSidebar() {
    sidebar.classList.add('open');
    scrim.classList.add('open');
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    scrim.classList.remove('open');
  }

  menuBtn.addEventListener('click', openSidebar);
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
    let justLoggedIn = false;
    try {
      justLoggedIn = !!sessionStorage.getItem('justLoggedIn');
      sessionStorage.removeItem('justLoggedIn');
    } catch (_) {}

    if (!justLoggedIn) {
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

