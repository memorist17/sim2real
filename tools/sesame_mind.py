#!/usr/bin/env python3
"""Sesame mind (skeleton): sound and chat in, new motions out, with memory.

    ears ──┬─ body: loudness / onset (no model, ms) ────────────┐
           └─ whisper: utterance text ──┐                       ▼
    chat page / stdin ──────────────────┴─▶ reflex ─▶ library ─▶ spine ─▶ robot
                                             │ "new"     ▲
                                             ▼           │
                                         designer (Claude) ┘
    memory.jsonl ◀─ every moment;   idle ─▶ ruminate (Claude) ─▶ notes.md

The reflex picks a library entry, an intensity and whether the speech was
addressed to the robot. Reflexes: `rules` (offline keywords, default) or `jev`
(TypeSafe System One; point --jev-url at a local-jev server for the edge). The
designer (Claude) only runs for new motions; it writes a small spec that the
spine expands into `pose` frames, so a learned motion replays without any model.

Specs are angles in internal degrees relative to a manifest pose (`base`):

    {"kind": "osc", "base": "stand", "hz": 1.5, "seconds": 4,
     "joints": [{"joint": "R1", "amp": 20, "offset": 0, "phase": 0.25}, ...]}
    {"kind": "keys", "base": "stand", "keys": [{"ms": 400, "angles": [{"joint": "L1", "deg": 30}]}]}
    {"kind": "firmware", "name": "wave"}

Frames are clamped to the manifest soft limits; the firmware applies its own
clamps and rate limit on top. Without --host nothing moves (dry run).

    uv run --group radio python tools/sesame_mind.py --text
    uv run --group radio python tools/sesame_mind.py --serve 8765 --host sesame-s3-8b2c.local
    uv run --group radio python tools/sesame_mind.py --mic --whisper-model ggml-small.bin \\
        --serve 8765 --reflex jev --host sesame-s3-8b2c.local
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from radio_paranoia import RATE, offer, rms, source_command, transcribe  # noqa: E402
from sesame_remote import Robot, run_lines, start_heartbeat  # noqa: E402
from sim.manifest import load_manifest  # noqa: E402

FRAME_HZ = 20  # pose frames per second over Wi-Fi
INTENSITY = ("still", "gentle", "lively", "wild")
NEW, NONE = "new", "none"


# ---------------------------------------------------------------- moments


@dataclass(frozen=True)
class Moment:
    """One thing the robot noticed. Text may be empty (a bare sound)."""
    source: str  # chat | voice | radio | sound
    text: str = ""
    loudness: float = 0.0  # int16 RMS
    onset: bool = False  # sudden jump in loudness


@dataclass(frozen=True)
class Decision:
    action: str  # library name, NEW or NONE
    intensity: float = 0.5  # 0..1
    addressed: float | None = None  # probability the speech was meant for the robot
    confidence: float | None = None
    by: str = ""


# ---------------------------------------------------------------- specs


def angles(manifest, spec, t, scale=1.0):
    """Internal degrees per joint at time t (s) relative to the spec's base pose."""
    if spec["kind"] == "osc":
        return {j["joint"]: scale * (j.get("offset", 0.0)
                                     + j["amp"] * math.sin(2 * math.pi * (spec["hz"] * t + j.get("phase", 0.0))))
                for j in spec["joints"]}
    keys, start = spec["keys"], 0.0
    prev = {}
    for key in keys:
        target = {a["joint"]: a["deg"] for a in key["angles"]}
        end = start + key["ms"] / 1000
        if t < end:
            u = (t - start) / (end - start) if end > start else 1.0
            names = set(prev) | set(target)
            return {n: scale * (prev.get(n, 0.0) + u * (target.get(n, 0.0) - prev.get(n, 0.0))) for n in names}
        prev, start = target, end
    return {n: scale * d for n, d in prev.items()}


def duration(spec):
    if spec["kind"] == "osc":
        return float(spec["seconds"])
    return sum(k["ms"] for k in spec["keys"]) / 1000


