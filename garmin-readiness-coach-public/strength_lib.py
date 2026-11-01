"""Built-in exercise library and program templates for the strength mode.

Exercises carry the muscles they train (primary counts 1 set, secondary half a set in weekly volume),
how they're loaded (barbell plates, a dumbbell pair, a machine stack or bodyweight), a default rest
and a default increment in both unit systems. Programs reference exercises by id; custom exercises
(store.strength_exercises) use the same shape with ids starting "c_".
"""
from __future__ import annotations

MUSCLES = {
    "quads": "Quads", "hamstrings": "Hamstrings", "glutes": "Glutes", "calves": "Calves", "chest": "Chest",
    "back": "Back", "lats": "Lats", "shoulders": "Shoulders", "rotator_cuff": "Rotator cuff", "triceps": "Triceps",
    "biceps": "Biceps", "forearms": "Forearms", "core": "Core",
}
LOADS = ("barbell", "dumbbell", "machine", "bodyweight")  # how a weight is made up (plates, a pair, a stack, none)


def _ex(id_, name, load, primary, secondary=(), rest=90, inc_lb=5.0, inc_kg=2.5, tags=(), lower=False):
    return dict(id=id_, name=name, load=load, primary=list(primary), secondary=list(secondary), rest_s=rest,
                inc_lb=inc_lb, inc_kg=inc_kg, tags=list(tags), lower=lower)


