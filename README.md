# EWR Window Watch

Which plane is that? A small Flask server that watches arrivals into Newark (EWR) runway 22,
works out which one is passing the window right now, and shows it on a 20×4 LCD driven by an
ESP32 and on a live web dashboard.

**Demo:** https://yannalam427.github.io/ewr-window-plane/ (a replay of real arrivals recorded
by the server; the live version runs at home)

## How it works

1. Aircraft positions come from the [OpenSky Network](https://opensky-network.org) API for a box
   around Newark.
2. A plane counts as "in the window" when it is on final for runway 22: 600–1,700 ft, track
   190–220°, descending, 1.5–5.5 nm from the airport. Tune these in the `CONFIGURATION` block of
   `server.py` to match your own window.
3. The origin comes from the official Newark arrivals board (including regional flights that
   fly as United, American or Delta), falling back to [adsbdb](https://www.adsbdb.com).

## Endpoints

| Path          | What                                                        |
|---------------|-------------------------------------------------------------|
| `/current`    | Four 20-character lines for the ESP32 LCD                   |
| `/watch`      | The dashboard (`docs/index.html`)                           |
| `/watch-data` | Everything the dashboard draws, as JSON                     |
| `/debug`      | Nearby aircraft and why each one does or doesn't match      |

## Run it

```sh
pip install -r requirements.txt
cp .env.example .env        # add your OpenSky API client id and secret
python server.py            # listens on port 5001 on all interfaces
```

Open `http://<your-computer>:5001/watch`. The ESP32 polls `http://<your-computer>:5001/current`.

## Refresh the GitHub Pages demo

With the server running:

```sh
python tools/record_demo.py --minutes 30
```

This rewrites `docs/demo.json`. Commit it and GitHub Pages serves the new replay. The page
switches to replay mode on its own when it is opened from github.io (or with `?demo`).
