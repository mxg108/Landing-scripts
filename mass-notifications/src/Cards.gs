/**
 * Mass Notifications — Cards
 *
 * Self-contained HTML "info cards" that appear between the disclaimer and the
 * body intro, giving residents key facts at a glance.
 *
 * USAGE IN CONFIG SHEET:
 *   notification_card = FIRE_INSPECTION | WATER_OUTAGE | MAINTENANCE | WEATHER_ALERT
 *                     | POWER_OUTAGE | WIFI_OUTAGE
 *   Leave blank to include no card.
 *
 * ADDING A NEW CARD TYPE:
 *   1. Extend NotificationCard with your new class below.
 *   2. Add it to CARD_REGISTRY with a SCREAMING_SNAKE key.
 *   3. Run "Config / Tools → Ensure Config UI" so the dropdown updates.
 *
 * TOKEN SUPPORT:
 *   Card HTML may include any {{token}} supported by the Tokenizer.
 *   renderWithTokens_() is called on the card's render() output inside
 *   buildCard_(), so tokens resolve automatically.
 *
 * DESIGN CONSTRAINTS:
 *   All styles must be inline — Gmail strips <style> blocks.
 *   Use table-based layouts for maximum email client compatibility.
 *   Landing brand palette is defined at the bottom of this file.
 */

// ── Brand palette (mirrors QA Automation's CONFIG.COLORS) ────────────────────
const LANDING = {
  DARK_NAVY:   '#15192D',
  ACCENT_BLUE: '#1A61D9',
  LIGHT_BLUE:  '#E7EFFB',
  WHITE:       '#FFFFFF',
  AMBER:       '#E8A317',
  ORANGE:      '#D4600A', // Power outage — urgent but distinct from RED
  RED:         '#D9534F',
  GREEN:       '#28A745',
  TEXT_GRAY:   '#4A4A4A',
};

// ── Base class ────────────────────────────────────────────────────────────────

class NotificationCard {
  /**
   * Returns the raw HTML for this card.
   * Tokens ({{date_range}}, {{property_name}}, …) are resolved by the caller
   * via renderWithTokens_() — do NOT call it yourself inside render().
   *
   * @return {string}
   */
  render() {
    return '';
  }

  /**
   * Shared helper: wraps content in the standard card shell.
   *
   * @param {string} accentColor  — left-border / header background
   * @param {string} codepoint    — HTML hex entity for the icon (e.g. &#x1F525;), avoids Gmail emoji-encoding issues
   * @param {string} title        — card heading
   * @param {string} bodyHtml     — inner content rows
   * @return {string}
   */
  _shell(accentColor, codepoint, title, bodyHtml) {
    return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="border:2px solid ${accentColor};border-radius:8px;overflow:hidden;margin:12px 0;font-family:Arial,Helvetica,sans-serif;">
  <tr>
    <td style="background:${accentColor};padding:10px 16px;color:${LANDING.WHITE};font-size:15px;font-weight:bold;">
      ${codepoint}&nbsp; ${title}
    </td>
  </tr>
  <tr>
    <td style="background:${LANDING.LIGHT_BLUE};padding:14px 16px;">
      ${bodyHtml}
    </td>
  </tr>
</table>`.trim();
  }

  /**
   * Shared helper: renders one key→value info row inside a card body.
   *
   * @param {string} label
   * @param {string} value   — may contain {{tokens}}, resolved later
   * @param {string} [valueColor]
   * @return {string}
   */
  _row(label, value, valueColor) {
    const color = valueColor || LANDING.DARK_NAVY;
    return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="margin-bottom:6px;">
  <tr>
    <td style="font-size:12px;color:${LANDING.TEXT_GRAY};text-transform:uppercase;
               letter-spacing:0.5px;width:38%;vertical-align:top;padding-right:8px;">
      ${label}
    </td>
    <td style="font-size:14px;font-weight:bold;color:${color};vertical-align:top;">
      ${value}
    </td>
  </tr>
</table>`.trim();
  }
}

// ── Concrete card implementations ─────────────────────────────────────────────

class FireInspectionCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property',   '{{property_name}}'),
      this._row('Dates',      '{{date_range}}',    LANDING.ACCENT_BLUE),
      this._row('Action required',
        'Inspectors will need <strong>access to your unit</strong>. ' +
        'Please ensure your smoke detectors are unobstructed.'),
    ].join('');

    return this._shell(LANDING.ACCENT_BLUE, '&#x1F525;', 'Annual Fire Inspection', body);
  }
}

class WaterOutageCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property',  '{{property_name}}'),
      this._row('Outage window', '{{date_range}}', LANDING.AMBER),
      this._row('What to expect',
        'Water service will be temporarily <strong>unavailable</strong> during this window. ' +
        'We apologize for the inconvenience.'),
    ].join('');

    return this._shell(LANDING.AMBER, '&#x1F6BF;', 'Planned Water Outage', body);
  }
}

class MaintenanceCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property',  '{{property_name}}'),
      this._row('Scheduled', '{{date_range}}',  LANDING.ACCENT_BLUE),
      this._row('Details',
        'Our maintenance team will be on-site for <strong>{{event_name}}</strong>. ' +
        'Some common areas may be temporarily unavailable.'),
    ].join('');

    return this._shell(LANDING.DARK_NAVY, '&#x1F527;', 'Scheduled Maintenance', body);
  }
}

class WeatherAlertCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property', '{{property_name}}'),
      this._row('Period',   '{{date_range}}', LANDING.RED),
      this._row('Advisory',
        'A <strong>weather advisory</strong> has been issued for your area. ' +
        'Please take necessary precautions for your safety and secure any outdoor belongings.'),
    ].join('');

    return this._shell(LANDING.RED, '&#x26C8;&#xFE0F;', 'Weather Advisory', body);
  }
}

class PowerOutageCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property', '{{property_name}}'),
      this._row('Reported', '{{today}}', LANDING.ORANGE),
      this._row('Status',
        '<strong>Active</strong> — Landing is engaged with the property ' +
        'and working toward the fastest possible resolution.'),
      this._row('Immediate steps',
        'Unplug sensitive electronics &nbsp;·&nbsp; ' +
        'Keep fridge &amp; freezer closed &nbsp;·&nbsp; ' +
        'Use flashlights, not candles'),
    ].join('');

    return this._shell(LANDING.ORANGE, '&#x26A1;', 'Power Outage — Active', body);
  }
}

class WiFiOutageCard extends NotificationCard {
  render() {
    const body = [
      this._row('Property', '{{property_name}}'),
      this._row('Reported', '{{today}}', LANDING.AMBER),
      this._row('Status',
        '<strong>Active</strong> — Landing is coordinating with the property ' +
        'and the internet service provider on a resolution.'),
      this._row('In the meantime',
        'Mobile data is available as a backup &nbsp;·&nbsp; ' +
        'Disable WiFi on your device to switch automatically'),
    ].join('');

    return this._shell(LANDING.AMBER, '&#x1F4F6;', 'WiFi Service Disruption — Active', body);
  }
}