def frames(manifest, spec, hz=FRAME_HZ, intensity=0.5):
    """`pose` angles (firmware degrees, canonical order, soft-limit clamped) per frame."""
    base = manifest.pose_internal(spec.get("base", "stand"))
    scale = 0.3 + 0.7 * intensity
    for i in range(int(duration(spec) * hz) + 1):
        delta = angles(manifest, spec, i / hz, scale)
        out = []
        for j in manifest.joints:
            deg = manifest.to_firmware(j, base[j.name] + math.radians(delta.get(j.name, 0.0)))
            out.append(round(min(max(deg, j.limits_deg[0]), j.limits_deg[1]), 1))
        yield out


def validate(manifest, spec):
    """Raise ValueError for specs the spine cannot play."""
    names = {j.name for j in manifest.joints}
    kind = spec.get("kind")
    if kind == "firmware":
        return
    if spec.get("base", "stand") not in manifest.raw["poses"]:
        raise ValueError(f"unknown base pose {spec.get('base')}")
    used = ([j["joint"] for j in spec["joints"]] if kind == "osc" else
            [a["joint"] for k in spec["keys"] for a in k["angles"]] if kind == "keys" else None)
    if used is None:
        raise ValueError(f"unknown spec kind {kind}")
    if unknown := set(used) - names:
        raise ValueError(f"unknown joints {sorted(unknown)}")
    if not 0 < duration(spec) <= 30:
        raise ValueError("duration must be in (0, 30] s")


# ---------------------------------------------------------------- library and memory


def firmware_motions():
    return list(yaml.safe_load((ROOT / "manifest/motions.yaml").read_text())["motions"])


class Library:
    """Named motions: firmware built-ins plus designed specs (JSON on disk)."""

    def __init__(self, path=None):
        self.path = path
        self.items = {m: {"about": f"built-in {m}", "spec": {"kind": "firmware", "name": m}}
                      for m in firmware_motions()}
        if path and path.exists():
            self.items.update(json.loads(path.read_text()))

    def names(self):
        return list(self.items)

    def menu(self):
        return {n: v["about"] for n, v in self.items.items()}

    def add(self, name, about, spec):
        self.items[name] = {"about": about, "spec": spec}
        if self.path:
            learned = {n: v for n, v in self.items.items() if v["spec"]["kind"] != "firmware"}
            self.path.write_text(json.dumps(learned, ensure_ascii=False, indent=1))


