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
- **Arrows** show who hears whom: the head points at the listener.
  Color is link quality, from red (barely) to green (ideal); the label
  on the line is the SNR in dB; the exact percentage is in the tooltip.
  A grey "no data" arrow means that direction has never been caught.
- **Distance = quality.** The better a pair hears each other, the
  closer their tokens; nodes with no shared links drift apart. Roaming
  nodes get a dashed frame. The map fits the window entirely and
  re-lays itself out when the window is resized.

## Hover and click

Hovering over a node highlights its links and dims everything else.
Clicking opens the details panel:

- device photo and model, ID, callsign, IP;
- battery, uptime, channel utilization, "last seen";
- **Messages** — unread on top; a reply goes on the air from the very
  node that was written to (➤), or just mark it as read (✓);
- **Compose** — send a direct message to this node; a selector picks
  which of your nodes speaks (the one that hears the recipient loudest
  is preselected);
- **Legs** — all the node's links: two-way ones grouped in "there and
  back" pairs, one-way ones separately, everything sorted by quality
  with the age of each measurement.

## Nice little things

- SNR labels sit right on their own lines — you can't mix up whose
  number it is.
- Lines try to go around other tokens with an arc; the endpoints stay
  put, so the honesty of distances doesn't suffer.
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

## Settings (collector/config.json)

- `subnets` — your sites' subnets: where to look for nodes;
- `snrScale` — the color scale: which SNR counts as zero and which as
  ideal;
- `worldMaxAgeH` — how many hours to keep a silent neighbor on the map;
- `mobile` — roaming nodes (dashed frame on the map);
- `known` / `names` — addresses and names as a fallback when a node
  does not answer.

## Roadmap

- [x] Live map with honest distances and device photos
- [x] Mail: unread markers, replying and sending from the right node
- [ ] Measurement history and link quality charts
