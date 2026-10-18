/**
 * Preview & Commit Logic (usd_preview.html, also loaded by clp_preview.html)
 * Handles:
 *  - Mayor a Menor / Menor a Mayor numeric column sorting
 *  - Card brand filter pills
 *  - Instant voucher search across every voucher tab (tbody[data-voucher-tab])
 *  - 2-step AJAX database commit with official Excel download
 */

let isCommitting = false;
let activeCardBrandFilter = 'ALL';
let activeCashierFilter = 'ALL';
const currentSortState = {};

function toggleFullscreenTable() {
  const isFocus = document.body.classList.toggle('table-focus-mode');
  const card = document.getElementById('voucherSectionCard');
  if (card) {
    card.classList.toggle('table-fullscreen-active', isFocus);
  }
  const icon = document.getElementById('iconFullscreen');
  const text = document.getElementById('textFullscreen');
  const btn = document.getElementById('btnToggleFullscreen');

  if (isFocus) {
    if (icon) icon.className = 'bi bi-fullscreen-exit me-1';
    if (text) text.textContent = 'Salir de Pantalla Completa (Esc)';
    if (btn) {
      btn.className = 'btn btn-sm btn-dark rounded-pill px-3 fw-bold shadow-sm';
    }
  } else {
    if (icon) icon.className = 'bi bi-arrows-fullscreen me-1';
    if (text) text.textContent = 'Pantalla Completa';
    if (btn) {
      btn.className = 'btn btn-sm btn-outline-secondary rounded-pill px-3 fw-semibold shadow-xs';
    }
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (document.querySelector('.modal.show')) {
      return;
    }
    if (document.body.classList.contains('table-focus-mode')) {
      toggleFullscreenTable();
    }
  }
});

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

function changeVoucherPageSize(newSize) {
  if (newSize === 'ALL') {
    voucherPageSize = Infinity;
  } else {
    voucherPageSize = parseInt(newSize, 10) || 20;
  }
  document.querySelectorAll('.voucher-page-size-select').forEach(sel => {
    sel.value = newSize;
  });
  Object.keys(voucherCurrentPages).forEach(k => {
    voucherCurrentPages[k] = 1;
  });
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
  const tabEl = document.getElementById(tabId);
  if (tabEl) {
    const scrollEl = tabEl.querySelector('.voucher-table-scroll');
    if (scrollEl) {
      scrollEl.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }
}

function buildVoucherPaginationHtml(tabId, currentPage, totalPages) {
  if (totalPages <= 1) return '';

  let html = '';
  const prevDisabled = currentPage === 1 ? ' disabled' : '';
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', 1)" title="Primera página">&laquo;</a></li>`;
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', ${currentPage - 1})" title="Anterior">&lsaquo;</a></li>`;

  let startP = Math.max(1, currentPage - 2);
  let endP = Math.min(totalPages, currentPage + 2);

  if (startP === 1) {
    endP = Math.min(totalPages, 5);
  } else if (endP === totalPages) {
    startP = Math.max(1, totalPages - 4);
  }

  if (startP > 1) {
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', 1)">1</a></li>`;
    if (startP > 2) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
  }

  for (let p = startP; p <= endP; p++) {
    if (p === currentPage) {
      html += `<li class="page-item active" aria-current="page"><span class="page-link">${p}</span></li>`;
    } else {
      html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', ${p})">${p}</a></li>`;
    }
  }

  if (endP < totalPages) {
    if (endP < totalPages - 1) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', ${totalPages})">${totalPages}</a></li>`;
  }

  const nextDisabled = currentPage === totalPages ? ' disabled' : '';
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', ${currentPage + 1})" title="Siguiente">&rsaquo;</a></li>`;
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToVoucherPage('${tabId}', ${totalPages})" title="Última página">&raquo;</a></li>`;

  return html;
}

function paginateVoucherTbody(tbodyId) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const cfg = VOUCHER_PAGINATION_CONFIG[tbodyId];
  const rows = Array.from(tbody.querySelectorAll('tr.voucher-row'));
  const matchedRows = rows.filter(r => r.dataset.filterMatch === 'true');
  const totalCount = matchedRows.length;

  if (!cfg) {
    matchedRows.forEach(r => r.classList.remove('d-none'));
    return;
  }

  const totalPages = voucherPageSize === Infinity ? 1 : Math.max(1, Math.ceil(totalCount / voucherPageSize));
  let curPage = voucherCurrentPages[cfg.tabId] || 1;
  if (curPage > totalPages) curPage = totalPages;
  if (curPage < 1) curPage = 1;
  voucherCurrentPages[cfg.tabId] = curPage;

  const startIdx = voucherPageSize === Infinity ? 0 : (curPage - 1) * voucherPageSize;
  const endIdx = voucherPageSize === Infinity ? totalCount : Math.min(startIdx + voucherPageSize, totalCount);

  matchedRows.forEach((row, i) => {
    if (i >= startIdx && i < endIdx) {
      row.classList.remove('d-none');
    } else {
      row.classList.add('d-none');
    }
  });

  const rangeEl = document.getElementById(cfg.rangeId);
  const totalEl = document.getElementById(cfg.totalId);
  if (rangeEl) {
    rangeEl.textContent = totalCount === 0 ? '0' : `${startIdx + 1}–${endIdx}`;
  }
  if (totalEl) {
    totalEl.textContent = totalCount;
  }

  const listEl = document.getElementById(cfg.listId);
  if (listEl) {
    listEl.innerHTML = buildVoucherPaginationHtml(cfg.tabId, curPage, totalPages);
  }
}