class Memory:
    """Append-only JSONL of moments and what the robot did; notes.md is the rumination."""

    def __init__(self, folder=None):
        self.folder = folder
        self.lines = []
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / "memory.jsonl"
            if log.exists():
                self.lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    def add(self, entry):
        self.lines.append(entry)
        if self.folder:
            with (self.folder / "memory.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, n=20):
        return self.lines[-n:]

    @property
    def notes(self):
        p = self.folder / "notes.md" if self.folder else None
        return p.read_text() if p and p.exists() else ""

    @notes.setter
    def notes(self, text):
        if self.folder:
            (self.folder / "notes.md").write_text(text)


# ---------------------------------------------------------------- reflexes


class RuleReflex:
    """Offline fallback: library names in the text, 作って/new for a new motion, onsets startle."""
    by = "rules"

    def decide(self, moment, library, notes=""):
        text = moment.text.lower()
        if moment.source == "chat" and any(w in text for w in ("作って", "つくって", "new ")):
            return Decision(NEW, 0.6, 1.0, by=self.by)
        for name in sorted(library.names(), key=len, reverse=True):
            if name in text:
                return Decision(name, 0.6, 1.0, by=self.by)
        if moment.onset:
            return Decision("shake", min(1.0, moment.loudness / 8000), by=self.by)
        return Decision(NONE, 0.0, by=self.by)


def jev_request(moment, library, notes, model="jev-latest"):
    """System One request. Field names follow docs.typesafe.ai as of 2026-11; verify with a key."""
    state = {"heard": moment.text, "source": moment.source, "loudness": round(moment.loudness),
             "sudden_sound": moment.onset, "motions": library.menu(), "notes": notes[-1500:]}
    return {
        "model": model,
        "state": json.dumps(state, ensure_ascii=False),
        "questions": {
            "action": {"type": "choice",
                       "question": "Which motion should the robot do now? 'new' = invent one, 'none' = keep still",
                       "options": library.names() + [NEW, NONE]},
            "intensity": {"type": "score", "question": "How energetically should it move?",
                          "criteria": list(INTENSITY)},
            "addressed": {"type": "noul", "question": "Is the speech addressed to the robot itself?"},
        },
    }


def _first(d, *keys):
    return next((d[k] for k in keys if isinstance(d, dict) and k in d), None)


def jev_decision(reply, options):
    """Tolerant parse: the SDK is young and minor versions have renamed fields."""
    answers = _first(reply, "answers", "results") or reply
    action = _first(answers.get("action", {}), "answer", "selected", "option", "value")
    score = _first(answers.get("intensity", {}), "score", "value", "answer")
    yes = _first(answers.get("addressed", {}), "probability", "value", "answer")
    conf = _first(answers.get("action", {}), "confidence")
    return Decision(action if action in options else NONE,
                    min(1.0, max(0.0, float(score) / (len(INTENSITY) - 1))) if score is not None else 0.5,
                    float(yes) if isinstance(yes, (int, float)) else None,
                    float(conf) if isinstance(conf, (int, float)) else None, by="jev")


class JevReflex:
    by = "jev"

    def __init__(self, url, key=None, model="jev-latest", timeout=2.0, post=None):
        self.url, self.key, self.model, self.timeout = url, key, model, timeout
        self.post = post or self._post

    def _post(self, body):
        req = urllib.request.Request(self.url, json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def decide(self, moment, library, notes=""):
        body = jev_request(moment, library, notes, self.model)
        return jev_decision(self.post(body), body["questions"]["action"]["options"])


# ---------------------------------------------------------------- designer (Claude)


DESIGN_SYSTEM = """\
あなたは四足歩行ロボット「セサミ」（8軸サーボ、各脚に hip と foot）の振付師です。
聞こえた言葉や指示から、新しい動きを一つ小さな設計図として作ってください。
角度は基準姿勢（base）からの差分で、内部座標の度数です（0 = 基準のまま）。
- osc: 周期運動。hz は 0.2〜3、seconds は 1〜10、各関節の amp（振幅）、offset、phase（0〜1周期）
- keys: キーポーズの列。各キーの ms は 100〜2000、合計10秒以内
可動範囲を超えた分はコード側で切り詰められます。脚の左右・前後の対称性を意識してください。
name は英小文字とアンダースコア、about は日本語で20字以内。
"""

RUMINATE_SYSTEM = """\
あなたはロボット「セサミ」の記憶です。最近の出来事と以前のメモを読み返し、
セサミの性格、好きな動き、よく聞く言葉、ひとりのときにやりたいことを、
箇条書き15行以内の新しいメモに書き直してください。古いメモの大事な点は残してください。
"""


def design_schema(manifest):
    joint = {"type": "string", "enum": [j.name for j in manifest.joints]}
    num = {"type": "number"}

    def obj(props):
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    return obj({
        "name": {"type": "string"},
        "about": {"type": "string"},
        "kind": {"type": "string", "enum": ["osc", "keys"]},
        "base": {"type": "string", "enum": list(manifest.raw["poses"])},
        "hz": num, "seconds": num,
        "joints": {"type": "array", "items": obj({"joint": joint, "amp": num, "offset": num, "phase": num})},
        "keys": {"type": "array", "items": obj({"ms": num, "angles": {"type": "array",
                                                                     "items": obj({"joint": joint, "deg": num})}})},
    })


def spec_of(design):
    """Designer output -> (name, about, spec); the unused kind's fields are dropped."""
    if design["kind"] == "osc":
        spec = {k: design[k] for k in ("kind", "base", "hz", "seconds", "joints")}
    else:
        spec = {k: design[k] for k in ("kind", "base", "keys")}
    return design["name"], design["about"], spec


class ClaudeDesigner:
    def __init__(self, manifest, model="claude-opus-5-5", effort="low"):
        try:
            import anthropic
        except ImportError:
            sys.exit("sesame_mind: the `anthropic` package is required (uv run --group radio ...)")
        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort
        self.schema = design_schema(manifest)

    def _ask(self, system, user, schema=None):
        fmt = {"format": {"type": "json_schema", "schema": schema}} if schema else {}
        r = self.client.beta.messages.create(
            model=self.model, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=system, output_config={"effort": self.effort, **fmt},
            messages=[{"role": "user", "content": user}])
        if r.stop_reason != "end_turn":
            raise RuntimeError(f"stop_reason={r.stop_reason}")
        return next(b.text for b in r.content if b.type == "text")

    def design(self, moment, library, notes=""):
        user = (f"メモ:\n{notes or '（まだない）'}\n\n既にある動き: {', '.join(library.names())}\n\n"
                f"聞こえたこと（{moment.source}）:「{moment.text}」")
        return spec_of(json.loads(self._ask(DESIGN_SYSTEM, user, self.schema)))

    def ruminate(self, recent, notes=""):
        events = "\n".join(json.dumps(e, ensure_ascii=False) for e in recent)
        return self._ask(RUMINATE_SYSTEM, f"以前のメモ:\n{notes or '（まだない）'}\n\n最近の出来事:\n{events}")


# ---------------------------------------------------------------- spine


class Spine:
    """Plays one library entry at a time; a newer play() interrupts the current one."""

    def __init__(self, manifest, robot=None, hz=FRAME_HZ, sleep=time.sleep):
        self.manifest, self.robot, self.hz, self.sleep = manifest, robot, hz, sleep
        self.cancel = threading.Event()
        self.thread = None

    def start(self, entry, intensity=0.5):
        """Interrupt the current motion and play this one in the background."""
        self.cancel.set()
        if self.thread:
            self.thread.join()
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self._play_logged, args=(entry, intensity), daemon=True)
        self.thread.start()

    def _play_logged(self, entry, intensity):
        try:
            self.play(entry, intensity)
        except OSError as e:
            print(f"  (spine) no link: {e}", file=sys.stderr)

    def play(self, entry, intensity=0.5):
        spec = entry["spec"]
        if spec["kind"] == "firmware":
            lines = [f"motion {spec['name']}"]
            if self.robot is None:
                print("  (dry) " + lines[0], flush=True)
                return 0
            return run_lines(self.robot, lines)
        poses = list(frames(self.manifest, spec, self.hz, intensity))
        if self.robot is None:
            print(f"  (dry) {len(poses)} frames @ {self.hz} Hz, first: pose {' '.join(map(str, poses[0]))}", flush=True)
            return 0
        t0 = time.monotonic()
        for i, p in enumerate(poses):
            if self.cancel.is_set():
                return 0
            ok, text = self.robot.command("pose " + " ".join(f"{x:g}" for x in p))
            if not ok:
                print(f"  (spine) {text}", file=sys.stderr)
                return 1
            self.sleep(max(0.0, t0 + (i + 1) / self.hz - time.monotonic()))
        return 0


# ---------------------------------------------------------------- mind


class Mind:
    def __init__(self, reflex, library, memory, spine, designer=None, clock=time.time):
        self.reflex, self.library, self.memory, self.spine = reflex, library, memory, spine
        self.designer, self.clock = designer, clock

    def handle(self, moment):
        t0 = time.monotonic()
        try:
            decision = self.reflex.decide(moment, self.library, self.memory.notes)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            print(f"(reflex {self.reflex.by}) {e}; falling back to rules", file=sys.stderr)
            decision = RuleReflex().decide(moment, self.library)
        reflex_ms = (time.monotonic() - t0) * 1000
        action, note = decision.action, ""
        if action == NEW:
            action, note = self.invent(moment)
        entry = {"t": self.clock(), **asdict(moment), **asdict(decision), "did": action,
                 "reflex_ms": round(reflex_ms), "note": note}
        self.memory.add(entry)
        print(f"🧠 {moment.source}「{moment.text}」→ {action} ({decision.by}, {reflex_ms:.0f} ms){note and ' ' + note}",
              flush=True)
        if action != NONE:
            self.spine.start(self.library.items[action], decision.intensity)
        return entry

    def invent(self, moment):
        if self.designer is None:
            return NONE, "no designer"
        try:
            name, about, spec = self.designer.design(moment, self.library, self.memory.notes)
            validate(self.spine.manifest, spec)
        except Exception as e:  # a bad design must not stop the robot listening
            return NONE, f"design failed: {e}"
        self.library.add(name, about, spec)
        return name, f"new motion {name}: {about}"

    def ruminate(self):
        if self.designer and self.memory.recent():
            self.memory.notes = self.designer.ruminate(self.memory.recent(50), self.memory.notes)
            print("💭 ruminated:\n" + self.memory.notes, flush=True)


# ---------------------------------------------------------------- ears


def utterances(stream, frame_ms=100, silence_rms=300.0, loud_rms=3000.0, end_ms=600, max_s=8.0):
    """Yield ("sound", loudness) when it gets loud and ("speech", pcm) per stretch above silence."""
    frame = int(RATE * frame_ms / 1000) * 2
    prev, buf, quiet = 0.0, bytearray(), 0
    while data := stream.read(frame):
        level = rms(data)
        if level >= loud_rms > prev:  # rising edge only: one startle per bang
            yield "sound", level
        prev = level
        if level >= silence_rms:
            buf += data
            quiet = 0
        elif buf:
            buf += data
            quiet += frame_ms
        if buf and (quiet >= end_ms or len(buf) >= max_s * RATE * 2):
            yield "speech", bytes(buf)
            buf, quiet = bytearray(), 0


def hear(args, moments):
    """Audio ears -> moments; the newest win (offer drops the oldest)."""
    proc = subprocess.Popen(source_command(args), stdout=subprocess.PIPE)
    source = "radio" if args.sdr is not None else "voice"
    try:
        for kind, value in utterances(proc.stdout, silence_rms=args.silence_rms):
            if kind == "sound":
                offer(moments, Moment("sound", loudness=value, onset=True))
            elif text := transcribe(value, args.whisper_cmd, args.whisper_model, args.language):
                offer(moments, Moment(source, text, loudness=rms(value)))
    finally:
        proc.terminate()


# ---------------------------------------------------------------- chat page


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Sesame mind</title><style>body{font:16px system-ui;margin:12px;max-width:640px}
#log{white-space:pre-wrap;font:13px ui-monospace,monospace}form{display:flex;gap:6px}input{flex:1;font:16px system-ui}
</style><h3>Sesame mind</h3><form id=f><input id=t placeholder="話しかける / ○○みたいな動きを作って" autofocus>
<button>送る</button></form><p><button id=r>反芻</button></p><div id=log></div><script>
const show=async()=>{const l=await (await fetch('log')).json();log.textContent=l.reverse().map(e=>
`${new Date(e.t*1000).toLocaleTimeString()} ${e.source}「${e.text}」→ ${e.did} (${e.by} ${e.reflex_ms}ms) ${e.note}`).join('\\n')};
f.onsubmit=async e=>{e.preventDefault();const s=t.value;t.value='';
await fetch('say',{method:'POST',body:s});show()};r.onclick=()=>fetch('ruminate',{method:'POST'});
setInterval(show,1000);show()</script>"""


def serve(port, mind, moments):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, kind="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self.reply(200, PAGE, "text/html")
            if self.path == "/log":
                return self.reply(200, json.dumps(mind.memory.recent(40), ensure_ascii=False))
            self.reply(404, "{}")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if self.path == "/say" and body.strip():
                offer(moments, Moment("chat", body.strip()))
            elif self.path == "/ruminate":
                threading.Thread(target=mind.ruminate, daemon=True).start()
            self.reply(202, "{}")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"chat page: http://{os.uname().nodename}:{port}/ (or this computer's LAN IP)", flush=True)
    return server


def think(mind, moments, idle_s=None):
    """Single worker: moments in arrival order; ruminate after idle_s without any."""
    while True:
        try:
            moment = moments.get(timeout=idle_s)
        except queue.Empty:
            mind.ruminate()
            continue
        if moment is None:
            return
        mind.handle(moment)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--sdr", type=float, metavar="MHZ", help="wide FM frequency for rtl_fm (Japan: 76-95)")
    src.add_argument("--mic", action="store_true", help="microphone through ffmpeg")
    src.add_argument("--file", help="audio file played in real time")
    src.add_argument("--text", action="store_true", help="chat lines on stdin")
    p.add_argument("--serve", type=int, metavar="PORT", help="chat page for phone/PC on this port")
    p.add_argument("--gain", type=float, help="rtl_fm tuner gain in dB (default: auto)")
    p.add_argument("--mic-device", help="ffmpeg input (macOS ':0', Linux pulse 'default')")
    p.add_argument("--whisper-cmd", default="whisper-cli")
    p.add_argument("--whisper-model", help="ggml model for whisper.cpp, e.g. ggml-small.bin")
    p.add_argument("--language", default="ja")
    p.add_argument("--silence-rms", type=float, default=300.0, help="speech threshold (int16 RMS)")
    p.add_argument("--reflex", choices=["rules", "jev"], default="rules")
    p.add_argument("--jev-url", default="https://api.typesafe.ai/v1/systemone",
                   help="System One endpoint; a local-jev server for the edge")
    p.add_argument("--jev-model", default="jev-latest")
    p.add_argument("--no-designer", action="store_true", help="never call Claude (no new motions, no rumination)")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--ruminate-s", type=float, default=300.0, help="ruminate after this long without input")
    p.add_argument("--state", type=Path, default=ROOT / ".sesame-mind", help="library.json, memory.jsonl, notes.md")
    p.add_argument("--host", help="robot address; omit for a dry run")
    args = p.parse_args(argv)
    audio = args.sdr is not None or args.mic or args.file
    if not (audio or args.text or args.serve):
        p.error("give an ear (--sdr/--mic/--file/--text) and/or --serve")
    if audio and not args.whisper_model:
        p.error("audio ears need --whisper-model")

    manifest = load_manifest()
    robot = Robot(args.host) if args.host else None
    reflex = (JevReflex(args.jev_url, os.environ.get("TYPESAFE_API_KEY"), args.jev_model)
              if args.reflex == "jev" else RuleReflex())
    designer = None if args.no_designer else ClaudeDesigner(manifest, args.model, args.effort)
    memory = Memory(args.state)
    mind = Mind(reflex, Library(args.state / "library.json"), memory, Spine(manifest, robot), designer)
    moments = queue.Queue(maxsize=4)
    done = start_heartbeat(robot) if robot else None
    try:
        if robot and run_lines(robot, ["?arm run", "motion stand"]):
            return 1
        if args.serve:
            serve(args.serve, mind, moments)
        if audio:
            threading.Thread(target=hear, args=(args, moments), daemon=True).start()
        if args.text:
            def typed():
                for line in sys.stdin:
                    if line.strip():
                        offer(moments, Moment("chat", line.strip()))
                if not (audio or args.serve):
                    offer(moments, None)
            threading.Thread(target=typed, daemon=True).start()
        think(mind, moments, None if args.no_designer else args.ruminate_s)
    except KeyboardInterrupt:
        if robot:
            print("> off (interrupted)", flush=True)
            robot.command("off")
        return 130
    finally:
        if done:
            done.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
