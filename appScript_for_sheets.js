function doPost(e) {
  // Guard against missing/malformed POST body
  if (!e || !e.postData || !e.postData.contents) {
    return ContentService.createTextOutput(JSON.stringify({ error: "No POST data" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  var data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ error: "Invalid JSON" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  if (!data.uid) {
    return ContentService.createTextOutput(JSON.stringify({ error: "Missing uid" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  var uid = data.uid;
  var ss = SpreadsheetApp.openById('1pz-G0MZtYh_iJyZmRYQyPYFAli_xZ7j6BD9htePOsds');

  // Sheet2 columns (0-indexed):
  // A(0) # Membresia | B(1) Nombre | C(2) UID | D(3) Matricula date |
  // E(4) Matricula Activa | F(5) Mensualidad date | G(6) Mensualidad Activo
  var rosterSheet = ss.getSheetByName("Registro de Clientes");
  var rosterData = rosterSheet.getDataRange().getValues();

  var result = {
    uid: uid,
    name: "DESCONOCIDO",
    matriculaActiva: "No",
    mensualidadActiva: "No"
  };

  for (var i = 1; i < rosterData.length; i++) {
    if (String(rosterData[i][2]).trim().toUpperCase() === uid.toUpperCase()) {
      result.membershipId = rosterData[i][0];
      result.name = rosterData[i][1];
      result.matriculaActiva = rosterData[i][4];
      result.mensualidadActiva = rosterData[i][6];
      break;
    }
  }

  // Log the check-in to this month's sheet - Name | UID | Date and Time.
  // getOrCreateMonthlySheet() finds/creates a tab named after the
  // current month and year (e.g. "September 2026") automatically.
  var now = new Date();
  var logSheet = getOrCreateMonthlySheet(ss, now);
  logSheet.appendRow([result.name, uid, now]);

  return ContentService.createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Returns the sheet tab for the given date's month/year (e.g. "September
 * 2026"), creating it with a header row if it doesn't exist yet. Month
 * names are hardcoded (not TEXT()/locale-based) so the sheet name stays
 * consistent regardless of the spreadsheet's locale setting.
 */
function getOrCreateMonthlySheet(ss, date) {
  var monthNames = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
  ];
  var sheetName = monthNames[date.getMonth()] + " " + date.getFullYear();

  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
    sheet.appendRow(["Nombre", "UID", "Fecha y Hora"]);
    sheet.getRange(1, 1, 1, 3).setFontWeight("bold");
  }
  return sheet;
}