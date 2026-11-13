/**
 * Upload & Auto-Detection Logic shared by usd_upload.html and clp_upload.html
 * Handles bulk drag-and-drop, structural fingerprint inspection,
 * manual slot selection, and MULTI-FILE ERP grouping.
 * All non-ERP roles strictly enforce 1 file per role.
 */

const USD_ROLE_INPUT_MAP = {
  erp: 'input_usd_erp',
  transbank: 'input_usd_tbk',
  bank: 'input_usd_bank',
  amex: 'input_usd_ax',
  diners: 'input_usd_di',
  visa: 'input_usd_vi',
  mastercard: 'input_usd_mc'
};

let bulkUploadedFiles = [];
let latestDetectionReport = null;
let extraErpIndices = new Set();
let extraTbkIndices = new Set();

function getUploadForm() {
  return document.querySelector('form[data-detect-url]');
}

function getRoleInputMap() {
  const form = getUploadForm();
  const declared = form ? form.getAttribute('data-role-inputs') : null;
  return declared ? JSON.parse(declared) : USD_ROLE_INPUT_MAP;
}

function getRequiredFileCount() {
  return Object.keys(getRoleInputMap()).length;
}

function getDetectFilesUrl() {
  const form = getUploadForm();
  return form ? form.getAttribute('data-detect-url') : '/reconciliation/usd/detect-files/';
}

function showProcessingOverlay() {
  const overlay = document.getElementById('processingOverlay');
  if (overlay) overlay.style.display = 'flex';
}

document.addEventListener('DOMContentLoaded', () => {
  const dropzoneBox = document.getElementById('bulkDropzoneBox');
  if (!dropzoneBox) return;

  ['dragenter', 'dragover'].forEach(evtName => {
    dropzoneBox.addEventListener(evtName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzoneBox.classList.add('drag-active');
    });
  });

  ['dragleave', 'drop'].forEach(evtName => {
    dropzoneBox.addEventListener(evtName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzoneBox.classList.remove('drag-active');
    });
  });

  dropzoneBox.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleBulkFilesSelected(e.dataTransfer.files);
    }
  });
});

async function handleBulkFilesSelected(fileList) {
  if (!fileList || fileList.length === 0) return;
  bulkUploadedFiles = Array.from(fileList);

  document.getElementById('expPhase1Dropzone').classList.add('d-none');
  document.getElementById('expPhase2Alignment').classList.add('d-none');
  document.getElementById('expScanningSpinner').classList.remove('d-none');

  const formData = new FormData();
  bulkUploadedFiles.forEach(file => formData.append('bulk_files', file));

  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok) {
      alert(data.error || 'Error al analizar los archivos.');
      resetExperimentalFiles();
      return;
    }
    latestDetectionReport = data;
    renderAlignmentTable(data);
  } catch (err) {
    alert('Error de conexión al inspeccionar los archivos: ' + err);
    resetExperimentalFiles();
  } finally {
    document.getElementById('expScanningSpinner').classList.add('d-none');
  }
}

function getConfidenceBadgeHtml(type) {
  switch (type) {
    case 'HIGH':
      return '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2 py-1"><i class="bi bi-check-circle-fill me-1"></i>100% Seguro</span>';
    case 'MEDIUM':
      return '<span class="badge bg-warning-subtle text-dark border border-warning-subtle rounded-pill px-2 py-1"><i class="bi bi-info-circle-fill me-1"></i>Por Nombre</span>';
    case 'MANUAL':
      return '<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill px-2 py-1"><i class="bi bi-hand-index-thumb me-1"></i>Asignado Manual</span>';
    default:
      return '<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Faltante</span>';
  }
}

