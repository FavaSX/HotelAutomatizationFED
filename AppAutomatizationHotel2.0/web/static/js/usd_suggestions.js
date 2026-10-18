/**
 * USD Level 2 Assisted Suggestions Modal Logic (usd_preview.html)
 * Relies on shared helpers from usd_preview.js (loaded first):
 *  - escapeHtmlStr(str)
 *  - buildPaginationItemsHtml(currentPage, totalPages, onClickForPage)
 */

let currentSugSectionVouchers = [];
let currentSugFilterMode = 'ALL';
let currentSugPageSize = 20;
let currentSugCurrentPage = 1;

function formatUsdClient(val) {
  const num = Number(val) || 0;
  return 'US$ ' + num.toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function resetSugFilterButtons(activeBtn) {
  const buttons = document.querySelectorAll('#sugModalFilterGroup .sug-filter-btn');
  buttons.forEach((btn, idx) => {
    btn.classList.remove('btn-dark', 'active');
    if (btn.textContent.includes('Con Sugerencia')) {
      btn.classList.add('btn-outline-success');
    } else {
      btn.classList.add('btn-outline-secondary');
    }
    if (!activeBtn && idx === 0) {
      btn.classList.remove('btn-outline-success', 'btn-outline-secondary');
      btn.classList.add('btn-dark', 'active');
    }
  });

  if (activeBtn) {
    activeBtn.classList.remove('btn-outline-success', 'btn-outline-secondary');
    activeBtn.classList.add('btn-dark', 'active');
  }
}

function changeSugPageSize(newSize) {
  currentSugPageSize = newSize === 'ALL' ? Infinity : (parseInt(newSize, 10) || 20);
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
  resetSugFilterButtons(btnElem);
  renderModalSuggestionsPage();
}

function renderModalSuggestionsPage() {
  const isPresent = (val) => {
    const s = String(val || '').trim();
    return Boolean(s && !s.startsWith('—'));
  };

  const renderKv = (label, val, isDiff, isMatched) => {
    const strVal = String(val || '').trim();
    if (!isPresent(strVal)) return '';
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
  let filtered = currentSugSectionVouchers;
  if (currentSugFilterMode === 'WITH') {
    filtered = currentSugSectionVouchers.filter(v => Boolean(v.suggestion));
  } else if (currentSugFilterMode === 'WITHOUT') {
    filtered = currentSugSectionVouchers.filter(v => !v.suggestion);
  }

  const indexedVouchers = filtered.map((v, i) => ({ voucher: v, displayIndex: i + 1 }));
  const totalCount = indexedVouchers.length;

  // 2. Cálculo de paginación
  const pageSize = currentSugPageSize;
  const totalPages = (pageSize === Infinity || totalCount === 0) ? 1 : Math.ceil(totalCount / pageSize);
  currentSugCurrentPage = Math.max(1, Math.min(currentSugCurrentPage, totalPages));

  const startIndex = (pageSize === Infinity || totalCount === 0) ? 0 : (currentSugCurrentPage - 1) * pageSize;
  const endIndex = pageSize === Infinity ? totalCount : Math.min(startIndex + pageSize, totalCount);

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
        const msg = (currentSugFilterMode === 'WITH' || totalCount === 0)
          ? 'No se detectaron sugerencias secundarias en esta sección.'
          : 'Los casos con sugerencia asistida de esta selección se encuentran en otras páginas del listado.';
        const italic = (currentSugFilterMode === 'WITH' || totalCount === 0) ? '' : ' fst-italic';
        withListEl.innerHTML = `<div class="p-3 rounded-3 border text-muted small text-center${italic}"><i class="bi bi-info-circle me-1"></i>${msg}</div>`;
      } else {
        withListEl.innerHTML = pageWith.map(item => {
          const v = item.voucher;
          const idx = item.displayIndex;
          const sug = v.suggestion || {};
          const files = sug.files_involved || {};
          const rows = Array.isArray(sug.comparison_rows) ? sug.comparison_rows : [];
          const [rAuth = {}, rDoc = {}, rUsd = {}, rClp = {}, rCtx = {}] = rows;

          const authDiff = rAuth.status_tone === 'gold';
          const usdDiff = rUsd.status_tone === 'gold';
          const clpDiff = rClp.status_tone === 'gold';

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
          return (
            `<tr>` +
              `<td class="ps-3 fw-bold font-monospace text-danger">#${item.displayIndex} · ${escapeHtmlStr(v.authorization_code)}</td>` +
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

  // 5. Actualizar barra de paginación reutilizando el constructor compartido
  const infoEl = document.getElementById('sugPaginationInfo');
  if (infoEl) {
    infoEl.textContent = totalCount === 0
      ? 'Mostrando 0 de 0 casos'
      : `Mostrando ${startIndex + 1} - ${endIndex} de ${totalCount} casos`;
  }

  const listEl = document.getElementById('sugPaginationList');
  if (listEl) {
    listEl.innerHTML = buildPaginationItemsHtml(currentSugCurrentPage, totalPages, (p) => `goToSugPage(${p})`);
  }
}

function openSectionSuggestionsModal(sectionKey) {
  const isDisc = sectionKey === 'discrepancies';
  const scriptEl = document.getElementById(isDisc ? 'jsonDiscrepanciesData' : 'jsonOrphansData');
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
  if (sizeSelect) sizeSelect.value = '20';

  const withCount = vouchers.filter(v => Boolean(v.suggestion)).length;
  const withoutCount = vouchers.length - withCount;

  const sectionBadge = document.getElementById('sugModalSectionBadge');
  if (sectionBadge) {
    sectionBadge.textContent = isDisc ? 'Con Diferencia de Monto' : 'En TBK pero NO en ERP';
  }

  const statsBadge = document.getElementById('sugModalStatsBadge');
  if (statsBadge) {
    statsBadge.textContent = `${withCount} con sugerencia · ${withoutCount} sin sugerencia`;
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
  if (countWith) countWith.textContent = withCount;
  if (countWithout) countWithout.textContent = withoutCount;

  resetSugFilterButtons(null);
  renderModalSuggestionsPage();

  const modalEl = document.getElementById('suggestionDetailModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}

