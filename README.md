# Hotel TV for Home Assistant

Connects Home Assistant to **Hotel TV**, the hotel IPTV system: every TV of a property becomes
a device in Home Assistant with a media player, and the guest can control Home Assistant devices
(air conditioning, lights, blinds, scenes, …) with the TV remote.

- **Staff → TV**: turn TVs on/off, change channel and volume, choose a TV's channel package, send
  messages to one TV, several or all, refresh the channels.
- **Guest → Home Assistant**: the admin picks, per TV, which entities the guest may control from
  it. Only those, from that TV, are ever touched.

Hotel TV API 2.0 has no rooms and no guests (check-in / check-out will come from a PMS
integration later); this integration therefore works with TVs only.
- **Local push**: one Server-Sent Events connection to the Hotel TV server; changes arrive in
  under a second, no polling.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Hotel-Assistant&repository=hotel-tv-ha&category=integration)

## Requirements

- Home Assistant 2025.6 or newer.
- A Hotel TV server Home Assistant can reach (usually in the hotel LAN, e.g.
  `http://192.168.1.20:3210`).
- Home Assistant must be reachable from the Hotel TV server: set its local network URL in
  **Settings → System → Network** (the server calls Home Assistant's webhook; no Home Assistant
  Cloud needed).

## Install

### HACS (recommended)

1. In HACS open **⋮ → Custom repositories**, add `https://github.com/iezhkv/hotel-tv-ha`
   with type **Integration** (or click the badge above).
2. Install **Hotel TV** and restart Home Assistant.

### Manual

Copy `custom_components/hotel_tv` into `/config/custom_components/hotel_tv/` (so that
`/config/custom_components/hotel_tv/manifest.json` exists) and restart Home Assistant.

## Pair with Hotel TV

1. In the Hotel TV admin open **Настройки → Home Assistant** (Settings → Home Assistant) and press
   **Генерирай код** (Generate code). The page shows the server address and a code like
   `ABCD-EFGH`, valid for 10 minutes and usable once.
2. In Home Assistant open **Settings → Devices & services → Add integration → Hotel TV**.
3. Enter the server address (e.g. `http://192.168.1.20:3210`; a trailing `/api/v1` is accepted)
   and the code. Home Assistant tells the server where its webhook is (the local network URL
   first, then the external URL if one is set); the server checks that it can call it back, and
   the pairing is done. The page in the admin then shows the connection as live.
4. The TVs appear as devices. The entry is named after the property; one Home Assistant can serve
   several properties.

Nothing has to be typed or copied besides the code: pairing gives the integration an `htv_` API
token for the server's API (it appears in **Настройки → API токени** as the Home Assistant
integration's) and a shared secret with which the server and the TVs sign what they send to Home
Assistant.

The server address must use `https://`, unless it is in the local network (a private IP address
or a `.local` / `.lan` name).

- **Pairing again** for the same property (for example after moving the server) updates the
  existing entry.
- **Disconnected in the admin, or the token revoked?** Home Assistant asks to *Reauthenticate*:
  generate a new code and enter it (only the code is asked; the address is kept). Entries made by
  older versions with a typed API token ask for a code the same way.
- **Server moved?** Use **⋮ → Reconfigure** on the entry: the new address and a new code. It must
  be the same property.
- **TV controls**: **Configure** on the entry — see [Guest controls](#guest-controls-tv--home-assistant).

### The webhook

The server and the TVs reach Home Assistant through one webhook (`/api/webhook/<id>`), every
message signed (`X-HotelTV-Signature`, HMAC-SHA256, valid ±5 minutes, each message id accepted
once):

- **The server** (signed with the secret) reads what the admin may bind to a TV and controls it:
  `info`, `entities` (climate, light, switch, cover, scene, script, fan, lock, media_player,
  sensor, binary_sensor, weather, input_boolean and button entities: state, attributes, name and
  area), `states`, and `call` — service calls only in the climate, light, switch, cover, scene,
  script, fan, lock, media_player, input_boolean and button domains, only on entities named by
  `entity_id` of the call's own domain (no areas, devices or labels).
- **An Android TV** (signed with its own key, derived from the secret) may, while the server is
  unreachable, read and control **its own** controls: `tv.controls` and `tv.control`. A TV the
  server does not list as paired, or an entity that is not one of that TV's controls, is refused.

## Devices and entities
## Devices and entities

One **property** device (the server) and one device per **TV** (`GET /devices`), named
`<property> · <TV>` (e.g. *Hotel Demo · TV 204*), with the TV's kind as model (*Android TV*,
*Linux TV*). A TV added in Hotel TV later gets its device by itself; a TV removed there loses it.
Entity ids below are for a property called *Hotel Demo* and a TV called *TV 204*.

