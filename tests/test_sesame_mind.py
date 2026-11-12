"""Sesame mind skeleton: specs, library, memory, reflexes and ears, without Claude/Jev/robot."""

import array
import io
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sesame_mind as sm  # noqa: E402
from sim.manifest import load_manifest  # noqa: E402

M = load_manifest()
OSC = {"kind": "osc", "base": "stand", "hz": 1.0, "seconds": 2,
       "joints": [{"joint": "R1", "amp": 20, "offset": 5, "phase": 0.25}]}
KEYS = {"kind": "keys", "base": "rest",
        "keys": [{"ms": 1000, "angles": [{"joint": "L1", "deg": 40}]}, {"ms": 1000, "angles": []}]}


def stand_fw():
    return [M.raw["poses"]["stand"][j.name] for j in M.joints]


def test_osc_frames_follow_the_sine_around_the_base():
    poses = list(sm.frames(M, OSC, hz=4, intensity=1.0))
    assert len(poses) == 9  # 2 s at 4 Hz, both ends
    r1 = [j.name for j in M.joints].index("R1")
    j = M.joint("R1")
    for i, p in enumerate(poses):
        q = math.radians(5 + 20 * math.sin(2 * math.pi * (i / 4 + 0.25)))
        want = M.to_firmware(j, M.pose_internal("stand")["R1"] + q)
        assert p[r1] == pytest.approx(min(max(want, j.limits_deg[0]), j.limits_deg[1]), abs=0.051)
    others = [k for k in range(8) if k != r1]
    assert all([p[k] for k in others] == [stand_fw()[k] for k in others] for p in poses)


def test_frames_clamp_to_manifest_soft_limits():
    wild = {"kind": "osc", "base": "rest", "hz": 1, "seconds": 1,
            "joints": [{"joint": j.name, "amp": 300, "offset": 0, "phase": 0} for j in M.joints]}
    for p in sm.frames(M, wild, intensity=1.0):
        for deg, j in zip(p, M.joints):
            assert j.limits_deg[0] <= deg <= j.limits_deg[1]


def test_keys_interpolate_then_return_and_intensity_scales():
    assert sm.duration(KEYS) == 2.0
    full = sm.angles(M, KEYS, 0.5)
    assert full["L1"] == pytest.approx(20)  # halfway to 40
    assert sm.angles(M, KEYS, 1.0)["L1"] == pytest.approx(40)
    assert sm.angles(M, KEYS, 1.5)["L1"] == pytest.approx(20)  # back toward base
    assert sm.angles(M, KEYS, 0.5, scale=0.5)["L1"] == pytest.approx(10)
    assert sm.angles(M, KEYS, 5.0) == {}


def test_validate_rejects_bad_specs():
    sm.validate(M, OSC)
    sm.validate(M, KEYS)
    for bad in ({**OSC, "joints": [{"joint": "Z9", "amp": 1}]}, {**OSC, "base": "moon"},
                {**OSC, "seconds": 60}, {"kind": "dance"}):
        with pytest.raises(ValueError):
            sm.validate(M, bad)


def test_library_seeds_firmware_motions_and_persists_learned(tmp_path):
    path = tmp_path / "library.json"
    lib = sm.Library(path)
    assert "wave" in lib.names() and lib.items["wave"]["spec"] == {"kind": "firmware", "name": "wave"}
    lib.add("wiggle", "くねくね", OSC)
    assert set(json.loads(path.read_text())) == {"wiggle"}  # built-ins are not written
    assert sm.Library(path).items["wiggle"]["spec"] == OSC


def test_memory_roundtrip_and_notes(tmp_path):
    mem = sm.Memory(tmp_path)
    mem.add({"t": 1, "text": "a"})
    mem.add({"t": 2, "text": "b"})
    mem.notes = "- 手を振るのが好き"
    again = sm.Memory(tmp_path)
    assert [e["text"] for e in again.recent(1)] == ["b"]
    assert again.notes == "- 手を振るのが好き"
    assert sm.Memory().notes == ""


def test_rule_reflex():
    lib, r = sm.Library(), sm.RuleReflex()
    assert r.decide(sm.Moment("chat", "walk_backward して"), lib).action == "walk_backward"
    assert r.decide(sm.Moment("chat", "猫みたいな動きを作って"), lib).action == sm.NEW
    assert r.decide(sm.Moment("radio", "作って"), lib).action == sm.NONE  # only chat invents
    assert r.decide(sm.Moment("sound", loudness=4000, onset=True), lib).action == "shake"