function renderAlignmentTable(data) {
  const mainTbody = document.getElementById('alignmentMainTableBody');
  const posTbody = document.getElementById('alignmentPositionTableBody');
  const defaultTbody = document.getElementById('alignmentTableBody');

  if (mainTbody) mainTbody.innerHTML = '';
  if (posTbody) posTbody.innerHTML = '';
  if (defaultTbody) defaultTbody.innerHTML = '';

  extraErpIndices.clear();
  extraTbkIndices.clear();

  // Prepopulate extra ERP files detected
  const erpRole = data.aligned_roles.find(r => r.role_key === 'erp');
  if (erpRole && erpRole.extra_files && Array.isArray(erpRole.extra_files)) {
    erpRole.extra_files.forEach(ef => extraErpIndices.add(ef.file_index));
  }

  // Prepopulate extra TBK files detected
  const tbkRole = data.aligned_roles.find(r => r.role_key === 'tbk');
  if (tbkRole && tbkRole.extra_files && Array.isArray(tbkRole.extra_files)) {
    tbkRole.extra_files.forEach(ef => extraTbkIndices.add(ef.file_index));
  }

  const positionRoleKeys = new Set(['amex', 'diners', 'visa', 'mastercard']);

  data.aligned_roles.forEach(role => {
    const tr = document.createElement('tr');
    tr.id = `row_${role.role_key}`;

    let optionsHtml = '<option value="">— Sin archivo asignado —</option>';
    data.inspected_files.forEach(f => {
      const selected = (role.file_index === f.file_index) ? 'selected' : '';
      optionsHtml += `<option value="${Number(f.file_index)}" ${selected}>${escapeHtmlStr(f.file_name)} (${Number(f.row_count) || 0} filas)</option>`;
    });

    const badgeHtml = getConfidenceBadgeHtml(role.confidence);
    const safeRoleKey = escapeHtmlStr(role.role_key);
    const safeBadgeClass = escapeHtmlStr(role.badge_class);
    const safeBadge = escapeHtmlStr(role.badge);
    const safeLabel = escapeHtmlStr(role.role_label);
    const safeEvidence = escapeHtmlStr(role.evidence);

    if (role.role_key === 'erp') {
      // ERP Role: Primary file select + Extra files container
      tr.innerHTML = `
        <td class="ps-3 py-3 align-top">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${safeBadgeClass}">${safeBadge}</span>
            <span class="fw-bold text-dark">${safeLabel}</span>
          </div>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2">
            <select class="form-select form-select-sm role-file-select" id="select_${safeRoleKey}" data-role-key="${safeRoleKey}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir archivo principal ERP">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${safeRoleKey}', this)">
            </label>
          </div>
          <small class="text-muted d-block mt-1" id="evidence_${safeRoleKey}">${safeEvidence}</small>
          <div id="extraErpContainer" class="mt-2">
            <!-- Rendered by renderExtraErpWidgets() -->
          </div>
        </td>
        <td class="text-center pe-3 py-3 align-top" id="confidence_${safeRoleKey}">
          ${badgeHtml}
        </td>
      `;
    } else if (role.role_key === 'tbk') {
      // TBK Role: Primary file select + Extra files container (allows consolidated or multiple separate modality files)
      tr.innerHTML = `
        <td class="ps-3 py-3 align-top">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${safeBadgeClass}">${safeBadge}</span>
            <span class="fw-bold text-dark">${safeLabel}</span>
          </div>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2">
            <select class="form-select form-select-sm role-file-select" id="select_${safeRoleKey}" data-role-key="${safeRoleKey}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir archivo principal Transbank">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${safeRoleKey}', this)">
            </label>
          </div>
          <small class="text-muted d-block mt-1" id="evidence_${safeRoleKey}">${safeEvidence}</small>
          <div id="extraTbkContainer" class="mt-2">
            <!-- Rendered by renderExtraTbkWidgets() -->
          </div>
        </td>
        <td class="text-center pe-3 py-3 align-top" id="confidence_${safeRoleKey}">
          ${badgeHtml}
        </td>
      `;
    } else {
      tr.innerHTML = `
        <td class="ps-3 py-3">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${safeBadgeClass}">${safeBadge}</span>
            <span class="fw-bold text-dark">${safeLabel}</span>
          </div>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2">
            <select class="form-select form-select-sm role-file-select" id="select_${safeRoleKey}" data-role-key="${safeRoleKey}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir un archivo nuevo directamente para este rol">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${safeRoleKey}', this)">
            </label>
          </div>
          <small class="text-muted d-block mt-1" id="evidence_${safeRoleKey}">${safeEvidence}</small>
        </td>
        <td class="text-center pe-3 py-3" id="confidence_${safeRoleKey}">
          ${badgeHtml}
        </td>
      `;
    }

    if (posTbody && positionRoleKeys.has(role.role_key)) {
      posTbody.appendChild(tr);
    } else if (mainTbody) {
      mainTbody.appendChild(tr);
    } else if (defaultTbody) {
      defaultTbody.appendChild(tr);
    }
  });

  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  document.getElementById('expPhase2Alignment').classList.remove('d-none');
  validateCurrentAlignmentSelection();
}

