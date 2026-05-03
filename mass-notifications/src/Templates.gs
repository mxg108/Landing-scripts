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
  // recipients_sheet_name resets to the default (Mass_Notification) on every
  // template load unless the template itself sets it (e.g. Move-In Notification
  // sets it to Move_In_Flow). This keeps modes from leaking between templates.
  'recipients_sheet_name',
  // Branded wrapper config — clear on every template load so the cream Move-In
  // wrapper doesn't leak into resident-mode notifications. Move-In sets these
  // explicitly so they survive the blanking pass.
  'email_background_color',
  'email_header_image_url',
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
    closing_html:      '<p>If you have any questions, please contact your General Manager {{manager_name}} or our 24/7 Member Support Line at <a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a>.</p><p>Warm regards,<br>The Landing Team</p>',
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
    closing_html:      '<p>We apologize for any inconvenience. For questions, contact your General Manager {{manager_name}} or Member Support at <a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a>.</p><p>Warm regards,<br>The Landing Team</p>',
    send_mode:         'INDIVIDUAL',
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
    closing_html:      '<p>Thank you for your patience. For questions, reach your General Manager {{manager_name}} or call Member Support at <a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a>.</p><p>Warm regards,<br>The Landing Team</p>',
    send_mode:         'INDIVIDUAL',
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
    closing_html:      '<p>Your safety is our priority. For urgent property concerns, contact your General Manager {{manager_name}} or our 24/7 Member Support at <a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a>.</p><p>Stay safe,<br>The Landing Team</p>',
    send_mode:         'INDIVIDUAL',
  },

  'Power Outage': {
    event_name:        'Power Outage',
    // Uses {{today}} instead of {{date_range}} — this is a live incident, not a scheduled window.
    subject_template:  'Urgent: Power Outage at {{property_name}} — {{today}}',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is an urgent notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'POWER_OUTAGE',
    body_intro_html:
      '<p>We are reaching out because <strong>{{property_name}}</strong> is currently ' +
      'experiencing an <strong>unexpected power outage</strong> affecting your unit. ' +
      'We understand how disruptive this is — Landing is actively engaged with the ' +
      'property management team and working toward the fastest possible resolution.</p>' +
      '<p>While service is being restored, please take the following precautions:</p>' +
      '<ul>' +
      '<li><strong>Unplug sensitive electronics</strong> (laptops, TVs, gaming consoles) ' +
      'to protect them from potential power surges when electricity is restored.</li>' +
      '<li><strong>Keep your refrigerator and freezer doors closed</strong> — a closed ' +
      'refrigerator will maintain safe temperatures for approximately 4 hours.</li>' +
      '<li>Use <strong>flashlights instead of candles</strong> for your safety.</li>' +
      '<li>If you rely on <strong>powered medical equipment</strong>, please contact us ' +
      'immediately so we can assist you.</li>' +
      '</ul>',
    include_unit_line: 'NO',
    closing_html:
      '<p>For the latest updates or if you need immediate assistance, please reach out to ' +
      'your General Manager <strong>{{manager_name}}</strong> directly, or contact our ' +
      '<strong>24/7 Member Support</strong> team at ' +
      '<a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a> — we are standing by to help.</p>' +
      '<p>We sincerely apologize for the inconvenience and thank you for your patience.<br>' +
      'The Landing Team</p>',
    send_mode: 'INDIVIDUAL',
    _hint:
      'Power Outage template loaded.\n\n' +
      'Please fill in:\n  • property_name\n  • manager_email\n\n' +
      'Dates are optional for live outages — {{today}} in the subject will\n' +
      'automatically reflect the send date.\n\n' +
      'Run Dry Run → Preview to verify before sending.',
  },

  'Move-In Notification': {
    event_name:           'New Landing Member Approved',
    recipients_sheet_name:'Move_In_Flow',
    send_mode:            'MOVE_IN',
    subject_template:     'New Landing Member Approved — {{member_name}} (Apt {{apartment_number}})',
    greeting_template:
      '<p>Hello {{property_name}},</p>' +
      '<p>A new Landing member has been approved!</p>' +
      '<p>Please see information below:</p>',
    include_disclaimer:   'NO',
    disclaimer_html:      '',
    notification_card:    'MOVE_IN',
    body_intro_html:      '',
    include_unit_line:    'NO',
    // The MoveInCard already includes the area-manager sign-off. We need a
    // truthy-but-empty value here so loadConfig_'s `||` fallback doesn't
    // inject the generic "Thank you for your cooperation…" default closing.
    closing_html:         '<!--noop-->',
    // Brand wrapper — cream background + LANDING wordmark mirror the
    // official Move-In template. Operators can swap the URL in the Config
    // sheet's `email_header_image_url` row if Landing rehosts the asset.
    email_background_color: '#F5F1E8',
    email_header_image_url: 'https://i.imgur.com/APD3YVb.png',
    signature_html:
      // Thank-you closing + Landing-branded footer (logo, phone, address,
      // copyright). Mirrors the official Move-In template; swap signature_html
      // in the Config sheet if a property requires a different footer.
      '<p style="margin:18px 0 4px 0;">Thank you,</p>' +
      '<p style="margin:0 0 14px 0;">The Landing Team</p>' +
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" ' +
      'style="background:' + LANDING.DARK_NAVY + ';color:' + LANDING.WHITE + ';' +
      'border-radius:6px;font-family:Arial,Helvetica,sans-serif;">' +
        '<tr><td align="center" style="padding:22px 16px 8px;">' +
          '<img src="https://www.hellolanding.com/blog/wp-content/uploads/2025/08/landing_logomark_landing-bright-blue.png" ' +
          'alt="Landing" width="36" height="38" style="display:block;border:0;outline:none;">' +
        '</td></tr>' +
        '<tr><td align="center" style="padding:6px 16px;color:' + LANDING.WHITE + ';font-size:13px;">' +
          '<a href="tel:' + LANDING_IVR_PHONE + '" style="color:' + LANDING.WHITE + ';text-decoration:underline;">' +
          LANDING_IVR_PHONE_DISPLAY + '</a>' +
          ' &nbsp;|&nbsp; 17 20th Street North, Suite 315, Birmingham, AL 35203' +
        '</td></tr>' +
        '<tr><td align="center" style="padding:4px 16px 22px;color:' + LANDING.WHITE + ';font-size:12px;">' +
          '© Copyright Landing 2025. All rights reserved' +
        '</td></tr>' +
      '</table>',
    _hint:
      'Move-In Notification template loaded.\n\n' +
      'Switch to the "Move_In_Flow" tab and fill in one row per approved\n' +
      'reservation. Required per row: Property Email, Apt Number, Member Name,\n' +
      'Member Email/Phone, Move-In Date, Area Manager fields, Attachment IDs.\n\n' +
      'Attachment IDs accept either:\n' +
      '  - Comma-separated Drive file IDs (background check, ID scan, etc.), OR\n' +
      '  - A single Drive FOLDER ID — every file in that folder is attached.\n' +
      'Folder mode is the recommended workflow: drop all per-reservation\n' +
      'documents into a Google Drive folder and paste the folder ID once.\n\n' +
      'Optional: Move-Out Date.\n\n' +
      'Vehicle Info (optional) is pipe-delimited:\n' +
      '  "Year|Make|Model|Color|License Plate|State"\n' +
      '  e.g. "2018|Toyota|4Runner|Black|LCS3767|TX"\n' +
      'Multiple vehicles separated by ";". Blank cell renders as "N/A".\n\n' +
      'Pet/ESA Info (optional) is pipe-delimited:\n' +
      '  "Animal|Breed|Weight|ESA?|Name"\n' +
      '  e.g. "Dog|Golden Retriever|65 lbs|Yes|Buddy"\n' +
      'Multiple pets separated by ";". Blank cell renders as "N/A".\n\n' +
      'Occupants (optional) is pipe-delimited similarly:\n' +
      '  "Name|Phone|Email; Name|Phone|Email"\n\n' +
      'Branded wrapper: cream background is on by default. To add the LANDING\n' +
      'wordmark, paste a public PNG URL into the Config sheet field\n' +
      '"email_header_image_url".\n\n' +
      'Then run Dry Run -> Move-In mode -> Preview to verify before sending.',
  },

  'WiFi Outage': {
    event_name:        'WiFi Service Disruption',
    subject_template:  'Service Notice: WiFi Outage at {{property_name}} — {{today}}',
    greeting_template: '<p>Dear {{first_name | Resident}},</p>',
    include_disclaimer: 'YES',
    disclaimer_html:   '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;border-radius:6px;padding:10px;color:#15192D;font-style:italic;">This is a notification to all active residents at {{property_name}}. Please see the message below:</div>',
    notification_card: 'WIFI_OUTAGE',
    body_intro_html:
      '<p>We are writing to let you know that <strong>{{property_name}}</strong> is ' +
      'currently experiencing a <strong>WiFi service disruption</strong> affecting your unit. ' +
      'Our team is actively coordinating with the property and the internet service provider ' +
      'to identify the cause and restore service as quickly as possible.</p>' +
      '<p>In the meantime, here are a few things that may help:</p>' +
      '<ul>' +
      '<li>Your <strong>mobile data plan</strong> can serve as a reliable backup for ' +
      'essential connectivity while service is restored.</li>' +
      '<li>If your device connects automatically to the property WiFi, consider ' +
      '<strong>disabling WiFi on your device temporarily</strong> so it falls back to ' +
      'mobile data without interruption.</li>' +
      '<li>Once service is restored, <strong>restarting your devices or toggling WiFi ' +
      'off and back on</strong> should reconnect you automatically.</li>' +
      '</ul>',
    include_unit_line: 'NO',
    closing_html:
      '<p>For updates on the status of this outage or if you need any assistance, please ' +
      'reach out to your General Manager <strong>{{manager_name}}</strong>, or contact our ' +
      '<strong>24/7 Member Support</strong> team at ' +
      '<a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a>.</p>' +
      '<p>We appreciate your patience and apologize for any inconvenience this causes.<br>' +
      'The Landing Team</p>',
    send_mode: 'INDIVIDUAL',
    _hint:
      'WiFi Outage template loaded.\n\n' +
      'Please fill in:\n  • property_name\n  • manager_email\n\n' +
      'Dates are optional for live outages — {{today}} in the subject will\n' +
      'automatically reflect the send date.\n\n' +
      'Run Dry Run → Preview to verify before sending.',
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

  // Apply template-defined values, skipping the internal _hint key
  Object.entries(template).forEach(([key, value]) => {
    if (!key.startsWith('_')) setConfigValue_(key, value);
  });

  // Clear run-specific fields that the operator must fill in
  TEMPLATE_BLANK_KEYS.forEach(key => {
    if (!(key in template)) setConfigValue_(key, '');
  });

  // Use the template's custom hint if provided, otherwise show the generic one
  safeAlert_(template._hint ||
    `Template "${templateName}" loaded.\n\n` +
    'Please fill in:\n  • property_name\n  • manager_email\n  • window_start / window_end\n\n' +
    'Then run Dry Run → Preview to verify before sending.'
  );
}
