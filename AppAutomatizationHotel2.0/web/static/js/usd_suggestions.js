/**
 * USD Level 2 Assisted Suggestions Modal Logic (usd_preview.html)
 * Relies on shared helpers from usd_preview.js (loaded first):
 *  - escapeHtmlStr(str)
 *  - buildPaginationItemsHtml(currentPage, totalPages, onClickForPage)
 */

let currentSugAllVouchers = [];
let currentSugWithItems = [];
let currentSugWithoutItems = [];
let currentSugFilterMode = 'ALL';
let currentSugPageSize = 20;
let currentSugWithPage = 1;
let currentSugWithoutPage = 1;
const SUG_WITHOUT_PAGE_SIZE = 20;

function formatUsdClient(val) {
  const num = Number(val) || 0;
  return 'US$ ' + num.toLocaleString('es-CL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function getUsdActionText(sug, v) {
  if (sug && sug.action) return sug.action;
  if (v && v.action) return v.action;

  const reasons = Array.isArray(sug?.reasons) ? sug.reasons : [];
  const goldReason = (reasons.find(r => r.tone === 'gold') || {}).label || '';
  const rows = Array.isArray(sug?.comparison_rows) ? sug.comparison_rows : [];
  const [rAuth = {}, rDoc = {}, rUsd = {}, rClp = {}] = rows;

  const docOp = String(rDoc.op_value || '').replace(/\(Sec\..*?\)/, '').trim();
  const docErp = String(rDoc.erp_value || '').replace(/\(Fact\..*?\)/, '').trim();

  if (goldReason.includes('Doc. Operadora') || (docOp && docErp && docOp !== docErp)) {
    return `Validar folio ${docOp || 'en Operadora'} y vincular con ${docErp || 'en ERP'}`;
  }
  if (goldReason.includes('Col. USD en 0') || String(rUsd.op_value || '').includes('0,00')) {
    return `Verificar columna USD en archivo Operadora para ${docErp || 'documento ERP'}; el saldo CLP cuadra exactamente`;
  }
  if (goldReason.includes('Variación Cambiaria')) {
    return `Ajustar diferencia cambiaria entre POS Transbank (${formatUsdClient(v.gross_amount_usd)}) y Operadora`;
  }
  if (goldReason.includes('Cobro Parcial') || goldReason.includes('Dif:')) {
    return `Revisar cobro parcial o propina por diferencia de monto con el cajero`;
  }
  if (goldReason.includes('error de tipeo') || (rAuth.erp_value && rAuth.tbk_value && rAuth.erp_value !== rAuth.tbk_value)) {
    return `Corregir código de autorización en ERP de '${rAuth.erp_value || 'sin código'}' a '${v.authorization_code}' (${docErp || 'ERP'})`;
  }
  if (goldReason.includes('Dividida') || String(rDoc.erp_value || '').includes('+')) {
    return `Unir facturas divididas bajo el voucher ${v.authorization_code} en el ERP`;
  }
  if (sug?.headline && sug.headline.includes('No facturado')) {
    return `Ingresar cobro de ${docOp || 'Operadora'} en ERP Hotel o verificar omisión de facturación`;
  }
  if (sug?.headline) {
    return sug.headline;
  }
  return 'Revisar antecedentes en archivos originales y conciliar manualmente';
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
  currentSugWithPage = 1;
  renderModalSuggestionsPage();
}

function goToSugWithPage(pageNum) {
  currentSugWithPage = pageNum;
  renderModalSuggestionsPage();
  const modalBody = document.querySelector('#suggestionDetailModal .modal-body');
  if (modalBody) {
    modalBody.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function goToSugWithoutPage(pageNum) {
  currentSugWithoutPage = pageNum;
  renderModalSuggestionsPage();
}

function filterModalSuggestions(mode, btnElem) {
  currentSugFilterMode = mode || 'ALL';
  currentSugWithPage = 1;
  currentSugWithoutPage = 1;
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

  // 1. Visibilidad de secciones según modo de filtro
  const withSec = document.getElementById('sugModalWithSection');
  const withListEl = document.getElementById('sugModalWithList');
  const withoutSec = document.getElementById('sugModalWithoutSection');
  const withoutListEl = document.getElementById('sugModalWithoutList');

  // =========================================================================
  // BLOQUE A: CASOS CON SUGERENCIA ASISTIDA (Paginación independiente)
  // =========================================================================
  if (withSec && withListEl) {
    if (currentSugFilterMode === 'WITHOUT') {
      withSec.classList.add('d-none');
    } else {
      withSec.classList.remove('d-none');
      const totalWith = currentSugWithItems.length;
      const pageSize = currentSugPageSize;
      const totalWithPages = (pageSize === Infinity || totalWith === 0) ? 1 : Math.ceil(totalWith / pageSize);
      currentSugWithPage = Math.max(1, Math.min(currentSugWithPage, totalWithPages));

      const startWith = (pageSize === Infinity || totalWith === 0) ? 0 : (currentSugWithPage - 1) * pageSize;
      const endWith = pageSize === Infinity ? totalWith : Math.min(startWith + pageSize, totalWith);
      const pageWith = currentSugWithItems.slice(startWith, endWith);

      if (totalWith === 0) {
        withListEl.innerHTML = '<div class="p-3 rounded-3 border text-muted small text-center"><i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias secundarias en esta sección.</div>';
      } else {
        withListEl.innerHTML = pageWith.map((v, i) => {
          const idx = startWith + i + 1;
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
            renderKv('Fecha ERP', erpDate, false, isDateMatched)
          ) : (
            `<div class="small text-muted py-4 text-center"><i class="bi bi-slash-circle me-1"></i>No facturado en ERP Hotel</div>`
          );

          const colOpHtml = (
            renderKv('Monto US$', rUsd.op_value, usdDiff, usdMatch) +
            renderKv('Documento', docOpShort || rDoc.op_value, false, docMatch) +
            renderKv('Saldo CLP', rClp.op_value, clpDiff, clpMatch) +
            renderKv('Tarjeta', operatorLabel, false, true)
          );

          const cashierBadge = (erpCashier && erpCashier !== '—')
            ? `<span class="text-muted">·</span><span class="badge bg-secondary-subtle text-secondary-emphasis"><i class="bi bi-person me-1"></i>Cajero: ${escapeHtmlStr(erpCashier)}</span>`
            : '';

          const actionText = getUsdActionText(sug, v);
          const actionHtml = actionText ? (
            `<div class="px-3 py-2 border-top small fw-semibold text-primary">` +
              `<i class="bi bi-arrow-right-circle me-1"></i>Acción sugerida: <span class="text-dark">${escapeHtmlStr(actionText)}</span>` +
            `</div>`
          ) : '';

          return (
            `<div class="rounded-3 border overflow-hidden shadow-sm">` +
              `<div class="px-3 py-2 border-bottom d-flex flex-wrap justify-content-between align-items-center gap-2 suggestion-manual-box rounded-0">` +
                `<div class="d-flex flex-wrap align-items-center gap-2 small">` +
                  `<span class="fw-bold font-monospace">#${idx} · Voucher ${escapeHtmlStr(v.authorization_code)}</span>` +
                  `<span class="text-muted">·</span>` +
                  `<span class="fw-semibold">${escapeHtmlStr(v.card_name)}</span>` +
                  `<span class="text-muted">·</span>` +
                  `<span class="fw-bold">${escapeHtmlStr(formatUsdClient(v.gross_amount_usd))}</span>` +
                  cashierBadge +
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
              `${actionHtml}` +
            `</div>`
          );
        }).join('');
      }

      // Paginación independiente de Bloque A
      const withInfoEl = document.getElementById('sugWithPaginationInfo');
      if (withInfoEl) {
        withInfoEl.textContent = totalWith === 0
          ? '0 sugerencias asistidas'
          : `Mostrando ${startWith + 1} - ${endWith} de ${totalWith} sugerencias asistidas`;
      }
      const withListNav = document.getElementById('sugWithPaginationList');
      if (withListNav) {
        withListNav.innerHTML = buildPaginationItemsHtml(currentSugWithPage, totalWithPages, (p) => `goToSugWithPage(${p})`);
      }
    }
  }

  // =========================================================================
  // BLOQUE B: CASOS SIN SUGERENCIA EN ARCHIVOS (Paginación independiente)
  // =========================================================================
  if (withoutSec && withoutListEl) {
    if (currentSugFilterMode === 'WITH') {
      withoutSec.classList.add('d-none');
    } else {
      withoutSec.classList.remove('d-none');
      const totalWithout = currentSugWithoutItems.length;
      const totalWithoutPages = Math.ceil(totalWithout / SUG_WITHOUT_PAGE_SIZE) || 1;
      currentSugWithoutPage = Math.max(1, Math.min(currentSugWithoutPage, totalWithoutPages));

      const startWithout = (currentSugWithoutPage - 1) * SUG_WITHOUT_PAGE_SIZE;
      const endWithout = Math.min(startWithout + SUG_WITHOUT_PAGE_SIZE, totalWithout);
      const pageWithout = currentSugWithoutItems.slice(startWithout, endWithout);

      if (totalWithout === 0) {
        withoutListEl.innerHTML = (
          '<div class="p-3 rounded-3 border text-success small text-center">' +
          '<i class="bi bi-check-circle-fill me-1"></i>¡Todos los vouchers de esta sección cuentan con una sugerencia asistida detectada!' +
          '</div>'
        );
        const withoutBar = document.getElementById('sugWithoutPaginationBar');
        if (withoutBar) withoutBar.classList.add('d-none');
      } else {
        const rowsNoSug = pageWithout.map((v, i) => {
          const idx = startWithout + i + 1;
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

        // Paginación independiente de Bloque B
        const withoutBar = document.getElementById('sugWithoutPaginationBar');
        if (withoutBar) withoutBar.classList.remove('d-none');
        const withoutInfoEl = document.getElementById('sugWithoutPaginationInfo');
        if (withoutInfoEl) {
          withoutInfoEl.textContent = `Mostrando ${startWithout + 1} - ${endWithout} de ${totalWithout} casos sin sugerencia`;
        }
        const withoutListNav = document.getElementById('sugWithoutPaginationList');
        if (withoutListNav) {
          withoutListNav.innerHTML = totalWithoutPages > 1
            ? buildPaginationItemsHtml(currentSugWithoutPage, totalWithoutPages, (p) => `goToSugWithoutPage(${p})`)
            : '';
        }
      }
    }
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
    console.error('Error parsing suggestions JSON:', e);
    return;
  }

  currentSugAllVouchers = vouchers;
  currentSugWithItems = vouchers.filter(v => Boolean(v.suggestion));
  currentSugWithoutItems = vouchers.filter(v => !v.suggestion);
  currentSugWithPage = 1;
  currentSugWithoutPage = 1;
  currentSugFilterMode = 'ALL';

  const total = vouchers.length;
  const countWith = currentSugWithItems.length;
  const countWithout = currentSugWithoutItems.length;

  const countAllEl = document.getElementById('sugCountAll');
  if (countAllEl) countAllEl.textContent = total;
  const countWithEl = document.getElementById('sugCountWith');
  if (countWithEl) countWithEl.textContent = countWith;
  const countWithoutEl = document.getElementById('sugCountWithout');
  if (countWithoutEl) countWithoutEl.textContent = countWithout;

  const badgeEl = document.getElementById('sugModalSectionBadge');
  if (badgeEl) {
    badgeEl.textContent = isDisc ? 'Con Diferencia' : 'En TBK pero NO en ERP';
  }

  const statsBadge = document.getElementById('sugModalStatsBadge');
  if (statsBadge) {
    statsBadge.textContent = `${countWith} con sugerencia · ${countWithout} sin sugerencia`;
  }

  const headlineEl = document.getElementById('sugModalHeadline');
  if (headlineEl) {
    headlineEl.textContent = isDisc
      ? 'Diagnóstico Asistido Nivel 2 — Vouchers con Diferencia de Monto'
      : 'Búsqueda Secundaria Asistida — Vouchers No Encontrados en ERP';
  }

  // Sincronizar selector de elementos por página con currentSugPageSize
  const pageSizeSelect = document.getElementById('sugPageSizeSelect');
  if (pageSizeSelect) {
    pageSizeSelect.value = String(currentSugPageSize === Infinity ? 'ALL' : currentSugPageSize);
  }

  resetSugFilterButtons();
  renderModalSuggestionsPage();

  const modalEl = document.getElementById('suggestionDetailModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}
