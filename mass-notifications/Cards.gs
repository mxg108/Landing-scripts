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
   * @param {string} icon         — emoji or text icon shown in the header
   * @param {string} title        — card heading
   * @param {string} bodyHtml     — inner content rows
   * @return {string}
   */
  _shell(accentColor, icon, title, bodyHtml) {
    return `
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="border:2px solid ${accentColor};border-radius:8px;overflow:hidden;margin:12px 0;font-family:Arial,Helvetica,sans-serif;">
  <tr>
    <td style="background:${accentColor};padding:10px 16px;color:${LANDING.WHITE};font-size:15px;font-weight:bold;">
      ${icon}&nbsp; ${title}
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

    return this._shell(LANDING.ACCENT_BLUE, '🔥', 'Annual Fire Inspection', body);
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

    return this._shell(LANDING.AMBER, '🚿', 'Planned Water Outage', body);
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

    return this._shell(LANDING.DARK_NAVY, '🔧', 'Scheduled Maintenance', body);
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

    return this._shell(LANDING.RED, '⛈️', 'Weather Advisory', body);
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

    return this._shell(LANDING.ORANGE, '⚡', 'Power Outage — Active', body);
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

    return this._shell(LANDING.AMBER, '📶', 'WiFi Service Disruption — Active', body);
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
};

/**
 * Instantiates and renders the card for the given type, then resolves tokens.
 * Returns an empty string for unknown or empty card types.
 *
 * @param {string} cardType — value from cfg.notificationCard
 * @param {Object} tokens   — result of buildPerRowTokens_() or buildGlobalTokens_()
 * @return {string}
 */
function buildCard_(cardType, tokens) {
  const CardClass = CARD_REGISTRY[String(cardType || '').toUpperCase()];
  if (!CardClass) return '';
  const rawHtml = new CardClass().render();
  return renderWithTokens_(rawHtml, tokens);
}
