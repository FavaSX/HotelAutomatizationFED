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

function escapeHtmlStr(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function openSuggestionModal(btnElem) {
  const cell = btnElem.closest('td');
  const scriptEl = cell ? cell.querySelector('.suggestion-json') : null;
  if (!scriptEl) return;

  let sug = null;
  try {
    sug = JSON.parse(scriptEl.textContent);
  } catch (e) {
    console.error('Error parsing suggestion JSON:', e);
    return;
  }
  if (!sug) return;

  const voucherCode = btnElem.getAttribute('data-voucher') || '—';
  const sectionLabel = btnElem.getAttribute('data-section') || 'Revisión Asistida';

  const voucherBadge = document.getElementById('sugModalVoucherBadge');
  if (voucherBadge) voucherBadge.textContent = 'Voucher ' + voucherCode;

  const sectionBadge = document.getElementById('sugModalSectionBadge');
  if (sectionBadge) sectionBadge.textContent = sectionLabel;

  const confBadge = document.getElementById('sugModalConfidenceBadge');
  if (confBadge) {
    const isAlta = sug.confidence === 'ALTA';
    confBadge.textContent = 'Confianza ' + (sug.confidence || 'MEDIA');
    confBadge.className = 'suggestion-confidence ' + (isAlta ? 'suggestion-confidence-alta' : 'suggestion-confidence-media');
  }

  const headlineEl = document.getElementById('sugModalHeadline');
  if (headlineEl) headlineEl.textContent = sug.headline || 'Sugerencia de Cruce Secundario';

  const summaryEl = document.getElementById('sugModalCandidateSummary');
  if (summaryEl) summaryEl.textContent = sug.candidate_summary || '';

  const reasonsContainer = document.getElementById('sugModalReasonsContainer');
  if (reasonsContainer) {
    const reasons = Array.isArray(sug.reasons) ? sug.reasons : [];
    reasonsContainer.innerHTML = reasons.map(r => (
      `<span class="reason-chip reason-${escapeHtmlStr(r.tone || 'cyan')}">` +
      `<i class="bi ${escapeHtmlStr(r.icon || 'bi-check2-circle')}"></i> ` +
      `${escapeHtmlStr(r.label)}` +
      `</span>`
    )).join('');
  }

  const files = sug.files_involved || {};
  const thTbk = document.getElementById('sugThTbkFile');
  if (thTbk) thTbk.textContent = files.tbk || 'ABONO TBK.xlsx';
  const thErp = document.getElementById('sugThErpFile');
  if (thErp) thErp.textContent = files.erp || 'TARJETAS ERP.xlsx';
  const thOp = document.getElementById('sugThOpFile');
  if (thOp) thOp.textContent = files.op || 'Operadora DOLAR.XLS';

  const tbody = document.getElementById('sugModalComparisonTbody');
  if (tbody) {
    const rows = Array.isArray(sug.comparison_rows) ? sug.comparison_rows : [];
    tbody.innerHTML = rows.map(row => (
      `<tr>` +
      `<td class="ps-3 fw-semibold"><i class="bi ${escapeHtmlStr(row.icon || 'bi-dot')} text-primary me-2"></i>${escapeHtmlStr(row.field)}</td>` +
      `<td class="font-monospace small">${escapeHtmlStr(row.tbk_value)}</td>` +
      `<td class="font-monospace small fw-semibold">${escapeHtmlStr(row.erp_value)}</td>` +
      `<td class="font-monospace small">${escapeHtmlStr(row.op_value)}</td>` +
      `<td class="pe-3"><span class="reason-chip reason-${escapeHtmlStr(row.status_tone || 'cyan')}">${escapeHtmlStr(row.status_label)}</span></td>` +
      `</tr>`
    )).join('');
  }

  const manualNoteEl = document.getElementById('sugModalManualNote');
  if (manualNoteEl) {
    manualNoteEl.textContent = sug.manual_check_note || 'Verifique manualmente los documentos originales antes de tomar una decisión contable.';
  }

  const modalEl = document.getElementById('suggestionDetailModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}


