#!/usr/bin/env python3
"""whosaid ab: compare two settings on the same recording, with no ground truth.

Run mode:   ab_compare.py <audio> --a "<flags>" --b "<flags>" [-o DIR] [--same-asr]
                          [--env-a K=V ...] [--env-b K=V ...] [--keep-registry-writes]
Compare:    ab_compare.py --compare DIR_A DIR_B [-o DIR] [--top N]

Run mode shells out to the whosaid script twice (DIR/a, DIR/b), by default on a
COPY of the speaker registry and an unused workspace so the A/B never writes a
voiceprint or ingests anything. Compare mode is stdlib only and reads the output
of one whosaid run per directory: <base>.json (ASR), <base>.diarization.json,
<base>.rttm, <base>.speakers.txt. Reports markdown on stdout plus
DIR/ab-report.md and DIR/ab-report.json when DIR is known.

The report shows what DIFFERS between arms (speaker counts, talk shares, words,
names). It cannot say which arm is right; that needs listening or ground truth.
"""

import argparse
import bisect
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ANON_RE = re.compile(r"^SPEAKER_\d+$")
MODEL_FLAGS = {"-m", "--model", "--fast", "--accurate"}
TURN_RE = re.compile(r"^\[(\d+):(\d\d):(\d\d)\] (.*?): (.*)$")


# ---- tokenizing (same as test/diarize_eval/score.py tokenize) -----------------

def tokenize(text):
    text = re.sub(r"[^\w\s]|_", "", (text or "").lower())
    return text.split()


# ---- readers --------------------------------------------------------------------

def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def find_base(d: Path):
    for suffix in (".diarization.json", ".rttm"):
        hits = sorted(d.glob(f"*{suffix}"))
        if hits:
            return hits[0].name[: -len(suffix)]
    hits = [p for p in sorted(d.glob("*.json")) if not p.name.endswith((".diarization.json", "ab-run.json", "ab-report.json"))]
    return hits[0].stem if hits else None


def read_rttm(path):
    """[(start, end, label)] from an RTTM file (label = field 8)."""
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        f = ln.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            try:
                s, dur = float(f[3]), float(f[4])
            except ValueError:
                continue
            out.append((s, s + dur, f[7]))
    return sorted(out)


def timed_words(d: Path, base: str):
    """[(word, time_seconds)] from the ASR json segments (words interpolated
    within a segment); falls back to speakers.txt turns (turn start time)."""
    asr = _load_json(d / f"{base}.json")
    words = []
    if isinstance(asr, dict) and isinstance(asr.get("segments"), list):
        for seg in asr["segments"]:
            toks = tokenize(seg.get("text", ""))
            try:
                s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
            except (TypeError, ValueError):
                s = e = 0.0
            for j, t in enumerate(toks):
                words.append((t, s + (e - s) * j / max(len(toks), 1)))
        if words:
            return words, "asr-json"
    txt = d / f"{base}.speakers.txt"
    try:
        lines = txt.read_text().splitlines()
    except OSError:
        return [], "none"
    for ln in lines:
        m = TURN_RE.match(ln)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
            words.extend((w, float(t)) for w in tokenize(m[5]))
    return words, "speakers-txt"


def parse_log(d: Path):
    """(model, wall_seconds, flags, env) from ab-run.json / run log if present."""
    info = {"model": None, "wall_seconds": None, "flags": None, "env": None}
    run = _load_json(d / "ab-run.json")
    if isinstance(run, dict):
        info["wall_seconds"] = run.get("wall_seconds")
        info["flags"] = run.get("flags")
        info["env"] = run.get("env")
    for name in ("ab-run.log", "run.log"):
        p = d / name
        if p.exists():
            m = re.search(r"model=(\S+)", p.read_text(errors="replace"))
            if m:
                info["model"] = m[1]
                break
    return info


