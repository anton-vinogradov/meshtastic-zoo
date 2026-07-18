# meshtastic-zoo 📡

**English** | [Русский](README.ru.md)

A live map of your Meshtastic node zoo: who is on the air, who hears
whom and how well, and which node has unread mail. Everything updates
by itself while the page is open.

## Running

```sh
python3 collector/hub.py
# the map: http://localhost:8814
```

One process does it all: keeps in touch with your nodes, listens to the
air, refreshes the map and serves the site. Which subnets count as yours
is a list in [`collector/config.json`](collector/config.json).

## What's on the map

- **Node tokens**: a device photo, name and address. Blue ones are your
  nodes (reachable over the network), black ones are neighbors heard
  over the radio.
- **A green dot** — the node is online right now. A "N min / h" badge —
  how long ago it was last heard on the air (orange when older than
  3 hours).
- **An envelope ✉** — the node has an unread direct message. The
  overall mail counter sits in the top-left corner; clicking it opens
  the node with the letter.
- **A lock 🔒** — the node's public key hasn't been received yet, so an
  encrypted DM to it can't be sent (the `PKI_SEND_FAIL_PUBLIC_KEY`
  error). The key is held **per sending node**: a DM only goes through
  from one of your nodes that already has the recipient's key, so the
  panel lists exactly which of your nodes hold it. Keys arrive on their
  own with NodeInfo; the badge disappears once every node has one.
- **Grey squares with a hop count** — a former direct (0-hop) neighbor
  that has genuinely slipped to relay-only. Direct neighbors constantly
  flap between 0 and 1–2 hops (normal RF), so a node turns grey only
  after its direct signal has been gone for a few minutes straight — a
  momentary flap doesn't count — and only while it's still within a
  couple of hops (a jump to 3+ right after being direct is routing noise,
  not a move). It keeps its place, drawn grey with a dashed frame, its
  leg showing the hop count (`1 hop`, `2 hops`…) instead of an SNR. It's
  held for up to an hour of no direct contact, then forgotten. A
  **former neighbor** checkbox in the map's legend hides or shows these
  grey nodes (your choice is remembered per browser).
- **Arrows** show who hears whom: the head points at the listener.
  Color is link quality, from red (barely) to green (ideal); the label
  on the line is the SNR in dB; the exact percentage is in the tooltip.
  A grey "no data" arrow means that direction has never been caught.
- **Distance = quality.** The better a pair hears each other, the
  closer their tokens; nodes with no shared links drift apart. The
  positions come from stress-majorization (weighted MDS) that lays out
  all links at once and finds the best compromise when signal distances
  disagree — two-way and fresh measurements are trusted more. It's a
  connectivity map, not a geographic one: SNR reflects link quality, not
  raw distance (power, antennas and terrain all bend it). Roaming nodes
  get a dashed frame. The map fits the window and re-lays out on resize.

## Hover and click

Hovering over a node highlights its links and dims everything else.
Clicking selects the node — it gets an orange outline, the same dimming
stays put, and the details panel opens. Inside the panel, hovering a
row in **Legs** outlines that neighbor in blue on the map, so you can
tell which card a link goes to. The panel shows:

- device photo and model, ID, callsign, IP;
- battery, uptime, channel utilization, "last seen";
- **Conversation** — the full message history with this node: incoming
  on the left, your replies on the right. Outgoing messages show a
  delivery status: ⏳ on air → ✓ delivered, ✗ error (with the reason
  spelled out, e.g. "no recipient key") or ⚠ no ack. A reply goes on the
  air from the very node that was written to (➤), or just mark it as
  read (✓) — the marker clears right away;
- **Compose** — send a direct message to this node; a selector picks
  which of your nodes speaks (the closest one — that hears the recipient
  loudest — is preselected);
- **Legs** — all the node's links: two-way ones grouped in "there and
  back" pairs, one-way ones separately, everything sorted by quality
  with the age of each measurement.

Every message (in DMs and the channel) can be **reacted to** (tapback
emoji — ＋ opens a picker) and **replied to with a quote** (↩). Incoming
reactions and quoted replies from the mesh are shown the same way.

