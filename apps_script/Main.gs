/**
 * MLB Phase 1 Apps Script trigger.
 *
 * Config sheet labels in column A, values in column B:
 * PYTHON_SERVICE_URL
 * SERVICE_AUTH_TOKEN (Script Property preferred; sheet fallback is supported)
 * DRIVE_FOLDER_ID
 * REPORT_TIMEZONE
 *
 * Run installDailyCollectorTrigger() once to create the daily trigger.
 */

function readPhaseOneConfig_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('Config');
  if (!sheet) throw new Error('Missing Config sheet.');
  const rows = sheet.getRange(1, 1, sheet.getLastRow(), 2).getDisplayValues();
  const config = {};
  rows.forEach(function(row) {
    const key = String(row[0] || '').trim().toUpperCase();
    if (key) config[key] = String(row[1] || '').trim();
  });
  const propertyToken = PropertiesService.getScriptProperties().getProperty('SERVICE_AUTH_TOKEN');
  if (propertyToken) config.SERVICE_AUTH_TOKEN = propertyToken;
  return config;
}

const MLB_PHASE1_MAX_POLLS_ = 30;
const MLB_PHASE1_MAX_FETCH_ATTEMPTS_ = 3;

function withCollectorLock_(operation) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('Collector operation is already in progress.');
  try {
    return operation();
  } finally {
    lock.releaseLock();
  }
}

function fetchCollector_(url, options, retrySafe) {
  let lastStatus = null;
  const attempts = retrySafe ? MLB_PHASE1_MAX_FETCH_ATTEMPTS_ : 1;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = UrlFetchApp.fetch(url, options);
      lastStatus = response.getResponseCode();
      if (lastStatus !== 429 && lastStatus < 500) return response;
      if (attempt === attempts) return response;
      const headers = response.getAllHeaders();
      const retryAfter = Number(headers['Retry-After'] || headers['retry-after'] || 0);
      Utilities.sleep(retryAfter > 0 ? Math.min(retryAfter * 1000, 10000) : attempt * 1000);
    } catch (error) {
      if (!retrySafe || attempt === attempts) {
        throw new Error('Collector request failed before a safe response was received.');
      }
      Utilities.sleep(attempt * 1000);
    }
  }
  throw new Error('Collector request failed with HTTP ' + String(lastStatus || 'unknown') + '.');
}

function triggerMlbPhaseOneCollector() {
  return withCollectorLock_(triggerMlbPhaseOneCollectorLocked_);
}

function triggerMlbPhaseOneCollectorLocked_() {
  const config = readPhaseOneConfig_();
  ['PYTHON_SERVICE_URL', 'SERVICE_AUTH_TOKEN', 'DRIVE_FOLDER_ID'].forEach(function(key) {
    if (!config[key]) throw new Error('Missing Config value: ' + key);
  });
  if (PropertiesService.getScriptProperties().getProperty('MLB_PHASE1_RUN_ID')) {
    throw new Error('An active collection run is already being polled.');
  }
  const timezone = config.REPORT_TIMEZONE || Session.getScriptTimeZone();
  const requestedDate = Utilities.formatDate(new Date(), timezone, 'yyyy-MM-dd');
  const response = fetchCollector_(config.PYTHON_SERVICE_URL.replace(/\/$/, '') + '/jobs/daily-collection', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + config.SERVICE_AUTH_TOKEN },
    payload: JSON.stringify({ requested_date: requestedDate }),
    muteHttpExceptions: true
  }, false);
  const status = response.getResponseCode();
  const body = response.getContentText();
  if (status < 200 || status >= 300) throw new Error('Collector start failed: HTTP ' + status + '.');
  const job = JSON.parse(body);
  PropertiesService.getScriptProperties().setProperties({
    MLB_PHASE1_RUN_ID: job.run_id,
    MLB_PHASE1_REQUESTED_DATE: requestedDate,
    MLB_PHASE1_POLL_COUNT: '0',
    MLB_PHASE1_STARTED_AT_MS: String(Date.now())
  });
  scheduleStatusPoll_();
  Logger.log('Started collection job ' + job.run_id);
}

function scheduleStatusPoll_() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'pollMlbPhaseOneCollector') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('pollMlbPhaseOneCollector').timeBased().after(2 * 60 * 1000).create();
}

function clearActiveRun_() {
  const props = PropertiesService.getScriptProperties();
  ['MLB_PHASE1_RUN_ID', 'MLB_PHASE1_REQUESTED_DATE', 'MLB_PHASE1_POLL_COUNT', 'MLB_PHASE1_STARTED_AT_MS']
    .forEach(function(key) { props.deleteProperty(key); });
}

function pollMlbPhaseOneCollector() {
  return withCollectorLock_(pollMlbPhaseOneCollectorLocked_);
}

function pollMlbPhaseOneCollectorLocked_() {
  const config = readPhaseOneConfig_();
  const props = PropertiesService.getScriptProperties();
  const runId = props.getProperty('MLB_PHASE1_RUN_ID');
  if (!runId) throw new Error('No active Phase 1 run ID.');
  const pollCount = Number(props.getProperty('MLB_PHASE1_POLL_COUNT') || 0);
  const startedAt = Number(props.getProperty('MLB_PHASE1_STARTED_AT_MS') || 0);
  if (pollCount >= MLB_PHASE1_MAX_POLLS_ || !startedAt || Date.now() - startedAt > 60 * 60 * 1000) {
    clearActiveRun_();
    throw new Error('Collection polling deadline exceeded for run ' + runId + '.');
  }
  props.setProperty('MLB_PHASE1_POLL_COUNT', String(pollCount + 1));
  const base = config.PYTHON_SERVICE_URL.replace(/\/$/, '');
  const options = { headers: { Authorization: 'Bearer ' + config.SERVICE_AUTH_TOKEN }, muteHttpExceptions: true };
  const response = fetchCollector_(base + '/jobs/' + encodeURIComponent(runId), options, true);
  if (response.getResponseCode() !== 200) throw new Error('Status check failed: HTTP ' + response.getResponseCode() + '.');
  const job = JSON.parse(response.getContentText());
  if (job.status === 'queued' || job.status === 'running') {
    scheduleStatusPoll_();
    Logger.log('Job still running: ' + runId);
    return;
  }
  if (job.status !== 'completed' && job.status !== 'completed_with_warnings') {
    clearActiveRun_();
    throw new Error('Collection failed: ' + (job.error_message || job.status));
  }
  const artifact = fetchCollector_(base + '/jobs/' + encodeURIComponent(runId) + '/artifact', options, true);
  if (artifact.getResponseCode() !== 200) throw new Error('Artifact download failed: HTTP ' + artifact.getResponseCode() + '.');
  const folder = DriveApp.getFolderById(config.DRIVE_FOLDER_ID);
  const requestedDate = props.getProperty('MLB_PHASE1_REQUESTED_DATE') || 'unknown-date';
  const filename = 'MLB-Phase1-' + requestedDate + '-' + runId + '.zip';
  if (folder.getFilesByName(filename).hasNext()) {
    clearActiveRun_();
    throw new Error('Drive artifact already exists for run ' + runId + '.');
  }
  folder.createFile(artifact.getBlob().setName(filename));
  clearActiveRun_();
  Logger.log('Saved ' + filename + ' to Drive.');
}

function installDailyCollectorTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'triggerMlbPhaseOneCollector') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('triggerMlbPhaseOneCollector')
    .timeBased()
    .everyDays(1)
    .atHour(7)
    .create();
  Logger.log('Installed daily Phase 1 collector trigger near 7 AM in the script timezone.');
}
