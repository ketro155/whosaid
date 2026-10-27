"""Read-only timing evaluation, not an automatic replacement for local ASR.

Plaud transaction timestamps are milliseconds. Anonymous vendor labels never
become enrolled identities. Long paragraphs cannot safely replace aligned words.
"""
from __future__ import annotations
import math
import statistics


def timing_report(transcript, diarization=None):
    if not isinstance(transcript, dict) or transcript.get('block') != 'transaction':
        raise ValueError('Only raw transaction transcripts may be evaluated')
    segments = transcript.get('segments')
    if not isinstance(segments, list) or not segments:
        raise ValueError('No raw transcript segments to evaluate')
    durations=[]; mixed=0; words=0
    for row in segments:
        if not isinstance(row, dict) or not isinstance(row.get('content'),str) or not row['content'].strip():
            raise ValueError('Invalid raw transcript segment')
        a,b=row.get('start_time'),row.get('end_time')
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in (a,b)) or not 0<=a<b:
            raise ValueError('Invalid raw transcript timing')
        start,end=a/1000,b/1000;duration=end-start;durations.append(duration)
        words+=bool(row.get('words'))
        if diarization is not None:
            overlaps={}
            for turn in diarization:
                overlap=max(0,min(end,turn['end'])-max(start,turn['start']))
                overlaps[turn['speaker']]=overlaps.get(turn['speaker'],0)+overlap
            # A diagnostic disagreement, not human-verified speaker truth.
            mixed+=sum(x>=max(.5,duration*.1) for x in overlaps.values())>1
    return {'file_id':transcript.get('file_id'),'segments':len(segments),
            'timing_unit':'milliseconds','segments_with_word_timestamps':words,
            'median_segment_seconds':round(statistics.median(durations),2),
            'max_segment_seconds':round(max(durations),2),'over_5_seconds':sum(x>5 for x in durations),
            'overlapping_multiple_local_speakers':mixed if diarization is not None else None,
            'local_speaker_overlap_is_diagnostic':True,
            'automatic_asr_replacement_enabled':False}
