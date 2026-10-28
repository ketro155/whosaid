"""Bounded private speaker comparisons. This module has no delivery/export path."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openbrain_summary import atomic_write
from speaker_turn_identity import interval_identity, non_overlapping_spans

INPUTS = ("original.audio", "metadata.json", "transcript.json", "transcript.diarization.json", "transcript.speakers.txt")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def anonymous(name):
    return name == 'UNASSIGNED' or name.startswith('SPEAKER_')


def overlays(sidecar, profiles, embed, allowed_names):
    """Name only eligible non-overlapping pieces of anonymous source clusters."""
    original, names = sidecar['segments'], dict(sidecar['names'])
    decisions, accepted = [], []
    calls = 0
    for speaker in sorted({s['speaker'] for s in original}):
        if not anonymous(names[speaker]):
            continue
        # Union duplicate/adjacent ranges for this cluster before subtracting others.
        merged = []
        for s in sorted((x for x in original if x['speaker'] == speaker), key=lambda x: x['start']):
            a, b = s['start'], s['end']
            non_overlapping_spans(a, b, [])  # validate even ranges too short to embed
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        blocked = [(s['start'], s['end']) for s in original if s['speaker'] != speaker]
        for start, end in merged:
            for a, b in non_overlapping_spans(start, end, blocked):
                if b - a < 4:
                    continue
                if calls >= 200:
                    raise ValueError('Pilot interval bound exceeded')
                calls += 1
                decision = interval_identity(embed(a, b), profiles, model=sidecar['emb_model'], duration=b-a)
                if decision['name'] and decision['name'] not in allowed_names:
                    decision = {**decision, 'name': None, 'reason': 'outside_pilot_names'}
                row = {'source_cluster': speaker, 'start': a, 'end': b, **decision}
                decisions.append(row)
                if decision['name']:
                    key = f'PILOT_{len(accepted):05d}'
                    names[key] = decision['name']
                    accepted.append({**row, 'speaker': key})
    result = []
    for segment in original:
        bounds = {segment['start'], segment['end']}
        relevant = [r for r in accepted if r['source_cluster'] == segment['speaker']
                    and min(r['end'], segment['end']) > max(r['start'], segment['start'])]
        for row in relevant:
            bounds.update((max(row['start'], segment['start']), min(row['end'], segment['end'])))
        ordered = sorted(bounds)
        for a, b in zip(ordered, ordered[1:]):
            match = next((r for r in relevant if r['start'] <= a and r['end'] >= b), None)
            result.append({'start': a, 'end': b, 'speaker': match['speaker'] if match else segment['speaker']})
    result.sort(key=lambda s: (s['start'], s['end'], s['speaker']))
    return result, names, decisions, calls


def render(turns, names, hidden):
    import diarize_sherpa as d
    return ''.join(f'[{d.hms(start)}] {"UNASSIGNED" if sp in hidden else names.get(sp, sp)}: '
                   + ' '.join(texts) + '\n\n' for sp, start, texts in turns)


def compare(snapshot, output, baseline, candidate, allowed_names, *, embed=None):
    import diarize_sherpa as d
    side = json.loads((snapshot/'transcript.diarization.json').read_text())
    whisper = str(snapshot/'transcript.json')
    if embed is None:
        extractor = d.make_ref_embedder()
        embed = lambda a, b: extractor(d.load_audio(str(snapshot/'original.audio'), a, b-a))
    start = time.monotonic()
    replay = d.name_clusters(side['cluster_emb'], .5, .85, baseline['speakers'])
    baseline_replay_equal = replay == side['names']
    base_turns = d.build_turns(side['segments'], whisper)
    base_text = render(base_turns, side['names'], set(side.get('uncounted', [])))
    baseline_seconds = time.monotonic()-start
    start = time.monotonic()
    segments, names, decisions, calls = overlays(side, candidate['speakers'], embed, allowed_names)
    turns = d.build_turns(segments, whisper)
    text = render(turns, names, set(side.get('uncounted', [])))
    words = lambda rows: ''.join(t for _, _, ts in rows for t in ts)
    if words(base_turns) != words(turns):
        raise ValueError('Speaker comparison changed source words')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_write(output/'baseline.transcript.txt', base_text)
    atomic_write(output/'candidate.transcript.txt', text)
    report = {'baseline_seconds': baseline_seconds, 'candidate_seconds': time.monotonic()-start,
              'baseline_replay_equal': baseline_replay_equal, 'decisions': decisions,
              'new_named_intervals': [r for r in decisions if r['name']], 'embedding_calls': calls,
              'source_words_preserved': True, 'existing_named_clusters_preserved': all(names[k] == v for k,v in side['names'].items()),
              'paid_api_calls': 0, 'summary_calls': 0, 'publication_calls': 0,
              'human_review': 'required', 'baseline_text_sha256': hashlib.sha256(base_text.encode()).hexdigest(),
              'candidate_text_sha256': hashlib.sha256(text.encode()).hexdigest()}
    atomic_write(output/'comparison.json', json.dumps(report, indent=2))
    return report


def run_record(source, folder, baseline, candidate, allowed_names):
    """Freeze a source once, then repeat both arms on identical input twice."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    frozen = folder/'snapshot'
    source_hashes = {name: digest(source/name) for name in INPUTS}
    if not frozen.exists():
        frozen.mkdir(mode=0o700)
        for name in INPUTS:
            shutil.copy2(source/name, frozen/name)
        atomic_write(folder/'source-hashes.json', json.dumps(source_hashes, indent=2))
    elif json.loads((folder/'source-hashes.json').read_text()) != source_hashes:
        raise ValueError('Source changed; fixed pilot set must not silently change')
    if {n: digest(frozen/n) for n in INPUTS} != source_hashes:
        raise ValueError('Pilot snapshot changed')
    metadata = json.loads((frozen/'metadata.json').read_text())
    if digest(frozen/'original.audio') != metadata['audio_sha256']:
        raise ValueError('Pilot audio does not match retained source')
    reports = []
    for number in (1, 2):
        run = folder/f'run-{number}'
        if run.exists():
            # Partial runs are never reused as completed evidence.
            if not (run/'comparison.json').exists():
                raise ValueError('Incomplete pilot run requires a new comparison folder')
            report = json.loads((run/'comparison.json').read_text())
            if (digest(run/'candidate.transcript.txt') != report['candidate_text_sha256'] or
                    digest(run/'baseline.transcript.txt') != report['baseline_text_sha256']):
                raise ValueError('Retained pilot output changed')
        else:
            report = compare(frozen, run, baseline, candidate, allowed_names)
        reports.append(report)
    repeat_equal = (reports[0]['decisions'] == reports[1]['decisions'] and
                    reports[0]['candidate_text_sha256'] == reports[1]['candidate_text_sha256'])
    if {name: digest(source/name) for name in INPUTS} != source_hashes:
        raise ValueError('Production source changed during shadow comparison')
    receipt = {'recording_id': metadata['file_id'], 'runs': 2, 'repeat_equal': repeat_equal,
               'named_intervals': len(reports[0]['new_named_intervals']), 'source_unchanged': True,
               'max_candidate_seconds': max(r['candidate_seconds'] for r in reports),
               'baseline_replay_equal': all(r['baseline_replay_equal'] for r in reports),
               'human_review': 'required', 'verdict': 'KEEP', 'publication_calls': 0,
               'blockers': ['New identity intervals require human truth', 'Full-registry acceptance is not established']}
    atomic_write(folder/'receipt.json', json.dumps(receipt, indent=2))
    return receipt


