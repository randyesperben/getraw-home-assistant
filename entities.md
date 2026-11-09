# RAW's entities and events in Home Assistant (contract 6)

One device per RAW account, named «RAW» and the person's name. Each key below is the entity's
translation key and the end of its unique id, and its English or Spanish name builds its entity id
(`binary_sensor.raw_randy_workout`, `binary_sensor.raw_randy_entreno`): keys, names, enum states
and event types are forever once released. Names and states in both languages:
`custom_components/getraw/translations/{en,es}.json`; event types: `custom_components/getraw/const.py`.

«Off»: disabled by default. «Category»: the health switch it needs on the phone (contract 1's
`health`); while that switch is off the entity is `unavailable`, from the next message that says so,
whichever kind it is. No event carries health (below). HA keeps a body's health only for the
categories that body says are on, Saved volume only with Body weight. No count is
`total_increasing`: deleting or editing a workout lowers it. The recorder keeps no workout notes or
titles held in attributes, and no event's `values` or `records`.

## The live workout (from `state`, `event`, `patch`)

| Key | Platform, class | Off | Category |
|---|---|---|---|
| `workout` | `binary_sensor`, `running` | | |
| `resting` | `binary_sensor` | | |
| `phase` | `sensor`, `enum`: `idle`, `active`, `resting`, `finishing` | | |
| `workout_title` | `sensor` | | titles |
| `workout_started` | `sensor`, `timestamp` | | |
| `current_exercise` | `sensor` (attributes: index, total) | | |
| `current_set` | `sensor` (attributes: total, set type) | | |
| `target_weight` | `sensor`, `weight`, kg | Off | |
| `target_reps` | `sensor` | Off | |
| `last_time` | `sensor` | Off | |
| `next_set` | `sensor` | Off | |
| `sets_done` | `sensor` (attribute: total) | | |
| `rest_end` | `sensor`, `timestamp` | | |
| `timed_set_started` | `sensor`, `timestamp` | Off | |
| `workout_volume` | `sensor`, `weight`, kg, `measurement` (bodyweight sets count 0) | | |
| `latest_record` | `sensor` (follows the state's `last_pr`, so an undo corrects it) | | |
| `last_update` | `sensor`, `timestamp`, diagnostic | | |
| `events` | `event` | | |

## The history summary (from `summary`)

| Key | Platform, class | Off | Category |
|---|---|---|---|
| `trained_today` | `binary_sensor` | | |
| `todays_plan` | `sensor`, `enum`: `due`, `done`, `rest`, `no_plan` (attribute `reason`) | | |
| `next_routine` | `sensor` | | titles |
| `next_routine_date` | `sensor`, `date` | | |
| `training_days_this_week` | `sensor`, `total`, reset weekly at the summary's `first_day_of_week` | | |
| `workouts_this_week` | `sensor`, `total`, reset weekly at the summary's `first_day_of_week` | | |
| `weekly_goal` | `sensor`, `measurement` | | |
| `weekly_goal_met` | `binary_sensor`; `unknown` without a goal | | |
| `weeks_in_a_row` | `sensor`, `measurement` | | |
| `last_workout` | `sensor`, `timestamp` (attributes: sets, reps, records; its title under titles, its notes under notes) | | |
| `last_workout_duration` | `sensor`, `duration` | | |
| `workouts_this_month` | `sensor`, `total`, reset monthly | | |
| `training_days_this_month` | `sensor`, `total`, reset monthly | Off | |
| `training_time_this_month` | `sensor`, `duration`, `total`, reset monthly | Off | |
| `sets_this_month` | `sensor`, `total`, reset monthly | Off | |
| `reps_this_month` | `sensor`, `total`, reset monthly | Off | |
| `records_this_month` | `sensor`, `total`, reset monthly: `month.records`, the monthly report's count | | |
| `workouts_all_time` | `sensor`, `total` | | |
| `workouts` | `calendar`: past workouts and planned days, all-day for planned ones | | titles for the events' names; without it «Workout» and «Planned workout» in HA's language |

HA turns the day at midnight in the summary's `time_zone`, shows each figure from that day's entry,
counts the month's totals as 0 once that zone enters a month the phone has not summed yet, and
shows `unknown` past the last day sent. A weekly or monthly `total` keeps its `last_reset` while its
week or month is the same, after a new `time_zone` or a restart too, so its long-term statistics
never count a period twice. Sensors with a unit are first added once a summary has
said the person's units, since HA reads a suggested unit only when an entity is first made.

`reason` is `scheduled_today`, `plan_day`, `pending` (a routine left from earlier this week, due
today), `next_scheduled` or `rotation` (`NextSessionReason`), `first_day` or `free` (no routine).

## Health (each under its category)

| Key | Platform, class | Off | Category |
|---|---|---|---|
| `body_weight` | `sensor`, `weight`, kg, `measurement` | | body_weight |
| `body_fat_pct` | `sensor`, %, `measurement` | | measurements |
| `neck`, `shoulders`, `chest`, `abdomen`, `waist`, `hips`, `bicep_l`, `bicep_r`, `forearm_l`, `forearm_r`, `thigh_l`, `thigh_r`, `calf_l`, `calf_r` | `sensor`, `distance`, cm, `measurement` | Off | measurements |
| `last_workout_effort` | `sensor`, 1 to 10 | | effort |
| `last_workout_feeling` | `sensor`, `enum`: `awful`, `bad`, `okay`, `good`, `great` | | effort |
| `last_workout_volume` | `sensor`, `weight`, kg (bodyweight sets count the body weight) | | saved_volume |
| `volume_this_month` | `sensor`, `weight`, kg, `total`, reset monthly | | saved_volume |

Session RPE and feeling are `last_workout_effort` and `last_workout_feeling`, from the summary
after the workout ends; the RPE of each set is not shown in HA. Saved volume entities need Body
weight on too, since saved volume counts the body weight in bodyweight sets.
Workout notes are an attribute of `last_workout`, never a state (HA caps a state at 255
characters and logs a longer one). Weights are stored in kg and shown in the person's unit, suggested
from the summary's `weight_unit` when the entity is first created; lengths in cm, from `length_unit`.

## Events (`events`, `trigger: event.received`)

| Type | Fired by |
|---|---|
| `workout_started`, `set_completed`, `exercise_changed`, `rest_started`, `rest_adjusted`, `rest_skipped`, `record`, `workout_finishing`, `workout_discarded`, `workout_finished` | the phone (contract 1 says which writer) |
| `test` | the phone's «Send a test», so an automation can be built before a workout |
| `rest_ended` | Home Assistant, at the rest's end (cancelled by a skip, the next set or the workout's end) |
| `workout_abandoned` | Home Assistant, at `abandoned_at` |

The event's attributes are its `data` (contract 1) and its `text`, never its `health`: HA shows the
last event's attributes until the next event and restores them after a restart, so a switch turned
off could not hide them. Every instant in `data` (`started_at`, `rest_started_at`, `ends_at`,
`abandoned_at`) is on HA's clock, corrected by the phone's offset as `rest_end` and
`workout_started` are, in the contract's form (`2026-11-09T09:00:00.000Z`). The workout's title is `workout_title`; session effort and notes come with
the summary. An unknown event type from a newer phone is ignored, and so is a `rest_ended` or
`workout_abandoned` from the phone. Events fire after the state they left is shown, so an
automation they trigger reads that state. HA rings `rest_ended` at most 10 s late (a restart, a
host that slept) and `workout_abandoned` at most 2 minutes late, once each; a plain `state` that
ends a rest within 5 s of its end rings it then (the phone's own timer ran out first).

## The lock screen's patch

A `patch` moves only what its tap moved. The kept names, targets and texts stay while the exercise
and set ids are the same and go to `unknown` when the tap moved past them (AUDIT-L1-1 I10), until
Dart's next message. Each tap moves `last_activity_at` and `abandoned_at` by the same amount, so
lock-screen sets with RAW closed keep the workout on.
