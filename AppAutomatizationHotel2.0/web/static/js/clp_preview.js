/**
 * CLP Preview Level 2 Suggestions Modal Logic (clp_preview.html)
 * Sorting, pagination, filters and the 2-step commit come from usd_preview.js (loaded first).
 * Supports opening the Level 2 Assistant modal for:
 *  - 'orphans' (En TBK pero NO en Borderó)
 *  - 'discrepancies' (Con Diferencia)
 *  - 'out_of_period' (Fuera de Período con sugerencia en Borderó histórico)
 */

const CLP_MODAL_SECTIONS = {
  orphans: {
    scriptId: 'jsonClpOrphansData',
    title: 'Sugerencias Asistidas — Vouchers en TBK pero NO en Borderó'
  },
  discrepancies: {
    scriptId: 'jsonClpDiscrepanciesData',
    title: 'Sugerencias Asistidas — Vouchers con Diferencia (TBK vs. Borderó)'
  },
  out_of_period: {
    scriptId: 'jsonClpOutOfPeriodData',
    title: 'Sugerencias Asistidas — Vouchers Fuera de Período (Borderó Histórico)'
  }
};

let currentClpSugAllVouchers = [];
let currentClpSugWithItems = [];
let currentClpSugWithoutItems = [];
let currentClpSugFilterMode = 'ALL';
let currentClpSugPageSize = 20;
let currentClpSugWithPage = 1;
let currentClpSugWithoutPage = 1;
const CLP_WITHOUT_PAGE_SIZE = 20;

function formatClpClient(val) {
  const num = Math.round(Number(val) || 0);
  return '$' + num.toLocaleString('es-CL');
}

function renderClpKv(label, value, tone) {
  const strVal = String(value ?? '').trim() || '—';
  const toneClass = tone === 'diff' ? 'suggestion-kv-row suggestion-kv-diff' : 'suggestion-kv-row';
  const icon = tone === 'match'
    ? '<i class="bi bi-check2 text-success fw-bold ms-1" title="Coincide entre archivos"></i>'
    : (tone === 'diff' ? '<i class="bi bi-exclamation-triangle-fill ms-1" title="Dato con diferencia"></i>' : '');
  return (
    `<div class="${toneClass}">` +
      `<span class="suggestion-kv-label">${escapeHtmlStr(label)}</span>` +
      `<span class="suggestion-kv-val" title="${escapeHtmlStr(strVal)}">${escapeHtmlStr(strVal)}${icon}</span>` +
    `</div>`
  );
}

