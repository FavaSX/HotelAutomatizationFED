/**
 * Shared Preview Table & Commit Engine (loaded by usd_preview.html and clp_preview.html)
 * Handles:
 *  - Fullscreen focus mode toggle (button + Escape key)
 *  - Client-side pagination across voucher tabs
 *  - Mayor a Menor / Menor a Mayor numeric column sorting
 *  - Card brand, cashier, and instant text search filtering
 *  - 2-step AJAX database commit with official Excel download
 *  - Shared HTML escaping & pagination item builder helpers
 */

let isCommitting = false;
let activeCardBrandFilter = 'ALL';
let activeCashierFilter = 'ALL';
const currentSortState = {};

let voucherPageSize = 20;
const voucherCurrentPages = {
  tabMatched: 1,
  tabDiscrepancies: 1,
  tabOrphans: 1
};

const VOUCHER_PAGINATION_CONFIG = {
  tbodyMatched: {
    tabId: 'tabMatched',
    rangeId: 'pageRangeMatched',
    totalId: 'pageTotalMatched',
    listId: 'paginationListMatched'
  },
  tbodyDiscrepancies: {
    tabId: 'tabDiscrepancies',
    rangeId: 'pageRangeDiscrepancies',
    totalId: 'pageTotalDiscrepancies',
    listId: 'paginationListDiscrepancies'
  },
  tbodyOrphans: {
    tabId: 'tabOrphans',
    rangeId: 'pageRangeOrphans',
    totalId: 'pageTotalOrphans',
    listId: 'paginationListOrphans'
  }
};

function escapeHtmlStr(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toggleFullscreenTable() {
  const isFocus = document.body.classList.toggle('table-focus-mode');
  const icon = document.getElementById('iconFullscreen');
  const text = document.getElementById('textFullscreen');
  const btn = document.getElementById('btnToggleFullscreen');

  if (isFocus) {
    if (icon) icon.className = 'bi bi-fullscreen-exit me-1';
    if (text) text.textContent = 'Salir de Pantalla Completa (Esc)';
    if (btn) btn.className = 'btn btn-sm btn-dark rounded-pill px-3 fw-bold shadow-sm';
  } else {
    if (icon) icon.className = 'bi bi-arrows-fullscreen me-1';
    if (text) text.textContent = 'Pantalla Completa';
    if (btn) btn.className = 'btn btn-sm btn-outline-secondary rounded-pill px-3 fw-semibold shadow-xs';
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.querySelector('.modal.show') && document.body.classList.contains('table-focus-mode')) {
    toggleFullscreenTable();
  }
});

/**
 * Shared pagination HTML builder used by both voucher tables and modal lists.
 * @param {number} currentPage
 * @param {number} totalPages
 * @param {(page: number) => string} onClickForPage - Returns the JS expression for onclick
 */
