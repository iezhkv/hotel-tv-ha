# Hotel TV for Home Assistant

Connects Home Assistant to **Hotel TV**, the hotel IPTV system: every room of a property becomes
a device in Home Assistant with its TV as a media player, and the guest can control the room's
Home Assistant devices (air conditioning, lights, blinds, scenes) with the TV remote.

- **Staff → TV**: turn TVs on/off, change channel and volume, send messages to one room, several
  or all, check guests in and out, set the guest name and welcome message, refresh the channels.
- **Guest → room**: the admin picks, per room, which entities the guest may control from the TV.
  Only those, in that room, are ever touched.
- **Local push**: one Server-Sent Events connection to the Hotel TV server; changes arrive in
  under a second, no polling.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Hotel-Assistant&repository=hotel-tv-ha&category=integration)

## Requirements

- Home Assistant 2025.6 or newer.
- A Hotel TV server Home Assistant can reach (usually in the hotel LAN, e.g.
  `http://192.168.1.20:3210`).
- An API token: in the Hotel TV admin open **Настройки → API токени** (Settings → API tokens) and
  create one. It starts with `htv_`. One token belongs to one property.

## Install

### HACS (recommended)

1. In HACS open **⋮ → Custom repositories**, add `https://github.com/iezhkv/hotel-tv-ha`
   with type **Integration** (or click the badge above).
2. Install **Hotel TV** and restart Home Assistant.

### Manual

Copy `custom_components/hotel_tv` into `/config/custom_components/hotel_tv/` (so that
`/config/custom_components/hotel_tv/manifest.json` exists) and restart Home Assistant.

## Configure

**Settings → Devices & services → Add integration → Hotel TV**, then enter:

| Field | Value |
|---|---|
| Server address | The address of the Hotel TV admin, e.g. `http://192.168.1.20:3210` (a trailing `/api/v1` is accepted too) |
| API token | The `htv_…` token |

The token is checked with `GET /api/v1/info`; the entry is named after the property and can be
added once per property (one Home Assistant can serve several properties).

- **Token revoked?** Home Assistant asks for a new one (*Reauthenticate*). It must belong to the
  same property.
- **Server moved?** Use **⋮ → Reconfigure** on the entry to change the address (and optionally the
  token).