def arm_summary(d: Path):
    base = find_base(d)
    if base is None:
        raise SystemExit(f"ab: no whosaid output (*.diarization.json / *.rttm) in {d}")
    side = _load_json(d / f"{base}.diarization.json") or {}
    segs = read_rttm(d / f"{base}.rttm")
    talk = {}
    for s, e, lab in segs:
        talk[lab] = talk.get(lab, 0.0) + (e - s)
    total = sum(talk.values())
    ranked = sorted(talk.items(), key=lambda kv: -kv[1])
    named = sum(v for k, v in talk.items() if not ANON_RE.match(k))
    embedder = side.get("emb_model")
    try:
        for ln in (d / f"{base}.speakers.txt").read_text().splitlines()[:4]:
            m = re.match(r"# Diarization: .*\+ (.*?)\), local", ln)
            if m:
                embedder = f"{embedder} ({m[1]})" if embedder else m[1]
                break
    except OSError:
        pass
    log = parse_log(d)
    return {
        "dir": str(d), "base": base,
        "flags": log["flags"], "env": log["env"],
        "wall_seconds": log["wall_seconds"],
        "model": log["model"], "embedder": embedder,
        "num_speakers": side.get("num_speakers"),
        "uncounted": side.get("uncounted", []),
        "total_talk_seconds": round(total, 3),
        "top_shares": [{"label": k, "seconds": round(v, 3),
                        "share": round(v / total, 4) if total else 0.0} for k, v in ranked[:5]],
        "named_share": round(named / total, 4) if total else 0.0,
        "anonymous_share": round((total - named) / total, 4) if total else 0.0,
        "labels": sorted(talk),
        "named_labels": sorted(k for k in talk if not ANON_RE.match(k)),
        "_segs": segs,
    }


# ---- comparisons ------------------------------------------------------------------

def word_diff(wa, wb, top):
    ta = [w for w, _ in wa]
    tb = [w for w, _ in wb]
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    denom = len(ta) + len(tb)
    agree = 100.0 * 2 * matched / denom if denom else 100.0
    spans = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i1 < len(wa):
            t = wa[i1][1]
        elif j1 < len(wb):
            t = wb[j1][1]
        else:
            t = 0.0
        spans.append({"time": round(t, 2), "kind": tag,
                      "a_words": i2 - i1, "b_words": j2 - j1,
                      "a": " ".join(ta[i1:min(i2, i1 + 14)]),
                      "b": " ".join(tb[j1:min(j2, j1 + 14)])})
    spans.sort(key=lambda s: -max(s["a_words"], s["b_words"]))
    return {"words_a": len(ta), "words_b": len(tb),
            "agreement_pct": round(agree, 2), "spans": spans[:top],
            "total_spans": len(spans)}


def _label_at(segs, starts, t):
    i = bisect.bisect_right(starts, t) - 1
    # overlap-tolerant: scan back a few for one that still covers t
    for k in range(i, max(i - 8, -1), -1):
        s, e, lab = segs[k]
        if s <= t < e:
            return lab
    return None


def attribution(segs_a, segs_b):
    sa, sb = sorted(segs_a), sorted(segs_b)
    starts_a, starts_b = [s[0] for s in sa], [s[0] for s in sb]
    bounds = sorted({x for s in sa for x in s[:2]} | {x for s in sb for x in s[:2]})
    both_speech = both_named = same = 0.0
    for lo, hi in zip(bounds, bounds[1:]):
        mid, w = (lo + hi) / 2, hi - lo
        la, lb = _label_at(sa, starts_a, mid), _label_at(sb, starts_b, mid)
        if la is None or lb is None:
            continue
        both_speech += w
        if ANON_RE.match(la) or ANON_RE.match(lb):
            continue
        both_named += w
        if la == lb:
            same += w
    return {"both_speech_seconds": round(both_speech, 2),
            "both_named_seconds": round(both_named, 2),
            "same_name_seconds": round(same, 2),
            "agreement_pct": round(100.0 * same / both_named, 2) if both_named else None}


def compare_dirs(da: Path, db: Path, top=8):
    a, b = arm_summary(da), arm_summary(db)
    wa, src_a = timed_words(da, a["base"])
    wb, src_b = timed_words(db, b["base"])
    rep = {"a": a, "b": b,
           "words": word_diff(wa, wb, top) | {"source_a": src_a, "source_b": src_b},
           "attribution": attribution(a["_segs"], b["_segs"])}
    la, lb = set(a["labels"]), set(b["labels"])
    na, nb = set(a["named_labels"]), set(b["named_labels"])
    rep["labels"] = {"only_a": sorted(na - nb), "only_b": sorted(nb - na),
                     "anon_a": len(la - na), "anon_b": len(lb - nb),
                     "same_set": la == lb}
    for arm in (a, b):
        arm.pop("_segs")
    return rep


# ---- rendering ----------------------------------------------------------------------

def _pct(x):
    return f"{100 * x:.1f}%"


def _hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _wall(w):
    return "-" if w is None else f"{w:.0f}s"


