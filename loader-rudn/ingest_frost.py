# ingest_frost.py
# Загрузка данных из FROST SensorThings в PostgreSQL/PostGIS.

import os
import time
import logging
import hashlib
from typing import Optional, Dict, Tuple, List, Set
from datetime import datetime, timezone
from dateutil import parser as dtparser

import requests
import psycopg2

# --- Импорты для геометрии ---
from shapely.geometry import shape, Point
from shapely.ops import transform as shp_transform
import pyproj

# ----------------------- Конфигурация -----------------------
FROST = os.getenv("FROST_URL", "http://94.154.11.74/frost/v1.1").rstrip("/")
DSN="host=localhost port=5433 dbname=frost user=frost password=frost"

# Дата начала загрузки
START_FROM = os.getenv("START_FROM", "2023-10-10T00:00:00Z")
START_FROM_DT = dtparser.isoparse(START_FROM).astimezone(timezone.utc)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "120"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "2000"))

# --- НАСТРОЙКА ФИЛЬТРАЦИИ ---
TARGET_LOCATIONS = [
    "Main RUDN University campus"
]

# Глобальные кэши для фильтрации зависимых сущностей
ALLOWED_LOC_IDS: Set[int] = set()
ALLOWED_THING_IDS: Set[int] = set()

# Смещения ID для генерации уникальных ключей
BIG_STR_OFFSET = 800_000_000_000_000
SYN_OP_OFFSET = 900_000_000_000

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_frost")

s = requests.Session()
s.headers.update({"Accept": "application/json"})

# Трансформер координат EPSG:3857 -> EPSG:4326
PROJECT_3857_TO_4326 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform

# ----------------------- Конфигурация РУДН (Mapping) -----------------------
# Принудительный маппинг по индексу в MultiDatastream
RUDN_OBS_PROPS = [
    {"code": "Dn", "name": "Минимальное направление ветра", "unit": "°"},
    {"code": "Dm", "name": "Среднее направление ветра", "unit": "°"},
    {"code": "Dx", "name": "Максимальное направление ветра", "unit": "°"},
    {"code": "Sn", "name": "Минимальная скорость ветра", "unit": "м/с"},
    {"code": "Sm", "name": "Средняя скорость ветра", "unit": "м/с"},
    {"code": "Sx", "name": "Максимальная скорость ветра", "unit": "м/с"},
    {"code": "Ta", "name": "Температура воздуха", "unit": "°C"},
    {"code": "Ua", "name": "Относительная влажность воздуха", "unit": "%"},
    {"code": "Pa", "name": "Атмосферное давление", "unit": "hPa"},
    {"code": "Rc", "name": "Осадки", "unit": "мм"},
]


# ----------------------- Утилиты -----------------------
def norm_bigint_id(raw, kind=""):
    if raw is None:
        raise ValueError(f"empty id for {kind}")
    if isinstance(raw, int):
        return raw
    sraw = str(raw).strip()
    try:
        return int(sraw)
    except Exception:
        pass
    h = hashlib.sha1(f"{kind}:{sraw}".encode("utf-8")).digest()
    v64 = int.from_bytes(h[:8], "big")
    return BIG_STR_OFFSET + (v64 % 100_000_000_000_000)


def entity_url(entity: str, raw_id):
    if raw_id is None:
        raise ValueError("empty key")
    if isinstance(raw_id, int):
        return f"{FROST}/{entity}({raw_id})"
    sraw = str(raw_id)
    try:
        i = int(sraw)
        return f"{FROST}/{entity}({i})"
    except Exception:
        pass
    s_odata = sraw.replace("'", "''")
    return f"{FROST}/{entity}('{s_odata}')"


def frost_get(url, params=None, retries=4, backoff=0.8):
    params = dict(params or {})
    while True:
        for attempt in range(retries):
            try:
                r = s.get(url, params=params, timeout=PAGE_TIMEOUT)
                if r.status_code == 404:
                    return
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} {r.text}")
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                sleep = backoff * (2 ** attempt)
                log.warning("GET %s failed: %s. Retry in %.1fs", url, e, sleep)
                time.sleep(sleep)
        else:
            raise RuntimeError(f"GET failed after retries: {url}")

        vals = data.get("value") or []
        for v in vals:
            yield v

        next_link = data.get("@iot.nextLink")
        if next_link:
            url = next_link
            params = None
            continue
        return


