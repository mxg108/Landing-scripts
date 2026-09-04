# Dialpad Stats API — export headers & semantics (SR0 capture)

*Captured 2026-09-03 against the Member Support call center
(`target_type: callcenter`, `target_id: 5699048497577984`,
`timezone: America/Mexico_City`, `days_ago_start: 1, days_ago_end: 2`
= local days 2026-09-01 and 2026-09-02). Headers and derived facts only —
no call rows. Closes ShiftReport.md §4.1 SR0: the records export carries
an explicit result column (`category`), so §4.1 rule 2 applies.*

## 1. `export_type: records`, `stat_type: calls` (54 columns)

```
date_started, call_id, category, direction, external_number, internal_number,
date_first_rang, date_queued, date_rang, date_connected, date_ended,
target_id, target_kind, target_type, name, email, was_recorded,
entry_point_call_id, entry_point_target_id, entry_point_target_kind,
proxy_target_id, voicemail, transferred_to, transferred_to_contact_id,
transferred_from_target_id, office_id, company_id, device,
salesforce_activity_id, timezone, availability, time_in_system,
callback_type, callback_id, master_call_id, time_to_answer,
date_callback_connected, date_callback_ended, date_anonymized, categories,
ringing_duration, ringing_occurrences, hold_duration, hold_occurrences,
talk_duration, queued_duration, queued_occurrences, wrapup_duration,
participant_type, percent_ai_talk_time, percent_ai_listen_time,
percent_ai_silent_time, campaign_id, is_internal
```

Semantics (verified on the two days):

