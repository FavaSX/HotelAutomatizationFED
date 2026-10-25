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
  const tbody = document.getElementById('alignmentTableBody');
  tbody.innerHTML = '';
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

  data.aligned_roles.forEach(role => {
    const tr = document.createElement('tr');
    tr.id = `row_${role.role_key}`;

    let optionsHtml = '<option value="">— Sin archivo asignado —</option>';
    data.inspected_files.forEach(f => {
      const selected = (role.file_index === f.file_index) ? 'selected' : '';
      optionsHtml += `<option value="${f.file_index}" ${selected}>${f.file_name} (${f.row_count} filas)</option>`;
    });

    const badgeHtml = getConfidenceBadgeHtml(role.confidence);

    if (role.role_key === 'erp') {
      // ERP Role: Primary file select + Extra files container
      tr.innerHTML = `
        <td class="ps-3 py-3 align-top">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${role.badge_class}">${role.badge}</span>
            <span class="fw-bold text-dark">${role.role_label}</span>
          </div>
          <small class="text-muted d-block mt-1">Soporta turnos y múltiples archivos</small>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2 mb-2">
            <select class="form-select form-select-sm role-file-select" id="select_${role.role_key}" data-role-key="${role.role_key}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir archivo principal ERP">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${role.role_key}', this)">
            </label>
          </div>
          <div id="extraErpContainer" class="p-2 bg-light border rounded-3">
            <!-- Rendered by renderExtraErpWidgets() -->
          </div>
        </td>
        <td class="py-3 text-muted align-top" id="evidence_${role.role_key}">
          <i class="bi bi-cpu me-1 text-primary"></i> ${role.evidence}
        </td>
        <td class="text-center pe-3 py-3 align-top" id="confidence_${role.role_key}">
          ${badgeHtml}
        </td>
      `;
    } else if (role.role_key === 'tbk') {
      // TBK Role: Primary file select + Extra files container (allows consolidated or multiple separate modality files)
      tr.innerHTML = `
        <td class="ps-3 py-3 align-top">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${role.badge_class}">${role.badge}</span>
            <span class="fw-bold text-dark">${role.role_label}</span>
          </div>
          <small class="text-muted d-block mt-1">Soporta 1 consolidado o varios archivos</small>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2 mb-2">
            <select class="form-select form-select-sm role-file-select" id="select_${role.role_key}" data-role-key="${role.role_key}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir archivo principal Transbank">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${role.role_key}', this)">
            </label>
          </div>
          <div id="extraTbkContainer" class="p-2 bg-light border rounded-3">
            <!-- Rendered by renderExtraTbkWidgets() -->
          </div>
        </td>
        <td class="py-3 text-muted align-top" id="evidence_${role.role_key}">
          <i class="bi bi-cpu me-1 text-primary"></i> ${role.evidence}
        </td>
        <td class="text-center pe-3 py-3 align-top" id="confidence_${role.role_key}">
          ${badgeHtml}
        </td>
      `;
    } else {
      tr.innerHTML = `
        <td class="ps-3 py-3">
          <div class="d-flex align-items-center gap-2">
            <span class="badge ${role.badge_class}">${role.badge}</span>
            <span class="fw-bold text-dark">${role.role_label}</span>
          </div>
        </td>
        <td class="py-3">
          <div class="d-flex align-items-center gap-2">
            <select class="form-select form-select-sm role-file-select" id="select_${role.role_key}" data-role-key="${role.role_key}" onchange="onManualAlignmentChange()">
              ${optionsHtml}
            </select>
            <label class="btn btn-outline-primary btn-sm rounded-pill px-3 text-nowrap mb-0" title="Subir un archivo nuevo directamente para este rol">
              <i class="bi bi-upload me-1"></i>Subir
              <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${role.role_key}', this)">
            </label>
          </div>
        </td>
        <td class="py-3 text-muted" id="evidence_${role.role_key}">
          <i class="bi bi-cpu me-1 text-primary"></i> ${role.evidence}
        </td>
        <td class="text-center pe-3 py-3" id="confidence_${role.role_key}">
          ${badgeHtml}
        </td>
      `;
    }
    tbody.appendChild(tr);
  });

  renderExtraErpWidgets();
  renderExtraTbkWidgets();
  document.getElementById('expPhase2Alignment').classList.remove('d-none');
  validateCurrentAlignmentSelection();
}

