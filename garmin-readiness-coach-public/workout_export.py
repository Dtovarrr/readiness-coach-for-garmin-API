"""Workout files for the watch and other tools, and the copy onto a USB-mounted Garmin.

FIT is written with fit-tool and always read back with fitdecode (a separate decoder) before it's
saved: if the decoded steps don't match the workout, nothing is written. Two FIT details the library
leaves to us: a custom heart-rate target is bpm + 100 (0-100 means %HRmax), and a repeat is a step
after its block that points back at the block's first step.

TCX (Garmin's older XML) is for tools that don't read FIT workouts. Its schema caps a workout at 20
step ids and names at 15 characters, so longer workouts are FIT only.

The device copy never deletes or overwrites anything: it only writes a new file into a workouts
folder that already exists on the watch.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import fitdecode
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
from fit_tool.profile.profile_type import (FileType, Intensity, Manufacturer, Sport, SubSport, WorkoutStepDuration,
                                           WorkoutStepTarget)

import workouts

HR_OFFSET = 100  # FIT: custom HR values above 100 are bpm + 100
INTENSITY = {"warmup": Intensity.WARMUP, "work": Intensity.ACTIVE, "recovery": Intensity.RECOVERY, "rest": Intensity.REST,
             "cooldown": Intensity.COOLDOWN}
SPORT = {"run": (Sport.RUNNING, SubSport.GENERIC), "ride": (Sport.CYCLING, SubSport.GENERIC),
         "strength": (Sport.TRAINING, SubSport.STRENGTH_TRAINING), "timed": (Sport.TRAINING, SubSport.CARDIO_TRAINING)}
TCX_SPORT = {"run": "Running", "ride": "Biking"}
TCX_MAX_STEPS, TCX_NAME = 20, 15
TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


class ExportError(RuntimeError):
    pass


def step_name(s: dict) -> str:
    return s.get("name") or workouts.KIND_NAMES[s["kind"]]


def filename(w: dict, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", w["name"]).strip("-")[:40] or "workout"
    return f"{slug}.{ext}"


# ------------------------------------- FIT ------------------------------------ #
def _fit_step(s: dict) -> WorkoutStepMessage:
    m = WorkoutStepMessage()
    m.message_index = s["index"]
    if s["kind"] == "repeat":
        m.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
        m.duration_step = s["back_to"]
        m.target_type = WorkoutStepTarget.OPEN
        m.target_repeat_steps = s["count"]
        return m
    m.workout_step_name = step_name(s)
    m.intensity = INTENSITY[s["kind"]]
    d = s["dur"]
    if d["type"] == "time":
        m.duration_type = WorkoutStepDuration.TIME
        m.duration_time = float(d["sec"])
    elif d["type"] == "distance":
        m.duration_type = WorkoutStepDuration.DISTANCE
        m.duration_distance = float(d["m"])
    else:
        m.duration_type = WorkoutStepDuration.OPEN
        m.duration_value = 0
    t = s["target"]  # target_type first: the custom-value fields are sub-fields chosen by it
    if t["type"] == "hr_zone":
        m.target_type = WorkoutStepTarget.HEART_RATE
        m.target_hr_zone = t["zone"]
    elif t["type"] == "hr_bpm":
        m.target_type = WorkoutStepTarget.HEART_RATE
        m.target_value = 0
        m.custom_target_heart_rate_low = t["lo"] + HR_OFFSET
        m.custom_target_heart_rate_high = t["hi"] + HR_OFFSET
    elif t["type"] == "pace":
        m.target_type = WorkoutStepTarget.SPEED
        m.target_value = 0
        m.custom_target_speed_low = float(t["lo"])
        m.custom_target_speed_high = float(t["hi"])
    else:
        m.target_type = WorkoutStepTarget.OPEN
        m.target_value = 0
    if s.get("notes"):
        m.notes = s["notes"]
    return m


def to_fit(w: dict) -> bytes:
    """The workout as a FIT workout file, verified by reading it back. Raises ExportError."""
    steps = workouts.fit_steps(w)
    fid = FileIdMessage()
    fid.type = FileType.WORKOUT
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.time_created = round(time.time() * 1000)
    fid.serial_number = int(hashlib.sha1(w["id"].encode()).hexdigest()[:8], 16) or 1
    wm = WorkoutMessage()
    wm.workout_name = w["name"][:50]
    wm.sport, wm.sub_sport = SPORT[w["sport"]]
    wm.num_valid_steps = len(steps)
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    b.add(fid)
    b.add(wm)
    b.add_all([_fit_step(s) for s in steps])
    data = b.build().to_bytes()
    check(w, read_back(data))
    return data


def read_back(data: bytes) -> dict:
    """Decode a FIT workout with fitdecode (CRC checked): file type, sport, name and the steps."""
    out = dict(file_type=None, sport=None, sub_sport=None, name=None, num_valid_steps=None, steps=[])
    try:
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.ENABLED) as fr:
            for f in fr:
                if not isinstance(f, fitdecode.FitDataMessage):
                    continue
                v = {fd.name: fd.value for fd in f.fields if fd.value is not None}
                if f.name == "file_id":
                    out["file_type"] = v.get("type")
                elif f.name == "workout":
                    out.update(sport=v.get("sport"), sub_sport=v.get("sub_sport"), name=v.get("wkt_name"), num_valid_steps=v.get("num_valid_steps"))
                elif f.name == "workout_step":
                    hl, hh = v.get("custom_target_heart_rate_low"), v.get("custom_target_heart_rate_high")
                    out["steps"].append(dict(
                        index=v.get("message_index"), name=v.get("wkt_step_name"), intensity=v.get("intensity"),
                        duration_type=v.get("duration_type"), sec=v.get("duration_time"), m=v.get("duration_distance"),
                        back_to=v.get("duration_step"), count=v.get("repeat_steps"), target_type=v.get("target_type"),
                        hr_zone=v.get("target_hr_zone"), hr_lo=hl - HR_OFFSET if hl else None, hr_hi=hh - HR_OFFSET if hh else None,
                        speed_lo=v.get("custom_target_speed_low"), speed_hi=v.get("custom_target_speed_high"), notes=v.get("notes")))
    except fitdecode.FitError as ex:
        raise ExportError(f"The FIT file didn't decode ({ex}).") from ex
    return out


FIT_INTENSITY = {"warmup": "warmup", "work": "active", "recovery": "recovery", "rest": "rest", "cooldown": "cooldown"}


def check(w: dict, back: dict) -> None:
    """Raise ExportError unless the decoded file matches the workout step for step."""
    want = workouts.fit_steps(w)

    def bad(i, what):
        raise ExportError(f"Read-back check failed at step {i + 1}: {what}.")
    if back["file_type"] != "workout":
        raise ExportError("Read-back check failed: the file isn't a workout file.")
    if back["name"] != w["name"][:50] or back["num_valid_steps"] != len(want) or len(back["steps"]) != len(want):
        raise ExportError("Read-back check failed: name or step count differs.")
    for i, (s, g) in enumerate(zip(want, back["steps"])):
        if g["index"] != s["index"]:
            bad(i, "order")
        if s["kind"] == "repeat":
            if g["duration_type"] != "repeat_until_steps_cmplt" or g["back_to"] != s["back_to"] or g["count"] != s["count"]:
                bad(i, "repeat")
            continue
        if g["intensity"] != FIT_INTENSITY[s["kind"]] or g["name"] != step_name(s):
            bad(i, "step type or name")
        d = s["dur"]
        if d["type"] == "time" and (g["duration_type"] != "time" or abs((g["sec"] or 0) - d["sec"]) > 0.01):
            bad(i, "duration")
        if d["type"] == "distance" and (g["duration_type"] != "distance" or abs((g["m"] or 0) - d["m"]) > 0.01):
            bad(i, "distance")
        if d["type"] == "open" and g["duration_type"] != "open":
            bad(i, "open duration")
        t = s["target"]
        ok = {"open": lambda: g["target_type"] == "open",
              "hr_zone": lambda: g["target_type"] == "heart_rate" and g["hr_zone"] == t.get("zone"),
              "hr_bpm": lambda: g["target_type"] == "heart_rate" and (g["hr_lo"], g["hr_hi"]) == (t.get("lo"), t.get("hi")),
              "pace": lambda: g["target_type"] == "speed" and abs((g["speed_lo"] or 0) - t.get("lo", 0)) <= 0.001
              and abs((g["speed_hi"] or 0) - t.get("hi", 0)) <= 0.001}[t["type"]]
        if not ok():
            bad(i, "target")


# ------------------------------------- TCX ------------------------------------ #
def _tcx_step(parent, s: dict, sid: int):
    st = ET.SubElement(parent, "Step" if parent.tag.endswith("Workout") else "Child", {f"{{{XSI}}}type": "Step_t"})
    ET.SubElement(st, "StepId").text = str(sid)
    ET.SubElement(st, "Name").text = step_name(s)[:TCX_NAME]
    d = s["dur"]
    if d["type"] == "time":
        ET.SubElement(ET.SubElement(st, "Duration", {f"{{{XSI}}}type": "Time_t"}), "Seconds").text = str(int(d["sec"]))
    elif d["type"] == "distance":
        ET.SubElement(ET.SubElement(st, "Duration", {f"{{{XSI}}}type": "Distance_t"}), "Meters").text = str(round(d["m"]))
    else:
        ET.SubElement(st, "Duration", {f"{{{XSI}}}type": "UserInitiated_t"})
    ET.SubElement(st, "Intensity").text = "Resting" if s["kind"] in ("rest", "recovery") else "Active"
    t = s["target"]
    if t["type"] == "hr_zone":
        tg = ET.SubElement(st, "Target", {f"{{{XSI}}}type": "HeartRate_t"})
        ET.SubElement(ET.SubElement(tg, "HeartRateZone", {f"{{{XSI}}}type": "PredefinedHeartRateZone_t"}), "Number").text = str(t["zone"])
    elif t["type"] == "hr_bpm":
        z = ET.SubElement(ET.SubElement(st, "Target", {f"{{{XSI}}}type": "HeartRate_t"}), "HeartRateZone", {f"{{{XSI}}}type": "CustomHeartRateZone_t"})
        for tag, v in (("Low", t["lo"]), ("High", t["hi"])):
            ET.SubElement(ET.SubElement(z, tag, {f"{{{XSI}}}type": "HeartRateInBeatsPerMinute_t"}), "Value").text = str(v)
    elif t["type"] == "pace":
        z = ET.SubElement(ET.SubElement(st, "Target", {f"{{{XSI}}}type": "Speed_t"}), "SpeedZone", {f"{{{XSI}}}type": "CustomSpeedZone_t"})
        ET.SubElement(z, "LowInMetersPerSecond").text = f"{t['lo']:.3f}"
        ET.SubElement(z, "HighInMetersPerSecond").text = f"{t['hi']:.3f}"
    else:
        ET.SubElement(st, "Target", {f"{{{XSI}}}type": "None_t"})


def tcx_step_count(w: dict) -> int:
    return sum(1 + len(s["steps"]) if s["kind"] == "repeat" else 1 for s in w["steps"])


def to_tcx(w: dict) -> bytes:
    """TCX v2 workout. Raises ExportError when the workout is too long for the format."""
    if tcx_step_count(w) > TCX_MAX_STEPS:
        raise ExportError(f"TCX allows {TCX_MAX_STEPS} steps; this workout has {tcx_step_count(w)}. Use the .fit file.")
    ET.register_namespace("", TCX_NS)
    ET.register_namespace("xsi", XSI)
    root = ET.Element(f"{{{TCX_NS}}}TrainingCenterDatabase")
    wk = ET.SubElement(ET.SubElement(root, f"{{{TCX_NS}}}Workouts"), f"{{{TCX_NS}}}Workout", Sport=TCX_SPORT.get(w["sport"], "Other"))
    ET.SubElement(wk, f"{{{TCX_NS}}}Name").text = re.sub(r"[^A-Za-z0-9 ]", "", w["name"])[:TCX_NAME].strip() or "Workout"
    sid = 0
    for s in w["steps"]:
        sid += 1
        if s["kind"] == "repeat":
            rp = ET.SubElement(wk, "Step", {f"{{{XSI}}}type": "Repeat_t"})
            ET.SubElement(rp, "StepId").text = str(sid)
            ET.SubElement(rp, "Repetitions").text = str(min(99, s["count"]))
            for c in s["steps"]:
                sid += 1
                _tcx_step(rp, c, sid)
        else:
            _tcx_step(wk, s, sid)
    if w.get("scheduled"):
        ET.SubElement(wk, "ScheduledOn").text = w["scheduled"]
    if w.get("notes"):
        ET.SubElement(wk, "Notes").text = w["notes"]
    for el in root.iter():  # children were added without the namespace for brevity; put them in it
        if not el.tag.startswith("{"):
            el.tag = f"{{{TCX_NS}}}{el.tag}"
    data = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    _check_tcx(w, data)
    return data


def _check_tcx(w: dict, data: bytes) -> None:
    root = ET.fromstring(data)
    ns = {"t": TCX_NS}
    top = root.findall("t:Workouts/t:Workout/t:Step", ns)
    ids = [int(e.text) for e in root.iter(f"{{{TCX_NS}}}StepId")]
    if len(top) != len(w["steps"]) or ids != list(range(1, tcx_step_count(w) + 1)):
        raise ExportError("TCX read-back check failed: step count or order differs.")


# ----------------------------------- device ----------------------------------- #
DEVICE_FOLDERS = ("NewFiles", "Workouts")  # NewFiles: the watch imports it on unplug; Workouts: older devices


def _child(p: Path, name: str) -> Path | None:
    """`name` inside `p`, matched case-insensitively (FAT volumes keep whatever case Garmin used)."""
    try:
        return next((c for c in p.iterdir() if c.name.lower() == name.lower() and c.is_dir()), None)
    except OSError:
        return None


def find_devices(volumes: Path = Path("/Volumes")) -> list[dict]:
    """Mounted volumes with a GARMIN folder, and which folder a workout would be copied into."""
    out = []
    try:
        mounts = sorted(p for p in volumes.iterdir() if p.is_dir())
    except OSError:
        return out
    for m in mounts:
        g = _child(m, "GARMIN")
        if not g:
            continue
        dest = next((d for d in (_child(g, f) for f in DEVICE_FOLDERS) if d), None)
        out.append(dict(name=m.name, mount=str(m), folder=str(dest) if dest else None,
                        note=None if dest else "No NewFiles or Workouts folder on this device; use the manual steps."))
    return out


def install(src: Path, device: dict, confirm: bool) -> dict:
    """Copy `src` into the device's workouts folder. Without confirm, only says where it would go.
    Never overwrites: an existing identical file counts as done, a different one gets a new name."""
    if not device.get("folder"):
        raise ExportError(device.get("note") or "No workouts folder on that device.")
    folder = Path(device["folder"])
    if not folder.is_dir() or Path(device["mount"]) not in folder.parents:
        raise ExportError("The device folder isn't there any more. Is the watch still connected?")
    data = src.read_bytes()
    dest = folder / src.name
    n = 2
    while dest.exists():
        try:
            if dest.read_bytes() == data:
                return dict(path=str(dest), copied=False, already=True)
        except OSError:
            pass
        dest = folder / f"{src.stem}-{n}{src.suffix}"
        n += 1
    if not confirm:
        return dict(path=str(dest), copied=False, already=False)
    with open(dest, "xb") as f:  # "x": fails rather than replace anything
        f.write(data)
        f.flush()
    return dict(path=str(dest), copied=True, already=False)