def test_jev_request_and_tolerant_parse():
    lib = sm.Library()
    body = sm.jev_request(sm.Moment("voice", "こんにちは", 900.0), lib, "notes")
    qs = body["questions"]
    assert body["model"] == "jev-latest" and json.loads(body["state"])["heard"] == "こんにちは"
    assert qs["action"]["options"][-2:] == [sm.NEW, sm.NONE] and len(qs["action"]["options"]) <= 255
    assert qs["intensity"]["criteria"] == list(sm.INTENSITY)
    opts = qs["action"]["options"]
    d = sm.jev_decision({"answers": {"action": {"answer": "wave", "confidence": 0.9},
                                     "intensity": {"score": 1.5}, "addressed": {"probability": 0.8}}}, opts)
    assert (d.action, d.intensity, d.addressed, d.confidence) == ("wave", 0.5, 0.8, 0.9)
    d = sm.jev_decision({"action": {"selected": "moonwalk"}}, opts)
    assert (d.action, d.intensity, d.addressed) == (sm.NONE, 0.5, None)


def test_design_schema_and_spec_of():
    schema = sm.design_schema(M)
    assert schema["properties"]["joints"]["items"]["properties"]["joint"]["enum"] == [j.name for j in M.joints]
    design = {"name": "wiggle", "about": "くねくね", **OSC, "keys": []}
    assert sm.spec_of(design) == ("wiggle", "くねくね", OSC)
    assert sm.spec_of({"name": "n", "about": "a", **KEYS, "hz": 0, "seconds": 0, "joints": []})[2] == KEYS


class FakeSpine:
    manifest = M

    def __init__(self):
        self.played = []

    def start(self, entry, intensity):
        self.played.append((entry["spec"], intensity))


class FakeDesigner:
    def __init__(self, spec=OSC):
        self.spec = spec

    def design(self, moment, library, notes=""):
        return "wiggle", "くねくね", self.spec

    def ruminate(self, recent, notes=""):
        return f"- {len(recent)} things happened"


class BrokenReflex:
    by = "jev"

    def decide(self, *a):
        raise OSError("no route to host")


def test_mind_invents_learns_and_replays(tmp_path):
    spine = FakeSpine()
    mind = sm.Mind(sm.RuleReflex(), sm.Library(tmp_path / "lib.json"), sm.Memory(tmp_path), spine,
                   FakeDesigner(), clock=lambda: 7.0)
    entry = mind.handle(sm.Moment("chat", "くねくねする動きを作って"))
    assert entry["did"] == "wiggle" and "new motion" in entry["note"] and entry["t"] == 7.0
    assert spine.played == [(OSC, 0.6)]
    assert mind.handle(sm.Moment("chat", "wiggle もう一回"))["did"] == "wiggle"  # no designer needed now
    mind.ruminate()
    assert mind.memory.notes == "- 2 things happened"


def test_mind_survives_bad_designs_and_reflex_outages(tmp_path):
    spine = FakeSpine()
    mind = sm.Mind(BrokenReflex(), sm.Library(), sm.Memory(), spine, FakeDesigner({"kind": "dance"}))
    assert mind.handle(sm.Moment("chat", "wave"))["by"] == "rules"
    entry = mind.handle(sm.Moment("chat", "新しいのを作って"))
    assert entry["did"] == sm.NONE and "design failed" in entry["note"]
    assert len(spine.played) == 1


def test_spine_dry_run_and_streaming():
    sent = []

    class FakeRobot:
        def command(self, text):
            sent.append(text)
            return True, "OK"

    sm.Spine(M).play({"spec": OSC})  # dry: prints, sends nothing
    assert sm.Spine(M, FakeRobot(), hz=4, sleep=lambda s: None).play({"spec": OSC}) == 0
    assert len(sent) == 9 and all(s.startswith("pose ") and len(s.split()) == 9 for s in sent)


def tone(level, seconds):
    n = int(sm.RATE * seconds)
    return array.array("h", [int(level * math.sqrt(2) * math.sin(i / 3)) for i in range(n)]).tobytes()


def test_utterances_split_on_silence_and_flag_onsets():
    audio = tone(50, 1.0) + tone(5000, 0.5) + tone(50, 1.0) + tone(1000, 0.3) + tone(50, 1.0)
    events = list(sm.utterances(io.BytesIO(audio)))
    speech = [v for k, v in events if k == "speech"]
    sounds = [v for k, v in events if k == "sound"]
    assert len(speech) == 2
    assert len(speech[0]) >= 0.5 * sm.RATE * 2  # whole loud stretch plus its silent tail
    assert sounds == [pytest.approx(5000, rel=0.05)]  # one per bang; the 1000 RMS voice is not one
