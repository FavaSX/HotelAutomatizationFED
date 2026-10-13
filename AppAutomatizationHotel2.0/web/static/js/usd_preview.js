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

function formatUsdClient(val) {
  const num = Number(val) || 0;
  return 'US$ ' + num.toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function filterModalSuggestions(mode, btnElem) {
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

  const withSec = document.getElementById('sugModalWithSection');
  const withoutSec = document.getElementById('sugModalWithoutSection');
  if (withSec) {
    withSec.classList.toggle('d-none', mode === 'WITHOUT');
  }
  if (withoutSec) {
    withoutSec.classList.toggle('d-none', mode === 'WITH');
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

  // Reset filter to 'ALL'
  const firstFilterBtn = document.querySelector('#sugModalFilterGroup .sug-filter-btn');
  filterModalSuggestions('ALL', firstFilterBtn);

  // Render Block A: With Suggestions
  const withListEl = document.getElementById('sugModalWithList');
  if (withListEl) {
    if (withSuggestions.length === 0) {
      withListEl.innerHTML = (
        '<div class="p-3 rounded-3 border text-muted small text-center">' +
        '<i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias secundarias en esta sección.' +
        '</div>'
      );
    } else {
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

      withListEl.innerHTML = withSuggestions.map((v, idx) => {
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

        // Limpiar valores largos para que cada fila ocupe exactamente 1 línea sin desalinear las alturas:
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

        // Filas compactas y alineadas horizontalmente:
        // Fila 1: Voucher (TBK)   <-> Cód. ERP (ERP)  | Monto US$ (Operadora)
        // Fila 2: Monto US$ (TBK) | Documento (ERP)   <-> Documento (Operadora)  [MISMA FILA 2]
        // Fila 3: Fecha POS (TBK) | Monto CLP (ERP)   <-> Saldo CLP (Operadora)  [MISMA FILA 3]
        // Fila 4: Tarjeta (TBK)   <-> Tarjeta (ERP)   <-> Tarjeta (Operadora)    [MISMA FILA 4]
        // Fila 5:                 | Fecha ERP (ERP)   |
        // Fila 6:                 | Registró (Cajero) |
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
                `<span class="fw-bold font-monospace">#${idx + 1} · Voucher ${escapeHtmlStr(v.authorization_code)}</span>` +
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

  // Render Block B: Without Suggestions
  const withoutListEl = document.getElementById('sugModalWithoutList');
  if (withoutListEl) {
    if (withoutSuggestions.length === 0) {
      withoutListEl.innerHTML = (
        '<div class="p-3 rounded-3 border text-success small text-center">' +
        '<i class="bi bi-check-circle-fill me-1"></i>¡Todos los vouchers de esta sección cuentan con una sugerencia asistida detectada!' +
        '</div>'
      );
    } else {
      const rowsNoSug = withoutSuggestions.map((v, idx) => (
        `<tr>` +
          `<td class="ps-3 fw-bold font-monospace text-danger">#${idx + 1} · ${escapeHtmlStr(v.authorization_code)}</td>` +
          `<td><span class="badge bg-dark">${escapeHtmlStr(v.card_name)}</span></td>` +
          `<td>${escapeHtmlStr(v.sale_date || '—')}</td>` +
          `<td class="text-end fw-bold">${escapeHtmlStr(formatUsdClient(v.gross_amount_usd))}</td>` +
          `<td class="pe-3 small text-muted">` +
            `<i class="bi bi-slash-circle text-warning me-1"></i>` +
            `Sin coincidencias en ERP ni Operadora (no existe ningún cobro libre con ese monto en dólares, saldo equivalente en pesos ni código similar en los archivos cargados).` +
          `</td>` +
        `</tr>`
      )).join('');

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

  const modalEl = document.getElementById('suggestionDetailModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}