function sortVoucherTable(tbodyId, dataKey, thElem) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;

  const stateKey = `${tbodyId}_${dataKey}`;
  // Primer clic: Mayor a Menor ('desc'); segundo clic: Menor a Mayor ('asc')
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
  Object.keys(voucherCurrentPages).forEach(k => { voucherCurrentPages[k] = 1; });
  applyVoucherFilters();
}

function setCashierFilter(cashierVal) {
  activeCashierFilter = (cashierVal || 'ALL').trim().toUpperCase();
  Object.keys(voucherCurrentPages).forEach(k => { voucherCurrentPages[k] = 1; });
  applyVoucherFilters();
}

function clearVoucherSearch() {
  const input = document.getElementById('voucherSearchInput');
  if (input) {
    input.value = '';
    input.focus();
  }
  Object.keys(voucherCurrentPages).forEach(k => { voucherCurrentPages[k] = 1; });
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
    if (rows.length > 0 && visibleCount === 0) {
      noMatchRow.classList.remove('d-none');
    } else {
      noMatchRow.classList.add('d-none');
    }
  }

  const badge = document.getElementById(countBadgeId);
  if (badge) {
    badge.textContent = visibleCount;
  }

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
  if (totalBadge) {
    totalBadge.textContent = totalVisible;
  }
}

async function executeCommitToDatabase() {
  if (isCommitting) return;
  isCommitting = true;

  const btnExecute = document.getElementById('btnExecuteCommit');
  const btnCancel = document.getElementById('btnCancelCommitModal');
  const btnClose = document.getElementById('btnCloseConfirmModal');
  const commitUrl = btnExecute?.getAttribute('data-commit-url') || '/reconciliation/usd/commit/';
  const originalHtml = btnExecute.innerHTML;

  btnExecute.disabled = true;
  if (btnCancel) btnCancel.disabled = true;
  if (btnClose) btnClose.disabled = true;
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
      btnExecute.disabled = false;
      if (btnCancel) btnCancel.disabled = false;
      if (btnClose) btnClose.disabled = false;
      btnExecute.innerHTML = originalHtml;
      isCommitting = false;
      return;
    }

    document.getElementById('modalProcessId').textContent = '#' + data.process_id;
    document.getElementById('modalPeriod').textContent = data.accounting_period;
    document.getElementById('modalExecDate').textContent = data.execution_date + ' (' + data.auditor + ')';
    document.getElementById('modalVoucherStats').textContent = `${data.total_vouchers} registrados (${data.matched_count} exactos)`;
    document.getElementById('modalExcelDownloadBtn').href = data.export_url;
    document.getElementById('modalFinishBtn').href = data.upload_url;

    document.getElementById('commitStepConfirm').classList.add('d-none');
    document.getElementById('commitStepSuccess').classList.remove('d-none');
  } catch (err) {
    alert('Error de conexión al guardar en la base de datos: ' + err);
    btnExecute.disabled = false;
    if (btnCancel) btnCancel.disabled = false;
    if (btnClose) btnClose.disabled = false;
    btnExecute.innerHTML = originalHtml;
    isCommitting = false;
  }
}