- **Two row kinds per call.** `target_kind = CallCenter` (`target_type =
  call_center`, `entry_point_call_id` EMPTY, `name` = "Member Support
  Line", `email` empty) is the entry-point leg — **one row per call the
  line received or placed**. `target_kind = UserProfile` (`target_type =
  user`, `participant_type = operator`, `entry_point_call_id` = the
  CallCenter row's `call_id`, `name`/`email` = the agent) is the agent leg.
  Join agent → call on `UserProfile.entry_point_call_id = CallCenter.call_id`.
- **`category`** (single value) on CallCenter rows: `incoming` (answered
  inbound), `outgoing`, `abandoned`, `missed`, `cancelled`, `forwarded`,
  `callback_connected`, `callback_cancelled`, `callback_requested`, `other`.
  **`category = abandoned` on CallCenter rows reproduces the daily stats
  export's `abandoned` EXACTLY** (21 and 37). This is Dialpad's official
  abandoned marker — ShiftReport §4.1 `definition` =
  `records: target_kind=CallCenter AND category=abandoned`.
- **`categories`** (comma list) carries the finer tokens: `answered`,
  `unanswered`, `abandoned`, `missed`, `cancelled`, `voicemail`, `spam`,
  `human_agent`, `transferred*`, `*callback*`, `inbound`/`outbound`, …
  Note `categories` contains `abandoned` on a few more rows than
  `category = abandoned` (24 vs 21) — use `category`, not the token.
- **Timestamps are naive local** in the row's `timezone` column
  (`YYYY-MM-DD HH:MM:SS.ffffff`) — same as the dispositions export.
- **All durations are MINUTES** (`talk_duration 2.86` = 172 s; checked:
  `date_ended − date_connected` = talk + hold). `time_to_answer` is also
  minutes (`0.03` ≈ 2 s). Wait before abandon ≈ `queued_duration +
  ringing_duration` on the CallCenter row.
- **`time_to_answer`** is present on answered inbound CallCenter rows only
  (353/385 answered rows on 09-01; the rest are callbacks/transfers).

## 2. `export_type: stats`, `stat_type: calls`, `group_by: date` (68 columns)

One row per local day for the call center:

```
date, all_calls, inbound_calls, outbound_calls, voicemails, missed, abandoned,
forwarded, cancelled, minutes, acd, aht, inbound_minutes, outbound_minutes,
time_in_system, service_level, callbacks_requested, callbacks_completed,
callbacks_cancelled, open_inbound_calls, open_missed_calls,
open_abandoned_calls, callbacks_connected, callbacks_unconnected,
missed_voicemails, other_voicemails, missed_transferred, asa,
open_transferred, short_abandoned, handled, answered, answered_transferred,
message, spam, in_queue_voicemail, dtmf_voicemail, direct_to_voicemail,
transfer_voicemail, outbound_connected, connected_transferred,
transferred_out, transferred_in, dtmf_transfer, auto_transfer,
router_transfer, forward_transfer, timezone, ringing_duration,
avg_ringing_duration, hold_duration, avg_hold_duration, talk_duration,
avg_talk_duration, queued_duration, avg_queued_duration, wrapup_duration,
avg_wrapup_duration, callback_agent_missed_rejected,
direct_callback_agent_missed_rejected, direct_callback_cancelled,
scripted_ivr_transfer, total_voicemails, unresolved_voicemails,
resolved_voicemails, avg_voicemail_handle_time_minutes,
avg_voicemail_assignment_time_minutes, avg_time_open_minutes
```

- **`service_level` is a COUNT, not a percentage** — Dialpad help ("Read
  your exported analytics"): *"Number of above service level calls
  answered during open/holiday hours."* The percentage denominator is NOT
  documented on that page; pick it to match Dialpad Analytics (open item).
- The MS call center's SL target (GET `/api/v2/callcenters/{id}` →
  `alerts`): **`cc_service_level: 80`, `cc_service_level_seconds: 30`**.
  Reverse-engineered from records: answered inbound CallCenter rows with
  `time_to_answer ≤ 0.5 min` = **286 on 09-01 (exact match)**, 223 on
  09-02 (export says 225). Good enough to compute SL per shift window from
  records; keep the daily export as the reconciliation check.
- Without `group_by` the same request returns **one row per user** (55
  columns, adds `user_id, name, email, type, internal_calls, device
  columns, rejected, ring_no_answer`) — a free per-agent daily table.
- Dialpad docs: `is_today` results cached 30 min, `days_ago` results
  cached 3 h; wait ~15–20 s before the first GET. Observed: 10–35 s.

## 3. `export_type: records`, `stat_type: onduty` (14 columns)

```
date, record_id, target_id, availability_status, on_duty_status, reason,
name, email, target_type, call_center_id, setter_name, setter_email,
setter_role, timezone
```

One row per status transition. `on_duty_status` ∈ {available, occupied,
wrapup, unavailable, busy}. `setter_*` says who flipped it (the agent, or
"Member Support Line" for system transitions). Off-duty is the absence of
rows; SR4 derives intervals by ordering per agent.

## 4. Reconciliation, 2026-09-01 / 2026-09-02 (records vs daily stats)

| Measure | Records rule | 09-01 | stats | 09-02 | stats |
|---|---|---|---|---|---|
| all calls | CallCenter rows | 504 | 506 | 432 | 432 |
| inbound | CallCenter ∧ direction=inbound | 396 | 396 | 345 | 345 |
| outbound | CallCenter ∧ direction=outbound | 108 | 110 | 87 | 87 |
| answered | CallCenter ∧ category=incoming | 353 | 353 | 289 | 289 |
| abandoned | CallCenter ∧ category=abandoned | 21 | 21 | 37 | 37 |
| missed | CallCenter ∧ category=missed | 7 | 6 | 0 | 0 |
| SL count | answered inbound ∧ time_to_answer ≤ 30 s | 286 | 286 | 223 | 225 |
| short abandoned | (no records rule found yet) | — | 5 | — | 11 |
| unique agents (handled a leg) | distinct UserProfile.email | 19 | 19 (user rows) | 18 | 18 |
| unique agents (went on duty) | distinct onduty.email | 21 | — | 19 | — |

The ±2 on all/outbound is day-bucketing at midnight (records bucketed by
`date_started`); inbound reconciles exactly.

## 5. Timezone caveat

The call center object reports `timezone: US/Central` (UTC-5 in
September); these exports were requested in `America/Mexico_City` (fixed
UTC-6). Daily totals differ by one hour of calls between the two — pick
one and state it in the report header (ShiftReport §0 chose Mexico City).
