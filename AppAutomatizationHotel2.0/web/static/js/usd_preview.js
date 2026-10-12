/**
 * USD Preview & Commit Logic (usd_preview.html)
 * Handles:
 *  - Mayor a Menor / Menor a Mayor numeric column sorting
 *  - Card brand filter pills
 *  - Instant voucher search across all 3 tabs
 *  - 2-step AJAX database commit with official Excel download
 */

let isCommitting = false;
let activeCardBrandFilter = 'ALL';
const currentSortState = {};

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
}

function setCardBrandFilter(brandKey, btnElem) {
  activeCardBrandFilter = brandKey;
  document.querySelectorAll('#cardBrandFilterGroup .card-filter-btn').forEach(btn => {
    btn.classList.remove('btn-dark', 'active');
    btn.classList.add('btn-outline-secondary');
  });
  btnElem.classList.remove('btn-outline-secondary');
  btnElem.classList.add('btn-dark', 'active');
  applyVoucherFilters();
}

function clearVoucherSearch() {
  const input = document.getElementById('voucherSearchInput');
  if (input) {
    input.value = '';
    input.focus();
  }
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
    const rowText = row.textContent.toLowerCase();

    const matchesCard = (activeCardBrandFilter === 'ALL') || cardAttr.includes(activeCardBrandFilter);
    const matchesQuery = (!query) || rowText.includes(query);

    if (matchesCard && matchesQuery) {
      row.classList.remove('d-none');
      visibleCount++;
    } else {
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

  return visibleCount;
}

function applyVoucherFilters() {
  const query = (document.getElementById('voucherSearchInput')?.value || '').trim();
  const clearBtn = document.getElementById('btnClearVoucherSearch');
  if (clearBtn) {
    clearBtn.classList.toggle('d-none', query.length === 0);
  }

  const vMatched = filterSingleTab('tbodyMatched', 'noMatchRowMatched', 'countBadgeMatched');
  const vDisc = filterSingleTab('tbodyDiscrepancies', 'noMatchRowDiscrepancies', 'countBadgeDiscrepancies');
  const vOrphans = filterSingleTab('tbodyOrphans', 'noMatchRowOrphans', 'countBadgeOrphans');

  const totalVisible = vMatched + vDisc + vOrphans;
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