| Entity | Example id | What it does |
|---|---|---|
| TV (media player, the device's main entity) | `media_player.hotel_demo_tv_204` | on/off, channel (`source`; the list is the TV's channel package — its own or the default — in package order, as `12 · BNT 1`), volume set/step/mute, next/previous = channel up/down, play/pause. Attributes `channel_number`, `channel_name`, `tv_id`, `kind`, `paired`, `last_seen_at`. Unavailable while the TV is offline. |
| Online | `binary_sensor.hotel_demo_tv_204_online` | connectivity of the TV (diagnostic) |
| Current channel | `sensor.hotel_demo_tv_204_current_channel` | channel name while the TV is on |
| Reload | `button.hotel_demo_tv_204_reload` | the TV refetches its configuration |
| Identify | `button.hotel_demo_tv_204_identify` | the TV shows its name |
| Channel package | `select.hotel_demo_tv_204_channel_package` | the TV's package (`PATCH /devices/{id}`); choosing the default package makes the TV follow the property default |
| Refresh channels | `button.hotel_demo_refresh_channels` | (property device) re-probes all channel sources and reloads every TV |

All entities become unavailable when the server cannot be reached.

## Services

Services that act on TVs take a **target**: TV devices, or any entity of a TV device (e.g. its
media player). Areas work too: every TV device in the area is targeted (put each TV in its room's
area and target the area).

| Service | Fields | Server call |
|---|---|---|
| `hotel_tv.send_message` | `title`*, `message`, `kind` (`info`/`alert`/`welcome`/`promo`), `display` (`banner`/`modal`/`fullscreen`), `expires_in` (duration), `all_tvs`, `config_entry_id` | `POST /messages` once per TV (`device_id`); with `all_tvs` and no target one property-wide message (`device_id: null`; per property, or only `config_entry_id`). Returns the message ids. |
| `hotel_tv.send_command` | `command`* (`power_on`, `set_channel`, `open_panel`, `identify`, …), `payload` | `POST /devices/{id}/command` per TV. Returns the TVs that got it. |
| `hotel_tv.refresh_channels` | `wait`, `config_entry_id` | `POST /channels/refresh` — returns at once by default (the TVs reload when it is done); `wait: true` waits (10–90 s) and returns the probe result |

`*` = required. Server-side refusals (e.g. an invalid command payload) are raised as errors with
the server's message.

### Event `hotel_tv_event`

Every event of the server's feed is fired on the Home Assistant bus, so automations can trigger on
it:

```yaml
event_type: hotel_tv_event
data:
  type: device.state          # device.state, device.online, device.offline, device.updated,
                              # channels.updated, message.created, control.requested
  device_id: "a1b2…"          # Home Assistant device id of the TV (null for property-wide events)
  tv_id: "9a58…"              # Hotel TV device id
  tv_name: "TV 204"
  payload: { power: true, channel_name: "BNT 1", volume: 30, … }
  config_entry_id: "…"
  property_id: "…"
  id: 121
  created_at: "2026-11-18T09:30:00+00:00"
```

## Example automations

**Welcome message when the TV is switched on** (here: the TV of room 204):

```yaml
alias: TV 204 – welcome
triggers:
  - trigger: state
    entity_id: media_player.hotel_demo_tv_204
    from: "off"
    to: "on"
actions:
  - action: hotel_tv.send_message
    target:
      entity_id: media_player.hotel_demo_tv_204
    data:
      title: "Welcome!"
      message: "Wi-Fi: Hotel-Guest · Breakfast 7:00–10:30"
      kind: welcome
      display: fullscreen
      expires_in: "00:10:00"
```

