# Map data refresh

CampusGo's map, place search and routes run entirely inside the app: no API keys, no outside
map service at run time. The data is OpenStreetMap's, downloaded once and shipped with the app.
Roads and shops change slowly, so refresh it every 3–6 months:

```sh
pip install -r requirements-dev.txt   # once
bash maintenance/build_maps.sh        # frontend/maps/*.pmtiles  (~105 MB, a minute or two)
python maintenance/build_places.py    # backend/places.db        (place search, ~210 MB download + a few minutes)
```

Then run the tests and deploy as usual.

- **Licence:** OpenStreetMap data is free for commercial use under the ODbL as long as it is
  credited. Every map shows "© OpenStreetMap"; don't remove it.
- **Service area:** both scripts read `LPU_CENTER` and `MAX_SERVICE_RADIUS_KM` from
  `backend/pricing_and_queue.py`, so a bigger area there means a rebuild here.
- **Real road routes (optional):** routes are an estimate (straight line × 1.3) unless
  `OSRM_URL` points at a self-hosted [OSRM](https://github.com/Project-OSRM/osrm-backend) server
  loaded with the same region. Fares never depend on it.
- **Our own places:** societies and spots OpenStreetMap doesn't have go in `my_places.txt`
  (`Name | Town | lat, lng`, coordinates copied from a Google Maps long-press), then rebuild
  `places.db`. They rank above OpenStreetMap places with similar names.