def render_md(rep):
    a, b, w, at, lab = rep["a"], rep["b"], rep["words"], rep["attribution"], rep["labels"]
    L = ["# whosaid A/B report", "",
         "No ground truth: this shows where the arms differ, not which is right.", "",
         "| | A | B |", "|---|---|---|"]

    def row(name, fa, fb):
        L.append(f"| {name} | {fa} | {fb} |")

    def fmt_env(e):
        return ", ".join(f"{k}={v}" for k, v in (e or {}).items()) or "-"
    row("flags", f"`{a['flags'] or '-'}`", f"`{b['flags'] or '-'}`")
    row("env", fmt_env(a["env"]), fmt_env(b["env"]))
    row("wall time", _wall(a["wall_seconds"]), _wall(b["wall_seconds"]))
    row("model", a["model"] or "-", b["model"] or "-")
    row("embedder", a["embedder"] or "-", b["embedder"] or "-")
    row("counted speakers", a["num_speakers"], b["num_speakers"])
    row("uncounted", ", ".join(a["uncounted"]) or "none", ", ".join(b["uncounted"]) or "none")
    row("named talk share", _pct(a["named_share"]), _pct(b["named_share"]))
    row("anonymous talk share", _pct(a["anonymous_share"]), _pct(b["anonymous_share"]))
    L += ["", "## Top talk shares", "", "| # | A | B |", "|---|---|---|"]
    for i in range(5):
        def cell(arm):
            return (f"{arm['top_shares'][i]['label']} {_pct(arm['top_shares'][i]['share'])}"
                    if i < len(arm["top_shares"]) else "-")
        row(i + 1, cell(a), cell(b))
    L += ["", "## Transcript",
          f"- word agreement: **{w['agreement_pct']:.1f}%** ({w['words_a']} vs {w['words_b']} words, "
          f"{w['total_spans']} differing spans; times from {w['source_a']})"]
    if w["spans"]:
        L += ["", f"Largest {len(w['spans'])} disagreement spans:", ""]
        for s in w["spans"]:
            L.append(f"- {_hms(s['time'])} ({s['a_words']} vs {s['b_words']} words): "
                     f"A `{s['a'] or '-'}` | B `{s['b'] or '-'}`")
    L += ["", "## Speaker attribution"]
    if at["agreement_pct"] is None:
        L.append("- name agreement: n/a (no time where both arms give a real name)")
    else:
        L.append(f"- name agreement: **{at['agreement_pct']:.1f}%** over {at['both_named_seconds']:.0f}s "
                 f"where both arms name the speaker ({at['both_speech_seconds']:.0f}s of shared speech)")
    if lab["same_set"]:
        L.append("- label sets are identical")
    else:
        L.append(f"- label sets differ: names only in A: {', '.join(lab['only_a']) or 'none'}; "
                 f"only in B: {', '.join(lab['only_b']) or 'none'}; "
                 f"anonymous clusters A {lab['anon_a']} vs B {lab['anon_b']} "
                 "(SPEAKER_NN numbers are not comparable across arms)")
    return "\n".join(L) + "\n"


def emit(rep, outdir):
    md = render_md(rep)
    sys.stdout.write(md)
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "ab-report.md").write_text(md)
        (outdir / "ab-report.json").write_text(json.dumps(rep, indent=2))
        print(f"\nreport: {outdir / 'ab-report.md'}  {outdir / 'ab-report.json'}", file=sys.stderr)


# ---- run mode -------------------------------------------------------------------------

def whosaid_bin():
    return os.environ.get("WHOSAID_AB_BIN") or str(REPO_DIR / "whosaid")


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"ab: env '{it}' must be K=V")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def registry_path():
    return Path(os.environ.get("WHOSAID_SPEAKER_DB") or Path.home() / ".config" / "whosaid" / "speakers.json")