function getAssignedPrimaryIndices() {
  return new Set(
    Array.from(document.querySelectorAll('.role-file-select'))
      .filter(sel => sel.value !== '')
      .map(sel => parseInt(sel.value, 10))
  );
}

function renderExtraFilesWidget({ containerId, indices, label, iconClass, removeFn, addFn, uploadFn }) {
  const container = document.getElementById(containerId);
  if (!container || !latestDetectionReport) return;

  const chipsHtml = Array.from(indices).map(idx => {
    const f = latestDetectionReport.inspected_files[idx];
    if (!f) return '';
    return `
      <span class="badge bg-body-secondary text-body border px-2 py-1 d-inline-flex align-items-center gap-1">
        <i class="bi bi-file-earmark-excel ${iconClass}"></i>
        <span>${escapeHtmlStr(f.file_name)} (${Number(f.row_count) || 0} filas)</span>
        <a href="javascript:void(0)" class="text-danger ms-1 text-decoration-none fw-bold" onclick="${removeFn}(${Number(idx)})" title="Quitar">&times;</a>
      </span>
    `;
  }).join('');

  // Only files with rows that no role is using yet can be added as extras.
  const assigned = getAssignedPrimaryIndices();
  const candidates = latestDetectionReport.inspected_files.filter(f =>
    !assigned.has(f.file_index) &&
    !extraErpIndices.has(f.file_index) &&
    !extraTbkIndices.has(f.file_index) &&
    (f.row_count || 0) > 0
  );
  const optionsHtml = candidates.map(f =>
    `<option value="${Number(f.file_index)}">${escapeHtmlStr(f.file_name)} (${Number(f.row_count) || 0} filas)</option>`
  ).join('');

  container.innerHTML = `
    ${chipsHtml ? `<div class="d-flex flex-wrap gap-1 align-items-center mb-2">${chipsHtml}</div>` : ''}
    <div class="d-flex flex-wrap align-items-center gap-2">
      ${candidates.length > 0 ? `
        <select class="form-select form-select-sm w-auto" style="max-width: 260px;" onchange="${addFn}(this)">
          <option value="">+ Agregar ${label} del lote</option>
          ${optionsHtml}
        </select>
      ` : ''}
      <label class="btn btn-outline-secondary btn-sm rounded-pill px-2 py-1 mb-0" style="font-size:0.75rem;">
        <i class="bi bi-file-earmark-plus me-1 ${iconClass}"></i>Subir otro ${label}
        <input type="file" class="d-none" accept=".xls,.xlsx" onchange="${uploadFn}(this)">
      </label>
    </div>
  `;
}

function renderExtraErpWidgets() {
  renderExtraFilesWidget({
    containerId: 'extraErpContainer',
    indices: extraErpIndices,
    label: 'ERP',
    iconClass: 'text-primary',
    removeFn: 'removeExtraErpFile',
    addFn: 'addExtraErpFileFromSelect',
    uploadFn: 'uploadExtraErpFile',
  });
}

function removeExtraErpFile(fileIndex) {
  extraErpIndices.delete(fileIndex);
  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

function addExtraErpFileFromSelect(selectElem) {
  if (!selectElem.value) return;
  const idx = parseInt(selectElem.value, 10);
  extraErpIndices.add(idx);
  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

async function uploadExtraErpFile(inputElem) {
  if (!inputElem.files || inputElem.files.length === 0) return;
  const singleFile = inputElem.files[0];
  inputElem.value = '';

  const formData = new FormData();
  formData.append('bulk_files', singleFile);
  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok || !data.inspected_files || data.inspected_files.length === 0) {
      alert(data.error || 'No se pudo analizar el archivo.');
      return;
    }
    const newFileIndex = bulkUploadedFiles.length;
    bulkUploadedFiles.push(singleFile);
    const inspected = data.inspected_files[0];
    inspected.file_index = newFileIndex;
    latestDetectionReport.inspected_files.push(inspected);

    extraErpIndices.add(newFileIndex);
    renderExtraErpWidgets();
    renderExtraTbkWidgets();
    validateCurrentAlignmentSelection();
  } catch (err) {
    alert('Error al subir el archivo ERP adicional: ' + err);
  }
}

