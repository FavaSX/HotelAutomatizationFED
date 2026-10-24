/**
 * Multi-File Upload & Auto-Detection Logic shared by usd_upload.html and clp_upload.html
 * Handles bulk drag-and-drop, structural fingerprint inspection,
 * multi-file assignment per role (chips with add/remove actions),
 * manual single-file uploads, and form submission via DataTransfer.
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
let roleAssignedFiles = {}; // role_key -> [file_index_1, file_index_2, ...]

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function getUploadForm() {
  return document.querySelector('form[data-detect-url]');
}

function getRoleInputMap() {
  const form = getUploadForm();
  const declared = form ? form.getAttribute('data-role-inputs') : null;
  return declared ? JSON.parse(declared) : USD_ROLE_INPUT_MAP;
}

function getRequiredRoles() {
  return Object.keys(getRoleInputMap());
}

function getRequiredFileCount() {
  return getRequiredRoles().length;
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
  roleAssignedFiles = {};

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

    // Initialize multi-file assignments from detection report
    data.aligned_roles.forEach(role => {
      if (Array.isArray(role.file_indices) && role.file_indices.length > 0) {
        roleAssignedFiles[role.role_key] = [...role.file_indices];
      } else if (role.file_index !== null && role.file_index !== undefined) {
        roleAssignedFiles[role.role_key] = [role.file_index];
      } else {
        roleAssignedFiles[role.role_key] = [];
      }
    });

    renderAlignmentTable();
  } catch (err) {
    alert('Error de conexión al inspeccionar los archivos: ' + err);
    resetExperimentalFiles();
  } finally {
    document.getElementById('expScanningSpinner').classList.add('d-none');
  }
}

function getConfidenceBadgeHtml(type, count) {
  const countText = count > 1 ? ` (${count} archivos)` : '';
  switch (type) {
    case 'HIGH':
      return `<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2 py-1"><i class="bi bi-check-circle-fill me-1"></i>100% Seguro${countText}</span>`;
    case 'MEDIUM':
      return `<span class="badge bg-warning-subtle text-dark border border-warning-subtle rounded-pill px-2 py-1"><i class="bi bi-info-circle-fill me-1"></i>Por Nombre${countText}</span>`;
    case 'MANUAL':
      return `<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill px-2 py-1"><i class="bi bi-hand-index-thumb me-1"></i>Asignado Manual${countText}</span>`;
    default:
      return '<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Faltante</span>';
  }
}

function renderAlignmentTable() {
  if (!latestDetectionReport) return;
  const tbody = document.getElementById('alignmentTableBody');
  tbody.innerHTML = '';

  const inspectedMap = {};
  latestDetectionReport.inspected_files.forEach(f => {
    inspectedMap[f.file_index] = f;
  });

  latestDetectionReport.aligned_roles.forEach(role => {
    const roleKey = role.role_key;
    const assignedIndices = roleAssignedFiles[roleKey] || [];
    const assignedFiles = assignedIndices.map(idx => inspectedMap[idx]).filter(Boolean);
    const totalRows = assignedFiles.reduce((sum, f) => sum + (f.row_count || 0), 0);

    const tr = document.createElement('tr');
    tr.id = `row_${roleKey}`;

    // Render chips for assigned files
    let chipsHtml = '';
    if (assignedFiles.length === 0) {
      chipsHtml = `<div class="mb-1"><span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill px-2 py-1"><i class="bi bi-exclamation-triangle-fill me-1"></i>Sin archivo asignado</span></div>`;
    } else {
      chipsHtml = `<div class="d-flex flex-wrap gap-1 align-items-center mb-1">`;
      assignedFiles.forEach(f => {
        chipsHtml += `
          <span class="badge bg-light text-dark border d-inline-flex align-items-center gap-1 py-1 px-2">
            <i class="bi bi-file-earmark-spreadsheet text-success"></i>
            <span class="text-truncate" style="max-width: 220px;" title="${escapeHtml(f.file_name)}">${escapeHtml(f.file_name)}</span>
            <span class="text-muted small">(${f.row_count} filas)</span>
            <button type="button" class="btn-close ms-1" style="font-size:0.55rem;" onclick="removeFileFromRole('${roleKey}', ${f.file_index})" title="Quitar este archivo" aria-label="Quitar"></button>
          </span>
        `;
      });
      chipsHtml += `</div>`;
      if (assignedFiles.length > 1) {
        chipsHtml += `<div class="small text-primary fw-semibold mb-1"><i class="bi bi-layers-fill me-1"></i>${assignedFiles.length} archivos agrupados (${totalRows} filas totales en conjunto)</div>`;
      }
    }

    // Dropdown to add another file from pool + direct upload button
    let addOptionsHtml = '<option value="">+ Asignar archivo del lote...</option>';
    latestDetectionReport.inspected_files.forEach(f => {
      const isAssignedToThis = assignedIndices.includes(f.file_index);
      addOptionsHtml += `<option value="${f.file_index}" ${isAssignedToThis ? 'disabled' : ''}>${escapeHtml(f.file_name)} (${f.row_count} filas)${isAssignedToThis ? ' (ya asignado)' : ''}</option>`;
    });

    const actionsHtml = `
      <div class="d-flex align-items-center gap-2 mt-1">
        <select class="form-select form-select-sm role-file-add-select" style="max-width: 250px;" onchange="onAddFileSelect('${roleKey}', this)">
          ${addOptionsHtml}
        </select>
        <label class="btn btn-outline-primary btn-sm rounded-pill px-2 py-1 text-nowrap mb-0" style="font-size:0.75rem;" title="Subir otro archivo para este rol">
          <i class="bi bi-upload me-1"></i>Subir
          <input type="file" class="d-none" accept=".xls,.xlsx" onchange="uploadSingleFileForRole('${roleKey}', this)">
        </label>
      </div>
    `;

    // Calculate evidence and confidence
    let evidenceText = '';
    let confidenceType = 'MISSING';
    if (assignedFiles.length === 0) {
      evidenceText = '<span class="text-danger">Seleccione o suba un archivo para este rol.</span>';
      confidenceType = 'MISSING';
    } else if (assignedFiles.length === 1) {
      const single = assignedFiles[0];
      evidenceText = `<i class="bi bi-cpu me-1 text-primary"></i> ${escapeHtml(single.evidence)}`;
      confidenceType = (single.detected_role === roleKey && single.confidence === 'HIGH') ? 'HIGH' : 'MANUAL';
    } else {
      const names = assignedFiles.map(f => f.file_name).join(', ');
      evidenceText = `<i class="bi bi-layers-fill me-1 text-primary"></i> ${assignedFiles.length} archivos concatenados (${totalRows} filas): ${escapeHtml(names)}`;
      const allHigh = assignedFiles.every(f => f.detected_role === roleKey && f.confidence === 'HIGH');
      confidenceType = allHigh ? 'HIGH' : 'MANUAL';
    }

    const badgeHtml = getConfidenceBadgeHtml(confidenceType, assignedFiles.length);

    tr.innerHTML = `
      <td class="ps-3 py-3" style="width: 25%;">
        <div class="d-flex align-items-center gap-2">
          <span class="badge ${role.badge_class}">${role.badge}</span>
          <span class="fw-bold text-dark">${role.role_label}</span>
        </div>
      </td>
      <td class="py-3" style="width: 38%;">
        ${chipsHtml}
        ${actionsHtml}
      </td>
      <td class="py-3 text-muted" id="evidence_${roleKey}" style="width: 25%;">
        ${evidenceText}
      </td>
      <td class="text-center pe-3 py-3" id="confidence_${roleKey}" style="width: 12%;">
        ${badgeHtml}
      </td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById('expPhase2Alignment').classList.remove('d-none');
  validateCurrentAlignmentSelection();
}

function onAddFileSelect(roleKey, selectElem) {
  const fileIdxStr = selectElem.value;
  if (!fileIdxStr) return;
  const fileIdx = parseInt(fileIdxStr, 10);
  selectElem.value = '';

  if (!roleAssignedFiles[roleKey]) {
    roleAssignedFiles[roleKey] = [];
  }
  if (!roleAssignedFiles[roleKey].includes(fileIdx)) {
    roleAssignedFiles[roleKey].push(fileIdx);
  }
  renderAlignmentTable();
}

function removeFileFromRole(roleKey, fileIdx) {
  if (roleAssignedFiles[roleKey]) {
    roleAssignedFiles[roleKey] = roleAssignedFiles[roleKey].filter(i => i !== fileIdx);
  }
  renderAlignmentTable();
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
      renderAlignmentTable();
      return;
    }

    const newFileIndex = bulkUploadedFiles.length;
    bulkUploadedFiles.push(singleFile);

    const inspected = data.inspected_files[0];
    inspected.file_index = newFileIndex;
    latestDetectionReport.inspected_files.push(inspected);

    if (!roleAssignedFiles[roleKey]) {
      roleAssignedFiles[roleKey] = [];
    }
    roleAssignedFiles[roleKey].push(newFileIndex);

    renderAlignmentTable();
  } catch (err) {
    alert('Error al subir el archivo individual: ' + err);
    renderAlignmentTable();
  }
}

function getExtraErpFiles(chosenSet) {
  if (!latestDetectionReport || !document.getElementById('input_clp_extra_erp')) return [];
  return (latestDetectionReport.inspected_files || []).filter(
    f => !chosenSet.has(f.file_index) && f.detected_role === 'erp'
  );
}

function validateCurrentAlignmentSelection() {
  const alertBox = document.getElementById('alignmentSummaryAlert');
  const confirmBtn = document.getElementById('btnConfirmAlignment');
  const requiredRoles = getRequiredRoles();

  // Find missing roles
  const missingRoles = requiredRoles.filter(
    rk => !roleAssignedFiles[rk] || roleAssignedFiles[rk].length === 0
  );

  // Check for cross-role duplicate assignments
  const fileToRoles = {};
  requiredRoles.forEach(rk => {
    (roleAssignedFiles[rk] || []).forEach(idx => {
      fileToRoles[idx] = (fileToRoles[idx] || []).concat(rk);
    });
  });

  const conflicts = Object.entries(fileToRoles).filter(([, roles]) => roles.length > 1);

  const totalAssignedFiles = requiredRoles.reduce(
    (sum, rk) => sum + (roleAssignedFiles[rk] || []).length, 0
  );

  if (conflicts.length > 0) {
    const conflictFile = latestDetectionReport.inspected_files.find(
      f => f.file_index === parseInt(conflicts[0][0], 10)
    );
    const fileName = conflictFile ? conflictFile.file_name : `Archivo #${conflicts[0][0]}`;
    alertBox.className = 'alert alert-warning rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-2"></i><strong>Conflicto de asignación:</strong> El archivo <strong>${escapeHtml(fileName)}</strong> está asignado a más de un rol diferente (${conflicts[0][1].join(', ')}). Por favor quite la asignación duplicada.`;
    confirmBtn.disabled = true;
  } else if (missingRoles.length > 0) {
    alertBox.className = 'alert alert-danger rounded-4 mb-3 py-2 px-3 small';
    alertBox.innerHTML = `<i class="bi bi-x-circle-fill me-2"></i><strong>Faltan ${missingRoles.length} rol(es) por asignar archivo:</strong> ${missingRoles.join(', ')}. Seleccione o suba los archivos faltantes.`;
    confirmBtn.disabled = true;
  } else {
    const chosenSet = new Set(Object.values(roleAssignedFiles).flat());
    const extraErp = getExtraErpFiles(chosenSet);
    const extraNote = extraErp.length > 0
      ? `<div class="mt-1 text-dark"><i class="bi bi-journal-plus text-primary me-1"></i><strong>Borderó histórico adicional incluido automáticamente para cruce fuera de período:</strong> ${extraErp.map(f => escapeHtml(f.file_name)).join(', ')}</div>`
      : '';
    alertBox.className = 'alert alert-success rounded-4 mb-3 py-2 px-3 small d-flex align-items-center justify-content-between';
    alertBox.innerHTML = `
      <div>
        <div><i class="bi bi-check-circle-fill me-2"></i><strong>¡Todos los ${requiredRoles.length} roles están listos sin conflictos!</strong> (${totalAssignedFiles} archivo(s) en total). Confirme para iniciar el cruce financiero.</div>
        ${extraNote}
      </div>
      <span class="badge bg-success rounded-pill">${bulkUploadedFiles.length} archivo(s) en lote</span>
    `;
    confirmBtn.disabled = false;
  }
}

function resetExperimentalFiles() {
  bulkUploadedFiles = [];
  latestDetectionReport = null;
  roleAssignedFiles = {};
  const fileInput = document.getElementById('bulkFilesInput');
  if (fileInput) fileInput.value = '';
  document.getElementById('expPhase2Alignment').classList.add('d-none');
  document.getElementById('expScanningSpinner').classList.add('d-none');
  document.getElementById('expPhase1Dropzone').classList.remove('d-none');
}

function confirmAlignmentAndSubmit() {
  const roleMap = getRoleInputMap();
  const chosenSet = new Set();

  for (const roleKey of Object.keys(roleMap)) {
    const assigned = roleAssignedFiles[roleKey] || [];
    if (assigned.length === 0) {
      alert(`Por favor asegúrese de que todos los roles requeridos tengan al menos un archivo asignado.`);
      return;
    }

    const inputId = roleMap[roleKey];
    const fileInput = document.getElementById(inputId);
    if (!fileInput) continue;

    const dt = new DataTransfer();
    assigned.forEach(idx => {
      chosenSet.add(idx);
      if (bulkUploadedFiles[idx]) {
        dt.items.add(bulkUploadedFiles[idx]);
      }
    });
    fileInput.files = dt.files;
  }

  // Handle extra ERP files for CLP if element exists
  const extraErpInput = document.getElementById('input_clp_extra_erp');
  if (extraErpInput) {
    const extraDt = new DataTransfer();
    getExtraErpFiles(chosenSet).forEach(f => {
      if (bulkUploadedFiles[f.file_index]) {
        extraDt.items.add(bulkUploadedFiles[f.file_index]);
      }
    });
    extraErpInput.files = extraDt.files;
  }

  showProcessingOverlay();
  getUploadForm().submit();
}