function renderClpSuggestionCard(voucher, idx) {
  const sug = voucher.suggestion || {};
  const erp = sug.erp_row || {};
  const files = sug.files_involved || {};
  const sameAmount = Math.abs(Number(voucher.tbk_amount) - Number(erp.amount)) < 1;
  const sameDate = voucher.sale_date === erp.transaction_date;
  const sameCode = String(voucher.authorization_code || '').replace(/^0+/, '') === String(erp.authorization_code || '').replace(/^0+/, '');
  const sameMedium = (voucher.modality_label === 'Crédito' && erp.payment_group === 'CREDIT') ||
                     (voucher.modality_label !== 'Crédito' && erp.payment_group === 'REDCOMPRA');

  // Symmetrical 5 rows aligned horizontally:
  // Row 1: Voucher TBK      <-> Código ERP
  // Row 2: Monto TBK        <-> Monto ERP
  // Row 3: Fecha Venta      <-> Fecha Venta
  // Row 4: Medio / Marca    <-> Medio / Marca
  // Row 5: Local / Terminal <-> Cuenta / Factura
  const colTbk = (
    renderClpKv('Voucher TBK', voucher.authorization_code, sameCode ? 'match' : 'diff') +
    renderClpKv('Monto TBK', formatClpClient(voucher.tbk_amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha Venta', voucher.sale_date || '—', sameDate ? 'match' : '') +
    renderClpKv('Medio / Marca', `${voucher.modality_label} · ${voucher.card_name}`, sameMedium ? 'match' : 'diff') +
    renderClpKv('Local / Terminal', voucher.merchant_location || '—', '')
  );

  const colErp = (
    renderClpKv('Código ERP', erp.authorization_code || '— (sin código)', sameCode ? 'match' : 'diff') +
    renderClpKv('Monto ERP', formatClpClient(erp.amount), sameAmount ? 'match' : 'diff') +
    renderClpKv('Fecha Venta', `${erp.transaction_date || '—'}${erp.transaction_time ? ' ' + erp.transaction_time : ''}`, sameDate ? 'match' : '') +
    renderClpKv('Medio / Marca', `${erp.payment_code || '—'} · ${erp.card_name || '—'}`, sameMedium ? 'match' : 'diff') +
    renderClpKv('Cuenta / Factura', `Cta: ${erp.account_code || '—'}${erp.invoice_number && erp.invoice_number !== '—' ? ' · Fact: ' + erp.invoice_number : ''}`, '')
  );

  const cashierBadge = erp.cashier_username
    ? `<span class="text-muted">·</span><span class="badge bg-secondary-subtle text-secondary-emphasis"><i class="bi bi-person me-1"></i>Cajero: ${escapeHtmlStr(erp.cashier_username)}</span>`
    : '';

  const causeChip = (Array.isArray(sug.reasons) ? sug.reasons : []).find(r => r.tone === 'gold')
    || (Array.isArray(sug.reasons) ? sug.reasons : []).find(r => r.tone === 'emerald');
  const causeBadgeHtml = causeChip
    ? `<span class="reason-chip reason-${escapeHtmlStr(causeChip.tone || 'gold')}"><i class="bi ${escapeHtmlStr(causeChip.icon || 'bi-exclamation-triangle-fill')} me-1"></i>${escapeHtmlStr(causeChip.label)}</span>`
    : `<span class="suggestion-confidence ${sug.confidence === 'ALTA' ? 'suggestion-confidence-alta' : ''}">Confianza ${escapeHtmlStr(sug.confidence || 'MEDIA')}</span>`;

  const actionText = sug.action || voucher.action || '';
  const actionHtml = actionText ? (
    `<div class="px-3 py-2 border-top small fw-semibold text-primary">` +
      `<i class="bi bi-arrow-right-circle me-1"></i>Acción sugerida: <span class="text-dark">${escapeHtmlStr(actionText)}</span>` +
    `</div>`
  ) : '';

  return (
    `<div class="rounded-3 border overflow-hidden shadow-sm">` +
      `<div class="px-3 py-2 border-bottom d-flex flex-wrap justify-content-between align-items-center gap-2 suggestion-manual-box rounded-0">` +
        `<div class="d-flex flex-wrap align-items-center gap-2 small">` +
          `<span class="fw-bold font-monospace">#${idx} · Voucher ${escapeHtmlStr(voucher.authorization_code)}</span>` +
          `<span class="text-muted">·</span><span class="fw-semibold">${escapeHtmlStr(voucher.modality_label)}</span>` +
          `<span class="text-muted">·</span><span class="fw-bold">${escapeHtmlStr(formatClpClient(voucher.tbk_amount))}</span>` +
          cashierBadge +
        `</div>` +
        `<div>${causeBadgeHtml}</div>` +
      `</div>` +
      `<div class="suggestion-2col-grid">` +
        `<div class="suggestion-col-box suggestion-col-tbk">` +
          `<div class="suggestion-col-title">1. Transbank (${escapeHtmlStr(files.tbk || 'TBK')})</div>${colTbk}` +
        `</div>` +
        `<div class="suggestion-col-box">` +
          `<div class="suggestion-col-title">2. Borderó ERP (${escapeHtmlStr(erp.source_file || files.erp || 'BORDERO')}) · Fila ${escapeHtmlStr(erp.excel_row || '—')}</div>${colErp}` +
        `</div>` +
      `</div>` +
      `${actionHtml}` +
    `</div>`
  );
}

function resetClpSugFilterButtons(activeBtn) {
  const group = document.getElementById('clpSugModalFilterGroup');
  if (!group) return;
  group.querySelectorAll('.clp-sug-filter-btn').forEach(btn => {
    btn.classList.remove('btn-dark', 'active');
    if (btn.textContent.includes('Con Sugerencia')) {
      btn.classList.add('btn-outline-success');
    } else if (btn.textContent.includes('Sin Coincidencia') || btn.textContent.includes('Sin Sugerencia')) {
      btn.classList.add('btn-outline-secondary');
    } else {
      btn.classList.add('btn-outline-secondary');
    }
  });

  if (activeBtn) {
    activeBtn.classList.remove('btn-outline-success', 'btn-outline-secondary');
    activeBtn.classList.add('btn-dark', 'active');
  }
}

function changeClpSugPageSize(newSize) {
  currentClpSugPageSize = newSize === 'ALL' ? Infinity : (parseInt(newSize, 10) || 20);
  currentClpSugWithPage = 1;
  renderClpModalSuggestionsPage();
}

function goToClpSugWithPage(pageNum) {
  currentClpSugWithPage = pageNum;
  renderClpModalSuggestionsPage();
  const modalBody = document.querySelector('#clpSuggestionModal .modal-body');
  if (modalBody) {
    modalBody.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function goToClpSugWithoutPage(pageNum) {
  currentClpSugWithoutPage = pageNum;
  renderClpModalSuggestionsPage();
}

function filterClpModalSuggestions(mode, btnElem) {
  currentClpSugFilterMode = mode || 'ALL';
  currentClpSugWithPage = 1;
  currentClpSugWithoutPage = 1;
  resetClpSugFilterButtons(btnElem);
  renderClpModalSuggestionsPage();
}

function renderClpModalSuggestionsPage() {
  const withSec = document.getElementById('clpSugModalWithSection');
  const withListEl = document.getElementById('clpSugModalWithList');
  const withoutSec = document.getElementById('clpSugModalWithoutSection');
  const withoutListEl = document.getElementById('clpSugModalWithoutList');

  // =========================================================================
  // BLOQUE A: CASOS CON SUGERENCIA ASISTIDA (Paginación independiente)
  // =========================================================================
  if (withSec && withListEl) {
    if (currentClpSugFilterMode === 'WITHOUT') {
      withSec.classList.add('d-none');
    } else {
      withSec.classList.remove('d-none');
      const totalWith = currentClpSugWithItems.length;
      const pageSize = currentClpSugPageSize;
      const totalWithPages = (pageSize === Infinity || totalWith === 0) ? 1 : Math.ceil(totalWith / pageSize);
      currentClpSugWithPage = Math.max(1, Math.min(currentClpSugWithPage, totalWithPages));

      const startWith = (pageSize === Infinity || totalWith === 0) ? 0 : (currentClpSugWithPage - 1) * pageSize;
      const endWith = pageSize === Infinity ? totalWith : Math.min(startWith + pageSize, totalWith);
      const pageWith = currentClpSugWithItems.slice(startWith, endWith);

      if (totalWith === 0) {
        withListEl.innerHTML = '<div class="p-3 rounded-3 border text-muted small text-center"><i class="bi bi-info-circle me-1"></i>No se detectaron sugerencias asistidas en esta sección.</div>';
      } else {
        withListEl.innerHTML = pageWith.map((v, i) => renderClpSuggestionCard(v, startWith + i + 1)).join('');
      }

      // Paginación independiente de Bloque A
      const withInfoEl = document.getElementById('clpWithPaginationInfo');
      if (withInfoEl) {
        withInfoEl.textContent = totalWith === 0
          ? '0 sugerencias asistidas'
          : `Mostrando ${startWith + 1} - ${endWith} de ${totalWith} sugerencias asistidas`;
      }
      const withListNav = document.getElementById('clpWithPaginationList');
      if (withListNav) {
        withListNav.innerHTML = buildPaginationItemsHtml(currentClpSugWithPage, totalWithPages, (p) => `goToClpSugWithPage(${p})`);
      }
    }
  }

  // =========================================================================
  // BLOQUE B: CASOS SIN COINCIDENCIA EN BORDERÓ (Paginación independiente)
  // =========================================================================
  if (withoutSec && withoutListEl) {
    if (currentClpSugFilterMode === 'WITH') {
      withoutSec.classList.add('d-none');
    } else {
      withoutSec.classList.remove('d-none');
      const totalWithout = currentClpSugWithoutItems.length;
      const totalWithoutPages = Math.ceil(totalWithout / CLP_WITHOUT_PAGE_SIZE) || 1;
      currentClpSugWithoutPage = Math.max(1, Math.min(currentClpSugWithoutPage, totalWithoutPages));

      const startWithout = (currentClpSugWithoutPage - 1) * CLP_WITHOUT_PAGE_SIZE;
      const endWithout = Math.min(startWithout + CLP_WITHOUT_PAGE_SIZE, totalWithout);
      const pageWithout = currentClpSugWithoutItems.slice(startWithout, endWithout);

      if (totalWithout === 0) {
        withoutListEl.innerHTML = (
          '<div class="p-3 rounded-3 border text-success small text-center">' +
          '<i class="bi bi-check-circle-fill me-1"></i>¡Todos los vouchers de esta sección cuentan con una sugerencia asistida detectada!' +
          '</div>'
        );
        const withoutBar = document.getElementById('clpWithoutPaginationBar');
        if (withoutBar) withoutBar.classList.add('d-none');
      } else {
        const rowsNoSug = pageWithout.map((v, i) => {
          const idx = startWithout + i + 1;
          return (
            `<tr>` +
              `<td class="ps-3 fw-bold font-monospace text-danger">#${idx} · ${escapeHtmlStr(v.authorization_code)}</td>` +
              `<td><span class="badge bg-dark">${escapeHtmlStr(v.modality_label)}</span> <small class="text-muted">${escapeHtmlStr(v.card_name || '')}</small></td>` +
              `<td>${escapeHtmlStr(v.sale_date || '—')}</td>` +
              `<td class="text-end fw-bold">${escapeHtmlStr(formatClpClient(v.tbk_amount))}</td>` +
              `<td class="pe-3 small text-muted"><i class="bi bi-slash-circle text-warning me-1"></i>${escapeHtmlStr(v.action || 'Sin coincidencia en Borderó ERP (no existe cobro con ese monto o código similar en el archivo cargado).')}</td>` +
            `</tr>`
          );
        }).join('');

        withoutListEl.innerHTML = (
          `<div class="table-responsive rounded-3 border">` +
            `<table class="table table-hover align-middle mb-0">` +
              `<thead class="table-light small text-uppercase text-muted">` +
                `<tr>` +
                  `<th class="ps-3">Cód. Autorización</th>` +
                  `<th>Medio</th>` +
                  `<th>Fecha Venta</th>` +
                  `<th class="text-end">Monto TBK</th>` +
                  `<th class="pe-3">Diagnóstico / Acción Sugerida</th>` +
                `</tr>` +
              `</thead>` +
              `<tbody>${rowsNoSug}</tbody>` +
            `</table>` +
          `</div>`
        );

        // Paginación independiente de Bloque B
        const withoutBar = document.getElementById('clpWithoutPaginationBar');
        if (withoutBar) withoutBar.classList.remove('d-none');
        const withoutInfoEl = document.getElementById('clpWithoutPaginationInfo');
        if (withoutInfoEl) {
          withoutInfoEl.textContent = `Mostrando ${startWithout + 1} - ${endWithout} de ${totalWithout} casos sin coincidencia`;
        }
        const withoutListNav = document.getElementById('clpWithoutPaginationList');
        if (withoutListNav) {
          withoutListNav.innerHTML = totalWithoutPages > 1
            ? buildPaginationItemsHtml(currentClpSugWithoutPage, totalWithoutPages, (p) => `goToClpSugWithoutPage(${p})`)
            : '';
        }
      }
    }
  }
}

function openClpSuggestionsModal(sectionKey = 'orphans') {
  const cfg = CLP_MODAL_SECTIONS[sectionKey] || CLP_MODAL_SECTIONS.orphans;
  const scriptEl = document.getElementById(cfg.scriptId);
  if (!scriptEl) return;

  let vouchers = [];
  try {
    vouchers = JSON.parse(scriptEl.textContent) || [];
  } catch (e) {
    console.error('Error parsing CLP suggestions JSON:', e);
    return;
  }

  currentClpSugAllVouchers = vouchers;
  currentClpSugWithItems = vouchers.filter(v => Boolean(v.suggestion));
  currentClpSugWithoutItems = vouchers.filter(v => !v.suggestion);
  currentClpSugWithPage = 1;
  currentClpSugWithoutPage = 1;
  currentClpSugFilterMode = 'ALL';

  const total = vouchers.length;
  const countWith = currentClpSugWithItems.length;
  const countWithout = currentClpSugWithoutItems.length;

  const titleEl = document.getElementById('clpSugModalTitle');
  if (titleEl) titleEl.textContent = cfg.title;

  const badgeEl = document.getElementById('clpSugStatsBadge');
  if (badgeEl) {
    badgeEl.textContent = `${countWith} con sugerencia · ${countWithout} sin coincidencia`;
  }

  const countAllEl = document.getElementById('clpSugCountAll');
  if (countAllEl) countAllEl.textContent = total;
  const countWithEl = document.getElementById('clpSugCountWith');
  if (countWithEl) countWithEl.textContent = countWith;
  const countWithoutEl = document.getElementById('clpSugCountWithout');
  if (countWithoutEl) countWithoutEl.textContent = countWithout;

  // Sincronizar selector de elementos por página con currentClpSugPageSize
  const pageSizeSelect = document.getElementById('clpSugPageSizeSelect');
  if (pageSizeSelect) {
    pageSizeSelect.value = String(currentClpSugPageSize === Infinity ? 'ALL' : currentClpSugPageSize);
  }

  // Resetear botón de filtro activo
  const group = document.getElementById('clpSugModalFilterGroup');
  if (group) {
    const firstBtn = group.querySelector('.clp-sug-filter-btn');
    resetClpSugFilterButtons(firstBtn);
  }

  renderClpModalSuggestionsPage();

  const modalEl = document.getElementById('clpSuggestionModal');
  if (modalEl && window.bootstrap) {
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }
}