def run_pending(production, cfg):
    """Admit at most two completed new work recordings under a frozen policy."""
    from plaud_pipeline import database, locked
    if cfg.get('mode') != 'shadow' or cfg.get('max_recordings') != 2:
        raise ValueError('Only a two-recording private shadow pilot is supported')
    since = datetime.fromisoformat(cfg['since']).date().isoformat()
    pilot = Path(cfg['workspace']).expanduser().resolve()
    if pilot == production.resolve() or production.resolve() in pilot.parents:
        raise ValueError('Pilot workspace must be separate from production')
    baseline_path, candidate_path = Path(cfg['baseline_registry']), Path(cfg['candidate_registry'])
    fingerprints = {'baseline': digest(baseline_path), 'candidate': digest(candidate_path)}
    if fingerprints != cfg['registry_sha256']:
        raise ValueError('Frozen pilot registry changed')
    baseline, candidate = json.loads(baseline_path.read_text()), json.loads(candidate_path.read_text())
    allowed_names = set(cfg['allowed_names'])
    base_names = {p['name'] for p in baseline['speakers']}
    candidate_names = {p['name'] for p in candidate['speakers']}
    if len(allowed_names) != 2 or not allowed_names <= candidate_names:
        raise ValueError('Exactly two explicit pilot identities are required')
    if candidate_names - base_names != allowed_names - base_names:
        raise ValueError('Candidate contains an unapproved identity')
    old_profiles = {p['name']:p for p in baseline['speakers']}
    new_profiles = {p['name']:p for p in candidate['speakers']}
    if any(new_profiles.get(name) != value for name,value in old_profiles.items()):
        raise ValueError('Pilot cannot replace existing production voice profiles')
    implementation = {name:digest(Path(__file__).parent/name) for name in ('speaker_shadow_pilot.py','speaker_turn_identity.py')}
    if implementation != cfg['implementation_sha256']:
        raise ValueError('Pilot implementation changed')
    identity = {**cfg, 'registry_sha256': fingerprints}
    with locked(pilot):
        policy = pilot/'policy.json'
        if policy.exists() and json.loads(policy.read_text()) != identity:
            raise ValueError('Pilot policy changed; use a new workspace')
        if not policy.exists():
            atomic_write(policy, json.dumps(identity, indent=2))
        admissions = pilot/'admissions.json'
        ids = json.loads(admissions.read_text()) if admissions.exists() else []
        with database(production) as db:
            rows = db.execute("select id,folder from recordings where stage='complete' order by first_seen,id").fetchall()
        folders = {}
        for row in rows:
            source = production/'meetings'/row['folder']
            metadata = json.loads((source/'metadata.json').read_text())
            if metadata['file_id'] != row['id']:
                raise ValueError('Pilot source identity mismatch')
            folders[row['id']] = source
            date = datetime.fromisoformat(metadata['date']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            if (len(ids) < 2 and row['id'] not in ids and date >= since
                    and re.search(r'\b(SMART|FMDS)\b', metadata.get('title',''), re.I)):
                ids.append(row['id'])
                atomic_write(admissions, json.dumps(ids, indent=2))
        receipts = []
        for recording_id in ids:
            key = hashlib.sha256(recording_id.encode()).hexdigest()[:24]
            if recording_id not in folders:
                raise ValueError('Admitted pilot source is unavailable')
            receipts.append(run_record(folders[recording_id], pilot/'recordings'/key, baseline, candidate, allowed_names))
        result = {'at': datetime.now(timezone.utc).isoformat(), 'mode':'shadow', 'admitted':ids,
                  'remaining_slots':2-len(ids), 'status':'awaiting_new_recordings' if not ids else 'awaiting_human_review',
                  'publication_calls':0, 'receipts':receipts}
        atomic_write(pilot/'status.json', json.dumps(result, indent=2))
        return result
