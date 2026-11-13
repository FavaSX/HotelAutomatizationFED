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
let currentSugPageSize = 25;
let currentSugWithPage = 1;
let currentSugWithoutPage = 1;
const SUG_WITHOUT_PAGE_SIZE = 25;

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
  currentSugPageSize = newSize === 'ALL' ? Infinity : (parseInt(newSize, 10) || 25);
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
        withListEl.innerHTML = pageWith.map((v, i) =>
          renderSuggestionCard(v, 'USD', { mode: 'preview', index: startWith + i + 1 })
        ).join('');
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
                `Sin coincidencias en ERP ni Posición por Clientes (no existe ningún cobro libre con ese monto en dólares, saldo equivalente en pesos ni código similar en los archivos cargados).` +
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