function renderExtraErpWidgets() {
  const container = document.getElementById('extraErpContainer');
  if (!container || !latestDetectionReport) return;

  const primaryErpSelect = document.getElementById('select_erp');
  const primaryIdx = primaryErpSelect && primaryErpSelect.value !== '' ? parseInt(primaryErpSelect.value, 10) : null;

  let chipsHtml = '';
  let extraRowsTotal = 0;
  extraErpIndices.forEach(idx => {
    const f = latestDetectionReport.inspected_files[idx];
    if (f) {
      extraRowsTotal += (f.row_count || 0);
      chipsHtml += `
        <span class="badge bg-white text-dark border px-2 py-1 d-inline-flex align-items-center gap-1 shadow-sm">
          <i class="bi bi-file-earmark-excel text-success"></i>
          <span>${f.file_name} (${f.row_count} filas)</span>
          <a href="javascript:void(0)" class="text-danger ms-1 text-decoration-none fw-bold" onclick="removeExtraErpFile(${idx})" title="Quitar">&times;</a>
        </span>
      `;
    }
  });

  // Available candidate files to add as extra ERP (must have > 0 rows and not be primary or already added)
  const availableCandidates = latestDetectionReport.inspected_files.filter(f => 
    f.file_index !== primaryIdx && !extraErpIndices.has(f.file_index) && (f.row_count || 0) > 0
  );

  let addSelectOptions = '<option value="">+ Agregar archivo ERP del lote...</option>';
  availableCandidates.forEach(f => {
    addSelectOptions += `<option value="${f.file_index}">${f.file_name} (${f.row_count} filas)</option>`;
  });

  container.innerHTML = `
    <div class="d-flex align-items-center justify-content-between mb-1">
      <small class="fw-bold text-secondary">
        <i class="bi bi-layers text-primary me-1"></i>Archivos ERP Adicionales (${extraErpIndices.size})
      </small>
      ${extraRowsTotal > 0 ? `<small class="text-success fw-semibold">+ ${extraRowsTotal} filas adicionales</small>` : ''}
    </div>
    <div class="d-flex flex-wrap gap-1 align-items-center mb-2">
      ${chipsHtml || '<small class="text-muted fst-italic">Ningún archivo ERP adicional asignado</small>'}
    </div>
    <div class="d-flex flex-wrap align-items-center gap-2">
      ${availableCandidates.length > 0 ? `
        <select class="form-select form-select-sm w-auto" style="max-width: 260px;" onchange="addExtraErpFileFromSelect(this)">
          ${addSelectOptions}
        </select>
      ` : ''}
      <label class="btn btn-outline-secondary btn-sm rounded-pill px-2 py-1 mb-0" style="font-size:0.75rem;" title="Subir otro archivo ERP (turno o histórico)">
        <i class="bi bi-file-earmark-plus me-1 text-primary"></i>Subir otro ERP
        <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadExtraErpFile(this)">
      </label>
    </div>
  `;
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
    validateCurrentAlignmentSelection();
  } catch (err) {
    alert('Error al subir el archivo ERP adicional: ' + err);
  }
}

function renderExtraTbkWidgets() {
  const container = document.getElementById('extraTbkContainer');
  if (!container || !latestDetectionReport) return;

  const primaryTbkSelect = document.getElementById('select_tbk');
  const primaryIdx = primaryTbkSelect && primaryTbkSelect.value !== '' ? parseInt(primaryTbkSelect.value, 10) : null;

  let chipsHtml = '';
  let extraRowsTotal = 0;
  extraTbkIndices.forEach(idx => {
    const f = latestDetectionReport.inspected_files[idx];
    if (f) {
      extraRowsTotal += (f.row_count || 0);
      chipsHtml += `
        <span class="badge bg-white text-dark border px-2 py-1 d-inline-flex align-items-center gap-1 shadow-sm">
          <i class="bi bi-file-earmark-excel text-info"></i>
          <span>${f.file_name} (${f.row_count} filas)</span>
          <a href="javascript:void(0)" class="text-danger ms-1 text-decoration-none fw-bold" onclick="removeExtraTbkFile(${idx})" title="Quitar">&times;</a>
        </span>
      `;
    }
  });

  const availableCandidates = latestDetectionReport.inspected_files.filter(f => 
    f.file_index !== primaryIdx && !extraTbkIndices.has(f.file_index) && !extraErpIndices.has(f.file_index) && (f.row_count || 0) > 0
  );

  let addSelectOptions = '<option value="">+ Agregar archivo Transbank del lote...</option>';
  availableCandidates.forEach(f => {
    addSelectOptions += `<option value="${f.file_index}">${f.file_name} (${f.row_count} filas)</option>`;
  });

  container.innerHTML = `
    <div class="d-flex align-items-center justify-content-between mb-1">
      <small class="fw-bold text-secondary">
        <i class="bi bi-collection text-info me-1"></i>Archivos Transbank Adicionales (${extraTbkIndices.size})
      </small>
      ${extraRowsTotal > 0 ? `<small class="text-info fw-semibold">+ ${extraRowsTotal} filas adicionales</small>` : ''}
    </div>
    <div class="d-flex flex-wrap gap-1 align-items-center mb-2">
      ${chipsHtml || '<small class="text-muted fst-italic">Ningún archivo Transbank adicional asignado</small>'}
    </div>
    <div class="d-flex flex-wrap align-items-center gap-2">
      ${availableCandidates.length > 0 ? `
        <select class="form-select form-select-sm w-auto" style="max-width: 260px;" onchange="addExtraTbkFileFromSelect(this)">
          ${addSelectOptions}
        </select>
      ` : ''}
      <label class="btn btn-outline-secondary btn-sm rounded-pill px-2 py-1 mb-0" style="font-size:0.75rem;" title="Subir otro archivo Transbank (Crédito, Débito o Prepago)">
        <i class="bi bi-file-earmark-plus me-1 text-info"></i>Subir otro TBK
        <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadExtraTbkFile(this)">
      </label>
    </div>
  `;
}

