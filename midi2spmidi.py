#!/usr/bin/env python3
"""
midi2spmidi.py - transcode a Standard MIDI File to SP-MIDI by phrase stealing.

Released into the public domain under the Unlicense (https://unlicense.org/);
see the UNLICENSE.md file.  Provided "as is", without warranty.

Implements the algorithm of Lui, Horner & Ayers, "MIDI to SP-MIDI Transcoding
Using Phrase Stealing"
(https://www.researchgate.net/publication/3338893_MIDI_to_SP-MIDI_Transcoding_Using_Phrase_Stealing):

  1. Preprocessing        drop same-channel unisons; merge the percussion
                          channels and reduce them to one voice using the
                          priority list of the paper's Table 1.
  2. Feature extraction   key (pitch-class ranking), triad chords, Top/Bass notes.
  3. Phrase identification  voice-leading chains of notes, per channel, closed
                          after V-I / vii-I cadences.
  4. Parallel phrase reduction  drop phrases rhythmically parallel to the melody.
  5. Phrase stealing      sweep through time; whenever more than MIP notes sound,
                          delete whole phrases, least important class first and
                          LIFO (most recently started first) within a class.

The reduced file gets an SP-MIDI MIP message:
    F0 7F 7F 0B 01  ch mip  [ch mip ...]  F7
(channels listed in priority order, each MIP being the polyphony needed to play
that channel plus all higher-priority ones).

Input and output are Standard MIDI Files; no external tools are needed.  Events
other than notes are kept as they are, except where the channel and track
options (--split-voices, --limit-channels, --tracks-by-channel,
--drop-empty-tracks) have to rewrite them.  Run with --help for all options.

    python3 midi2spmidi.py in.mid out.mid --mip 4
"""
from __future__ import annotations

import argparse
import bisect
import copy
import statistics
import struct
import sys
from collections import Counter, defaultdict

# ======================================================================
#  Standard MIDI File model, reading and writing
# ======================================================================

def _vlq_read(d, p):
    v = 0
    while True:
        b = d[p]
        p += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, p