LIBRARY = [
    # main barbell lifts
    _ex("squat", "Squat", "barbell", ["quads", "glutes"], ["hamstrings", "core"], 180, lower=True),
    _ex("bench", "Bench Press", "barbell", ["chest"], ["triceps", "shoulders"], 180),
    _ex("row", "Barbell Row", "barbell", ["back", "lats"], ["biceps", "forearms"], 120),
    _ex("ohp", "Overhead Press", "barbell", ["shoulders"], ["triceps", "core"], 180),
    _ex("deadlift", "Deadlift", "barbell", ["hamstrings", "glutes", "back"], ["quads", "forearms", "core"], 180, 10.0, 5.0, lower=True),
    _ex("front_squat", "Front Squat", "barbell", ["quads"], ["glutes", "core"], 180, lower=True),
    _ex("rdl", "Romanian Deadlift", "barbell", ["hamstrings", "glutes"], ["back", "forearms"], 120, lower=True),
    _ex("incline_bench", "Incline Bench Press", "barbell", ["chest", "shoulders"], ["triceps"], 150),
    _ex("close_grip_bench", "Close-Grip Bench Press", "barbell", ["triceps", "chest"], ["shoulders"], 150),
    _ex("power_clean", "Power Clean", "barbell", ["glutes", "hamstrings", "back"], ["quads", "shoulders"], 150, lower=True),
    _ex("hip_thrust", "Hip Thrust", "barbell", ["glutes"], ["hamstrings"], 120, 10.0, 5.0, lower=True),
    _ex("trap_bar_deadlift", "Trap-Bar Deadlift", "barbell", ["quads", "glutes", "hamstrings"], ["back", "forearms"], 150, 10.0, 5.0, lower=True),
    # dumbbells & kettlebells (the weight is one dumbbell)
    _ex("goblet_squat", "Goblet Squat", "dumbbell", ["quads", "glutes"], ["core"], 90, lower=True),
    _ex("db_bench", "Dumbbell Bench Press", "dumbbell", ["chest"], ["triceps", "shoulders"], 90),
    _ex("db_row", "One-Arm Dumbbell Row", "dumbbell", ["back", "lats"], ["biceps"], 90),
    _ex("db_rdl", "Dumbbell Romanian Deadlift", "dumbbell", ["hamstrings", "glutes"], ["back"], 90, lower=True),
    _ex("db_press", "Dumbbell Shoulder Press", "dumbbell", ["shoulders"], ["triceps"], 90),
    _ex("split_squat", "Bulgarian Split Squat", "dumbbell", ["quads", "glutes"], ["hamstrings", "core"], 90, lower=True),
    _ex("walking_lunge", "Walking Lunge", "dumbbell", ["quads", "glutes"], ["hamstrings", "calves"], 90, lower=True),
    _ex("step_up", "Step-Up", "dumbbell", ["quads", "glutes"], ["calves"], 90, lower=True),
    _ex("curl", "Dumbbell Curl", "dumbbell", ["biceps"], ["forearms"], 60),
    _ex("hammer_curl", "Hammer Curl", "dumbbell", ["biceps", "forearms"], [], 60),
    _ex("lateral_raise", "Lateral Raise", "dumbbell", ["shoulders"], [], 60),
    _ex("kb_swing", "Kettlebell Swing", "dumbbell", ["glutes", "hamstrings"], ["core", "back"], 90, lower=True),
    _ex("farmer_carry", "Farmer's Carry", "dumbbell", ["forearms", "core"], ["back"], 90),
    # machines & cables
    _ex("lat_pulldown", "Lat Pulldown", "machine", ["lats"], ["biceps"], 90),
    _ex("cable_row", "Seated Cable Row", "machine", ["back", "lats"], ["biceps"], 90),
    _ex("leg_press", "Leg Press", "machine", ["quads", "glutes"], ["hamstrings"], 120, 10.0, 5.0, lower=True),
    _ex("leg_curl", "Leg Curl", "machine", ["hamstrings"], [], 75, lower=True),
    _ex("leg_extension", "Leg Extension", "machine", ["quads"], [], 75, lower=True),
    _ex("calf_raise", "Calf Raise", "machine", ["calves"], [], 60, lower=True),
    _ex("triceps_pushdown", "Triceps Pushdown", "machine", ["triceps"], [], 60),
    _ex("face_pull", "Face Pull", "machine", ["rotator_cuff", "shoulders"], ["back"], 60, 2.5, 1.25, tags=["tennis"]),
    _ex("pallof_press", "Pallof Press", "machine", ["core"], [], 60, 2.5, 1.25, tags=["tennis"]),
    # bodyweight & bands (no load; progress by reps)
    _ex("pull_up", "Pull-Up", "bodyweight", ["lats", "back"], ["biceps"], 120),
    _ex("chin_up", "Chin-Up", "bodyweight", ["lats", "biceps"], ["back"], 120),
    _ex("dip", "Dip", "bodyweight", ["triceps", "chest"], ["shoulders"], 120),
    _ex("push_up", "Push-Up", "bodyweight", ["chest"], ["triceps", "shoulders", "core"], 60),
    _ex("plank", "Plank (seconds)", "bodyweight", ["core"], [], 60),
    _ex("side_plank", "Side Plank (seconds)", "bodyweight", ["core"], [], 45, tags=["tennis"]),
    _ex("dead_bug", "Dead Bug", "bodyweight", ["core"], [], 45),
    _ex("hanging_knee_raise", "Hanging Knee Raise", "bodyweight", ["core"], ["forearms"], 60),
    _ex("band_external_rotation", "Band External Rotation", "bodyweight", ["rotator_cuff"], [], 45, tags=["tennis"]),
    _ex("ytw_raise", "Y-T-W Raise", "bodyweight", ["rotator_cuff", "shoulders"], ["back"], 45, tags=["tennis"]),
    _ex("med_ball_throw", "Rotational Med-Ball Throw", "bodyweight", ["core"], ["shoulders", "glutes"], 60, tags=["tennis"]),
    _ex("lateral_bound", "Lateral Bound", "bodyweight", ["glutes", "quads"], ["calves"], 60, tags=["tennis"], lower=True),
    _ex("wrist_roller", "Wrist Curl", "dumbbell", ["forearms"], [], 45, 2.5, 1.0, tags=["tennis"]),
]
BY_ID = {e["id"]: e for e in LIBRARY}


def lookup(eid: str, custom: list[dict] | None = None) -> dict | None:
    return BY_ID.get(eid) or next((c for c in custom or [] if c["id"] == eid), None)


# --------------------------------------------------------------------------- templates
LB = 0.45359237


def _kg(lb_value: float, kg_value: float, system: str) -> float:
    """A template amount given as round numbers in both systems, stored in kg."""
    return round(lb_value * LB, 6) if system == "us" else float(kg_value)