- **Room controls**: **Configure** on the entry — see [Guest controls](#guest-controls-tv--home-assistant).

## Devices and entities

One **property** device (the server) and one device per **room**, named
`<property> · <room>` (e.g. *Hotel Demo · 204*). Rooms without a TV get only the guest and
occupancy entities; when a TV is paired to the room later, its entities appear by themselves.
Entity ids below are for a property called *Hotel Demo*.

| Entity | Example id | What it does |
|---|---|---|
| TV (media player) | `media_player.hotel_demo_204_tv` | on/off, channel (`source`; the list is the room's channel package — its own or the default — in package order, as `12 · BNT 1`), volume set/step/mute, next/previous = channel up/down, play/pause. State and attributes (`channel_number`, `channel_name`, `last_seen_at`) come from the room's first TV; unavailable while that TV is offline. Commands go to every TV in the room. |
| TV online | `binary_sensor.hotel_demo_204_tv_online` | connectivity of the room's first TV |
| Occupied | `binary_sensor.hotel_demo_204_occupied` | a guest is checked in |
| Guest | `sensor.hotel_demo_204_guest` | guest name; attributes `guest_locale`, `welcome_message`, `checkin_at`, `checkout_at`, `room_id` |
| Current channel | `sensor.hotel_demo_204_current_channel` | channel name while the TV is on |
| Reload TV | `button.hotel_demo_204_reload_tv` | the TV refetches its configuration |
| Identify TV | `button.hotel_demo_204_identify_tv` | the TV shows its room and name |
| Channel package | `select.hotel_demo_204_channel_package` | the room's package; choosing the default package makes the room follow the property default |
| Refresh channels | `button.hotel_demo_refresh_channels` | (property device) re-probes all channel sources and reloads every TV |

All entities become unavailable when the server cannot be reached.

## Services

Services that act on rooms take a **target**: room devices, or any entity of a room device
(e.g. the room's TV media player). Areas work too when room devices are assigned to them.

| Service | Fields | Server call |
|---|---|---|
| `hotel_tv.send_message` | `title`*, `message`, `kind` (`info`/`alert`/`welcome`/`promo`), `display` (`banner`/`modal`/`fullscreen`), `expires_in` (duration), `all_rooms`, `config_entry_id` | `POST /messages` once per room; with `all_rooms` and no target one property-wide message (per property, or only `config_entry_id`). Returns the message ids. |
| `hotel_tv.check_in` | `guest_name`*, `guest_locale`, `welcome_message`, `checkout_at` | `POST /rooms/{id}/checkin` — occupied, TV on to the welcome screen. All guest fields are **replaced** (omitted ones are cleared). |
| `hotel_tv.check_out` | — | `POST /rooms/{id}/checkout` — guest data and the room's messages cleared, TV off |
| `hotel_tv.set_guest` | any of `guest_name`, `guest_locale`, `welcome_message`, `occupied`, `checkin_at`, `checkout_at` | `PATCH /rooms/{id}` — only the given fields; an empty text clears a field |
| `hotel_tv.send_command` | `command`* (`power_on`, `set_channel`, `open_panel`, `identify`, …), `payload` | `POST /rooms/{id}/command`. Returns how many TVs got it. |
| `hotel_tv.refresh_channels` | `wait`, `config_entry_id` | `POST /channels/refresh` — returns at once by default (the TVs reload when it is done); `wait: true` waits (10–90 s) and returns the probe result |

`*` = required. Server-side refusals (e.g. a room without TV for `send_command`) are raised as
errors with the server's message.

### Event `hotel_tv_event`

Every event of the server's feed is fired on the Home Assistant bus, so automations can trigger on
it:

```yaml
event_type: hotel_tv_event
data:
  type: device.state          # device.state, device.online, device.offline, room.updated,
                              # channels.updated, message.created, control.requested
  room_id: "ccf7b739-…"       # Hotel TV room id (null for property-wide events)
  room_name: "204"
  device_id: "a1b2…"          # Home Assistant device id of the room (for device-based logic)
  tv_device_id: "9a58…"       # Hotel TV device id, when the event is about a TV
  payload: { power: true, channel_name: "BNT 1", volume: 30, … }
  config_entry_id: "…"
  property_id: "…"
  id: 121
  created_at: "2026-11-18T09:30:00+00:00"
```

## Example automations

**Welcome message and TV on at check-in** — when the room becomes occupied:

```yaml
alias: Room 204 – welcome
triggers:
  - trigger: state
    entity_id: binary_sensor.hotel_demo_204_occupied
    from: "off"
    to: "on"
actions:
  - action: media_player.turn_on
    target:
      entity_id: media_player.hotel_demo_204_tv
  - action: hotel_tv.send_message
    target:
      entity_id: media_player.hotel_demo_204_tv
    data:
      title: "Welcome, {{ states('sensor.hotel_demo_204_guest') }}!"
      message: "Wi-Fi: Hotel-Guest · Breakfast 7:00–10:30"
      kind: welcome
      display: fullscreen
      expires_in: "00:10:00"
```

When the check-in itself comes from Home Assistant (e.g. from a PMS integration), call
`hotel_tv.check_in` instead — it sets the guest, marks the room occupied and turns the TV on to
the personalised welcome screen in one step:

```yaml
action: hotel_tv.check_in
target:
  entity_id: media_player.hotel_demo_204_tv
data:
  guest_name: "Maria Ivanova"
  guest_locale: bg
  checkout_at: "2026-11-20 11:00:00"
```

**TV off when the room becomes unoccupied:**

```yaml
alias: TV off in empty rooms
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.hotel_demo_101_occupied
      - binary_sensor.hotel_demo_102_occupied
      - binary_sensor.hotel_demo_204_occupied
    to: "off"
    for: "00:05:00"
actions:
  - action: media_player.turn_off
    target:
      entity_id: >
        {{ trigger.entity_id | replace('binary_sensor.', 'media_player.')
                             | replace('_occupied', '_tv') }}
```

**Message to all rooms:**

```yaml
action: hotel_tv.send_message
data:
  all_rooms: true
  title: "Fire alarm test at 11:00"
  message: "No action is needed."
  kind: alert
  display: modal
  expires_in:
    hours: 2
```

**React to a guest's channel change** (any room):

```yaml
triggers:
  - trigger: event
    event_type: hotel_tv_event
    event_data:
      type: device.state
conditions: "{{ trigger.event.data.payload.channel_name == 'Hotel Info' }}"
actions:
  - action: light.turn_on
    target:
      area_id: lobby
```

## Guest controls (TV → Home Assistant)

Guests can operate some of the room's Home Assistant devices with the TV remote (the TV shows a
"Room" panel). **Nothing is exposed by default.**

### Choose the entities of each room

Open **Settings → Devices & services → Hotel TV → Configure**:

- **Choose the entities of a room** — pick the room, then the entities its guest may control:
  `climate`, `light`, `switch`, `cover` and `scene` entities (multi-select; choose none to
  expose nothing), then optionally the name the TV shows for each (otherwise its Home Assistant
  name). Rooms that already have entities are marked `✓ n` in the list.
- **Fill other rooms by pattern** — set up one room, then let the integration do the rest: choose
  that room as the source and it looks, for every other room, for the same entities with the
  room's name in the entity id
  (`climate.room_203_thermostat` → `climate.room_204_thermostat`, room names `203` → `204`).
  The names typed for the TV are copied too. Before anything is saved you see, per room, what was
  found (`✓`) and what does not exist (`✗`, skipped); rooms where nothing was found are left
  alone. Rooms that already have entities are skipped unless you tick **Also replace rooms that
  already have entities** — their list is then replaced by what was found.

  The match is on the room's name as Hotel TV has it, as a whole number or word inside the entity
  id: `203` matches `room_203_main` and `room203main` but not `room_2030_main`; a room called
  `A-1` is looked for as `a_1`; for a room called `Room 203` the number alone is used. Entities
  whose id does not carry the room name (a shared `scene.night`) cannot be copied and are listed
  as not found.

### Link a thermostat to a room

1. **Configure → Choose the entities of a room**, pick the room and add its `climate` entity
   (for example `climate.room_203_thermostat`). Optionally name it ("Климатик").
2. For the other rooms use **Fill other rooms by pattern** with that room as the source.
3. The integration reports to Hotel TV what the thermostat **can** do (see the table below):
   on/off, target temperature with its range, step and unit, the measured temperature and
   humidity, and the hvac, fan, swing and preset modes it offers.
4. The hotel then decides what the guest **may** use: in the Hotel TV admin open
   **Admin → Стаи → стая → Контроли в стаята → Настрой** and switch capabilities and modes on or
   off, or narrow the temperature range. That policy lives on the server (`settings` of the
   control); the integration never writes it, and the TV only offers — and the server only
   accepts — what both the thermostat and the hotel allow.

### What the integration does

The integration then:

- **sends the definitions** (`PUT /rooms/{id}/controls`, the entity id is the control key) when
  Home Assistant starts and whenever the selection changes; a room whose selection is emptied
  gets an empty list;
- **keeps the TV up to date**: entity state changes are pushed (`PATCH …/controls/{entity_id}`),
  debounced by 0.5 s; changed capabilities (modes, limits) or names re-send the definitions; an
  unavailable entity is shown as unavailable;
- **carries out guest requests**: on a `control.requested` event it checks that the entity was
  exposed **for that room**, validates the action against the entity's *current* capabilities,
  calls the Home Assistant service, acknowledges the request `done` or `failed`, and pushes the
  real state back (the TV changes its display optimistically; a failed request is undone this way).
  Requests for entities that are not exposed for the room, invalid parameters, unavailable
  entities and requests older than 60 s are refused (`failed`).
- **catches up**: at start-up and after every reconnect of the event stream it reads the pending
  requests (`GET /control-requests?status=pending`) and handles them the same way; a request is
  never handled twice.

| Kind | State sent to the TV | Capabilities | Guest action → Home Assistant service |
|---|---|---|---|
| climate | `hvac_mode`, `hvac_action`, `current_temperature`, `target_temperature`, `current_humidity`, `fan_mode`, `swing_mode`, `preset_mode` | see [Thermostats](#thermostats) | see [Thermostats](#thermostats) |
| light | `on`, `brightness` 0–100 (HA 0–255 converted) | `dimmable` (from the color modes) | `turn_on` / `turn_off` → `light.turn_on` / `light.turn_off` · `set_brightness` → `light.turn_on` with `brightness` (0 = off) |
| switch | `on` | — | `turn_on` / `turn_off` → `switch.turn_on` / `switch.turn_off` |
| cover | `position` 0–100 (open/closed = 100/0 without a position) | — | `set_position` → `cover.set_cover_position`; covers without positioning open (≥ 50) or close |
| scene | — | — | `activate` → `scene.turn_on` |

### Thermostats

Capabilities are read from the entity's `supported_features` and attributes, so the TV only
shows what the thermostat really has:

| Capability | Reported as supported when |
|---|---|
| `power` (on/off button) | `off` is one of `hvac_modes`, or the entity has both the turn-on and turn-off features |
| `target_temperature` | the entity has a single target temperature (`TARGET_TEMPERATURE`). A thermostat that only takes a low/high range (`TARGET_TEMPERATURE_RANGE`, e.g. in `heat_cool`) reports `false`: the guest cannot set a temperature on it yet |
| `current_temperature`, `current_humidity` | the entity has the attribute. It stays reported while the value is missing for a while (a thermostat that is off often reports no temperature), so the TV shows "—" instead of the control changing shape |
| `min_temp`, `max_temp`, `step` | `min_temp`, `max_temp`, `target_temp_step` (defaults 7, 35, 0.5) |
| `unit` | Home Assistant's temperature unit (`°C` or `°F`) |
| `hvac_modes` | the entity's `hvac_modes`, `off` included |
| `fan_modes`, `swing_modes`, `preset_modes` | the entity's lists, each only when the entity has the matching feature (otherwise empty) |

| Guest action | Home Assistant service |
|---|---|
| `turn_off` | `climate.turn_off` when the entity has the turn-off feature, otherwise `climate.set_hvac_mode` `off`; refused when it has neither |
| `turn_on` | `climate.turn_on` when the entity has the turn-on feature; otherwise `climate.set_hvac_mode` with the mode the thermostat was last seen running in (remembered since Home Assistant started), or its first mode other than `off` |
| `set_temperature {temperature}` | `climate.set_temperature` — refused outside `min_temp`…`max_temp` or without a single target temperature; the value is put on the entity's `target_temp_step` grid |
| `set_hvac_mode`, `set_fan_mode`, `set_swing_mode`, `set_preset_mode` | the `climate` service of the same name — only values the entity lists |

Every request is checked against the entity's **current** attributes. The hotel's policy
(what the guest may use) is applied by the Hotel TV server before a request is created.

## How it stays in sync

- At start: `GET /rooms`, `/channels`, `/packages`.
- Then one long-lived `GET /api/v1/events` (SSE). Device state, online/offline, room changes and
  channel changes update the entities by push; room changes re-read the room. The connection
  resumes with `Last-Event-ID`, reconnects with backoff (1 s → 30 s) and is considered dead after
  45 s without data (the server pings every 20 s).
- A full refresh every 5 minutes and after each reconnect as a safety net.
- The event cursor is kept in memory only: after a Home Assistant restart the stream starts at
  "now" and a full refresh fills in the state, so old events (and old guest requests) are not
  replayed into automations.

## Diagnostics

**⋮ → Download diagnostics** on the entry gives the connection state, the last event id, the rooms
(guest names and welcome messages removed), and the exposed and last-sent controls. The token is
redacted.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components tests   # pip install ruff
```

The tests mock the HTTP API (`pytest-homeassistant-custom-component`). `tests/test_live.py` runs
the integration against a real server and is skipped unless configured:

```sh
HOTEL_TV_LIVE_URL=http://127.0.0.1:3210 HOTEL_TV_LIVE_TOKEN=htv_… .venv/bin/python -m pytest tests/test_live.py
```

It only reads, plus one empty `PATCH /rooms/{id}` to produce an event; no controls are exposed.