**TV off when nobody is in the room** (an occupancy sensor of the room's area):

```yaml
alias: TV 204 off in an empty room
triggers:
  - trigger: state
    entity_id: binary_sensor.room_204_occupancy
    to: "off"
    for: "00:15:00"
actions:
  - action: media_player.turn_off
    target:
      entity_id: media_player.hotel_demo_tv_204
```

**Message to all TVs:**

```yaml
action: hotel_tv.send_message
data:
  all_tvs: true
  title: "Fire alarm test at 11:00"
  message: "No action is needed."
  kind: alert
  display: modal
  expires_in:
    hours: 2
```

**React to a guest's channel change** (any TV):

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

Guests can operate some Home Assistant devices with the TV remote (the TV shows a "Room"
panel). **Nothing is exposed by default.** The entities are chosen per TV — usually the devices
of the room the TV is in.

A TV's controls are the entities chosen for it here (**Configure**) **plus** the entities the
hotel bound to it in the Hotel TV admin (the TV's cards; the server lists them as `ha_entities`
of the TV). Bound entities show under their Home Assistant name; a binding changed in the admin
reaches Home Assistant at once (`device.updated`). Bound entities that cannot be a TV control
(sensors, weather, …) are shown by the server itself and are not synced as controls.

### Choose the entities of each TV

Open **Settings → Devices & services → Hotel TV → Configure**:

- **Choose the entities of a TV** — pick the TV, then the entities its guest may control:
  `climate`, `light`, `switch`, `cover`, `scene`, `fan`, `lock` and `media_player` entities
  (multi-select; choose none to expose nothing), then optionally the name the TV shows for each
  (otherwise its Home Assistant name). TVs that already have entities are marked `✓ n` in the
  list.
- **Fill other TVs by pattern** — set up one TV, then let the integration do the rest: choose that
  TV as the source and it looks, for every other TV, for the same entities with the number in that
  TV's name in the entity id (TV `TV 203` → `TV 204`:
  `climate.room_203_thermostat` → `climate.room_204_thermostat`).
  The names typed for the TV are copied too. Before anything is saved you see, per TV, what was
  found (`✓`) and what does not exist (`✗`, skipped); TVs where nothing was found are left
  alone. TVs that already have entities are skipped unless you tick **Also replace TVs that
  already have entities** — their list is then replaced by what was found.

  The match is on the TV's name as Hotel TV has it, as a whole number or word inside the entity
  id: a TV called `203` matches `room_203_main` and `room203main` but not `room_2030_main`; one
  called `A-1` is looked for as `a_1`; for a TV called `TV 203` (exactly one number in the name)
  the number alone is used. Entities whose id does not carry the number (a shared `scene.night`)
  cannot be copied and are listed as not found. So name the TVs after their rooms.

### Link a thermostat to a TV

1. **Configure → Choose the entities of a TV**, pick the TV and add the room's `climate` entity
   (for example `climate.room_203_thermostat`). Optionally name it ("Климатик").
2. For the other TVs use **Fill other TVs by pattern** with that TV as the source.
3. The integration reports to Hotel TV what the thermostat **can** do (see the table below):
   on/off, target temperature with its range, step and unit, the measured temperature and
   humidity, and the hvac, fan, swing and preset modes it offers.
4. The hotel then decides what the guest **may** use: in the Hotel TV admin open
   **Устройства → the TV → Уреди и съобщения** and switch capabilities and modes on or off, or
   narrow the temperature range. That policy lives on the server (`settings` of the control); the
   integration never writes it, and the TV only offers — and the server only accepts — what both
   the thermostat and the hotel allow.

### What the integration does

The integration then:

- **sends the definitions** (`PUT /devices/{id}/controls`, the entity id is the control key) when
  Home Assistant starts and whenever the selection changes; a TV whose selection is emptied
  gets an empty list;
