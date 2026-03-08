/**
 * Mass Notifications — Constants
 * Shared global constants used across all files in this project.
 *
 * In GAS (V8) every .gs file shares one global scope, so anything defined
 * here is available project-wide without imports.
 */

const VERSION                    = 'v3.1.1';
const LANDING_IVR_PHONE          = '+14152311701';
const LANDING_IVR_PHONE_DISPLAY  = '(415) 231-1701';
const CONFIG_SHEET               = 'Config';
const DEFAULT_RECIPIENTS_SHEET   = 'Mass_Notification';
const RUN_LOG_SHEET              = 'Run_Log';

// Recipients sheet columns (1-based)
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
