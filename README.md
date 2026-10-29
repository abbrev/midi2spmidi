# midi2spmidi

Converts a Standard MIDI File to an SP-MIDI (Scalable Polyphony MIDI) file by *phrase stealing*, the algorithm in Lui, Horner and Ayers, ["MIDI to SP-MIDI Transcoding Using Phrase Stealing"](https://www.researchgate.net/publication/3338893_MIDI_to_SP-MIDI_Transcoding_Using_Phrase_Stealing). The output is an ordinary MIDI file whose polyphony never exceeds the target MIP (Maximum Instantaneous Polyphony), plus an SP-MIDI MIP message so SP-MIDI players can scale down further.

Single file, Python 3, no dependencies. Written by Claude, an AI assistant made by Anthropic, from the paper's description of the algorithm and directed and tested by its user. Released to the public domain under the Unlicense (see [License](#license)).

```
python3 midi2spmidi.py in.mid out.mid -m 4
```

Exit status is 0 when the output meets the target MIP, 1 otherwise. A summary is printed to stderr unless `-q` is given.

## The algorithm

1. **Preprocessing.** Same-channel unisons are merged. All percussion channels are merged into one (the lowest-numbered) and cut to one voice at a time, choosing among simultaneous hits with the paper's Table 1 priorities.
2. **Feature extraction.** Key (pitch-class ranking), triad chords per beat, and Top/Bass notes.
3. **Phrase identification.** Notes of each channel are chained into voice-leading lines (phrases), closed at long rests and after V-I / vii-I cadences. The bass notes form one monophonic bass line. Phrases are classed as melody, inner, bass or percussion.
4. **Parallel phrase reduction.** Inner phrases that follow a higher melody phrase rhythmically are flagged and (by default) removed up front.
5. **Phrase stealing.** Sweeping through time, whenever more than MIP notes sound, whole phrases are deleted: least important class first, and within a class the most recently started first (LIFO).

Importance order (most to least important): melody, bass, percussion, inner. At MIP 1 the order is melody, bass, inner, percussion. At MIP 2 with percussion present it is melody, percussion, bass, inner.

If the song already fits the target, nothing is removed and only the MIP message is added (unless one of the options below asks for more).

## Output

The MIP message is a universal real-time SysEx event at tick 0 of track 0:

```
F0 7F 7F 0B 01  ch mip  [ch mip ...]  F7
```

Channels are listed in priority order, most important first. Each `mip` value is the polyphony needed to play that channel plus all channels before it. Any MIP message already in the input is replaced. The layout was checked against the Sonivox/Android parser; the exact meaning of the cumulative values comes from the SP-MIDI specification as I recall it, so check it against RP-034 if you rely on it. The output has not been tested on hardware or with external SP-MIDI players.

## Options

`input` and `output` are `.mid` files. SMPTE time division is not supported.

### Target

| Option | Default | Description |
|---|---|---|
| `-m`, `--mip N` | 4 | Target MIP: the largest number of notes allowed to sound at once. |
| `-q`, `--quiet` | off | Suppress the summary on stderr. |

### Percussion

| Option | Default | Description |
|---|---|---|
| `--drum-channels LIST` | `10` | Comma-separated, 1-based percussion channels, for example `10` or `10,11`. Only these channels get percussion treatment. Pitched drums such as timpani on melodic channels are treated as ordinary instruments. |
| `--mono-drums` | off | Always cut the percussion to one voice (Table 1 priorities), even if the song already fits the target MIP. |
| `--reserve-drums` | off | Give the percussion its own voice that is never stolen, and reduce everything else to `MIP - 1` voices. Implies `--mono-drums`. Needs `-m 2` or higher. Has no effect on a song without percussion. |
| `--drum-phrase-bars N` | 1 | Percussion is cut into phrases of N bars, so an overload removes the drums for N bars only. `0` keeps whole drum lines between rests and cadences (removing a whole line for a single overload). |

### Phrase analysis

| Option | Default | Description |
|---|---|---|
| `--gap-beats X` | 2.0 | A rest longer than this many beats ends a phrase. |
| `--max-leap N` | 12 | Largest melodic leap, in semitones, kept inside one phrase. |
| `--parallel {eager,lazy,off}` | `eager` | Parallel phrase reduction. `eager` removes parallel phrases up front (as in the paper), `lazy` only prefers them as victims when stealing, `off` disables it. |
| `--parallel-thr X` | 0.8 | Fraction of a phrase's onsets that must coincide with a melody phrase for it to count as parallel. |
| `--parallel-min N` | 3 | Minimum number of overlapping notes to call a phrase parallel. |
| `--legato-frac X` | 0.0625 | Overlaps up to this fraction of a beat are trimmed instead of causing a phrase to be stolen. |

### Channels

| Option | Default | Description |
|---|---|---|
| `--split-voices` | off | Make every channel monophonic. Voices that overlap within one channel move to extra channels, which copy that channel's program, controller and pitch-bend settings. If there are too few free channels, channels are shared between instruments and re-programmed each time a channel switches to a different source instrument. Implies `--mono-drums`. Works even when the song already fits the target. |
| `--limit-channels` | off | Use at most MIP channels in total, counting the percussion channel as one. Implies `--split-voices` and, for `-m 2` and higher, `--reserve-drums`. At `-m 1` the percussion is dropped. Events of channels without notes are removed so the unused channels do not appear in the file. |

Notes on channel handling:

- Notes keep their pitch, timing and velocity. Only the channel can change.
- Voices are grouped by importance (melody, bass, inner) so SP-MIDI channel priorities stay meaningful. If that needs too many channels, the groups are mixed. Because of that, a device that drops one low-priority channel drops all of its notes, which may come from several instruments.
- Each channel switch sends program and controller messages. A device that is slow to change instruments may lag at the switches.
- A channel keeps its own number when it supplies most of the notes on its output channel.

### Tracks

| Option | Default | Description |
|---|---|---|
| `--tracks-by-channel` | off | Write a format 1 file with track 0 for everything that is not channel-specific (tempo, time signature, markers, text, SysEx, the MIP message) and track N for all events of channel N, numbered from 1. Each channel track is named after the input track that supplied most of its notes. Track-bound meta events (MIDI port, channel prefix) are dropped. Unused channels in between leave empty tracks. |
| `--drop-empty-tracks` | off | Leave out tracks that hold nothing but track or instrument names. Track 0 is always kept and the remaining tracks are renumbered, so with `--tracks-by-channel` the track number no longer equals the channel number. A track with channel settings (program, volume, ...) is kept, because the settings may belong to a channel whose notes are on another track; `--limit-channels` removes the settings of channels without notes. |

Without `--tracks-by-channel`, notes stay on the track they came from, even when `--split-voices` or `--limit-channels` changes their channel. Instrument settings copied to a new channel go on the track of the settings they were copied from, or, for shared channels, on the track of the first note of that stretch.

### Option dependencies

```
--limit-channels  -> --split-voices -> --mono-drums
                  -> --reserve-drums (when -m >= 2) -> --mono-drums
```

## Examples

```
# Plain reduction to 4 voices
python3 midi2spmidi.py in.mid out.mid -m 4

# Drums never dropped, everything else limited to 3 voices
python3 midi2spmidi.py in.mid out.mid -m 4 --reserve-drums

# Every channel monophonic, instruments split across channels as needed
python3 midi2spmidi.py in.mid out.mid -m 6 --split-voices

# At most 4 channels in total, one track per channel, no empty tracks
python3 midi2spmidi.py in.mid out.mid -m 4 --limit-channels --tracks-by-channel --drop-empty-tracks

# Keep more of a song that has many parts rhythmically doubling the melody
python3 midi2spmidi.py in.mid out.mid -m 4 --parallel lazy
```

## Interpretations and limits

- The paper leaves some details open. Choices made here: chords are grouped when their onsets are within 1/128 of a note; the bass line is split at rests and cadences so one conflict does not delete it for the whole song; percussion is cut into one-bar phrases by default; at MIP 1 the percussion ranks last.
- Voices that outnumber the available channels are handled by sharing channels (see `--split-voices`). The tool stops with an error only if even mixed sharing cannot fit.
- The tool works on the 16 channels of one port. MIDI port events are not interpreted.
- Phrase stealing deletes whole phrases, so a brief overload can remove more notes than strictly needed. This is how the algorithm works; `--drum-phrase-bars` softens it for percussion.
- Receiving devices may count note-release tails as active voices, so hardware with exactly the target number of voices may need some headroom.
- No protection for a chosen channel yet: an important part (for example a chordal synth) can be dropped when higher-ranked parts use all the voices. `--parallel off` or a higher `-m` helps in some cases.

## Tests

`test_demo.py` builds a synthetic song and random songs and checks the guarantees: output MIP at most the target, MIP message present, monophonic channels with `--split-voices`, correct instruments on shared channels, channel and track counts, and the drum behavior. Run it with `python3 test_demo.py`.

## License

This software is released into the public domain under the [Unlicense](https://unlicense.org/). The full text is in the `UNLICENSE.md` file.