function renderExtraTbkWidgets() {
  renderExtraFilesWidget({
    containerId: 'extraTbkContainer',
    indices: extraTbkIndices,
    label: 'TBK',
    iconClass: 'text-info',
    removeFn: 'removeExtraTbkFile',
    addFn: 'addExtraTbkFileFromSelect',
    uploadFn: 'uploadExtraTbkFile',
  });
}

function removeExtraTbkFile(fileIndex) {
  extraTbkIndices.delete(fileIndex);
  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

function addExtraTbkFileFromSelect(selectElem) {
  if (!selectElem.value) return;
  const idx = parseInt(selectElem.value, 10);
  extraTbkIndices.add(idx);
  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

async function uploadExtraTbkFile(inputElem) {
  if (!inputElem.files || inputElem.files.length === 0) return;
  const singleFile = inputElem.files[0];
  inputElem.value = '';

  const formData = new FormData();
  formData.append('bulk_files', singleFile);
  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok || !data.inspected_files || data.inspected_files.length === 0) {
      alert(data.error || 'No se pudo analizar el archivo.');
      return;
    }
    const newFileIndex = bulkUploadedFiles.length;
    bulkUploadedFiles.push(singleFile);
    const inspected = data.inspected_files[0];
    inspected.file_index = newFileIndex;
    latestDetectionReport.inspected_files.push(inspected);

    extraTbkIndices.add(newFileIndex);
    renderExtraErpWidgets();
    renderExtraTbkWidgets();
    validateCurrentAlignmentSelection();
  } catch (err) {
    alert('Error al subir el archivo Transbank adicional: ' + err);
  }
}


async function uploadSingleFileForRole(roleKey, inputElem) {
  if (!inputElem.files || inputElem.files.length === 0) return;
  const singleFile = inputElem.files[0];
  inputElem.value = '';

  const evidenceCell = document.getElementById('evidence_' + roleKey);
  if (evidenceCell) {
    evidenceCell.innerHTML = '<span class="text-primary"><span class="spinner-border spinner-border-sm me-1"></span>Analizando...</span>';
  }

  const formData = new FormData();
  formData.append('bulk_files', singleFile);
  const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

  try {
    const response = await fetch(getDetectFilesUrl(), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken },
      body: formData
    });
    const data = await response.json();
    if (!response.ok || !data.inspected_files || data.inspected_files.length === 0) {
      alert(data.error || 'No se pudo analizar el archivo.');
      onManualAlignmentChange();
      return;
    }

    const newFileIndex = bulkUploadedFiles.length;
    bulkUploadedFiles.push(singleFile);

    const inspected = data.inspected_files[0];
    inspected.file_index = newFileIndex;
    latestDetectionReport.inspected_files.push(inspected);

    document.querySelectorAll('.role-file-select').forEach(sel => {
      const opt = document.createElement('option');
      opt.value = newFileIndex;
      opt.textContent = `${inspected.file_name} (${inspected.row_count} filas)`;
      sel.appendChild(opt);
    });

    const targetSelect = document.getElementById('select_' + roleKey);
    if (targetSelect) {
      targetSelect.value = String(newFileIndex);
    }

    onManualAlignmentChange();
  } catch (err) {
    alert('Error al subir el archivo individual: ' + err);
    onManualAlignmentChange();
  }
}