def frost_probe_count(url: str) -> int:
    try:
        r = s.get(url, params={"$top": 0, "$count": "true"}, timeout=PAGE_TIMEOUT)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        data = r.json()
        return int(data.get("@iot.count", 0))
    except Exception:
        return 0


def parse_time(ts: str) -> datetime:
    if not ts:
        raise ValueError("empty time")
    s = ts.split("/")[-1] if "/" in ts else ts
    dt = dtparser.isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def connect_db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    return conn


def ensure_aux_tables(conn):
    # --- 1. Создание вспомогательных таблиц и немедленный коммит ---
    try:
        cur = conn.cursor()
        cur.execute('CREATE EXTENSION IF NOT EXISTS postgis;')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_state(
                datastream_id bigint PRIMARY KEY,
                last_time timestamptz
            )
        """)

        cur.execute('''
            CREATE TABLE IF NOT EXISTS observation_hour (
                datastream_id bigint,
                thing_id bigint,
                location_id bigint,
                hour timestamptz,
                avg_val double precision,
                min_val double precision,
                max_val double precision,
                cnt int
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS thing_location (
                thing_id bigint,
                location_id bigint,
                start_time timestamptz,
                end_time timestamptz,
                PRIMARY KEY (thing_id, start_time)
            )
        ''')

        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        log.error("Error during auxiliary table creation: %s", e)
        raise

    # --- 2. Миграция схемы observed_property (с новым курсором) ---
    cur = conn.cursor()
    # 1. Удаляем старое ограничение УНИКАЛЬНОСТИ только по имени
    try:
        cur.execute("ALTER TABLE observed_property DROP CONSTRAINT observed_property_name_key;")
        conn.commit()
        log.info("Successfully dropped old 'observed_property_name_key' constraint for migration.")
    except psycopg2.errors.UndefinedObject:
        conn.rollback()
        log.info("Constraint 'observed_property_name_key' not found, skipping drop.")
    except Exception as e:
        conn.rollback()
        log.warning("Could not drop old unique constraint (may not exist): %s", e)

    # 2. Создаем составной уникальный индекс (Name + Unit)
    try:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_op_name_unit 
            ON observed_property (name, unit_symbol);
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.warning("Could not create composite unique index: %s", e)

    cur.close()


# ----------------------- Гео разбор -----------------------
def is_epsg3857(x: float, y: float) -> bool:
    return abs(x) > 180 or abs(y) > 90


def parse_location_coords(loc_obj) -> Optional[Tuple[float, float]]:
    if not loc_obj:
        return None
    geo = None
    try:
        if isinstance(loc_obj, dict):
            if "type" in loc_obj and "coordinates" in loc_obj:
                geo = shape(loc_obj)
            elif loc_obj.get("type") == "Feature" and "geometry" in loc_obj:
                geo = shape(loc_obj["geometry"])
            elif "value" in loc_obj:
                v = loc_obj["value"]
                if isinstance(v, dict):
                    if "type" in v and "coordinates" in v:
                        geo = shape(v)
                    elif v.get("type") == "Feature" and "geometry" in v:
                        geo = shape(v["geometry"])
    except Exception as e:
        log.debug("GeoJSON parsing error: %s", e)

    if geo is not None:
        if geo.geom_type == "Point":
            x, y = geo.x, geo.y
        else:
            c = geo.centroid
            x, y = c.x, c.y

        if is_epsg3857(x, y):
            p = shp_transform(PROJECT_3857_TO_4326, Point(x, y))
            return (p.x, p.y)
        else:
            return (x, y)

    if isinstance(loc_obj, dict):
        lon = loc_obj.get("longitude") or loc_obj.get("lon")
        lat = loc_obj.get("latitude") or loc_obj.get("lat")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return (float(lon), float(lat))
    return None


# ----------------------- Логика фильтрации и загрузки сущностей -----------------------

def upsert_locations_things(conn):
    cur = conn.cursor()

    # 1. Locations
    log.info("Fetching Locations (Filter: %s)...", TARGET_LOCATIONS if TARGET_LOCATIONS else "ALL")
    n_loc = 0

    for loc in frost_get(f"{FROST}/Locations", params={"$select": "@iot.id,name,location"}):
        try:
            loc_name = loc.get("name")
            if TARGET_LOCATIONS and loc_name not in TARGET_LOCATIONS:
                continue

            loc_id = norm_bigint_id(loc.get("@iot.id"), kind="Locations")
            ALLOWED_LOC_IDS.add(loc_id)

            final_name = loc_name or f"Location-{loc_id}"
            lonlat = parse_location_coords(loc.get("location"))
            lon, lat = (lonlat if lonlat else (None, None))

            cur.execute(
                """
                INSERT INTO location(location_id, name, geom)
                VALUES (%s,%s,
                        CASE WHEN %s IS NOT NULL AND %s IS NOT NULL
                             THEN ST_SetSRID(ST_Point(%s,%s),4326)
                             ELSE NULL END)
                ON CONFLICT(location_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    geom = COALESCE(EXCLUDED.geom, location.geom)
                """,
                (loc_id, final_name, lon, lat, lon, lat)
            )
            n_loc += 1
        except Exception as e:
            log.warning("Error processing Location: %s", e)

    log.info("Upserted %d allowed locations.", n_loc)

    # 2. Things + History
    log.info("Fetching Things...")
    n_tl = 0

    params = {
        "$expand": "HistoricalLocations($orderby=time asc;$expand=Locations($select=@iot.id)),Locations($select=@iot.id)",
        "$select": "@iot.id,name"
    }

    for thing in frost_get(f"{FROST}/Things", params=params):
        try:
            tid_raw = thing.get("@iot.id")
            tid = norm_bigint_id(tid_raw, kind="Things")

            visited_loc_ids = set()
            for l in (thing.get("Locations") or []):
                visited_loc_ids.add(norm_bigint_id(l.get("@iot.id"), kind="Locations"))
            hist_locs = thing.get("HistoricalLocations") or []
            for hl in hist_locs:
                for l in (hl.get("Locations") or []):
                    visited_loc_ids.add(norm_bigint_id(l.get("@iot.id"), kind="Locations"))

            if TARGET_LOCATIONS:
                if not visited_loc_ids.intersection(ALLOWED_LOC_IDS):
                    continue

            ALLOWED_THING_IDS.add(tid)
            tname = thing.get("name") or f"Thing {tid_raw}"
            cur.execute(
                "INSERT INTO thing(thing_id,name) VALUES (%s,%s) "
                "ON CONFLICT(thing_id) DO UPDATE SET name=EXCLUDED.name",
                (tid, tname)
            )

            cur.execute("DELETE FROM thing_location WHERE thing_id = %s", (tid,))

            if hist_locs:
                rows = []
                for hl in hist_locs:
                    ts_str = hl.get("time")
                    if not ts_str: continue
                    try:
                        start_time = parse_time(ts_str)
                    except Exception:
                        continue
                    locs = hl.get("Locations") or []
                    if not locs: continue
                    lid = norm_bigint_id(locs[0].get("@iot.id"), kind="Locations")
                    rows.append((start_time, lid))

                rows.sort(key=lambda x: x[0])

                for i, (start, lid) in enumerate(rows):
                    end = rows[i + 1][0] if i + 1 < len(rows) else datetime.max.replace(tzinfo=timezone.utc)
                    if not TARGET_LOCATIONS or lid in ALLOWED_LOC_IDS:
                        cur.execute(
                            """
                            INSERT INTO thing_location(thing_id, location_id, start_time, end_time)
                            VALUES (%s,%s,%s,%s)
                            """, (tid, lid, start, end)
                        )
                        n_tl += 1
            else:
                locs = thing.get("Locations") or []
                if locs:
                    lid = norm_bigint_id(locs[0].get("@iot.id"), kind="Locations")
                    if not TARGET_LOCATIONS or lid in ALLOWED_LOC_IDS:
                        cur.execute(
                            """
                            INSERT INTO thing_location(thing_id, location_id, start_time, end_time)
                            VALUES (%s,%s,%s,%s)
                            """,
                            (tid, lid, datetime.min.replace(tzinfo=timezone.utc),
                             datetime.max.replace(tzinfo=timezone.utc))
                        )
                        n_tl += 1

        except Exception as e:
            log.warning("Error processing Thing %s: %s", thing.get("@iot.id"), e)

    conn.commit()
    log.info("Upserted things and map. Total intervals: %s", n_tl)
    cur.close()


# ----------------------- Datastreams -----------------------
def upsert_props_and_ds(conn):
    cur = conn.cursor()
    n_ds = 0
    select = "@iot.id,unitOfMeasurement"
    expand = "ObservedProperty($select=@iot.id,name),Thing($select=@iot.id)"

    for ds in frost_get(f"{FROST}/Datastreams", params={"$select": select, "$expand": expand}):
        try:
            thing = ds.get("Thing") or {}
            thing_id = norm_bigint_id(thing.get("@iot.id"), kind="Things") if thing.get("@iot.id") is not None else None

            if thing_id is not None and TARGET_LOCATIONS:
                if thing_id not in ALLOWED_THING_IDS:
                    continue

            ds_id = norm_bigint_id(ds.get("@iot.id"), kind="Datastreams")
            op = ds.get("ObservedProperty") or {}

            unit = None
            uom = ds.get("unitOfMeasurement") or {}
            if isinstance(uom, dict):
                unit = uom.get("symbol") or uom.get("name")

            op_id = norm_bigint_id(op.get("@iot.id"), kind="ObservedProperties") if op.get(
                "@iot.id") is not None else None
            op_name = op.get("name")

            if op_id is not None:
                # 1. Search for existing property by (name, unit_symbol)
                cur.execute("""
                    SELECT obs_prop_id FROM observed_property
                    WHERE name = %s 
                      AND unit_symbol IS NOT DISTINCT FROM %s
                """, (op_name, unit))

                existing_row = cur.fetchone()

                if existing_row:
                    final_op_id = existing_row[0]
                else:
                    final_op_id = op_id
                    # 2. Insert if not found
                    cur.execute(
                        """
                        INSERT INTO observed_property(obs_prop_id, name, unit_symbol)
                        VALUES (%s,%s,%s)
                        ON CONFLICT(obs_prop_id) DO UPDATE SET
                          name = COALESCE(EXCLUDED.name, observed_property.name),
                          unit_symbol = COALESCE(EXCLUDED.unit_symbol, observed_property.unit_symbol)
                        """, (final_op_id, op_name, unit)
                    )

            else:
                final_op_id = op_id  # will be None if op_id is None

            cur.execute(
                """
                INSERT INTO datastream(datastream_id, thing_id, obs_prop_id, unit_symbol)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(datastream_id) DO UPDATE SET
                  thing_id=EXCLUDED.thing_id,
                  obs_prop_id=COALESCE(EXCLUDED.obs_prop_id, datastream.obs_prop_id),
                  unit_symbol=COALESCE(EXCLUDED.unit_symbol, datastream.unit_symbol)
                """, (ds_id, thing_id, final_op_id, unit)
            )
            n_ds += 1
        except Exception as e:
            log.warning("skip Datastream: %s", e)
    conn.commit()
    log.info("Upserted %s datastreams (filtered).", n_ds)
    cur.close()


def upsert_props_and_virtual_ds_from_md(conn):
    """
    Загрузка MultiDatastreams с принудительным исправлением имен и единиц измерения.
    Исправлено: теперь ищет существующее ObservedProperty по (Name, Unit) перед вставкой,
    чтобы избежать UniqueViolation.
    """
    cur = conn.cursor()
    n_vds = 0

    select = "@iot.id,unitOfMeasurements"
    expand = "Thing($select=@iot.id),ObservedProperties($select=@iot.id,name)"
    for md in frost_get(f"{FROST}/MultiDatastreams", params={"$select": select, "$expand": expand}):
        try:
            th = md.get("Thing") or {}
            thing_id = norm_bigint_id(th.get("@iot.id"), kind="Things") if th.get("@iot.id") is not None else None

            if thing_id is not None and TARGET_LOCATIONS:
                if thing_id not in ALLOWED_THING_IDS:
                    continue

            md_raw_id = md.get("@iot.id")
            md_id = norm_bigint_id(md_raw_id, kind="MultiDatastreams")
            obs_props = md.get("ObservedProperties") or []

            for idx, op in enumerate(obs_props):

                # 1. Определяем финальное имя и юнит (RUDN override logic)
                rudn_conf = None
                if idx < len(RUDN_OBS_PROPS):
                    rudn_conf = RUDN_OBS_PROPS[idx]

                final_name = (op.get("name") or "").strip()
                final_unit = None

                if rudn_conf:
                    final_name = rudn_conf["name"]
                    final_unit = rudn_conf["unit"]
                elif not final_name:
                    final_name = f"MD{md_raw_id}_c{idx}"

                # 2. Ищем существующее Observed Property по (Name, Unit)
                real_op_id = None
                if final_name:
                    cur.execute("""
                        SELECT obs_prop_id FROM observed_property
                        WHERE name = %s 
                          AND unit_symbol IS NOT DISTINCT FROM %s
                    """, (final_name, final_unit))

                    existing_row = cur.fetchone()
                    if existing_row:
                        real_op_id = existing_row[0]

                # 3. Если не найдено, создаем новое с синтетическим ID
                if real_op_id is None:
                    # Генерируем новый синтетический ID для вставки
                    real_op_id = SYN_OP_OFFSET + md_id * 100 + idx

                    # Вставка нового свойства (это произойдет только один раз для пары Name+Unit)
                    cur.execute(
                        """
                        INSERT INTO observed_property(obs_prop_id, name, unit_symbol)
                        VALUES (%s,%s,%s)
                        ON CONFLICT(obs_prop_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            unit_symbol = COALESCE(EXCLUDED.unit_symbol, observed_property.unit_symbol)
                        """, (real_op_id, final_name, final_unit)
                    )

                # 4. Upsert виртуального датастрима (используем найденный или созданный real_op_id)
                vds_id = md_id * 100 + idx
                cur.execute(
                    """
                    INSERT INTO datastream(datastream_id, thing_id, obs_prop_id, unit_symbol)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(datastream_id) DO UPDATE SET
                        thing_id=EXCLUDED.thing_id,
                        obs_prop_id=EXCLUDED.obs_prop_id, 
                        unit_symbol=COALESCE(EXCLUDED.unit_symbol, datastream.unit_symbol)
                    """, (vds_id, thing_id, real_op_id, final_unit)
                )
                n_vds += 1
        except Exception as e:
            log.warning("skip MD: %s", e)
    conn.commit()
    log.info("Upserted %s virtual datastreams from MD (forced fixed names/units).", n_vds)
    cur.close()


# ----------------------- Observations -----------------------
def get_watermark(cur, ds_id: int, start_default: datetime):
    cur.execute("SELECT last_time FROM ingestion_state WHERE datastream_id=%s", (ds_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else start_default


def set_watermark(cur, ds_id: int, ts: datetime):
    cur.execute(
        """
        INSERT INTO ingestion_state(datastream_id,last_time)
        VALUES (%s,%s)
        ON CONFLICT(datastream_id) DO UPDATE SET last_time=EXCLUDED.last_time
        """, (ds_id, ts)
    )


def resolve_location_id(cur, thing_id: int, at_time: datetime, cache: Dict) -> Optional[int]:
    if len(cache) > 10000: cache.clear()
    h = floor_hour(at_time)
    key = (thing_id, h)
    if key in cache: return cache[key]

    cur.execute("""
        SELECT location_id
        FROM thing_location
        WHERE thing_id=%s AND start_time <= %s AND end_time > %s
        LIMIT 1
    """, (thing_id, at_time, at_time))
    row = cur.fetchone()
    loc_id = row[0] if row else None
    cache[key] = loc_id
    return loc_id


def aggregate_and_upsert_hourly(cur, ds_id: int, thing_id: int, points: list,
                                loc_cache: Dict,
                                skipped_counter: Dict[int, int]) -> Optional[datetime]:
    buckets = {}
    last_ts = None
    for ts, val in points:
        h = floor_hour(ts)
        fv = float(val)
        if h not in buckets: buckets[h] = {"sum": 0.0, "min": fv, "max": fv, "cnt": 0, "last_ts": ts}
        b = buckets[h]
        b["sum"] += fv
        b["cnt"] += 1
        if fv < b["min"]: b["min"] = fv
        if fv > b["max"]: b["max"] = fv
        if ts > b["last_ts"]: b["last_ts"] = ts
        if last_ts is None or ts > last_ts: last_ts = ts

    for hour, a in buckets.items():
        loc_id = resolve_location_id(cur, thing_id, hour, loc_cache)
        if loc_id is None:
            skipped_counter[thing_id] = skipped_counter.get(thing_id, 0) + 1
            continue

        avg_val = round(a["sum"] / a["cnt"], 3)
        cur.execute(
            """
            INSERT INTO observation_hour(datastream_id, thing_id, location_id, hour,
                                         avg_val, min_val, max_val, cnt)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (datastream_id, location_id, hour) DO UPDATE SET
              avg_val = (observation_hour.avg_val * observation_hour.cnt + EXCLUDED.avg_val * EXCLUDED.cnt) / (observation_hour.cnt + EXCLUDED.cnt),
              min_val = LEAST(EXCLUDED.min_val, observation_hour.min_val),
              max_val = GREATEST(EXCLUDED.max_val, observation_hour.max_val),
              cnt     = observation_hour.cnt + EXCLUDED.cnt
            """, (ds_id, thing_id, loc_id, hour, avg_val, a["min"], a["max"], a["cnt"])
        )
    return last_ts


def ingest_ds_observations(conn):
    cur = conn.cursor()
    n_ds = n_pts = 0
    skipped_counter: Dict[int, int] = {}
    loc_cache = {}

    log.info("Ingesting Datastreams observations...")
    expand = "Thing($select=@iot.id)"

    for ds in frost_get(f"{FROST}/Datastreams", params={"$select": "@iot.id", "$expand": expand}):
        ds_raw = ds.get("@iot.id")
        thing = ds.get("Thing") or {}
        thing_id = norm_bigint_id(thing.get("@iot.id"), kind="Things") if thing.get("@iot.id") is not None else None

        if TARGET_LOCATIONS and thing_id not in ALLOWED_THING_IDS:
            continue

        ds_id = norm_bigint_id(ds_raw, kind="Datastreams")
        wm = get_watermark(cur, ds_id, START_FROM_DT)
        url_obs = entity_url("Datastreams", ds_raw) + "/Observations"

        # <-- ИСПРАВЛЕНИЕ 1: Использование полной точности времени в фильтре
        filter_time = wm.strftime('%Y-%m-%dT%H:%M:%S.') + f"{wm.microsecond:06}Z"

        params = {
            "$select": "result,phenomenonTime",
            "$orderby": "phenomenonTime asc",
            "$filter": f"phenomenonTime gt {filter_time}"
        }

        buffer = []
        last_ts = None
        try:
            for ob in frost_get(url_obs, params=params):
                ts = ob.get("phenomenonTime")
                val = ob.get("result")
                if ts is None or val is None: continue
                try:
                    dt = parse_time(ts)
                    fv = float(str(val).replace(",", "."))
                except Exception:
                    continue
                buffer.append((dt, fv))
                n_pts += 1
                if len(buffer) >= BATCH_SIZE:
                    l = aggregate_and_upsert_hourly(cur, ds_id, thing_id, buffer, loc_cache, skipped_counter)
                    if l and (last_ts is None or l > last_ts): last_ts = l
                    buffer.clear()
                    conn.commit()
        except RuntimeError:
            buffer.clear()

        if buffer:
            l = aggregate_and_upsert_hourly(cur, ds_id, thing_id, buffer, loc_cache, skipped_counter)
            if l and (last_ts is None or l > last_ts): last_ts = l
        if last_ts: set_watermark(cur, ds_id, last_ts)
        conn.commit()
        n_ds += 1

    log.info("DS done: %d streams, %d points.", n_ds, n_pts)
    cur.close()


def ingest_md_observations(conn):
    cur = conn.cursor()
    n_md = n_pts = 0
    skipped_counter: Dict[int, int] = {}
    loc_cache = {}

    log.info("Ingesting MD observations...")
    expand = "Thing($select=@iot.id)"

    for md in frost_get(f"{FROST}/MultiDatastreams", params={"$select": "@iot.id", "$expand": expand}):
        md_raw = md.get("@iot.id")
        thing = md.get("Thing") or {}
        thing_id = norm_bigint_id(thing.get("@iot.id"), kind="Things") if thing.get("@iot.id") is not None else None

        if TARGET_LOCATIONS and thing_id not in ALLOWED_THING_IDS:
            continue

        md_id = norm_bigint_id(md_raw, kind="MultiDatastreams")
        url_obs = entity_url("MultiDatastreams", md_raw) + "/Observations"
        if frost_probe_count(url_obs) == 0: continue

        start_wm = get_watermark(cur, md_id * 100 + 0, START_FROM_DT)

        # <-- ИСПРАВЛЕНИЕ 2: Использование полной точности времени в фильтре
        filter_time = start_wm.strftime('%Y-%m-%dT%H:%M:%S.') + f"{start_wm.microsecond:06}Z"

        params = {
            "$select": "result,phenomenonTime",
            "$orderby": "phenomenonTime asc",
            "$filter": f"phenomenonTime gt {filter_time}"  # <-- Использовать filter_time
        }

        buffers = {}
        latest_ts = None

        def flush_buffers():
            nonlocal n_pts
            nonlocal latest_ts
            for idx, arr in buffers.items():
                if not arr: continue
                vds_id = md_id * 100 + idx
                l = aggregate_and_upsert_hourly(cur, vds_id, thing_id, arr, loc_cache, skipped_counter)
                if l and (latest_ts is None or l > latest_ts): latest_ts = l
                n_pts += len(arr)
            buffers.clear()

        try:
            count = 0
            for ob in frost_get(url_obs, params=params):
                ts = ob.get("phenomenonTime")
                res = ob.get("result")
                if ts is None or res is None or not isinstance(res, (list, tuple)): continue
                try:
                    dt = parse_time(ts)
                except Exception:
                    continue
                if latest_ts is None or dt > latest_ts: latest_ts = dt
                for idx, raw in enumerate(res):
                    if raw is None: continue
                    try:
                        val = float(str(raw).replace(",", "."))
                    except ValueError:
                        continue
                    if idx not in buffers: buffers[idx] = []
                    buffers[idx].append((dt, val))
                count += 1
                if count >= BATCH_SIZE:
                    flush_buffers()
                    conn.commit()
                    count = 0
        except RuntimeError:
            pass

        flush_buffers()
        if latest_ts:
            for idx in range(20):
                vds_id = md_id * 100 + idx
                cur.execute("SELECT 1 FROM datastream WHERE datastream_id=%s", (vds_id,))
                if cur.fetchone(): set_watermark(cur, vds_id, latest_ts)
        conn.commit()
        n_md += 1

    log.info("MD done: %d streams, %d points.", n_md, n_pts)
    cur.close()


def main():
    log.info("Start. FROST_URL=%s", FROST)
    try:
        r = s.get(FROST, timeout=PAGE_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.error("FROST healthcheck failed: %s", e)
        return

    conn = connect_db()
    try:
        ensure_aux_tables(conn)  # Гарантируем, что таблицы существуют
        upsert_locations_things(conn)
        upsert_props_and_ds(conn)
        upsert_props_and_virtual_ds_from_md(conn)
        ingest_ds_observations(conn)
        ingest_md_observations(conn)
        log.info("All done.")
    except Exception as e:
        log.exception("Global error:")
    finally:
        conn.close()


if __name__ == "__main__":
    main()