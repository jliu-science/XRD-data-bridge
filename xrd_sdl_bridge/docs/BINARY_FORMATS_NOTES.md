# Binary formats: notes for Bruker .raw and PANalytical .rd

Status (2026-11-22): **not implemented yet**. The UI chips stay disabled until real sample files are
available to validate against. These notes describe the public file layouts as documented by the
open-source xylib project (LGPL, `xylib/bruker_raw.cpp`, `xylib/philips_raw.cpp`). They are a layout
reference only; no code was copied. All values are little-endian.

## PANalytical / Philips PC-APD `.rd` (V3) and `.sd` (V5)

- Magic: bytes 0–3 = `V3RD` or `V5RD`.
- 84: uint8 diffractometer type; 85: uint8 anode index (0 Cu, 1 Mo, 2 Fe, 3 Cr, 4 Other); 86: uint8 focus.
- 138: char[8] file name; 146: char[20] sample id.
- 214: float64 step; 222: float64 start 2θ; 230: float64 end 2θ. Points = (end-start)/step + 1.
- Data: V3 at 250, V5 at 810. uint16 per point, **sqrt-encoded**: counts = floor(0.01 · p²).
- No wavelength or time per step is stored (only the anode index).

**Important: lossless writing is impossible for `.rd`.** A count c can only be stored exactly if some
integer p satisfies floor(p²/100) = c. Above roughly 2,500 counts the gap between neighbouring squares
is larger than 100, so most count values cannot be represented. Reading `.rd` is fine; writing `.rd`
would change data points. This conflicts with the lab rule that conversion must never alter data, so
`.rd` writing should stay off unless the user explicitly accepts that loss.

## Bruker `.raw`

Version is detected from the magic: `RAW ` (v1), `RAW2` (v2), `RAW1.01` (v3), `RAW4.00` (v4).
Intensities are always **float32**, so integer counts up to 16,777,216 round-trip exactly. Non-integer
intensities lose precision beyond float32 (~7 significant digits).

- **v1**: no file header. Each range: +0 uint32 steps, +4 float32 time/step, +8 float32 step, +12 uint32
  scan mode, +20 float32 start 2θ, +24/28/32 float32 theta/khi/phi start, +36 char[32] sample, +68/72
  float32 Kα1/Kα2, +148 uint32 "following range" flag, data float32[steps] at +152. Later ranges may be
  prefixed with an extra `RAW ` tag.
- **v2**: 256-byte file header: 4 uint16 range count, 168 char[20] date/time, 188 char[2] anode,
  190/194 float32 λ1/λ2, 198 float32 ratio, 210 float32 runtime. Each block: +0 uint16 header length,
  +2 uint16 steps, +8 float32 s/step, +12 float32 step, +16 float32 start 2θ, +46 uint16 temperature K,
  data float32[steps] at block + header length.
- **v3 (`RAW1.01`)**: 712-byte file header: 8 uint32 status, 12 uint32 range count, 16 char[10] date,
  26 char[10] time, 36 char[72] user, 108 char[218] site, 326 char[60] sample id, 386 char[160] comment,
  608 char[4] anode, 616/624/632/640/648 float64 Kα-average/Kα1/Kα2/Kβ/ratio, 664 float32 measure time.
  Each range: 304-byte header (+0 uint32 = 304, +4 uint32 steps, +8 float64 start θ, +16 float64 start
  2θ, +176 float64 step, +192 float32 time/step, +224/228 uint32 kV/mA, +240 float64 λ used, +256 uint32
  supplementary-header size), then the supplementary bytes, then float32[steps].
- **v4 (`RAW4.00`)**: 61-byte header (12 char[12] date, 24 char[10] time), then segments
  (uint32 type, uint32 length): 10 = VarInfo, 30 = hardware (+80 float64 Kα1, +88 Kα2, +116 char[4]
  anode), 60 = drive alignment. Ranges start at a type word 0 or 160: +32 char[24] scan type,
  +72 float64 start, +80 float64 step, +88 uint32 steps, +92 float32 time/step, +112 float64 λ used,
  +136 uint32 datum size (4), +140 uint32 sub-segment bytes; data float32[steps] at +160 + sub-segment
  bytes.

## Plan for Monday

1. Read the user's real `.raw` (Bruker) and `.rd` (PANalytical) sample files; confirm the version and
   fields against this layout.
2. Implement readers for both, and a Bruker RAW v3 writer (the most widely read version).
3. Validate: our output read by an independent reader (HighScore / EVA / xylib) gives identical points.
4. Keep `.rd` read-only unless the user accepts the sqrt-encoding loss.