function removeExtraTbkFile(fileIndex) {
  extraTbkIndices.delete(fileIndex);
  renderExtraTbkWidgets();
  validateCurrentAlignmentSelection();
}

function addExtraTbkFileFromSelect(selectElem) {
  if (!selectElem.value) return;
  const idx = parseInt(selectElem.value, 10);
  extraTbkIndices.add(idx);
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
    evidenceCell.innerHTML = '<span class="text-primary"><span class="spinner-border spinner-border-sm me-1"></span>Analizando archivo subido...</span>';
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
      evidenceCell.innerHTML = `<i class="bi bi-cpu me-1 text-primary"></i> ${fileInfo.evidence}`;
      confidenceCell.innerHTML = getConfidenceBadgeHtml(
        (fileInfo.detected_role === roleKey && fileInfo.confidence === 'HIGH') ? 'HIGH' : 'MANUAL'
      );
    } else {
      evidenceCell.innerHTML = '<span class="text-danger">Seleccione un archivo para este rol.</span>';
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
    const extraCount = extraErpIndices.size;
    let extraNotes = [];
    if (extraErpIndices.size > 0) {
      extraNotes.push(`<div class="mt-1 text-dark"><i class="bi bi-layers-half text-primary me-1"></i><strong>Archivos ERP adicionales:</strong> ${Array.from(extraErpIndices).map(idx => latestDetectionReport.inspected_files[idx]?.file_name).filter(Boolean).join(', ')}</div>`);
    }
    if (extraTbkIndices.size > 0) {
      extraNotes.push(`<div class="mt-1 text-dark"><i class="bi bi-collection text-info me-1"></i><strong>Archivos Transbank adicionales (Crédito/Débito/Prepago):</strong> ${Array.from(extraTbkIndices).map(idx => latestDetectionReport.inspected_files[idx]?.file_name).filter(Boolean).join(', ')}</div>`);
    }
    const extraNote = extraNotes.join('');
    alertBox.className = 'alert alert-success rounded-4 mb-3 py-2 px-3 small d-flex align-items-center justify-content-between';
    alertBox.innerHTML = `
      <div>
        <div><i class="bi bi-check-circle-fill me-2"></i><strong>¡${required} de ${required} roles alineados sin conflictos!</strong> Revise la tabla y confirme para iniciar el cruce financiero.</div>
        ${extraNote}
      </div>
      <span class="badge bg-success rounded-pill">${bulkUploadedFiles.length} archivo(s) cargado(s)</span>
    `;
    confirmBtn.disabled = false;
  } else if (chosenIndices.length === required && uniqueIndices.size < required) {
    alertBox.className = 'alert alert-warning rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Atención:</strong> Ha asignado el mismo archivo en más de un rol principal. Por favor asigne archivos distintos para cada rol.`;
    confirmBtn.disabled = true;
  } else {
    const missingCount = required - chosenIndices.length;
    alertBox.className = 'alert alert-danger rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-x-circle-fill me-2"></i><strong>Faltan ${missingCount} archivo(s) por alinear</strong> (se cargaron ${bulkUploadedFiles.length} archivo(s)). Seleccione los faltantes o vuelva a cargar el lote.`;
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
