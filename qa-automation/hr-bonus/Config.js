/**
 * HR Bonus Sheet — Config (static)
 *
 * Deliberately NOT build_config.py-generated: the backend month payload
 * carries every dynamic value (section labels, order, display strings),
 * so this suite only needs to know which team to ask for. Deployment-
 * specific values (backend URL, API key, workbook id) live in Script
 * Properties — see README.md.
 */

var CONFIG = {
  TEAM_ID: "member_support",
};
