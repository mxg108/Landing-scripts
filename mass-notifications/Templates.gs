/**
 * Mass Notifications — Templates
 *
 * Pre-built email configurations for frequently-sent notification types.
 * Selecting a template writes its values to the Config sheet, pre-filling
 * every field except the run-specific ones listed in TEMPLATE_BLANK_KEYS.
 *
 * ADDING A NEW TEMPLATE:
 *   1. Add a new entry to EMAIL_TEMPLATES below.
 *   2. Keys must match known Config sheet keys (see KNOWN_CONFIG_KEYS in Config.gs).
 *   3. Leave run-specific fields (property_name, manager_email, …) out of the
 *      template object — they will be cleared so the operator fills them in.
 *
 * HOW IT DIFFERS FROM TOKENIZATION:
 *   • Tokenization (Tokenizer.gs) = runtime substitution of {{placeholders}}
 *     inside a string that is already stored in the Config sheet.
 *   • Templating (this file)      = pre-populating the Config sheet itself
 *     with a known-good starting configuration for a common scenario.
 */

// ── Fields cleared on every template load (operator must fill these in) ───────
const TEMPLATE_BLANK_KEYS = [
  'property_name',
  'manager_email',
  'manager_name',
  'window_start',
  'window_end',
  'reply_to',
  'cc_extra',
  'bcc_extra',
  'test_email',
  'attachment_file_ids',
  'body_full_html',
];

// ── Template registry ─────────────────────────────────────────────────────────

const EMAIL_TEMPLATES = {

  'Annual Fire Inspection': {
    event_name:        'Annual Fire Inspection',
    subject_template:  'Important Notice: Annual Fire Inspection — {{property_name}} ({{date_range}})',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'FIRE_INSPECTION',
    body_intro_html:   '<p>Our property will undergo its <strong>annual fire inspection</strong> during the window shown above. Inspectors will require access to all units — please ensure your unit is accessible and your smoke/CO detectors are unobstructed.</p>',
    include_unit_line: 'YES',
    closing_html:      '<p>If you have any questions, please contact your General Manager {{manager_name}} or our 24/7 Member Support Line at <a href="tel:+12058528798">(205) 852-8798</a>.</p><p>Warm regards,<br>The Landing Team</p>',
    send_mode:         'INDIVIDUAL',
  },

  'Water Outage': {
    event_name:        'Planned Water Outage',
    subject_template:  'Water Outage Notice — {{property_name}} ({{date_range}})',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'WATER_OUTAGE',
    body_intro_html:   '<p>We want to give you advance notice of a <strong>planned water outage</strong> at {{property_name}}. Our maintenance team will be working to complete this as quickly as possible and minimize disruption.</p><p>We recommend storing some water beforehand for your convenience.</p>',
    include_unit_line: 'NO',
    closing_html:      '<p>We apologize for any inconvenience. For questions, contact your General Manager {{manager_name}} or Member Support at <a href="tel:+12058528798">(205) 852-8798</a>.</p><p>Warm regards,<br>The Landing Team</p>',
    send_mode:         'BCC',
  },

  'General Maintenance': {
    event_name:        'Scheduled Property Maintenance',
    subject_template:  'Maintenance Notice — {{property_name}} ({{date_range}})',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'MAINTENANCE',
    body_intro_html:   '<p>We will be conducting <strong>scheduled maintenance</strong> at {{property_name}} during the window shown above. Some common areas may be temporarily unavailable, and maintenance staff may be present on the property.</p>',
    include_unit_line: 'NO',
    closing_html:      '<p>Thank you for your patience. For questions, reach your General Manager {{manager_name}} or call Member Support at <a href="tel:+12058528798">(205) 852-8798</a>.</p><p>Warm regards,<br>The Landing Team</p>',
    send_mode:         'BCC',
  },

  'Weather Alert': {
    event_name:        'Weather Advisory',
    subject_template:  'Weather Advisory — {{property_name}} ({{date_range}})',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'WEATHER_ALERT',
    body_intro_html:   '<p>A <strong>weather advisory</strong> has been issued for the area surrounding {{property_name}}. Please take appropriate precautions for your safety, secure any outdoor belongings, and follow guidance from local authorities.</p>',
    include_unit_line: 'NO',
    closing_html:      '<p>Your safety is our priority. For urgent property concerns, contact your General Manager {{manager_name}} or our 24/7 Member Support at <a href="tel:+12058528798">(205) 852-8798</a>.</p><p>Stay safe,<br>The Landing Team</p>',
    send_mode:         'BCC',
  },

};

// ── Template loader ───────────────────────────────────────────────────────────

/**
 * Writes the selected template's config values to the Config sheet,
 * then clears all run-specific fields so the operator fills them in.
 *
 * Called from the "Mass Notify → Load Template → …" submenu.
 *
 * @param {string} templateName — must match a key in EMAIL_TEMPLATES
 */
function loadTemplate_(templateName) {
  const template = EMAIL_TEMPLATES[templateName];
  if (!template) {
    safeAlert_(`Template "${templateName}" not found.`);
    return;
  }

  // Apply template-defined values
  Object.entries(template).forEach(([key, value]) => setConfigValue_(key, value));

  // Clear run-specific fields that the operator must fill in
  TEMPLATE_BLANK_KEYS.forEach(key => {
    if (!(key in template)) setConfigValue_(key, '');
  });

  safeAlert_(
    `Template "${templateName}" loaded.\n\n` +
    'Please fill in:\n  • property_name\n  • manager_email\n  • window_start / window_end\n\n' +
    'Then run Dry Run → Preview to verify before sending.'
  );
}