def _vlq(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


class Note:
    __slots__ = ("start", "end", "pitch", "ch", "vel", "off_vel", "track",
                 "top", "bass", "is_top", "is_bass", "phrase")

    def __init__(self, start, end, pitch, ch, vel, off_vel, track):
        self.start, self.end, self.pitch, self.ch = start, end, pitch, ch
        self.vel, self.off_vel, self.track = vel, off_vel, track
        self.top = self.bass = 0            # votes from chord analysis
        self.is_top = self.is_bass = False
        self.phrase = None


class Ev:
    """Any event that is not a note on/off, kept as the raw bytes that follow
    its delta time.  `ch` is the channel of channel-voice messages, else None."""
    __slots__ = ("track", "time", "idx", "data", "ch")

    def __init__(self, track, time, idx, data, ch):
        self.track, self.time, self.idx, self.data, self.ch = track, time, idx, data, ch


class Song:
    def __init__(self):
        self.events = []         # every non-note event, in file order
        self.notes = []
        self.division = 480
        self.fmt = 1
        self.ntracks = 0
        self.time_sigs = []      # (tick, numerator, denominator)
        self.track_end = {}


def parse_smf(data: bytes) -> Song:
    """Read a Standard MIDI File into notes (note-on/off paired) plus raw events."""
    if data[:4] != b"MThd":
        raise ValueError("not a Standard MIDI File (no MThd header)")
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, _, div = struct.unpack(">HHH", data[8:14])
    if not 0 < div < 0x8000:
        raise ValueError("SMPTE time division is not supported")
    song = Song()
    song.fmt, song.division = fmt, div
    pos, track, idx = 8 + hlen, -1, 0
    open_ = defaultdict(list)             # (track, channel, pitch) -> sounding notes, FIFO
    while pos + 8 <= len(data):
        cid, clen = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
        p, end = pos + 8, min(pos + 8 + clen, len(data))
        pos += 8 + clen
        if cid != b"MTrk":
            continue
        track += 1
        tick, run = 0, None
        while p < end:
            dt, p = _vlq_read(data, p)
            tick += dt
            st = data[p]
            if st & 0x80:
                p += 1
            elif run is None:
                raise ValueError("corrupt MIDI file: running status without a status byte")
            else:
                st = run
            idx += 1
            if st == 0xFF:                                   # meta event
                run = None
                mt = data[p]
                ln, p = _vlq_read(data, p + 1)
                body = data[p:p + ln]
                p += ln
                if mt == 0x2F:                               # End_track is regenerated
                    song.track_end[track] = tick
                    continue
                if mt == 0x58 and ln == 4:
                    song.time_sigs.append((tick, body[0], 2 ** body[1]))
                song.events.append(Ev(track, tick, idx, bytes([0xFF, mt]) + _vlq(ln) + body, None))
            elif st in (0xF0, 0xF7):                         # system exclusive
                run = None
                ln, p = _vlq_read(data, p)
                body = data[p:p + ln]
                p += ln
                if st == 0xF0 and body[:4] == b"\x7f\x7f\x0b\x01":
                    continue                                 # an old MIP message is replaced
                song.events.append(Ev(track, tick, idx, bytes([st]) + _vlq(ln) + body, None))
            else:                                            # channel-voice message
                run = st
                hi, ch = st & 0xF0, st & 0x0F
                nb = 1 if hi in (0xC0, 0xD0) else 2
                a = data[p:p + nb]
                p += nb
                if hi in (0x80, 0x90):
                    key = (track, ch, a[0])
                    if hi == 0x90 and a[1] > 0:
                        n = Note(tick, None, a[0], ch, a[1], 64, track)
                        open_[key].append(n)
                        song.notes.append(n)
                    elif open_[key]:
                        n = open_[key].pop(0)
                        n.end, n.off_vel = max(tick, n.start + 1), a[1]
                else:
                    song.events.append(Ev(track, tick, idx, bytes([st]) + a, ch))
        song.track_end.setdefault(track, tick)
    song.ntracks = track + 1
    for q in open_.values():              # notes that never got a note-off
        for n in q:
            n.end = max(song.track_end.get(n.track, n.start + 1), n.start + 1)
    return song


class Meter:
    """Time-signature map: measure/beat positions of ticks."""

    def __init__(self, ppqn, sigs):
        d = {}
        for t, num, den in sorted(sigs):
            d[t] = (num, den)
        if 0 not in d:
            d[0] = (4, 4)
        self.ppqn = ppqn
        self.segs = [(t, *d[t]) for t in sorted(d)]
        self.ticks = [s[0] for s in self.segs]

    def beat_len(self, den):
        return max(1, self.ppqn * 4 // den)

    def beat_info(self, t, tol):
        """-> (on_beat, beat_index, first_beat_of_measure, strong_beat)"""
        st, num, den = self.segs[bisect.bisect_right(self.ticks, t) - 1]
        bl = self.ppqn * 4 / den
        pos = (t - st) % (bl * num)
        idx = int(round(pos / bl))
        on = abs(pos - idx * bl) <= tol
        if idx >= num:
            idx = 0
        strong = on and (idx == 0 or (num == 4 and idx == 2)
                         or (den == 8 and num > 3 and num % 3 == 0 and idx % 3 == 0))
        return on, idx, on and idx == 0, strong

    def measure(self, t):
        """(time-signature segment, measure number inside it) of tick t."""
        i = bisect.bisect_right(self.ticks, t) - 1
        st, num, den = self.segs[i]
        return i, int((t - st) // (self.ppqn * 4 / den * num))

    def beat_bounds(self, end_tick):
        b = []
        t = 0
        for i, (st, num, den) in enumerate(self.segs):
            bl = self.beat_len(den)
            lim = self.segs[i + 1][0] if i + 1 < len(self.segs) else end_tick + bl
            t = st
            while t < lim:
                b.append(t)
                t += bl
        b.append(t)
        return b


def max_polyphony(notes):
    ev = []
    for n in notes:
        ev.append((n.start, 1))
        ev.append((n.end, -1))
    ev.sort()
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


# ======================================================================
#  1. Preprocessing
# ======================================================================

def dedupe_unisons(notes):
    """Same channel + same pitch sounding twice: keep one voice (re-attacks win)."""
    groups = defaultdict(list)
    for n in notes:
        groups[(n.ch, n.pitch)].append(n)
    keep = []
    for lst in groups.values():
        lst.sort(key=lambda n: (n.start, -n.end))
        cur = None
        for n in lst:
            if cur is not None and n.start == cur.start:      # exact duplicate
                cur.end = max(cur.end, n.end)
                cur.vel = max(cur.vel, n.vel)
                continue
            if cur is not None and n.start < cur.end:         # re-attack
                cur.end = n.start
            cur = n
            keep.append(n)
    return keep


CRASH, RIDE, TAMB = {49, 52, 55, 57}, {51, 53, 59}, {54}
HATS, KICKS, SNARES = {42, 44, 46}, {35, 36}, {37, 38, 40}
TOMS, CHIMES = {41, 43, 45, 47, 48, 50}, {84}


def drum_priority(n, meter, rolling, tol):
    """Table 1 of the paper; smaller number = more important."""
    on, _, first, strong = meter.beat_info(n.start, tol)
    p = n.pitch
    if p in CRASH:
        return 1 if first else 10
    if p in CHIMES:
        return 10
    if p in TOMS:
        return 2 if id(n) in rolling else 8
    if p in KICKS:
        return 3
    if p in SNARES:
        return 4
    if p in RIDE or p in TAMB:
        return 7 if strong else 5
    if p in HATS:
        return 6
    return 9


def reduce_percussion(drums, meter, ppqn, tol):
    """Merge all percussion channels into one and reduce them to one voice."""
    if not drums:
        return []
    ch = min(n.ch for n in drums)
    toms = sorted((n for n in drums if n.pitch in TOMS), key=lambda n: n.start)
    rolling, run = set(), []
    for n in toms + [None]:
        if n is not None and run and n.start - run[-1].start <= ppqn // 4:
            run.append(n)
            continue
        if len(run) >= 3:
            rolling.update(id(x) for x in run)
        run = [n] if n is not None else []
    drums = sorted(drums, key=lambda n: n.start)
    chosen, i = [], 0
    while i < len(drums):
        j = i
        while j < len(drums) and drums[j].start - drums[i].start <= tol:
            j += 1
        best = min(drums[i:j], key=lambda n: (drum_priority(n, meter, rolling, tol), -n.vel))
        chosen.append(best)
        i = j
    for a, b in zip(chosen, chosen[1:]):
        if a.end > b.start:
            a.end = b.start
    for n in chosen:
        n.ch = ch
    return chosen


# ======================================================================
#  2. Feature extraction
# ======================================================================

def find_key(P):
    """Rank the 12 pitch classes (12 pts best .. 1 pt worst); the tonic whose
    diatonic scale collects most points wins."""
    cnt, dur = [0] * 12, [0] * 12
    for n in P:
        cnt[n.pitch % 12] += 1
        dur[n.pitch % 12] += n.end - n.start
    order = sorted(range(12), key=lambda pc: (cnt[pc], dur[pc], -pc))
    pts = [0] * 12
    for rank, pc in enumerate(order):
        pts[pc] = rank + 1
    best = None
    for t in range(12):
        s = sum(pts[(t + d) % 12] for d in (0, 2, 4, 5, 7, 9, 11))
        key = (s, pts[t], pts[(t + 7) % 12])
        if best is None or key > best[0]:
            best = (key, t)
    return best[1]


_TRIADS = {"M": ((0, 4, 7), 1.0), "m": ((0, 3, 7), 1.0), "d": ((0, 3, 6), 0.9)}


def classify_chord(w):
    """Best-matching triad (root, quality) for a 12-bin chroma vector."""
    tot = sum(w)
    if tot <= 0:
        return None
    best = None
    for root in range(12):
        for q, (iv, prior) in _TRIADS.items():
            mem = {(root + i) % 12 for i in iv}
            s = sum(w[i] for i in mem) - 0.5 * sum(w[i] for i in range(12) if i not in mem)
            if s > 0:
                s *= prior
            key = (s, q == "M")
            if best is None or key > best[0]:
                best = (key, (root, q))
    return best[1] if best[0][0] > 0 else None


class Harmony:
    """Beat-by-beat triad analysis, merged into harmonic regions."""

    def __init__(self, P, meter, end_tick, tonic):
        self.tonic = tonic
        b = meter.beat_bounds(end_tick)
        w = [[0.0] * 12 for _ in range(len(b) - 1)]
        for n in P:
            i = bisect.bisect_right(b, n.start) - 1
            while i < len(b) - 1 and b[i] < n.end:
                ov = min(n.end, b[i + 1]) - max(n.start, b[i])
                if ov > 0:
                    w[i][n.pitch % 12] += ov
                i += 1
        self.regions = []                       # [start, end, chord|None]
        for i, vec in enumerate(w):
            c = classify_chord(vec)
            if self.regions and self.regions[-1][2] == c:
                self.regions[-1][1] = b[i + 1]
            else:
                self.regions.append([b[i], b[i + 1], c])
        self.starts = [r[0] for r in self.regions]

    def _is_dom(self, chord):
        if chord is None:
            return False
        rel, q = (chord[0] - self.tonic) % 12, chord[1]
        return (rel == 7 and q == "M") or (rel == 11 and q == "d")

    def dominant_at(self, t):
        i = max(0, bisect.bisect_right(self.starts, t) - 1)
        return self._is_dom(self.regions[i][2])

    def cadences(self, meter, tol):
        """Ticks at which a V-I / vii-I cadence (landing on a strong beat) ends."""
        out = set()
        for a, b in zip(self.regions, self.regions[1:]):
            if a[2] is None or b[2] is None or not self._is_dom(a[2]):
                continue
            rel, q = (b[2][0] - self.tonic) % 12, b[2][1]
            if rel == 0 and q in "Mm" and meter.beat_info(b[0], tol)[3]:
                out.add(b[1])
        return sorted(out)


def label_top_bass(P, tol):
    """Top Notes / Bass Notes per chord (onset slice), as defined in the paper."""
    if not P:
        return
    pitches = [n.pitch for n in P]
    mean, std = statistics.fmean(pitches), statistics.pstdev(pitches)
    notes = sorted(P, key=lambda n: n.start)
    active, prev_top, prev_bass, i = [], None, None, 0
    while i < len(notes):
        tmin = notes[i].start
        j = i
        while j < len(notes) and notes[j].start - tmin <= tol:
            j += 1
        active = [n for n in active + notes[i:j] if n.end > tmin]
        i = j
        hi = max(active, key=lambda n: (n.pitch, n.start))
        lo = min(active, key=lambda n: (n.pitch, -n.start))
        top = bass = None
        if hi.pitch > mean - 0.1 * std and (
                prev_top is None or prev_bass is None
                or abs(hi.pitch - prev_top) <= abs(hi.pitch - prev_bass)):
            top = hi
            top.top += 1
        if lo.pitch < mean + 0.1 * std and lo is not top:
            bass = lo
            bass.bass += 1
        prev_top = top.pitch if top else None
        prev_bass = bass.pitch if bass else None
    for n in P:
        n.is_top = n.top > n.bass
        n.is_bass = n.bass > n.top


# ======================================================================
#  3. Phrase identification
# ======================================================================

class Phrase:
    _count = 0

    def __init__(self, kind, notes):
        self.kind = kind                # melody | inner | bass | perc
        self.notes = notes
        self.parallel = False
        self.id = Phrase._count
        Phrase._count += 1
        for n in notes:
            n.phrase = self
        self.start = notes[0].start

    @property
    def end(self):
        return max(n.end for n in self.notes)

    def mean_pitch(self):
        return sum(n.pitch for n in self.notes) / len(self.notes)


def make_mono(notes):
    """One voice: a note ends where the next one starts; same-onset extras are dropped."""
    out = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if out and n.start == out[-1].start:
            continue
        if out and out[-1].end > n.start:
            out[-1].end = n.start
        out.append(n)
    return out


def segment_line(notes, cad, gap_max):
    """Split a monophonic line at rests and after cadences."""
    segs, cur = [], []
    for n in notes:
        if cur:
            a = cur[-1]
            ci = bisect.bisect_right(cad, a.start)
            if n.start - a.end > gap_max or (ci < len(cad) and cad[ci] <= n.start):
                segs.append(cur)
                cur = []
        cur.append(n)
    if cur:
        segs.append(cur)
    return segs


def onset_groups(notes, tol):
    groups = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and n.start - groups[-1][0].start <= tol:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def build_chains(notes, harmony, cad, cfg, tol, gap_max):
    """Group the notes of one channel into voice-leading chains.

    For each consecutive pair of chords (A, B) every note of the smaller chord is
    linked to a note of the other, nearest neighbours first; tendency tones of
    V / vii chords (leading tone up, chordal 4th down) are linked before anything
    else.  Links never cross a cadence, a long rest or a leap > cfg.max_leap.
    """
    groups = onset_groups(notes, tol)
    succ = {}
    for A, B in zip(groups, groups[1:]):
        dom = harmony.dominant_at(A[0].start)
        pairs = []
        for ia, a in enumerate(A):
            ci = bisect.bisect_right(cad, a.start)
            rel = (a.pitch - harmony.tonic) % 12
            for ib, b in enumerate(B):
                if b.start - a.end > gap_max or (ci < len(cad) and cad[ci] <= b.start):
                    continue
                d = b.pitch - a.pitch
                if dom and ((rel == 11 and d == 1) or (rel == 5 and d == -1)):
                    cost = -1
                elif abs(d) <= cfg.max_leap:
                    cost = abs(d)
                else:
                    continue
                pairs.append((cost, ia, ib))
        pairs.sort()
        ua, ub = set(), set()
        for _, ia, ib in pairs:
            if ia not in ua and ib not in ub:
                ua.add(ia)
                ub.add(ib)
                succ[A[ia]] = B[ib]
    has_pred = set(succ.values())
    chains = []
    for g in groups:
        for n in g:
            if n not in has_pred:
                c = [n]
                while c[-1] in succ:
                    c.append(succ[c[-1]])
                chains.append(c)
    return chains


# ======================================================================
#  4. Parallel phrase reduction
# ======================================================================

def parallel_reduction(phrases, tol, thr, min_notes):
    """Flag non-melody phrases whose rhythm follows a (higher) melody phrase."""
    mel = sorted((p for p in phrases if p.kind == "melody"), key=lambda p: p.start)
    cache = {p.id: [n.start for n in p.notes] for p in mel}
    for p in phrases:
        if p.kind != "inner":
            continue
        pm, pend = p.mean_pitch(), p.end
        for r in mel:
            if r.start >= pend:
                break
            if r.end <= p.start or r.mean_pitch() <= pm:
                continue
            lo, hi = max(p.start, r.start), min(pend, r.end)
            ons = [n.start for n in p.notes if lo <= n.start < hi]
            if len(ons) < min_notes:
                continue
            ron, hit = cache[r.id], 0
            for s in ons:
                k = bisect.bisect_left(ron, s)
                if (k < len(ron) and ron[k] - s <= tol) or (k > 0 and s - ron[k - 1] <= tol):
                    hit += 1
            if hit / len(ons) >= thr:
                p.parallel = True
                break


# ======================================================================
#  5. Phrase stealing
# ======================================================================

def make_rank(mip, has_perc):
    """Importance classes (smaller = keep longer), following section 5.3."""
    if mip == 1:
        return {"melody": 0, "bass": 1, "inner": 2, "perc": 3}
    if mip == 2 and has_perc:
        return {"melody": 0, "perc": 1, "bass": 2, "inner": 3}
    if mip == 2:
        return {"melody": 0, "bass": 1, "inner": 2, "perc": 3}
    return {"melody": 0, "bass": 1, "perc": 2, "inner": 3}


def steal_phrases(phrases, cfg, rank):
    """Sweep through time; when more than MIP notes sound, delete whole phrases:
    least important class first, latest-started (LIFO) within the class."""
    def importance(p):
        return rank[p.kind] + (0.5 if p.parallel and p.kind == "inner" else 0)

    by_start = defaultdict(list)
    for p in phrases:
        for n in p.notes:
            by_start[n.start].append(n)
    deleted, active = set(), []
    for t in sorted(by_start):
        active = [n for n in active if n.end > t and n.phrase not in deleted]
        active += [n for n in by_start[t] if n.phrase not in deleted]
        if len(active) <= cfg.mip:
            continue
        # a tiny legato overlap is cured by trimming the sounding note, not by
        # killing a phrase
        for n in sorted((n for n in active if n.start < t and n.end - t <= cfg.legato_ticks),
                        key=lambda n: n.end):
            if len(active) <= cfg.mip:
                break
            n.end = t
            active.remove(n)
        while len(active) > cfg.mip:
            victim = max({n.phrase for n in active},
                         key=lambda p: (importance(p), p.start, p.id))
            deleted.add(victim)
            active = [n for n in active if n.phrase is not victim]
    return [p for p in phrases if p not in deleted]


# ======================================================================
#  Driver
# ======================================================================

def _lane_free(lane, notes):
    """True if none of `notes` overlaps a note already in the (monophonic) lane."""
    starts = lane["starts"]
    for n in notes:
        i = bisect.bisect_right(starts, n.start)
        if i and lane["notes"][i - 1].end > n.start:
            return False
        if i < len(starts) and starts[i] < n.end:
            return False
    return True


def _lane_add(lane, notes):
    for n in notes:
        i = bisect.bisect_right(lane["starts"], n.start)
        lane["starts"].insert(i, n.start)
        lane["notes"].insert(i, n)


def _pack_pieces(pieces, rank):
    """Whole phrase pieces into lanes (first fit, most important first)."""
    lanes = defaultdict(list)
    for rk, home, run in sorted(pieces, key=lambda x: (x[0], x[2][0].start)):
        for lane in lanes[home]:
            if _lane_free(lane, run):
                _lane_add(lane, run)
                lane["rank"] = min(lane["rank"], rk)
                break
        else:
            lane = {"starts": [], "notes": [], "rank": rk}
            _lane_add(lane, run)
            lanes[home].append(lane)
    return lanes


def _pack_notes(pieces, rank):
    """Note by note in start order, first free lane: needs exactly as many lanes
    as the largest number of simultaneous notes in the channel (the minimum)."""
    by_home = defaultdict(list)
    for rk, home, run in pieces:
        by_home[home] += [(n, rk) for n in run]
    lanes = defaultdict(list)
    for home, items in by_home.items():
        for n, rk in sorted(items, key=lambda x: (x[0].start, x[1])):
            for lane in lanes[home]:
                if lane["notes"][-1].end <= n.start:
                    lane["notes"].append(n)
                    lane["rank"] = min(lane["rank"], rk)
                    break
            else:
                lanes[home].append({"notes": [n], "rank": rk})
    return lanes


def _share_channels(items, rank, reserved, max_lanes=None):
    """Fallback when there are not enough free channels: every output channel plays
    material of several source channels, one after another (monophonic), and is
    re-programmed whenever the source changes.  `items` = [(note, rank, source)].
    Voices are first grouped by importance so SP-MIDI channel priorities stay
    meaningful; if even that needs too many channels, all voices are mixed.
    Returns {out_channel: [(t0, t1, source_channel, track), ...]}; the notes'
    channels are updated."""
    avail = [c for c in range(16) if c not in reserved]
    cap = len(avail) if max_lanes is None else min(max_lanes, len(avail))

    def pack(group):
        lanes = []
        for n, rk, src in sorted(items, key=lambda x: (x[0].start, x[1])):
            cand = [l for l in lanes if l["g"] == group(rk) and l["notes"][-1][0].end <= n.start]
            same = [l for l in cand if l["notes"][-1][2] == src]
            lane = (same or cand or [None])[0]
            if lane is None:
                lane = {"g": group(rk), "notes": [], "rank": rk}
                lanes.append(lane)
            lane["notes"].append((n, rk, src))
            lane["rank"] = min(lane["rank"], rk)
        return lanes

    lanes = pack(lambda rk: rk)
    if len(lanes) > cap:
        lanes = pack(lambda rk: 0)             # mixed: needs only as many as the peak polyphony
    if len(lanes) > cap:
        raise SystemExit(f"--split-voices needs {len(lanes)} channels but only {cap} are allowed; "
                         f"use a lower --mip")
    taken, plan = set(), {}
    for lane in sorted(lanes, key=lambda l: (l["rank"], -len(l["notes"]))):
        count = Counter(src for _, _, src in lane["notes"])
        pref = [c for c, _ in count.most_common() if c in avail and c not in taken]
        ch = (pref or [c for c in avail if c not in taken])[0]
        taken.add(ch)
        segs = []
        for n, _, src in lane["notes"]:
            if not segs or segs[-1][2] != src:
                segs.append([n.start, None, src, n.track])
                if len(segs) > 1:
                    segs[-2][1] = n.start
            n.ch = ch
        for s in segs:
            if s[1] is None:
                s[1] = float("inf")
        plan[ch] = [tuple(s) for s in segs]
    return plan


def split_voices(kept, rank, reserved, max_channels=None):
    """Make every output channel monophonic.

    Phrases are cut into pieces at channel changes (only the merged bass line can
    have those).  Preferred layout: each original channel keeps its number for its
    most important voice and further voices of that channel move to free channel
    numbers, which copy the channel's program/controller settings.  Whole phrases
    stay on one channel when there are enough free channels; otherwise notes are
    packed individually, which needs the fewest channels possible.  If that still
    does not fit (many instruments, each with a few surviving notes), channels are
    shared between instruments, see _share_channels.
    Returns (chan_map, plan): chan_map {new_channel: source_channel} for the
    preferred layout, plan for the shared layout; the other one is empty."""
    pieces = []
    for p in kept:
        run = [p.notes[0]]
        for n in p.notes[1:] + [None]:
            if n is not None and n.ch == run[-1].ch:
                run.append(n)
                continue
            pieces.append((rank[p.kind], run[0].ch, run, p.kind))
            run = [n] if n is not None else []
    homes = {home for _, home, _, _ in pieces}
    free = [c for c in range(16) if c not in homes and c not in reserved]
    plain = [(rk, home, run) for rk, home, run, _ in pieces]

    def extras(lanes):
        return sorted((lane["rank"], home, i) for home, ls in lanes.items()
                      for i, lane in enumerate(ls) if i > 0)

    lanes = _pack_pieces(plain, rank)
    extra = extras(lanes)
    if len(extra) > len(free):
        lanes = _pack_notes(plain, rank)           # phrases may now be split across channels
        extra = extras(lanes)
    n_static = len(homes - set(reserved)) + len(extra)
    if len(extra) <= len(free) and (max_channels is None or n_static <= max_channels):
        chan_map = {}
        for (_, home, i), new in zip(extra, free):
            chan_map[new] = home
            for n in lanes[home][i]["notes"]:
                n.ch = new
        return chan_map, {}
    items = [(n, rk, n.ch) for rk, home, run, kind in pieces if kind != "perc" for n in run]
    return {}, _share_channels(items, rank, reserved, max_channels)


def channel_priority(kept, rank):
    best, cnt = {}, Counter()
    for p in kept:
        for n in p.notes:
            best[n.ch] = min(best.get(n.ch, 99), rank[p.kind])
            cnt[n.ch] += 1
    return sorted(best, key=lambda c: (best[c], -cnt[c], c))


def build_smf(song, kept_notes, pairs, chan_map=None, plan=None, drop_idle=False,
              by_channel=False, drop_empty=False):
    """Write the song back as a Standard MIDI File: the original non-note events,
    the kept notes, and (if `pairs`) the SP-MIDI MIP message at tick 0 of track 0.
    `chan_map` {new_channel: source_channel} copies the source channel's
    program/controller/pitch-bend events onto the new channel and removes the
    new channel's own.  `plan` {channel: [(t0, t1, source, track)]} (shared
    channels) replaces the events of those channels by the state of whichever
    source channel they play at the moment.  `by_channel` writes a format 1 file
    whose track 0 holds everything that is not channel-specific (tempo, time
    signature, markers, sysex...) and whose track N holds all events of channel N
    (numbered from 1), named after the input track that supplied most of its notes.
    `drop_empty` leaves out tracks (other than track 0) that hold nothing but
    track/instrument names, which renumbers the tracks that remain."""
    chan_map = chan_map or {}
    plan = plan or {}
    active = {n.ch for n in kept_notes}
    per = defaultdict(list)               # track -> [(time, order, seq, bytes)]
    names = defaultdict(dict)             # input track -> {3: track-name meta, 4: instrument-name meta}
    for ev in song.events:
        if ev.ch is None and ev.data[0] == 0xFF and ev.data[1] in (0x03, 0x04):
            names[ev.track][ev.data[1]] = ev.data

    def put(track, ch, item):
        per[(0 if ch is None else ch + 1) if by_channel else track].append(item)

    for ev in song.events:
        if by_channel and ev.ch is None and ev.data[0] == 0xFF and ev.data[1] in (0x03, 0x04, 0x20, 0x21):
            continue                       # track-bound meta events: regenerated below / meaningless
        if drop_idle and ev.ch is not None and ev.ch not in active:
            continue                       # a channel without notes must not appear at all
        if ev.ch is not None and ev.ch in plan:
            continue                       # regenerated below
        if ev.ch is not None and chan_map:
            if ev.ch in chan_map:          # channel now belongs to a split voice
                continue
            for new, src in sorted(chan_map.items()):
                if src == ev.ch:
                    put(ev.track, new, (ev.time, 1, ev.idx,
                                        bytes([(ev.data[0] & 0xF0) | new]) + ev.data[1:]))
        put(ev.track, ev.ch, (ev.time, 1, ev.idx, ev.data))
    if plan:
        by_src = defaultdict(list)         # source channel -> state events, in time order
        keyed = defaultdict(lambda: defaultdict(lambda: ([], [])))
        for ev in sorted((e for e in song.events if e.ch is not None), key=lambda e: (e.time, e.idx)):
            by_src[ev.ch].append(ev)
            hi = ev.data[0] & 0xF0
            key = (hi, ev.data[1]) if hi == 0xB0 else (hi,) if hi in (0xC0, 0xE0) else None
            if key:
                keyed[ev.ch][key][0].append(ev.time)
                keyed[ev.ch][key][1].append(ev)
        etimes = {s: [e.time for e in evs] for s, evs in by_src.items()}
        snap = -10 ** 9
        for ch, segs in sorted(plan.items()):
            for k, (t0, t1, src, track) in enumerate(segs):
                state = []
                for times, evs in keyed[src].values():
                    i = bisect.bisect_right(times, t0) - 1
                    if i >= 0:
                        state.append(evs[i])
                # controllers in their original order, then program, then pitch bend
                state.sort(key=lambda e: ((e.data[0] & 0xF0) != 0xB0, (e.data[0] & 0xF0) == 0xE0,
                                          e.time, e.idx))
                at = 0 if k == 0 else t0
                for e in state:
                    snap += 1
                    put(track, ch, (at, 1, snap, bytes([(e.data[0] & 0xF0) | ch]) + e.data[1:]))
                lo = bisect.bisect_right(etimes.get(src, []), t0)
                hi_ = bisect.bisect_left(etimes.get(src, []), t1) if t1 != float("inf") \
                    else len(etimes.get(src, []))
                for e in by_src.get(src, [])[lo:hi_]:
                    put(track, ch, (e.time, 1, e.idx, bytes([(e.data[0] & 0xF0) | ch]) + e.data[1:]))
    for seq, n in enumerate(kept_notes):
        put(n.track, n.ch, (n.start, 2, seq, bytes([0x90 | n.ch, n.pitch, n.vel])))
        put(n.track, n.ch, (n.end, 0, seq, bytes([0x80 | n.ch, n.pitch, n.off_vel])))
    if pairs:
        body = bytes([0x7F, 0x7F, 0x0B, 0x01] + [x for pr in pairs for x in pr] + [0xF7])
        per[0].append((0, -1, 0, b"\xf0" + _vlq(len(body)) + body))
    if by_channel:
        label = -3 * 10 ** 9
        src_track = {}
        for ch in active:                  # input track that supplies most notes of the channel
            src_track[ch + 1] = Counter(n.track for n in kept_notes if n.ch == ch).most_common(1)[0][0]
        src_track[0] = 0
        for out_track, in_track in src_track.items():
            for mt in (0x03, 0x04):
                if mt in names.get(in_track, {}) and (out_track or mt == 0x03):
                    label += 1
                    per[out_track].append((0, -3, label, names[in_track][mt]))
        ntracks = 1 + max(per, default=0)
        fmt = 1
        end_all = max(song.track_end.values(), default=0)
    else:
        ntracks, fmt, end_all = max(1, song.ntracks), song.fmt, None
    tracks = list(range(ntracks))
    if drop_empty:
        tracks = [t for t in tracks if t == 0 or any(
            not (item[3][0] == 0xFF and item[3][1] in (0x03, 0x04)) for item in per.get(t, []))]
    out = [b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), song.division)]
    for t in tracks:
        buf, prev = bytearray(), 0
        for time, _, _, data in sorted(per.get(t, [])):
            buf += _vlq(time - prev) + data
            prev = time
        end = max(end_all, prev) if by_channel else max(song.track_end.get(t, 0), prev)
        buf += _vlq(end - prev) + b"\xff\x2f\x00"
        out.append(b"MTrk" + struct.pack(">I", len(buf)) + bytes(buf))
    return b"".join(out)


def transcode(data, cfg, log=lambda *a: None):
    song = parse_smf(data)
    notes = song.notes
    stats = {"orig_notes": len(notes), "orig_mip": max_polyphony(notes)}
    ppqn = song.division
    meter = Meter(ppqn, song.time_sigs)
    tol = max(1, ppqn // 32)                              # "same chord" onset tolerance
    drum_ch = set(cfg.drum_channels)
    fits = stats["orig_mip"] <= cfg.mip
    drums = None
    # --reserve-drums: percussion gets its own voice and is never stolen; everything
    # else is reduced to MIP-1 voices
    reserve = cfg.reserve_drums and any(n.ch in drum_ch for n in notes)
    limit = cfg.mip - 1 if reserve else cfg.mip
    if reserve:
        drums = reduce_percussion([n for n in notes if n.ch in drum_ch], meter, ppqn, tol)
        fits = max_polyphony([n for n in notes if n.ch not in drum_ch]) <= limit
    elif cfg.mono_drums or not fits:
        # merge the percussion channels and keep one voice at a time
        drums = reduce_percussion([n for n in notes if n.ch in drum_ch], meter, ppqn, tol)
        if cfg.mono_drums:
            fits = max_polyphony([n for n in notes if n.ch not in drum_ch] + drums) <= cfg.mip
    chan_map, plan = {}, {}
    if fits and not cfg.split_voices:
        log(f"Song fits MIP {cfg.mip} (original MIP {stats['orig_mip']}); only adding the MIP message.")
        kept = notes if drums is None else [n for n in notes if n.ch not in drum_ch] + drums
        kept_phr, rank = None, make_rank(cfg.mip, False)
        stats.update(phrases=0, avg_len=0.0, tonic=None, cadences=0, parallel=0)
    else:
        gap_max = int(cfg.gap_beats * ppqn)
        cfg.legato_ticks = max(1, int(cfg.legato_frac * ppqn))
        if drums is None:
            drums = reduce_percussion([n for n in notes if n.ch in drum_ch], meter, ppqn, tol)
        # a song that already fits only needs voice splitting: nothing to steal,
        # so don't remove parallel phrases either
        par_mode = "off" if fits else cfg.parallel
        pitched = dedupe_unisons([n for n in notes if n.ch not in drum_ch])
        end_tick = max(n.end for n in notes)

        tonic = find_key(pitched) if pitched else 0
        harmony = Harmony(pitched, meter, end_tick, tonic)
        cad = harmony.cadences(meter, tol)
        label_top_bass(pitched, tol)

        phrases = []
        bass_line = make_mono([n for n in pitched if n.is_bass])
        for seg in segment_line(bass_line, cad, gap_max):
            phrases.append(Phrase("bass", seg))
        # bass-flagged notes that lost out when the bass line was made monophonic
        # (same onset) are dropped, everything else goes through chain building
        rest = [n for n in pitched if not n.is_bass]
        by_ch = defaultdict(list)
        for n in rest:
            by_ch[n.ch].append(n)
        for ch in sorted(by_ch):
            for chain in build_chains(by_ch[ch], harmony, cad, cfg, tol, gap_max):
                for x, y in zip(chain, chain[1:]):        # a phrase is one voice
                    if x.end > y.start:
                        x.end = y.start
                top = sum(n.is_top for n in chain) / len(chain)
                phrases.append(Phrase("melody" if top >= 0.5 else "inner", chain))
        for seg in segment_line(drums, cad, gap_max):
            if cfg.drum_phrase_bars <= 0:
                phrases.append(Phrase("perc", seg))
                continue
            # a drum line is a repeating pattern, not one gesture: cut it into
            # bar-sized phrases so a brief overload costs only that bar of drums
            part = []
            for n in seg:
                i, mnum = meter.measure(n.start)
                key = (i, mnum // cfg.drum_phrase_bars)
                if part and key != part[0][0]:
                    phrases.append(Phrase("perc", [x for _, x in part]))
                    part = []
                part.append((key, n))
            if part:
                phrases.append(Phrase("perc", [x for _, x in part]))

        n_formed = len(phrases)
        n_par = 0
        if par_mode != "off":
            parallel_reduction(phrases, max(tol, ppqn // 16), cfg.parallel_thr, cfg.parallel_min)
            n_par = sum(p.parallel for p in phrases)
            if par_mode == "eager":
                phrases = [p for p in phrases if not p.parallel]
        rank = make_rank(cfg.mip, bool(drums))      # channel priorities in the MIP message
        if reserve:
            perc = [p for p in phrases if p.kind == "perc"]
            scfg = copy.copy(cfg)
            scfg.mip = limit
            kept_phr = steal_phrases([p for p in phrases if p.kind != "perc"], scfg,
                                     make_rank(limit, False)) + perc
        else:
            kept_phr = steal_phrases(phrases, cfg, rank)
        if cfg.split_voices:
            if cfg.limit_channels and cfg.mip < 2:
                kept_phr = [p for p in kept_phr if p.kind != "perc"]   # no room for a drum channel
            max_ch = None
            if cfg.limit_channels:                  # the percussion channel counts as one
                max_ch = cfg.mip - (1 if any(p.kind == "perc" for p in kept_phr) else 0)
            chan_map, plan = split_voices(kept_phr, rank, drum_ch | {9}, max_ch)
        kept = [n for p in kept_phr for n in p.notes]
        tot = sum(len(p.notes) for p in phrases)
        stats.update(phrases=n_formed, avg_len=(tot / len(phrases) if phrases else 0.0),
                     tonic=tonic, cadences=len(cad), parallel=n_par,
                     kinds=Counter(p.kind for p in kept_phr))
    stats["kept_notes"] = len(kept)
    stats["new_mip"] = max_polyphony(kept)

    pairs = []
    if kept_phr is not None:
        order = channel_priority(kept_phr, rank)
    else:
        order = sorted({n.ch for n in kept})
    for i, ch in enumerate(order):
        sel = set(order[:i + 1])
        pairs.append((ch, min(127, max_polyphony([n for n in kept if n.ch in sel]))))
    stats["priority"] = pairs
    stats["split_channels"] = len(chan_map)
    stats["shared_channels"] = bool(plan)
    stats["channels"] = len({n.ch for n in kept})
    return build_smf(song, kept, pairs, chan_map, plan, cfg.limit_channels, cfg.tracks_by_channel,
                     cfg.drop_empty_tracks), stats


def parse_args(argv):
    ap = argparse.ArgumentParser(description="MIDI -> SP-MIDI transcoder using phrase stealing.")
    ap.add_argument("input", help="input .mid file")
    ap.add_argument("output", help="output SP-MIDI .mid file")
    ap.add_argument("-m", "--mip", type=int, default=4, help="target MIP (default 4)")
    ap.add_argument("--drum-channels", default="10",
                    help="comma-separated 1-based percussion channels (default 10)")
    ap.add_argument("--gap-beats", type=float, default=2.0,
                    help="a rest longer than this ends a phrase (default 2 beats)")
    ap.add_argument("--max-leap", type=int, default=12,
                    help="largest melodic leap (semitones) kept inside one phrase (default 12)")
    ap.add_argument("--parallel", choices=("eager", "lazy", "off"), default="eager",
                    help="parallel phrase reduction: eager = paper (remove up front), "
                         "lazy = only prefer them as victims, off")
    ap.add_argument("--parallel-thr", type=float, default=0.8,
                    help="fraction of onsets that must coincide with the melody (default 0.8)")
    ap.add_argument("--parallel-min", type=int, default=3,
                    help="minimum overlapping notes to call a phrase parallel (default 3)")
    ap.add_argument("--legato-frac", type=float, default=0.0625,
                    help="overlaps up to this fraction of a beat are trimmed instead of "
                         "stealing a phrase (default 1/16)")
    ap.add_argument("--mono-drums", action="store_true",
                    help="always reduce the percussion channel to one voice (Table 1 priorities), "
                         "even if the song already fits the target MIP")
    ap.add_argument("--reserve-drums", action="store_true",
                    help="give the percussion its own voice (one hit at a time, never removed) and "
                         "reduce everything else to MIP-1 voices; implies --mono-drums")
    ap.add_argument("--limit-channels", action="store_true",
                    help="use at most MIP channels in total (percussion counts as one): implies "
                         "--split-voices and, for --mip >= 2, --reserve-drums; at --mip 1 the "
                         "percussion is dropped")
    ap.add_argument("--tracks-by-channel", action="store_true",
                    help="write a format 1 file with track 0 for global events (tempo, time "
                         "signature, markers) and track N for all events of channel N "
                         "(numbered from 1)")
    ap.add_argument("--drop-empty-tracks", action="store_true",
                    help="leave out tracks that contain nothing but names (track 0 is always kept); "
                         "the remaining tracks are renumbered, so with --tracks-by-channel the track "
                         "number no longer equals the channel number.  Channel settings "
                         "(program, volume...) keep their track alive; --limit-channels removes "
                         "the settings of channels without notes")
    ap.add_argument("--drum-phrase-bars", type=int, default=1, metavar="N",
                    help="percussion is cut into phrases of N bars, so overloads remove drums for "
                         "N bars only (default 1); 0 = whole drum lines between rests/cadences")
    ap.add_argument("--split-voices", action="store_true",
                    help="make every channel monophonic: voices that overlap within one channel "
                         "move to extra channels, which copy that channel's program/controller "
                         "settings; if there are too few free channels, channels are shared "
                         "between instruments and re-programmed as needed (implies --mono-drums)")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    a.drum_channels = [int(x) - 1 for x in a.drum_channels.split(",") if x.strip()]
    if a.mip < 1:
        ap.error("--mip must be >= 1")
    if a.limit_channels:
        a.split_voices = True
        a.reserve_drums = a.mip >= 2
    if a.reserve_drums:
        a.mono_drums = True
        if a.mip < 2:
            ap.error("--reserve-drums needs --mip >= 2")
    if a.split_voices:
        a.mono_drums = True                 # a monophonic percussion channel is required
    return a


def main(argv=None):
    cfg = parse_args(argv)
    log = (lambda *a: None) if cfg.quiet else (lambda *a: print(*a, file=sys.stderr))
    with open(cfg.input, "rb") as fh:
        data = fh.read()
    out, st = transcode(data, cfg, log)
    with open(cfg.output, "wb") as fh:
        fh.write(out)
    if not cfg.quiet:
        names = "C C# D D# E F F# G G# A A# B".split()
        drop = 100.0 * (1 - st["kept_notes"] / max(1, st["orig_notes"]))
        log(f"MIP {st['orig_mip']} -> {st['new_mip']} (target {cfg.mip})")
        log(f"notes kept {st['kept_notes']}/{st['orig_notes']}  ({drop:.1f}% dropped)")
        if st["phrases"]:
            log(f"key {names[st['tonic']]}  cadences {st['cadences']}  phrases formed {st['phrases']}  "
                f"avg phrase length {st['avg_len']:.1f} notes  parallel phrases {st['parallel']}")
            log("phrases kept: " + ", ".join(f"{k} {v}" for k, v in sorted(st["kinds"].items())))
        if cfg.split_voices:
            log(f"channels used: {st['channels']}")
            log("voice splitting: " + ("channels shared between instruments (too many instruments "
                                      "for one channel each)" if st["shared_channels"]
                                      else f"{st['split_channels']} extra channel(s)"))
        log("channel priority (1-based ch: cumulative MIP): " +
            ", ".join(f"{c + 1}:{m}" for c, m in st["priority"]))
    return 0 if st["new_mip"] <= cfg.mip else 1


if __name__ == "__main__":
    sys.exit(main())