def run_arm(name, audio, outdir, flags, env_over, extra, isolate, root):
    outdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if isolate:
        reg = root / f".registry-{name}.json"
        src = registry_path()
        if src.exists():
            shutil.copy2(src, reg)
        else:
            reg.write_text('{"speakers": []}')
        ws = root / f".workspace-{name}"
        ws.mkdir(exist_ok=True)
        env["WHOSAID_SPEAKER_DB"] = str(reg)
        env["WHOSAID_WORKSPACE"] = str(ws)
    env.update(env_over)
    cmd = [whosaid_bin(), "transcribe", audio, "-o", str(outdir)] + shlex.split(flags) + extra
    t0 = time.monotonic()
    with open(outdir / "ab-run.log", "w") as lf:
        rc = subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT).returncode
    wall = time.monotonic() - t0
    (outdir / "ab-run.json").write_text(json.dumps(
        {"flags": flags, "env": env_over, "wall_seconds": round(wall, 1), "returncode": rc,
         "command": cmd}, indent=2))
    print(f"ab: arm {name.upper()} rc={rc} wall={wall:.0f}s -> {outdir}", file=sys.stderr)
    return rc


def sanitize_base(audio):
    return re.sub(r"[^A-Za-z0-9_-]", "_", Path(audio).stem)


def run_mode(args):
    audio = args.audio
    if args.same_asr:
        bad = [t for t in shlex.split(args.b) if t in MODEL_FLAGS]
        if bad or "WHOSAID_MODEL" in parse_kv(args.env_b):
            raise SystemExit("ab: --same-asr reuses A's transcript, so B must not change the ASR "
                             f"model ({', '.join(bad) or 'WHOSAID_MODEL'} in B's settings). "
                             "Drop --same-asr or the model flag.")
    if not Path(audio).is_file():
        raise SystemExit(f"ab: audio not found: {audio}")
    base = sanitize_base(audio)
    root = Path(args.outdir or f"./whosaid-ab-{base}-{datetime.now():%Y%m%d-%H%M%S}")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    print(f"ab: output dir {root}", file=sys.stderr)
    isolate = not args.keep_registry_writes
    env_a, env_b = parse_kv(args.env_a), parse_kv(args.env_b)

    rc_a = run_arm("a", audio, root / "a", args.a, env_a, [], isolate, root)
    extra_b = []
    if args.same_asr:
        if rc_a != 0:
            raise SystemExit("ab: arm A failed; cannot reuse its transcript for B")
        (root / "b").mkdir(exist_ok=True)
        for ext in ("json", "txt"):
            src = root / "a" / f"{base}.{ext}"
            if src.exists():
                shutil.copy2(src, root / "b" / src.name)
        if not (root / "b" / f"{base}.json").exists():
            raise SystemExit(f"ab: A produced no {base}.json to reuse")
        extra_b = ["--reuse-asr"]
    rc_b = run_arm("b", audio, root / "b", args.b, env_b, extra_b, isolate, root)
    for n in ("a", "b"):  # drop the unused workspaces if nothing wrote there
        ws = root / f".workspace-{n}"
        try:
            ws.rmdir()
        except OSError:
            pass
    if rc_a or rc_b:
        print(f"ab: WARN whosaid exited A={rc_a} B={rc_b}; see ab-run.log in each arm dir", file=sys.stderr)
    rep = compare_dirs(root / "a", root / "b", args.top)
    emit(rep, root)
    return 0 if not (rc_a or rc_b) else 1


def build_parser():
    p = argparse.ArgumentParser(prog="whosaid ab", description=__doc__.split("\n\n")[0])
    p.add_argument("audio", nargs="?")
    p.add_argument("--a", default="", help='whosaid flags for arm A, e.g. --a="--speakers 2"')
    p.add_argument("--b", default="", help="whosaid flags for arm B")
    p.add_argument("-o", "--outdir")
    p.add_argument("--same-asr", action="store_true")
    p.add_argument("--env-a", action="append", default=[], metavar="K=V")
    p.add_argument("--env-b", action="append", default=[], metavar="K=V")
    p.add_argument("--keep-registry-writes", action="store_true")
    p.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"))
    p.add_argument("--top", type=int, default=8)
    return p


def glue_arm_flags(argv):
    """`--a --fast` would read --fast as an option; glue to `--a=--fast`."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--a", "--b") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main(argv=None):
    args = build_parser().parse_args(glue_arm_flags(sys.argv[1:] if argv is None else argv))
    if args.compare:
        rep = compare_dirs(Path(args.compare[0]), Path(args.compare[1]), args.top)
        emit(rep, Path(args.outdir) if args.outdir else None)
        return 0
    if not args.audio:
        raise SystemExit('usage: whosaid ab <audio> --a "<flags>" --b "<flags>" [-o DIR] [--same-asr]\n'
                         "       whosaid ab --compare DIR_A DIR_B")
    return run_mode(args)


if __name__ == "__main__":
    sys.exit(main())