- **sends what Home Assistant shows**: each entity's own icon (the one set on it, else its
  domain's default — `mdi:…`; the TV draws it instead of an icon picked by hand) and a light's
  favourite colours from its more-info dialog (the shortcuts of the TV's light panel);
- **keeps the TV up to date**: entity state changes are pushed (`PATCH …/controls/{entity_id}`),
  debounced by 0.5 s; changed capabilities (modes, limits) or names re-send the definitions; an
  unavailable entity is shown as unavailable;
- **carries out guest requests**: on a `control.requested` event (or a `tv.control` the TV sends
  straight to the webhook while the server is down) it checks that the entity is a control
  **of that TV**, validates the action against the entity's *current* capabilities,
  calls the Home Assistant service, acknowledges the request `done` or `failed`, and pushes the
  real state back (the TV changes its display optimistically; a failed request is undone this way).
  Requests for entities that are not exposed for the TV, invalid parameters, unavailable
  entities and requests older than 60 s are refused (`failed`).
- **catches up**: at start-up and after every reconnect of the event stream it reads the pending
  requests (`GET /control-requests?status=pending`) and handles them the same way; a request is
  never handled twice.

| Kind | State sent to the TV | Capabilities | Guest action → Home Assistant service |
|---|---|---|---|
| climate | `hvac_mode`, `hvac_action`, `current_temperature`, `target_temperature`, `current_humidity`, `fan_mode`, `swing_mode`, `preset_mode` | see [Thermostats](#thermostats) | see [Thermostats](#thermostats) |
| light | `on`, `brightness` 0–100 (HA 0–255 converted); colour lights also `hue`, `saturation`, `kelvin`, `color_mode` | `dimmable` (from the color modes); `color`, `color_temp` with `min_kelvin` / `max_kelvin` when the light has them | `turn_on` / `turn_off` → `light.turn_on` / `light.turn_off` · `set_brightness` → `light.turn_on` with `brightness` (0 = off) · `set_color` → `hs_color` · `set_color_temp` → `color_temp_kelvin` |
| switch | `on` | — | `turn_on` / `turn_off` → `switch.turn_on` / `switch.turn_off` |
| cover | `position` 0–100 (open/closed = 100/0 without a position) | `position: false` for open/close-only covers | `set_position` → `cover.set_cover_position`; covers without positioning open (≥ 50) or close |
| scene | — | — | `activate` → `scene.turn_on` |
| fan | `on`, `percentage` | `speed`, `percentage_step` | `turn_on` / `turn_off` · `set_percentage` → `fan.set_percentage` (0 = off) |
| lock | `locked`, `state` | — | `lock` / `unlock` → `lock.lock` / `lock.unlock` |
| media (media_player) | `state`, `volume` 0–100, `muted` | `play_pause`, `volume` | `play_pause` → `media_player.media_play_pause` · `set_volume` → `media_player.volume_set` |

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

- At start: `GET /devices` (with each TV's `ha_entities`), `/channels`, `/packages`.
- Then one long-lived `GET /api/v1/events` (SSE). TV state, online/offline, TV changes
  (`device.updated`: name, package) and channel changes update the entities by push;
  `device.updated` re-reads the TV, and an event of an unknown TV triggers a full refresh. The connection
  resumes with `Last-Event-ID`, reconnects with backoff (1 s → 30 s) and is considered dead after
  45 s without data (the server pings every 20 s).
- A full refresh every 5 minutes and after each reconnect as a safety net.
- **Without the server**: what it last said (the TVs, which are paired, the entities bound to
  them) is kept in `.storage/hotel_tv.<entry id>`. When the server does not answer at start-up
  the entry loads from it: its entities are unavailable, but the webhook answers, so the TVs keep
  driving their own devices — after a restart during an outage, or long after the server is gone.
  A hotel it has never heard from waits for the server, as before.
- Upgrading from a version that worked with rooms: the old room devices are removed at start-up,
  and the exposed controls have to be chosen again per TV (**Configure**), since they were stored
  per room.
- The event cursor is kept in memory only: after a Home Assistant restart the stream starts at
  "now" and a full refresh fills in the state, so old events (and old guest requests) are not
  replayed into automations.

## Diagnostics

**⋮ → Download diagnostics** on the entry gives the connection state, the last event id, the TVs,
and the chosen, exposed and last-sent controls. The token, the secret and the webhook id are
redacted.

The shared secret and the token are stored in the config entry, so they are part of your Home
Assistant **backups**. If one leaks, disconnect in the Hotel TV admin and pair again: the old
secret and token stop working at once.

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

It only reads, plus one empty `PATCH /devices/{id}` to produce an event; no controls are exposed.