function onManualAlignmentChange() {
  if (!latestDetectionReport) return;
  const selects = document.querySelectorAll('.role-file-select');
  selects.forEach(sel => {
    const roleKey = sel.getAttribute('data-role-key');
    const fileIdx = sel.value !== '' ? parseInt(sel.value, 10) : null;
    const evidenceCell = document.getElementById('evidence_' + roleKey);
    const confidenceCell = document.getElementById('confidence_' + roleKey);

    if (fileIdx !== null && latestDetectionReport.inspected_files[fileIdx]) {
      extraErpIndices.delete(fileIdx);
      extraTbkIndices.delete(fileIdx);
      const fileInfo = latestDetectionReport.inspected_files[fileIdx];
      evidenceCell.textContent = fileInfo.evidence || '';
      confidenceCell.innerHTML = getConfidenceBadgeHtml(
        (fileInfo.detected_role === roleKey && fileInfo.confidence === 'HIGH') ? 'HIGH' : 'MANUAL'
      );
    } else {
      evidenceCell.innerHTML = '<span class="text-danger">Sin archivo</span>';
      confidenceCell.innerHTML = getConfidenceBadgeHtml('MISSING');
    }
  });

  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

function validateCurrentAlignmentSelection() {
  const selects = Array.from(document.querySelectorAll('.role-file-select'));
  const chosenIndices = selects.map(s => s.value).filter(v => v !== '');
  const uniqueIndices = new Set(chosenIndices);
  const alertBox = document.getElementById('alignmentSummaryAlert');
  const confirmBtn = document.getElementById('btnConfirmAlignment');
  const required = getRequiredFileCount();

  if (chosenIndices.length === required && uniqueIndices.size === required) {
    alertBox.className = 'alert alert-success rounded-4 mb-3 py-2 px-3 small d-flex align-items-center justify-content-between';
    alertBox.innerHTML = `
      <div><i class="bi bi-check-circle-fill me-2"></i><strong>¡${required} de ${required} roles alineados sin conflictos!</strong></div>
      <span class="badge bg-success rounded-pill">${bulkUploadedFiles.length} archivo(s)</span>
    `;
    confirmBtn.disabled = false;
  } else if (chosenIndices.length === required && uniqueIndices.size < required) {
    alertBox.className = 'alert alert-warning rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Mismo archivo asignado a más de un rol.</strong>`;
    confirmBtn.disabled = true;
  } else {
    const missingCount = required - chosenIndices.length;
    alertBox.className = 'alert alert-danger rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-x-circle-fill me-2"></i><strong>Faltan ${missingCount} archivo(s) por alinear.</strong>`;
    confirmBtn.disabled = true;
  }
}

function resetExperimentalFiles() {
  bulkUploadedFiles = [];
  latestDetectionReport = null;
  extraErpIndices.clear();
  extraTbkIndices.clear();
  document.getElementById('bulkFilesInput').value = '';
  document.getElementById('expPhase2Alignment').classList.add('d-none');
  document.getElementById('expScanningSpinner').classList.add('d-none');
  document.getElementById('expPhase1Dropzone').classList.remove('d-none');
}

function confirmAlignmentAndSubmit() {
  const selects = document.querySelectorAll('.role-file-select');
  const chosenSet = new Set();
  for (const sel of selects) {
    const roleKey = sel.getAttribute('data-role-key');
    const fileIdx = sel.value !== '' ? parseInt(sel.value, 10) : null;
    if (fileIdx === null || !bulkUploadedFiles[fileIdx]) {
      alert(`Por favor asegúrese de que los ${getRequiredFileCount()} roles tengan un archivo asignado.`);
      return;
    }
    chosenSet.add(String(fileIdx));
    const inputId = getRoleInputMap()[roleKey];
    const fileInput = document.getElementById(inputId);
    if (fileInput) {
      const dt = new DataTransfer();
      dt.items.add(bulkUploadedFiles[fileIdx]);
      fileInput.files = dt.files;
    }
  }

  // Populate extra ERP files (USD or CLP)
  const extraErpInput = document.getElementById('input_usd_extra_erp') || document.getElementById('input_clp_extra_erp');
  if (extraErpInput) {
    const extraDt = new DataTransfer();
    extraErpIndices.forEach(idx => {
      if (bulkUploadedFiles[idx] && !chosenSet.has(String(idx))) {
        extraDt.items.add(bulkUploadedFiles[idx]);
      }
    });
    extraErpInput.files = extraDt.files;
  }

  // Populate extra Transbank files (CLP)
  const extraTbkInput = document.getElementById('input_clp_extra_tbk');
  if (extraTbkInput) {
    const extraTbkDt = new DataTransfer();
    extraTbkIndices.forEach(idx => {
      if (bulkUploadedFiles[idx] && !chosenSet.has(String(idx))) {
        extraTbkDt.items.add(bulkUploadedFiles[idx]);
      }
    });
    extraTbkInput.files = extraTbkDt.files;
  }

  showProcessingOverlay();
  getUploadForm().submit();
}