def _item(ex: str, sets: int, reps: int, rest: int | None = None, rest_fail: int | None = None, warmup: bool | None = None,
          trim_near_match: bool = False) -> dict:
    e = BY_ID[ex]
    return dict(exercise_id=ex, sets=sets, reps=reps, rest_s=rest or e["rest_s"], rest_fail_s=rest_fail or 180,
                warmup=e["load"] == "barbell" if warmup is None else warmup, trim_near_match=trim_near_match)


def _lift(ex: str, system: str, start_lb: float | None = None, start_kg: float | None = None, inc_lb: float | None = None,
          inc_kg: float | None = None) -> dict:
    e = BY_ID[ex]
    bar = (45.0, 20.0)
    if e["load"] == "barbell":
        s = (start_lb or bar[0], start_kg or bar[1])
    elif e["load"] == "bodyweight":
        s = (0.0, 0.0)
    else:
        s = (start_lb or 20.0, start_kg or 8.0)
    return dict(start_kg=_kg(s[0], s[1], system), increment_kg=_kg(inc_lb or e["inc_lb"], inc_kg or e["inc_kg"], system),
                deload_pct=10, fail_limit=3)


TEMPLATES = {
    "5x5": dict(
        name="5×5 Strength (A/B)",
        description="Two alternating barbell workouts, three days a week. Add weight every session you complete every rep.",
        weekdays=[0, 2, 4],
        workouts=[("A", "Workout A", [_item("squat", 5, 5), _item("bench", 5, 5), _item("row", 5, 5)]),
                  ("B", "Workout B", [_item("squat", 5, 5), _item("ohp", 5, 5), _item("deadlift", 1, 5)])],
        lifts=["squat", "bench", "row", "ohp", "deadlift"]),
    "beginner3": dict(
        name="Beginner Full Body (3 days)",
        description="Dumbbell and machine basics for the first months: moderate reps, small jumps, lots of practice.",
        weekdays=[0, 2, 4],
        workouts=[("A", "Full body A", [_item("goblet_squat", 3, 10), _item("db_bench", 3, 10), _item("db_row", 3, 10),
                                        _item("plank", 3, 30, 45, 60, False)]),
                  ("B", "Full body B", [_item("db_rdl", 3, 10), _item("db_press", 3, 10), _item("lat_pulldown", 3, 10),
                                        _item("dead_bug", 3, 10, 45, 60, False)])],
        lifts=["goblet_squat", "db_bench", "db_row", "plank", "db_rdl", "db_press", "lat_pulldown", "dead_bug"]),
    "tennis": dict(
        name="Tennis Strength (2 days)",
        description="Lower-volume strength for players: single-leg work, hinges, pulling and shoulder care. Leg work is "
                    "trimmed on days next to a match or tennis session in your training plan.",
        weekdays=[1, 4],
        workouts=[("A", "Strength A", [_item("trap_bar_deadlift", 3, 5, trim_near_match=True), _item("split_squat", 3, 8, trim_near_match=True),
                                       _item("db_press", 3, 8), _item("face_pull", 2, 15, 60, 90, False),
                                       _item("band_external_rotation", 2, 15, 45, 60, False)]),
                  ("B", "Strength B", [_item("goblet_squat", 3, 8, trim_near_match=True), _item("db_row", 3, 10), _item("push_up", 3, 12, 60, 90, False),
                                       _item("ytw_raise", 2, 10, 45, 60, False), _item("med_ball_throw", 3, 6, 60, 90, False),
                                       _item("side_plank", 2, 30, 45, 60, False)])],
        lifts=["trap_bar_deadlift", "split_squat", "db_press", "face_pull", "band_external_rotation", "goblet_squat", "db_row",
               "push_up", "ytw_raise", "med_ball_throw", "side_plank"]),
}


STARTS = {"deadlift": (95.0, 40.0), "row": (65.0, 30.0), "trap_bar_deadlift": (95.0, 40.0)}  # lifts pulled from the floor start with plates on


def from_template(key: str, system: str, pid: str) -> dict:
    t = TEMPLATES[key]
    return dict(id=pid, name=t["name"], description=t["description"], template=key, active=False,
                schedule=dict(weekdays=list(t["weekdays"])),
                workouts=[dict(key=k, name=n, exercises=[dict(x) for x in items]) for k, n, items in t["workouts"]],
                lifts={ex: _lift(ex, system, *STARTS.get(ex, (None, None))) for ex in t["lifts"]}, adjustments=[])
