/**
 * "QA ready" emails for the tracker's QA Sign-up tab (spec v1, plan §3.5).
 * Replaces MakeSignUpSheet.gs for projects built from the new schema.
 *
 * Install: open the tracker > Extensions > Apps Script, paste this file, run installTrigger()
 * once as the operator who owns the sheet (that account must be an editor of the protected
 * ranges, since the script writes "Notified iter"). It is TIME-DRIVEN on purpose: the
 * controller writes Stage through the Sheets API, and edits made through the API do not fire
 * onEdit triggers.
 *
 * Rule: email a row when Stage = AWAITING_REVIEW, Email is set and valid, and Iteration is
 * greater than Notified iter. So a reviewer who signed up early gets exactly one email per
 * iteration, and nothing is sent twice if the script runs again.
 * Columns are found by header name, never by position.
 */
var TAB = 'QA Sign-up';
var MAX_PER_RUN = 50;  // stays well inside the MailApp daily quota

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'sendReviewNotifications') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendReviewNotifications').timeBased().everyMinutes(10).create();
}

function sendReviewNotifications() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;  // a previous run is still going
  try {
    var sheet = SpreadsheetApp.getActive().getSheetByName(TAB);
    var values = sheet.getDataRange().getValues();
    var col = {};
    values[0].forEach(function (h, i) { col[String(h).trim()] = i; });
    ['Stage', 'Iteration', 'Email', 'Email Subject', 'Email Body', 'Notified iter'].forEach(function (h) {
      if (!(h in col)) throw new Error('QA Sign-up tab has no "' + h + '" column');
    });
    var sent = 0;
    for (var r = 1; r < values.length && sent < MAX_PER_RUN; r++) {
      var row = values[r];
      var iter = Number(row[col['Iteration']]) || 0;
      var notified = Number(row[col['Notified iter']]) || 0;
      var email = String(row[col['Email']]).trim();
      if (row[col['Stage']] !== 'AWAITING_REVIEW' || iter <= notified) continue;
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) continue;  // dashboard shows sign-ups without a valid email
      var subject = String(row[col['Email Subject']]);
      var body = String(row[col['Email Body']]);
      if (!subject) continue;
      MailApp.sendEmail(email, subject, body);
      sheet.getRange(r + 1, col['Notified iter'] + 1).setValue(iter);  // record before the next send
      sent++;
    }
  } finally {
    lock.releaseLock();
  }
}