// ── Move-In Notification (matches Landing's official template 1:1) ───────────
//
// Unlike the cards above, this one does NOT use _shell — the official template
// is plain text on a cream background, no colored header bar.  It also needs
// JS-level access to the per-row tokens to render the variadic occupants list,
// so render() takes the tokens map directly (other cards ignore it).
//
class MoveInCard extends NotificationCard {
  render(tokens) {
    const t = tokens || {};

    // Inline-styled rows render as one <div> with <br> separators so the
    // buildHtmlBody_ post-processor (which adds margin to bare <p>) leaves
    // them alone — keeps spacing tight inside the info block.
    const memberBlock = `
<div style="margin:0 0 14px 0;font-size:14px;color:${LANDING.DARK_NAVY};line-height:1.5;">
  <strong>Apartment Number:</strong> ${escapeHtml_(t.apartment_number || '')}<br>
  <strong>Member Name:</strong> ${escapeHtml_(t.member_name || '')}<br>
  <strong>Move In Date:</strong> ${escapeHtml_(t.move_in_date || '')}<br>
  <strong>Phone Number:</strong> ${this._telLink(t.member_phone)}<br>
  <strong>Contact Email:</strong> ${this._mailtoLink(t.member_email)}
</div>`.trim();

    // Vehicles render structured (labeled per field) when the cell is
    // pipe-delimited "Year|Make|Model|Color|License Plate|State". Multiple
    // vehicles can be ';'-separated. Falls back to the raw cell value (or
    // "N/A") for legacy rows that were entered as free text.
    const vehiclesList = (t._vehicles || []).map(v => {
      const rows = [];
      if (v.year)  rows.push(`<strong>Year:</strong> ${escapeHtml_(v.year)}`);
      if (v.make)  rows.push(`<strong>Make:</strong> ${escapeHtml_(v.make)}`);
      if (v.model) rows.push(`<strong>Model:</strong> ${escapeHtml_(v.model)}`);
      if (v.color) rows.push(`<strong>Color:</strong> ${escapeHtml_(v.color)}`);
      if (v.plate) rows.push(`<strong>License Plate:</strong> ${escapeHtml_(v.plate)}`);
      if (v.state) rows.push(`<strong>State:</strong> ${escapeHtml_(v.state)}`);
      if (!rows.length) return '';
      return `<div style="margin:0 0 10px 0;font-size:14px;color:${LANDING.DARK_NAVY};line-height:1.5;">${rows.join('<br>')}</div>`;
    }).filter(Boolean).join('');

    const vehicleHeader = `<p style="margin:0 0 4px 0;"><strong>Vehicle Information</strong></p>`;
    const vehicle = vehiclesList
      ? `${vehicleHeader}<div style="margin:0 0 14px 0;">${vehiclesList}</div>`
      : `${vehicleHeader}<p style="margin:0 0 14px 0;">${escapeHtml_(t.vehicle_info || 'N/A')}</p>`;

    // Pets render structured (labeled per field) when the cell is
    // pipe-delimited "Animal|Breed|Weight|ESA?|Name". Multiple pets can be
    // ';'-separated. Falls back to the raw cell value (or "N/A") for legacy
    // rows entered as free text.
    const petsList = (t._pets || []).map(p => {
      const rows = [];
      if (p.animal) rows.push(`<strong>Animal:</strong> ${escapeHtml_(p.animal)}`);
      if (p.breed)  rows.push(`<strong>Breed:</strong> ${escapeHtml_(p.breed)}`);
      if (p.weight) rows.push(`<strong>Weight:</strong> ${escapeHtml_(p.weight)}`);
      if (p.esa)    rows.push(`<strong>ESA:</strong> ${escapeHtml_(p.esa)}`);
      if (p.name)   rows.push(`<strong>Name:</strong> ${escapeHtml_(p.name)}`);
      if (!rows.length) return '';
      return `<div style="margin:0 0 10px 0;font-size:14px;color:${LANDING.DARK_NAVY};line-height:1.5;">${rows.join('<br>')}</div>`;
    }).filter(Boolean).join('');

    const petHeader = `<p style="margin:0 0 4px 0;"><strong>Pet Information</strong></p>`;
    const pet = petsList
      ? `${petHeader}<div style="margin:0 0 14px 0;">${petsList}</div>`
      : `${petHeader}<p style="margin:0 0 14px 0;">${escapeHtml_(t.pet_info || 'N/A')}</p>`;

    const occupantsList = (t._occupants || []).map(o => `
<div style="margin:0 0 10px 0;font-size:14px;color:${LANDING.DARK_NAVY};line-height:1.5;">
  <strong>Name:</strong> ${escapeHtml_(o.name || '')}<br>
  <strong>Phone Number:</strong> ${this._telLink(o.phone)}<br>
  <strong>Email Address:</strong> ${this._mailtoLink(o.email)}
</div>`.trim()).join('');

    const occupants = occupantsList
      ? `<p style="margin:0 0 6px 0;"><strong>All additional occupants listed below:</strong></p>${occupantsList}`
      : '';

    const bgCheck =
      `<p>A copy of the member&#39;s background check is attached. ` +
      `If your property requires additional information about the member, ` +
      `you will receive it in a follow-up email.</p>`;

    const reachOut =
      `<p>Please reach out to your Landing area manager ` +
      `with any additional questions or concerns.</p>`;

    const areaMgr = `
<div style="margin:0 0 14px 0;font-size:14px;color:${LANDING.DARK_NAVY};line-height:1.5;">
  <strong>Name:</strong> ${escapeHtml_(t.area_mgr_name || '')}<br>
  <strong>Phone:</strong> ${this._telLink(t.area_mgr_phone)}<br>
  <strong>Email address:</strong> ${this._mailtoLink(t.area_mgr_email)}
</div>`.trim();

    return [memberBlock, vehicle, pet, occupants, bgCheck, reachOut, areaMgr]
      .filter(Boolean).join('');
  }

  _telLink(raw) {
    const display = String(raw || '').trim();
    if (!display) return '';
    // normalizeTelLinks_ strips non-digits from the href at composition time.
    return `<a href="tel:${escapeHtml_(display)}" style="color:${LANDING.ACCENT_BLUE};">${escapeHtml_(display)}</a>`;
  }

  _mailtoLink(addr) {
    const display = String(addr || '').trim();
    if (!display) return '';
    return `<a href="mailto:${escapeHtml_(display)}" style="color:${LANDING.ACCENT_BLUE};">${escapeHtml_(display)}</a>`;
  }
}

// ── Registry & factory ────────────────────────────────────────────────────────

/**
 * Maps the Config sheet value of `notification_card` to a card class.
 * Keys must be UPPER_SNAKE to match toUpperCase() applied in loadConfig_().
 */
const CARD_REGISTRY = {
  FIRE_INSPECTION: FireInspectionCard,
  WATER_OUTAGE:    WaterOutageCard,
  MAINTENANCE:     MaintenanceCard,
  WEATHER_ALERT:   WeatherAlertCard,
  POWER_OUTAGE:    PowerOutageCard,
  WIFI_OUTAGE:     WiFiOutageCard,
  MOVE_IN:         MoveInCard,
};

/**
 * Instantiates and renders the card for the given type, then resolves tokens.
 * Returns an empty string for unknown or empty card types.
 *
 * @param {string} cardType — value from cfg.notificationCard
 * @param {Object} tokens   — result of buildPerRowTokens_(), buildGlobalTokens_(),
 *                            or buildMoveInTokens_(). Passed to render() so cards
 *                            with non-trivial structure (variadic lists, etc.)
 *                            can consume tokens directly; existing cards ignore it.
 * @return {string}
 */
function buildCard_(cardType, tokens) {
  const CardClass = CARD_REGISTRY[String(cardType || '').toUpperCase()];
  if (!CardClass) return '';
  const rawHtml = new CardClass().render(tokens);
  return renderWithTokens_(rawHtml, tokens);
}
