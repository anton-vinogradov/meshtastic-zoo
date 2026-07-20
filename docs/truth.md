# Truth stack v2 — reclassification (2026-07-20)

Revision of every "bring nodes to clean water" approach after measuring on the
live mesh (7 days of data, 66 positioned nodes, 171 xlink edges from traceroutes).

## Measured baseline: SNR→distance does not work in the city

Calibration `snr = A + B·log10(d)` on every available subset:

| subset                              | n  | r     | slope B      | σ      | distance error |
|-------------------------------------|----|-------|--------------|--------|----------------|
| our yard → GPS neighbors (all)      | 39 | −0.32 | −5.1 dB/dec  | 8.5 dB | ×46            |
| city-wide, GPS↔GPS from traces      | 46 | −0.29 | −4.9         | 8.4    | ×50            |
| city-wide, pairs ≥300 m             | 42 | −0.18 | −3.5         | 8.6    | ×298           |
| stable links only (LOS candidates)  | 25 | −0.41 | −7.0         | 8.2    | ×15            |

Shadowing spread (~8.5 dB) dwarfs the informative slope (−5 dB/decade instead
of the theoretical −20: only links with a lucky clearance survive — survivor
bias flattens the fit). A "1 km" ring really means "70 m to 15 km".
**Metric SNR multilateration is dead**; the `r ≤ −0.4` gate stays — it will
revive itself if mesh geometry ever changes (spread anchors, wardriving).

Second problem: our own anchor baseline is **360 meters** (all 4 nodes nearly
co-located). Self-triangulation is range-blind; only city anchors (GPS/address
nodes) reached through the traceroute xlink graph help.

## Position trust classes (`node.posCls`)

| class | meaning                              | source                            |
|-------|--------------------------------------|-----------------------------------|
| **A** | manually placed / verified           | config.geo; geocode matched to GPS |
| **B** | claimed GPS, unrefuted               | broadcast position, no posSus     |
| **C** | claimed GPS refuted by physics       | posSus: direct RX incompatible with claimed range |
| **D** | address from name (soft anchor)      | Nominatim geocode, unverified     |
| **E** | graph/signal estimate                | est: centroid of hearers + xlink partners, honest unc |
| **F** | no position                          | —                                 |

## Components: alive / dormant / new

**Alive (unchanged):** `posSus` (needs only "direct RX from 300 km is
impossible", not precise ranging), `traceNbr` (ground truth vs re-broadcast
nodeDB copies), address anchors (the only OSM contribution that pays).

**Dormant (gated):** fine SNR-ring multilateration — auto-revives at r ≤ −0.4.

**New (this revision):**
- **Outgoing legs** (`link.snrOut/outTs`): traceroute `snrTowards` tells how
  neighbors hear US (passively we only know the reverse). Asymmetry runs
  systematically against us — up to Δ=14 dB; antenna/power diagnostics.
- **Graph placement** (est v2): centroid over own hearing sites (the 360 m
  "yard") PLUS positioned city xlink partners. No distance metric — weights are
  only monotonic in SNR; uncertainty = anchor spread floored at the mesh's
  median link length.
- **Ghosts** (`data.ghosts`): nodes we never hear ourselves but which appear in
  the xlink graph with ≥2 positioned partners — drawn dashed on the geo map
  ("heard in the mesh, beyond our hearing").

**Rejected by data:** OSM building snap (false precision at km-class
uncertainty), 3D propagation modeling (data-hungry, unreliable with 4 nodes).

## Possible next
- DV-hop (km/hop calibrated on GPS↔GPS pairs) to refine graph placement.
- Wardriving with the mobile node (FADV) → spread calibration pairs → a real
  chance to revive metric rings.
