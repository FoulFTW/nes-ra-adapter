# RA2Snes Achievement Detection Analysis

## Overview

This document analyzes why RA2Snes may fail to trigger achievements on some games, particularly **timed achievements**, and proposes potential fixes. The analysis is based on the [RA2Snes](https://github.com/Factor-64/RA2Snes) codebase and the [FXPak/SD2Snes](https://github.com/wuest/fxpak) USB2SNES protocol.

## How RA2Snes Works

1. **Connection**: RA2Snes connects to QUsb2Snes/SNI (WebSocket at `ws://localhost:23074`) which bridges to the FXPak Pro/SD2Snes via USB.

2. **Memory Reading**: Two modes exist:
   - **Standard mode** (stock firmware): Polls memory via `GetAddress` commands when `programTime + vgetTime > 15ms` (~60–66 polls/second)
   - **NMI mode** (custom firmware 2025/2026): Injects an NMI hook that captures memory at vblank; more frame-accurate

3. **Frame Estimation**: When memory is received, RA2Snes calculates elapsed frames:
   ```cpp
   framesPassed = std::round((vgetTime + programTime) * 0.0600988138974405);
   ```
   The constant `0.0600988138974405` ≈ NTSC SNES refresh rate (60.098 Hz). So ~16.64ms per frame.

4. **Trigger Evaluation**: The same memory snapshot is passed to `processFrames()` which evaluates rcheevos triggers for each "frame" in a loop—treating one snapshot as if it represented N consecutive frames.

## Root Causes of Achievement Failures

### 1. **Frame-Perfect Timing (Primary Issue)**

The README explicitly states:
> "Achievements might have a tight frame window. This may cause achievements not to activate because reading the memory of SD2Snes is not frame-perfect."

**Why**: Many achievements require a specific memory state at a specific frame. With polling:
- You get **one snapshot** per poll (every ~16ms)
- The snapshot is taken at an **arbitrary moment** in the frame
- USB latency varies (typically 1–10ms+)
- If the achievement condition is true for only 1–2 frames, you can easily miss it

### 2. **Timed/Measured Achievements**

Timed achievements (e.g., "Beat the level in under 5 minutes") rely on:
- **Measured values** from memory (e.g., in-game timer)
- **Frame counting** for when to sample

**Problems**:
- One memory read is treated as N frames with identical data
- In reality, the in-game timer increments each frame
- Frame estimation can drift (e.g., 59 vs 61 frames between polls)
- Measured progress may be evaluated at wrong "frames"

### 3. **Polling Frequency vs. USB Latency**

- Minimum interval: 15ms between polls
- SNES frame: ~16.64ms
- USB round-trip: variable (often 2–8ms)
- Effective rate: often 50–60 samples/sec instead of a steady 60.098

### 4. **Games with Enhancement Chips**

RA2Snes cannot read memory from games using:
- GSU (Super FX)
- SA1
- SDD1
- OBC1
- CX4
- Super Game Boy

These games will not trigger achievements at all.

## Potential Fixes

### Fix 1: Use Custom Firmware with NMI Hook (Recommended)

**Impact**: High for frame-sensitive achievements

The custom firmware (detected when version contains "2025" or "2026") uses an NMI hook to capture memory at vblank. This is the most accurate approach.

**Action**: Ensure you are using firmware that supports the NMI hook. Check RA2Snes displays "Custom firmware detected" when a game is loaded.

### Fix 2: Increase Polling Frequency (RA2Snes Code Change)

**Current**: Poll when `programTime + vgetTime > 15`

**Proposed**: Reduce the threshold to poll more often:
```cpp
// In ra2snes.cpp, GetConsoleAddresses and GetNMIData cases
if(programTime + vgetTime > 10)  // was 15 - poll ~100 times/sec
```

**Trade-off**: More USB traffic; may stress the connection. Test for stability.

### Fix 3: Process Each Snapshot as 1 Frame (Conservative Approach)

**Current behavior**: One snapshot → N frames (estimated from elapsed time)

**Alternative**: Process each memory read as exactly 1 frame. This avoids "replaying" the same state across multiple frames, which can confuse measured achievements.

In `ra2snes.cpp`:
```cpp
// Change from:
unsigned int framesPassed = std::round(std::abs((vgetTime + programTime) * 0.0600988138974405));
if(framesPassed < 1) framesPassed = 1;

// To:
unsigned int framesPassed = 1;  // One poll = one frame
```

**Trade-off**: May miss achievements that require conditions to persist multiple frames. Could be combined with higher poll rate.

### Fix 4: Reduce Memory Read Size for Faster Polls

**Current**: RA2Snes reads all unique addresses needed by achievements. Fewer achievements = fewer addresses = faster response.

**From README**: "I recommend unlocking more achievements before retrying an achievement, as the fewer achievements there are, the fewer memory values need to be read from SD2Snes."

**Action**: When retrying a stubborn achievement, temporarily reset other achievements for that game to reduce payload size and improve poll rate.

### Fix 5: FXPak Firmware Enhancements

The [FXPak firmware](https://github.com/wuest/fxpak) could potentially:
- Add a **frame-synchronized read** mode that captures at vblank
- Return a **frame counter** with each read for more accurate timing
- Optimize the GetAddress response path for lower latency

This would require firmware changes and corresponding RA2Snes updates.

### Fix 6: Improve Frame Estimation (RA2Snes)

The current formula uses wall-clock time. Improvements:
- Account for **average USB latency** (measure round-trip and subtract)
- Use **exponential smoothing** for frame rate if it drifts
- Add **PAL detection** (50 Hz) for European games

## Summary Table

| Fix | Effort | Impact | Risk |
|-----|--------|--------|------|
| Use custom NMI firmware | Low | High | Low |
| Increase poll frequency | Low | Medium | Medium |
| 1 snapshot = 1 frame | Low | Unknown | Medium |
| Reduce address count | User action | Medium | None |
| FXPak firmware changes | High | High | Medium |
| Better frame estimation | Medium | Medium | Low |

## Recommended Actions

1. **Users**: Use custom firmware with NMI support when available. Unlock easier achievements first to reduce memory read size.
2. **RA2Snes developers**: Consider a configurable poll interval and/or "aggressive mode" for timed achievements. Add PAL frame rate support.
3. **FXPak developers**: Explore vblank-synchronized reads or frame counter in the protocol.

## References

- [RA2Snes GitHub](https://github.com/Factor-64/RA2Snes)
- [RA2Snes Releases](https://github.com/Factor-64/RA2Snes/releases)
- [FXPak Pro GitHub](https://github.com/wuest/fxpak)
- [rcheevos Library](https://github.com/RetroAchievements/rcheevos)
- [QUsb2Snes](https://github.com/Skarsnik/QUsb2snes)