## Public channel

A collapsible panel on the left (the 💬 tab) shows the **public channel**
feed — the broadcast messages your nodes hear. Each message lists, right
under it, **which of your nodes received it** and at what SNR, so you can
see the coverage of a broadcast at a glance. You can also post to the
channel from any of your online nodes. It stays collapsed by default;
the tab remembers your choice.

## Nice little things

- SNR labels sit right on their own lines — you can't mix up whose
  number it is.
- Legs try to route around other tokens — bending into an arc and, if
  that isn't enough, attaching at a different edge of the card. Node
  positions never move, so distances stay honest; only the attachment
  points do.
- There are always two arrows between your own nodes. If one direction
  hasn't been caught for a while it is drawn as a grey "no data":
  a one-way link is a suspicious link, and the map pushes such a pair
  farther apart.
- A neighbor silent for more than 6 hours leaves the map (the threshold
  is configurable).
- The last update time is in the bottom-right corner; if the data goes
  stale, a warning appears next to it.
- Device photos are the official renders from the Meshtastic project
  (web-flasher); an unknown model gets a placeholder.
- Your message history — both DMs and the channel — is kept on disk and
  survives a hub restart or reboot. Writes are atomic, so a crash in the
  middle of a save can't corrupt or wipe it.

## Settings

The **⚙** button in the top-right corner of the map opens the settings
panel. Every field, top to bottom:

- **Language** — interface language, English or Russian. Stored in your
  browser (not on the server), so each viewer picks their own.
- **Site subnets** — the IP subnets (CIDR, one per line, e.g.
  `10.88.88.0/24`) the collector scans for nodes. These are "your"
  nodes — they show up as blue cards. Order doesn't matter.
- **0% quality at SNR, dB** — the SNR that the color scale treats as the
  worst (0%, red). Links at or below it are drawn fully red.
- **100% quality at SNR, dB** — the SNR treated as perfect (100%,
  green). Between the two values the color and the on-map distance
  scale smoothly. Default −20 … +10 dB fits Meshtastic's usable range;
  narrow it to make the coloring stricter.
- **Keep a silent neighbor, hours** — how long an outside node stays on
  the map after it was last heard on the air. Lower it (1–2 h) to keep
  the map to currently active nodes; raise it to remember rare ones.
- **Remember legs in cache, hours** — how long a link's last measured
  SNR is reused when a node is reachable but didn't report that link
  this round (e.g. it answered with a light query). Keeps the map from
  flickering; doesn't invent data, only holds the last real reading.
- **Map refresh, seconds** — how often the map is rebuilt from the live
  node databases. Default 60 s.
- **New-node discovery, seconds** — how often the subnets are re-scanned
  for nodes that just came online. Default 300 s.
- **Roaming nodes** — radio ids (one per line, e.g. `!702bde48`) of
  nodes that move around and change IP; they get a dashed frame so you
  don't trust their address.
- **Slow subnets** — IP prefixes (one per line, e.g. `10.77.77.`) of
  sites whose nodes choke when their full node database is pulled at
  once. The collector queries those lightly after two failed full
  attempts. An advanced knob — leave it empty unless a site keeps
  timing out.

Changes apply on the fly and are saved to `collector/config.json`.
A few rarely-touched keys live in that file only: `port` (the node
API port, 4403), the connect/query timeouts, `hopMaxShow` (largest hop
count a former neighbor may show at before it's treated as routing noise
rather than a real move — default 2), `hopSettleMin` (minutes of no
direct contact before a slipped neighbor turns grey, so momentary flaps
are ignored — default 3), `hopStaleMin` (how long a grey multi-hop node
is kept before it's forgotten — default 60), and `known` / `names` —
fallback IP↔radio-id and name maps used when a node doesn't answer.

## Roadmap

- [x] Live map with honest distances and device photos
- [x] Mail: unread markers, conversation history, delivery status,
      replying and sending from the right node
- [ ] Measurement history and link quality charts