function escapeHtmlStr(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatUsdClient(val) {
  const num = Number(val) || 0;
  return 'US$ ' + num.toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

let currentSugSectionVouchers = [];
let currentSugFilterMode = 'ALL';
let currentSugPageSize = 20;
let currentSugCurrentPage = 1;

function changeSugPageSize(newSize) {
  if (newSize === 'ALL') {
    currentSugPageSize = Infinity;
  } else {
    currentSugPageSize = parseInt(newSize, 10) || 20;
  }
  currentSugCurrentPage = 1;
  renderModalSuggestionsPage();
}

function goToSugPage(pageNum) {
  currentSugCurrentPage = pageNum;
  renderModalSuggestionsPage();
  const modalBody = document.querySelector('#suggestionDetailModal .modal-body');
  if (modalBody) {
    modalBody.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function filterModalSuggestions(mode, btnElem) {
  currentSugFilterMode = mode || 'ALL';
  currentSugCurrentPage = 1;

  document.querySelectorAll('#sugModalFilterGroup .sug-filter-btn').forEach(btn => {
    btn.classList.remove('btn-dark', 'active');
    if (btn.textContent.includes('Con Sugerencia')) {
      btn.classList.add('btn-outline-success');
    } else {
      btn.classList.add('btn-outline-secondary');
    }
  });

  if (btnElem) {
    btnElem.classList.remove('btn-outline-success', 'btn-outline-secondary');
    btnElem.classList.add('btn-dark', 'active');
  }

  renderModalSuggestionsPage();
}

function buildPaginationHtml(currentPage, totalPages) {
  if (totalPages <= 1) return '';

  let html = '';
  const prevDisabled = currentPage === 1 ? ' disabled' : '';
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(1)" title="Primera página">&laquo;</a></li>`;
  html += `<li class="page-item${prevDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(${currentPage - 1})" title="Anterior">&lsaquo;</a></li>`;

  let startP = Math.max(1, currentPage - 2);
  let endP = Math.min(totalPages, currentPage + 2);

  if (startP === 1) {
    endP = Math.min(totalPages, 5);
  } else if (endP === totalPages) {
    startP = Math.max(1, totalPages - 4);
  }

  if (startP > 1) {
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(1)">1</a></li>`;
    if (startP > 2) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
  }

  for (let p = startP; p <= endP; p++) {
    if (p === currentPage) {
      html += `<li class="page-item active" aria-current="page"><span class="page-link">${p}</span></li>`;
    } else {
      html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(${p})">${p}</a></li>`;
    }
  }

  if (endP < totalPages) {
    if (endP < totalPages - 1) {
      html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
    }
    html += `<li class="page-item"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(${totalPages})">${totalPages}</a></li>`;
  }

  const nextDisabled = currentPage === totalPages ? ' disabled' : '';
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(${currentPage + 1})" title="Siguiente">&rsaquo;</a></li>`;
  html += `<li class="page-item${nextDisabled}"><a class="page-link" href="javascript:void(0)" onclick="goToSugPage(${totalPages})" title="Última página">&raquo;</a></li>`;

  return html;
}

function renderModalSuggestionsPage() {
  const isPresent = (val) => {
    const s = String(val || '').trim();
    return Boolean(s && !s.startsWith('—'));
  };

  const renderKv = (label, val, isDiff, isMatched) => {
    const strVal = String(val || '').trim();
    if (!isPresent(strVal)) {
      return '';
    }
    const rowClass = isDiff ? 'suggestion-kv-row suggestion-kv-diff' : 'suggestion-kv-row';
    const iconHtml = isDiff
      ? '<i class="bi bi-exclamation-triangle-fill ms-1" title="Dato con diferencia"></i>'
      : (isMatched ? '<i class="bi bi-check2 text-success fw-bold ms-1" title="Coincide entre archivos"></i>' : '');
    return (
      `<div class="${rowClass}">` +
        `<span class="suggestion-kv-label">${escapeHtmlStr(label)}</span>` +
        `<span class="suggestion-kv-val" title="${escapeHtmlStr(strVal)}">${escapeHtmlStr(strVal)}${iconHtml}</span>` +
      `</div>`
    );
  };

  // 1. Filtrar vouchers según el modo activo
  let filtered = [];
  if (currentSugFilterMode === 'WITH') {
    filtered = currentSugSectionVouchers.filter(v => Boolean(v.suggestion));
  } else if (currentSugFilterMode === 'WITHOUT') {
    filtered = currentSugSectionVouchers.filter(v => !v.suggestion);
  } else {
    filtered = currentSugSectionVouchers.slice();
  }

  // Asociar índice correlativo (1-based) para numeración estable
  const indexedVouchers = filtered.map((v, i) => ({ voucher: v, displayIndex: i + 1 }));
  const totalCount = indexedVouchers.length;

  // 2. Cálculo de paginación
  const pageSize = currentSugPageSize;
  const totalPages = (pageSize === Infinity || totalCount === 0) ? 1 : Math.ceil(totalCount / pageSize);

  if (currentSugCurrentPage < 1) currentSugCurrentPage = 1;
  if (currentSugCurrentPage > totalPages) currentSugCurrentPage = totalPages;

  const startIndex = (pageSize === Infinity || totalCount === 0) ? 0 : (currentSugCurrentPage - 1) * pageSize;
  const endIndex = (pageSize === Infinity) ? totalCount : Math.min(startIndex + pageSize, totalCount);

  const pageItems = indexedVouchers.slice(startIndex, endIndex);
  const pageWith = pageItems.filter(item => Boolean(item.voucher.suggestion));
  const pageWithout = pageItems.filter(item => !item.voucher.suggestion);

  // 3. Renderizar Bloque A: Con Sugerencia Asistida
  const withSec = document.getElementById('sugModalWithSection');
  const withListEl = document.getElementById('sugModalWithList');

  if (withSec && withListEl) {
    if (currentSugFilterMode === 'WITHOUT') {
      withSec.classList.add('d-none');
    } else {
      withSec.classList.remove('d-none');
      if (pageWith.length === 0) {
        if (currentSugFilterMode === 'WITH' || totalCount === 0) {
          withListEl.innerHTML = (
            '<div class="p-3 rounded-3 border text-muted small text-center">' +
            '<i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias secundarias en esta sección.' +
            '</div>'
          );
        } else {
          withListEl.innerHTML = (
            '<div class="p-3 rounded-3 border text-muted small text-center fst-italic">' +
            '<i class="bi bi-info-circle me-1"></i>Los casos con sugerencia asistida de esta selección se encuentran en otras páginas del listado.' +
            '</div>'
          );
        }
      } else {
        withListEl.innerHTML = pageWith.map(item => {
          const v = item.voucher;
          const idx = item.displayIndex;
          const sug = v.suggestion || {};
          const files = sug.files_involved || {};
          const rows = Array.isArray(sug.comparison_rows) ? sug.comparison_rows : [];
          const rAuth = rows[0] || {};
          const rDoc = rows[1] || {};
          const rUsd = rows[2] || {};
          const rClp = rows[3] || {};
          const rCtx = rows[4] || {};

          const authDiff = rAuth.status_tone === 'gold';
          const usdDiff = rUsd.status_tone === 'gold';
          const clpDiff = rClp.status_tone === 'gold';

          // Un campo lleva ticket (✓) SOLO si existe en al menos 2 archivos y coincide entre ellos
          const authMatch = !authDiff && rAuth.status_tone === 'emerald' && isPresent(rAuth.tbk_value) && isPresent(rAuth.erp_value);
          const docMatch = (rDoc.status_tone === 'emerald' || rDoc.status_tone === 'cyan') && isPresent(rDoc.erp_value) && isPresent(rDoc.op_value);
          const usdMatch = !usdDiff && rUsd.status_tone === 'emerald' && isPresent(rUsd.tbk_value) && isPresent(rUsd.op_value);
          const clpMatch = !clpDiff && rClp.status_tone === 'emerald' && isPresent(rClp.erp_value) && isPresent(rClp.op_value);
          const dateMatch = rCtx.status_tone === 'emerald' && isPresent(rCtx.tbk_value) && isPresent(rCtx.erp_value);

          const causeChip = (Array.isArray(sug.reasons) ? sug.reasons : []).find(r => r.tone === 'gold');
          const causeBadgeHtml = causeChip
            ? `<span class="reason-chip reason-gold"><i class="bi bi-exclamation-triangle-fill"></i> ${escapeHtmlStr(causeChip.label)}</span>`
            : `<span class="suggestion-confidence">${escapeHtmlStr(sug.headline || '')}</span>`;

          const hasErpData = isPresent(rAuth.erp_value) || isPresent(rDoc.erp_value) || isPresent(rClp.erp_value);

          const docErpShort = String(rDoc.erp_value || '').split('(')[0].trim();
          const docOpShort = String(rDoc.op_value || '').split('(')[0].trim();

          let erpCashier = sug.erp_cashier || v.cashier_username || '';
          const ctxStr = String(rCtx.erp_value || '');
          if (!erpCashier || erpCashier === '—') {
            const cMatch = ctxStr.match(/Cajero:\s*([^\s·)]+)/i) || ctxStr.match(/\(([^)]+)\)\s*$/);
            if (cMatch) erpCashier = cMatch[1].trim();
          }
          let erpDate = sug.erp_date || '';
          if (!erpDate || erpDate === '—') {
            const dMatch = ctxStr.match(/\b(\d{2}\/\d{2}\/\d{4})\b/);
            erpDate = dMatch ? dMatch[1] : (v.sale_date || '—');
          }
          const tbkDate = v.sale_date || '—';
          const isDateMatched = isPresent(tbkDate) && isPresent(erpDate) && (tbkDate === erpDate || dateMatch);

          const operatorLabel = `${v.card_name} (${v.card_code || ''})`;
          const erpCardLabel = sug.erp_card || operatorLabel;

          const colTbkHtml = (
            renderKv('Voucher', rAuth.tbk_value || v.authorization_code, authDiff, authMatch) +
            renderKv('Monto US$', rUsd.tbk_value || formatUsdClient(v.gross_amount_usd), usdDiff, usdMatch) +
            renderKv('Fecha POS', tbkDate, false, isDateMatched) +
            renderKv('Tarjeta', operatorLabel, false, true)
          );

          const colErpHtml = hasErpData ? (
            renderKv('Cód. ERP', rAuth.erp_value, authDiff, authMatch) +
            renderKv('Documento', docErpShort || rDoc.erp_value, false, docMatch) +
            renderKv('Monto CLP', rClp.erp_value, clpDiff, clpMatch) +
            renderKv('Tarjeta', erpCardLabel, false, true) +
            renderKv('Fecha ERP', erpDate, false, isDateMatched) +
            renderKv('Registró', erpCashier || '—', false, false)
          ) : (
            `<div class="small text-muted py-4 text-center"><i class="bi bi-slash-circle me-1"></i>No facturado en ERP Hotel</div>`
          );

          const colOpHtml = (
            renderKv('Monto US$', rUsd.op_value, usdDiff, usdMatch) +
            renderKv('Documento', docOpShort || rDoc.op_value, false, docMatch) +
            renderKv('Saldo CLP', rClp.op_value, clpDiff, clpMatch) +
            renderKv('Tarjeta', operatorLabel, false, true)
          );

          return (
            `<div class="rounded-3 border overflow-hidden shadow-sm">` +
              `<div class="px-3 py-2 border-bottom d-flex flex-wrap justify-content-between align-items-center gap-2 suggestion-manual-box rounded-0">` +
                `<div class="d-flex flex-wrap align-items-center gap-2 small">` +
                  `<span class="fw-bold font-monospace">#${idx} · Voucher ${escapeHtmlStr(v.authorization_code)}</span>` +
                  `<span class="text-muted">·</span>` +
                  `<span class="fw-semibold">${escapeHtmlStr(v.card_name)}</span>` +
                  `<span class="text-muted">·</span>` +
                  `<span class="fw-bold">${escapeHtmlStr(formatUsdClient(v.gross_amount_usd))}</span>` +
                `</div>` +
                `<div>${causeBadgeHtml}</div>` +
              `</div>` +
              `<div class="suggestion-3col-grid">` +
                `<div class="suggestion-col-box suggestion-col-tbk">` +
                  `<div class="suggestion-col-title">1. Transbank (${escapeHtmlStr(files.tbk || 'ABONO TBK.xlsx')})</div>` +
                  `${colTbkHtml}` +
                `</div>` +
                `<div class="suggestion-col-box">` +
                  `<div class="suggestion-col-title">2. ERP Hotel (${escapeHtmlStr(files.erp || 'TARJETAS ERP.xlsx')})</div>` +
                  `${colErpHtml}` +
                `</div>` +
                `<div class="suggestion-col-box">` +
                  `<div class="suggestion-col-title">3. Operadora (${escapeHtmlStr(files.op || 'DOLAR.XLS')})</div>` +
                  `${colOpHtml}` +
                `</div>` +
              `</div>` +
            `</div>`
          );
        }).join('');
      }
    }
  }

  // 4. Renderizar Bloque B: Sin Sugerencia en Archivos
  const withoutSec = document.getElementById('sugModalWithoutSection');
  const withoutListEl = document.getElementById('sugModalWithoutList');

  if (withoutSec && withoutListEl) {
    if (currentSugFilterMode === 'WITH') {
      withoutSec.classList.add('d-none');
    } else {
      withoutSec.classList.remove('d-none');
      if (pageWithout.length === 0) {
        if (currentSugFilterMode === 'WITHOUT' || totalCount === 0) {
          withoutListEl.innerHTML = (
            '<div class="p-3 rounded-3 border text-success small text-center">' +
            '<i class="bi bi-check-circle-fill me-1"></i>¡Todos los vouchers de esta sección cuentan con una sugerencia asistida detectada!' +
            '</div>'
          );
        } else {
          withoutListEl.innerHTML = (
            '<div class="p-3 rounded-3 border text-muted small text-center fst-italic">' +
            '<i class="bi bi-info-circle me-1"></i>Los vouchers sin sugerencia de esta selección se encuentran en otras páginas del listado.' +
            '</div>'
          );
        }
      } else {
        const rowsNoSug = pageWithout.map(item => {
          const v = item.voucher;
          const idx = item.displayIndex;
          return (
            `<tr>` +
              `<td class="ps-3 fw-bold font-monospace text-danger">#${idx} · ${escapeHtmlStr(v.authorization_code)}</td>` +
              `<td><span class="badge bg-dark">${escapeHtmlStr(v.card_name)}</span></td>` +
              `<td>${escapeHtmlStr(v.sale_date || '—')}</td>` +
              `<td class="text-end fw-bold">${escapeHtmlStr(formatUsdClient(v.gross_amount_usd))}</td>` +
              `<td class="pe-3 small text-muted">` +
                `<i class="bi bi-slash-circle text-warning me-1"></i>` +
                `Sin coincidencias en ERP ni Operadora (no existe ningún cobro libre con ese monto en dólares, saldo equivalente en pesos ni código similar en los archivos cargados).` +
              `</td>` +
            `</tr>`
          );
        }).join('');

        withoutListEl.innerHTML = (
          `<div class="table-responsive rounded-3 border">` +
            `<table class="table table-hover align-middle mb-0">` +
              `<thead class="table-light small text-uppercase text-muted">` +
                `<tr>` +
                  `<th class="ps-3">Cód. Autorización</th>` +
                  `<th>Tarjeta</th>` +
                  `<th>Fecha Venta</th>` +
                  `<th class="text-end">Monto TBK</th>` +
                  `<th class="pe-3">Diagnóstico de Búsqueda Secundaria</th>` +
                `</tr>` +
              `</thead>` +
              `<tbody>${rowsNoSug}</tbody>` +
            `</table>` +
          `</div>`
        );
      }
    }
  }

  // 5. Actualizar barra de paginación
  const infoEl = document.getElementById('sugPaginationInfo');
  if (infoEl) {
    if (totalCount === 0) {
      infoEl.textContent = 'Mostrando 0 de 0 casos';
    } else {
      infoEl.textContent = `Mostrando ${startIndex + 1} - ${endIndex} de ${totalCount} casos`;
    }
  }

  const listEl = document.getElementById('sugPaginationList');
  if (listEl) {
    listEl.innerHTML = buildPaginationHtml(currentSugCurrentPage, totalPages);
  }
}

function openSectionSuggestionsModal(sectionKey) {
  const isDisc = sectionKey === 'discrepancies';
  const scriptId = isDisc ? 'jsonDiscrepanciesData' : 'jsonOrphansData';
  const scriptEl = document.getElementById(scriptId);
  if (!scriptEl) return;

  let vouchers = [];
  try {
    vouchers = JSON.parse(scriptEl.textContent) || [];
  } catch (e) {
    console.error('Error parsing section suggestions JSON:', e);
    return;
  }

  currentSugSectionVouchers = vouchers;
  currentSugFilterMode = 'ALL';
  currentSugCurrentPage = 1;
  currentSugPageSize = 20;

  const sizeSelect = document.getElementById('sugPageSizeSelect');
  if (sizeSelect) {
    sizeSelect.value = '20';
  }

  const withSuggestions = vouchers.filter(v => Boolean(v.suggestion));
  const withoutSuggestions = vouchers.filter(v => !v.suggestion);

  const sectionLabel = isDisc ? 'Con Diferencia de Monto' : 'En TBK pero NO en ERP';
  const sectionBadge = document.getElementById('sugModalSectionBadge');
  if (sectionBadge) sectionBadge.textContent = sectionLabel;

  const statsBadge = document.getElementById('sugModalStatsBadge');
  if (statsBadge) {
    statsBadge.textContent = `${withSuggestions.length} con sugerencia · ${withoutSuggestions.length} sin sugerencia`;
  }

  const headlineEl = document.getElementById('sugModalHeadline');
  if (headlineEl) {
    headlineEl.textContent = isDisc
      ? 'Listado de Sugerencias Asistidas — Vouchers con Diferencia de Monto'
      : 'Listado de Sugerencias Asistidas — Vouchers en TBK pero NO en ERP';
  }

  const countAll = document.getElementById('sugCountAll');
  const countWith = document.getElementById('sugCountWith');
  const countWithout = document.getElementById('sugCountWithout');
  if (countAll) countAll.textContent = vouchers.length;
  if (countWith) countWith.textContent = withSuggestions.length;
  if (countWithout) countWithout.textContent = withoutSuggestions.length;

  // Restablecer estilos visuales de botones de filtro
  document.querySelectorAll('#sugModalFilterGroup .sug-filter-btn').forEach((btn, idx) => {
    btn.classList.remove('btn-dark', 'active');
    if (idx === 0) {
      btn.classList.add('btn-dark', 'active');
    } else if (btn.textContent.includes('Con Sugerencia')) {
      btn.classList.remove('btn-dark', 'active');
      btn.classList.add('btn-outline-success');
    } else {
      btn.classList.remove('btn-dark', 'active');
      btn.classList.add('btn-outline-secondary');
    }
  });

  // Renderizar la primera página (default 20 elementos)
  renderModalSuggestionsPage();

  const modalEl = document.getElementById('suggestionDetailModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}

// Auto-initialize voucher tables pagination and filters on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    applyVoucherFilters();
  });
} else {
  applyVoucherFilters();
}