function buildPaginationItemsHtml(currentPage, totalPages, onClickForPage) {
  if (totalPages <= 1) return '';

  let html = '';
  const prevDisabled = currentPage === 1 ? ' disabled' : '';
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(1)}" title="Primera página">&laquo;</a></li>`;
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(currentPage - 1)}" title="Anterior">&lsaquo;</a></li>`;

  let startP = Math.max(1, currentPage - 2);
  let endP = Math.min(totalPages, currentPage + 2);

  if (startP === 1) {
    endP = Math.min(totalPages, 5);
  } else if (endP === totalPages) {
    startP = Math.max(1, totalPages - 4);
  }

  if (startP > 1) {
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(1)}">1</a></li>`;
    if (startP > 2) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
  }

  for (let p = startP; p <= endP; p++) {
    if (p === currentPage) {
      html += `<li class="page-item active" aria-current="page"><span class="page-link">${p}</span></li>`;
    } else {
      html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(p)}">${p}</a></li>`;
    }
  }

  if (endP < totalPages) {
    if (endP < totalPages - 1) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(totalPages)}">${totalPages}</a></li>`;
  }

  const nextDisabled = currentPage === totalPages ? ' disabled' : '';
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(currentPage + 1)}" title="Siguiente">&rsaquo;</a></li>`;
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="${onClickForPage(totalPages)}" title="Última página">&raquo;</a></li>`;

  return html;
}

function resetVoucherPages() {
  Object.keys(voucherCurrentPages).forEach(k => {
    voucherCurrentPages[k] = 1;
  });
}

function changeVoucherPageSize(newSize) {
  voucherPageSize = newSize === 'ALL' ? Infinity : (parseInt(newSize, 10) || 20);
  document.querySelectorAll('.voucher-page-size-select').forEach(sel => {
    sel.value = newSize;
  });
  resetVoucherPages();
  applyVoucherFilters();
}

function goToVoucherPage(tabId, pageNum) {
  voucherCurrentPages[tabId] = pageNum;
  const tbodyId = Object.keys(VOUCHER_PAGINATION_CONFIG).find(
    k => VOUCHER_PAGINATION_CONFIG[k].tabId === tabId
  );
  if (tbodyId) {
    paginateVoucherTbody(tbodyId);
  }
  const scrollEl = document.getElementById(tabId)?.querySelector('.voucher-table-scroll');
  if (scrollEl) {
    scrollEl.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function paginateVoucherTbody(tbodyId) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const cfg = VOUCHER_PAGINATION_CONFIG[tbodyId];
  const matchedRows = Array.from(tbody.querySelectorAll('tr.voucher-row')).filter(
    r => r.dataset.filterMatch === 'true'
  );
  const totalCount = matchedRows.length;

  if (!cfg) {
    matchedRows.forEach(r => r.classList.remove('d-none'));
    return;
  }

  const totalPages = voucherPageSize === Infinity ? 1 : Math.max(1, Math.ceil(totalCount / voucherPageSize));
  const curPage = Math.max(1, Math.min(voucherCurrentPages[cfg.tabId] || 1, totalPages));
  voucherCurrentPages[cfg.tabId] = curPage;

  const startIdx = voucherPageSize === Infinity ? 0 : (curPage - 1) * voucherPageSize;
  const endIdx = voucherPageSize === Infinity ? totalCount : Math.min(startIdx + voucherPageSize, totalCount);

  matchedRows.forEach((row, i) => {
    row.classList.toggle('d-none', i < startIdx || i >= endIdx);
  });

  const rangeEl = document.getElementById(cfg.rangeId);
  const totalEl = document.getElementById(cfg.totalId);
  if (rangeEl) rangeEl.textContent = totalCount === 0 ? '0' : `${startIdx + 1}–${endIdx}`;
  if (totalEl) totalEl.textContent = totalCount;

  const listEl = document.getElementById(cfg.listId);
  if (listEl) {
    listEl.innerHTML = buildPaginationItemsHtml(
      curPage,
      totalPages,
      (p) => `goToVoucherPage('${cfg.tabId}', ${p})`
    );
  }

  initVoucherTooltips();
}

function sortVoucherTable(tbodyId, dataKey, thElem) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;

  const stateKey = `${tbodyId}_${dataKey}`;
  const nextDir = (currentSortState[stateKey] === 'desc') ? 'asc' : 'desc';
  currentSortState[stateKey] = nextDir;

  const table = tbody.closest('table');
  if (table) {
    table.querySelectorAll('thead .sort-icon').forEach(icon => {
      icon.className = 'bi bi-arrow-down-up ms-1 sort-icon opacity-50';
    });
  }
  const activeIcon = thElem.querySelector('.sort-icon');
  if (activeIcon) {
    activeIcon.className = nextDir === 'desc'
      ? 'bi bi-caret-down-fill ms-1 sort-icon text-primary'
      : 'bi bi-caret-up-fill ms-1 sort-icon text-primary';
  }

  const rows = Array.from(tbody.querySelectorAll('tr.voucher-row'));
  const parseNum = (el) => {
    let raw = String(el.getAttribute('data-' + dataKey) || '0').trim();
    if (raw.includes(',')) {
      raw = raw.replace(/\./g, '').replace(',', '.');
    }
    return parseFloat(raw) || 0;
  };

  rows.sort((a, b) => {
    const valA = parseNum(a);
    const valB = parseNum(b);
    return nextDir === 'desc' ? (valB - valA) : (valA - valB);
  });

  const noMatchRow = tbody.querySelector('tr[id^="noMatchRow"]');
  rows.forEach(row => {
    if (noMatchRow) {
      tbody.insertBefore(row, noMatchRow);
    } else {
      tbody.appendChild(row);
    }
  });

  paginateVoucherTbody(tbodyId);
}

function setCardBrandFilter(brandKey, btnElem) {
  activeCardBrandFilter = brandKey;
  document.querySelectorAll('#cardBrandFilterGroup .card-filter-btn').forEach(btn => {
    btn.classList.remove('btn-dark', 'active');
    btn.classList.add('btn-outline-secondary');
  });
  btnElem.classList.remove('btn-outline-secondary');
  btnElem.classList.add('btn-dark', 'active');
  resetVoucherPages();
  applyVoucherFilters();
}

function setCashierFilter(cashierVal) {
  activeCashierFilter = (cashierVal || 'ALL').trim().toUpperCase();
  resetVoucherPages();
  applyVoucherFilters();
}

function clearVoucherSearch() {
  const input = document.getElementById('voucherSearchInput');
  if (input) {
    input.value = '';
    input.focus();
  }
  resetVoucherPages();
  applyVoucherFilters();
}

function filterSingleTab(tbodyId, noMatchRowId, countBadgeId) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return 0;

  const query = (document.getElementById('voucherSearchInput')?.value || '').trim().toLowerCase();
  const rows = tbody.querySelectorAll('tr.voucher-row');
  let visibleCount = 0;

  rows.forEach(row => {
    const cardAttr = (row.getAttribute('data-card') || '').toUpperCase();
    const cashierAttr = (row.getAttribute('data-cashier') || '').toUpperCase();
    const rowText = row.textContent.toLowerCase();

    const matchesCard = (activeCardBrandFilter === 'ALL') || cardAttr.includes(activeCardBrandFilter);
    const matchesCashier = (activeCashierFilter === 'ALL') || (cashierAttr === activeCashierFilter);
    const matchesQuery = (!query) || rowText.includes(query);

    if (matchesCard && matchesCashier && matchesQuery) {
      row.dataset.filterMatch = 'true';
      visibleCount++;
    } else {
      row.dataset.filterMatch = 'false';
      row.classList.add('d-none');
    }
  });

  const noMatchRow = document.getElementById(noMatchRowId);
  if (noMatchRow) {
    noMatchRow.classList.toggle('d-none', !(rows.length > 0 && visibleCount === 0));
  }

  const badge = document.getElementById(countBadgeId);
  if (badge) badge.textContent = visibleCount;

  paginateVoucherTbody(tbodyId);
  return visibleCount;
}

function applyVoucherFilters() {
  const query = (document.getElementById('voucherSearchInput')?.value || '').trim();
  const clearBtn = document.getElementById('btnClearVoucherSearch');
  if (clearBtn) {
    clearBtn.classList.toggle('d-none', query.length === 0);
  }

  let totalVisible = 0;
  document.querySelectorAll('tbody[data-voucher-tab]').forEach(tbody => {
    const visible = filterSingleTab(tbody.id, tbody.dataset.noMatchRow, tbody.dataset.countBadge);
    if (tbody.dataset.voucherTab !== 'aux') totalVisible += visible;
  });

  const totalBadge = document.getElementById('visibleTotalBadge');
  if (totalBadge) totalBadge.textContent = totalVisible;
}

async function executeCommitToDatabase() {
  if (isCommitting) return;
  isCommitting = true;

  const btnExecute = document.getElementById('btnExecuteCommit');
  const btnCancel = document.getElementById('btnCancelCommitModal');
  const btnClose = document.getElementById('btnCloseConfirmModal');
  const commitUrl = btnExecute?.getAttribute('data-commit-url') || '/reconciliation/usd/commit/';
  const originalHtml = btnExecute.innerHTML;

  const setButtonsDisabled = (disabled) => {
    btnExecute.disabled = disabled;
    if (btnCancel) btnCancel.disabled = disabled;
    if (btnClose) btnClose.disabled = disabled;
  };

  setButtonsDisabled(true);
  btnExecute.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Guardando en Base de Datos...';

  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(commitUrl, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrfToken,
        'X-Requested-With': 'XMLHttpRequest'
      }
    });

    const data = await response.json();
    if (!response.ok || !data.success) {
      alert(data.error || 'No se pudo guardar el proceso en la base de datos.');
      setButtonsDisabled(false);
      btnExecute.innerHTML = originalHtml;
      isCommitting = false;
      return;
    }

    document.getElementById('modalProcessId').textContent = '#' + data.process_id;
    document.getElementById('modalPeriod').textContent = data.accounting_period;
    document.getElementById('modalExecDate').textContent = `${data.execution_date} (${data.auditor})`;
    document.getElementById('modalVoucherStats').textContent = `${data.total_vouchers} registrados (${data.matched_count} exactos)`;
    document.getElementById('modalExcelDownloadBtn').href = data.export_url;
    document.getElementById('modalFinishBtn').href = data.upload_url;

    document.getElementById('commitStepConfirm').classList.add('d-none');
    document.getElementById('commitStepSuccess').classList.remove('d-none');
  } catch (err) {
    alert('Error de conexión al guardar en la base de datos: ' + err);
    setButtonsDisabled(false);
    btnExecute.innerHTML = originalHtml;
    isCommitting = false;
  }
}

function initVoucherTooltips() {
  if (window.bootstrap && typeof window.bootstrap.Tooltip === 'function') {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
      window.bootstrap.Tooltip.getOrCreateInstance(el);
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    applyVoucherFilters();
    initVoucherTooltips();
  });
} else {
  applyVoucherFilters();
  initVoucherTooltips();
}
