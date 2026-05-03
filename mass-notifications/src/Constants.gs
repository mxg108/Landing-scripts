/**
 * Mass Notifications — Constants
 * Shared global constants used across all files in this project.
 *
 * In GAS (V8) every .gs file shares one global scope, so anything defined
 * here is available project-wide without imports.
 */

const VERSION                    = 'v3.3.0';
const LANDING_IVR_PHONE          = '+14152311701';
const LANDING_IVR_PHONE_DISPLAY  = '(415) 231-1701';
const CONFIG_SHEET               = 'Config';
const DEFAULT_RECIPIENTS_SHEET   = 'Mass_Notification';
const RUN_LOG_SHEET              = 'Run_Log';

// Recipients sheet columns (1-based) — INDIVIDUAL/BCC modes
const COL = {
  EMAIL:      1,
  NAME:       2,
  UNIT:       3,
  STATUS:     4,
  LAST_SENT:  5,
  NOTES:      6,
  ATTACH_IDS: 7,
};
const COL_MAX = 7;

// ── Move-In Flow mode ─────────────────────────────────────────────────────────
// Separate tab targeting Property Management contacts (not members).
// One row = one approved reservation; one email = one row.

const MOVE_IN_SHEET = 'Move_In_Flow';

const MOVEIN_COL = {
  RESERVATION_ID: 1,
  PROPERTY_NAME:  2,
  PROPERTY_EMAIL: 3,   // comma-separated, primary recipients
  APT_NUMBER:     4,
  MEMBER_NAME:    5,
  MEMBER_EMAIL:   6,
  MEMBER_PHONE:   7,
  MOVE_IN_DATE:   8,
  MOVE_OUT_DATE:  9,
  VEHICLE_INFO:  10,   // blank → "N/A"
  PET_INFO:      11,   // blank → "N/A"
  OCCUPANTS:     12,   // pipe-delimited: "Name|Phone|Email; Name|Phone|Email"
  AREA_MGR_NAME: 13,
  AREA_MGR_PHONE:14,
  AREA_MGR_EMAIL:15,
  ATTACH_IDS:    16,   // comma-separated Drive IDs (background check + ID scan)
  STATUS:        17,
  LAST_SENT:     18,
  NOTES:         19,
};
const MOVEIN_COL_MAX = 19;

// ── Looker integration ────────────────────────────────────────────────────────

const LOOKER_BASE_URL    = 'https://landing.cloud.looker.com';
const LOOKER_API_VERSION = '4.0';
const LOOKER_MODEL       = 'landing';
const LOOKER_EXPLORE     = 'dimreservation';
const LOOKER_DATA_SHEET  = 'Looker_Data';

// Looker field names (model: landing, explore: dimreservation)
// Source: dashboard 4552 — "Active Occupants" tile
const LFIELD = {
  EMAIL:       'dimuser.user_email',
  MEMBER_NAME: 'dimuser.user_full_name',
  UNIT:        'dimhome.unit_number',
  OCC_NAME:    'dimoccupant.occ_name',
  PHONE:       'dimuser.user_phone',
  RES_ID:      'dimreservation.reservation_id',
  CHECK_IN:    'dimreservation.reservation_check_in_date',
  CHECK_OUT:   'dimreservation.reservation_check_out_at_date',
  PROPERTY:    'dimproperty.property_name',
  ALT_EMAIL_1: 'tblapplication.first_applicant_email',
  ALT_EMAIL_2: 'tblapplication.second_applicant_email',
  ALT_EMAIL_3: 'tblapplication.third_applicant_email',
  ALT_EMAIL_4: 'tblapplication.fourth_applicant_email',
  PLATFORM:    'dimreservation.reservation_platform',
};
