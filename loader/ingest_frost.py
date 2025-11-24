# ingest_frost.py
import os
import time
import logging
from datetime import datetime, timezone
from dateutil import parser as dtparser
import requests
import psycopg2

# ---------- НАСТРОЙКИ ----------
FROST = "http://90.156.134.128:8080/FROST-Server/v1.1".rstrip("/")
DSN="host=localhost port=5433 dbname=frost user=frost password=frost"
START_FROM_DT = datetime(2024, 1, 1, tzinfo=timezone.utc)

DS_INCLUDE = set()  # напр. {1,2,3}
DS_EXCLUDE = set()  # напр. {10,11}

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
# -------------------------------------------

logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('frost_etl')

s = requests.Session()
s.headers.update({'Accept': 'application/json'})

def frost_get(url, params=None, retries=4, backoff=0.8):
    params = dict(params or {})
    while True:
        for attempt in range(retries):
            try:
                r = s.get(url, params=params, timeout=60)
                
                # ИСПРАВЛЕНИЕ: Если Datastream не найден (404), это означает, что он 
                # пришел с другого сервера. Мы просто пропускаем его, не повторяя запрос.
                if r.status_code == 404:
                    log.warning('GET %s failed: 404 Not Found. Skipping URL.', url)
                    return # Завершаем итерацию генератора для этой URL
                
                if r.status_code >= 500:
                    raise requests.HTTPError(f'{r.status_code} {r.text}')
                
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                sleep = backoff * (2 ** attempt)
                log.warning('GET %s failed: %s. Retry in %.1fs', url, e, sleep)
                time.sleep(sleep)
        else:
            # Если все попытки исчерпаны, и это не была ошибка 404, выбрасываем ошибку
            raise RuntimeError(f'GET failed after retries: {url}')

        vals = data.get('value') or []
        for v in vals:
            yield v

        next_link = data.get('@iot.nextLink')
        if next_link:
            url = next_link
            params = None
            continue
        return

def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

def parse_time(ts: str) -> datetime:
    dt = dtparser.isoparse(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def connect_db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    return conn

def ensure_aux_tables(conn):
    
    # --- 1. Создание вспомогательных таблиц и немедленный коммит ---
    try:
        cur = conn.cursor()
        cur.execute('CREATE EXTENSION IF NOT EXISTS postgis;')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS ingestion_state (
                datastream_id bigint PRIMARY KEY,
                last_time timestamptz
            )
        ''')
        
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
    # 1. Удаляем старое ограничение УНИКАЛЬНОСТИ только по имени, если оно есть
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
    
    # 2. Создаем составной уникальный индекс, если его нет
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


def ensure_strict_observation_table(conn):
    """
    Создает уникальный индекс (datastream_id, hour).
    """
    cur = conn.cursor()
    
    # Проверка существования индекса
    cur.execute("""
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'observation_hour_strict_idx'
    """)
    if cur.fetchone():
        cur.close()
        return

    log.info("Enforcing strict uniqueness on (datastream_id, hour)...")

    # Удаление полных дублей перед созданием индекса
    cur.execute("""
        DELETE FROM observation_hour a USING observation_hour b
        WHERE a.datastream_id = b.datastream_id 
          AND a.hour = b.hour 
          AND a.ctid < b.ctid
    """)
    
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS observation_hour_strict_idx 
        ON observation_hour (datastream_id, hour)
    """)
    conn.commit()
    cur.close()

def upsert_locations_things(conn):
    """
    Обновляет Things и Locations.
    КРИТИЧНО: Правильно строит историю перемещений (HistoricalLocations).
    """
    cur = conn.cursor()
    
    # 1. Загрузка Locations
    log.info("Syncing Locations...")
    for loc in frost_get(f"{FROST}/Locations", params={'$select': '@iot.id,name,location'}):
        loc_id = int(loc.get('@iot.id'))
        name = loc.get('name')
        geo = loc.get('location') or {}
        lon = lat = None
        if isinstance(geo, dict) and geo.get('type') == 'Point':
            coords = geo.get('coordinates') or []
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])

        cur.execute(
            '''
            INSERT INTO location(location_id, name, geom)
            VALUES (%s,%s,
                CASE WHEN %s IS NOT NULL
                     THEN ST_SetSRID(ST_Point(%s,%s),4326)
                     ELSE NULL
                END)
            ON CONFLICT (location_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                geom = COALESCE(EXCLUDED.geom, location.geom)
            ''',
            (loc_id, name, lat if lat is not None else None, lon, lat)
        )
    conn.commit()

    # 2. Загрузка Things и построение истории локаций
    log.info("Syncing Things and HistoricalLocations...")
    
    select = '@iot.id,name'
    expand = 'HistoricalLocations($select=time;$orderby=time asc;$expand=Locations($select=@iot.id)),Locations($select=@iot.id)'
    
    for thing in frost_get(f"{FROST}/Things", params={'$select': select, '$expand': expand}):
        tid = int(thing.get('@iot.id'))
        tname = thing.get('name')
        
        cur.execute(
            '''
            INSERT INTO thing(thing_id, name)
            VALUES (%s,%s)
            ON CONFLICT (thing_id) DO UPDATE SET name = EXCLUDED.name
            ''',
            (tid, tname)
        )

        hls = thing.get('HistoricalLocations') or []
        events = []
        
        for hl in hls:
            ts_str = hl.get('time')
            if not ts_str:
                continue
            ts = parse_time(ts_str)
            
            locs = hl.get('Locations') or []
            if locs:
                lid = int(locs[0].get('@iot.id'))
                events.append({'time': ts, 'lid': lid})
        
        events.sort(key=lambda x: x['time'])

        if not events:
            curr_locs = thing.get('Locations') or []
            if curr_locs:
                lid = int(curr_locs[0].get('@iot.id'))
                events.append({'time': datetime.min.replace(tzinfo=timezone.utc), 'lid': lid})

        intervals = []
        for i, ev in enumerate(events):
            start = ev['time']
            lid = ev['lid']
            
            if i + 1 < len(events):
                end = events[i+1]['time']
            else:
                end = datetime.max.replace(tzinfo=timezone.utc)
            
            if start < end:
                intervals.append((tid, lid, start, end))

        if intervals:
            cur.execute("DELETE FROM thing_location WHERE thing_id = %s", (tid,))
            for (t_id, l_id, s_t, e_t) in intervals:
                cur.execute(
                    """
                    INSERT INTO thing_location(thing_id, location_id, start_time, end_time)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (t_id, l_id, s_t, e_t)
                )

    conn.commit()
    cur.close()


def upsert_observed_properties_and_datastreams(conn):
    cur = conn.cursor()
    expand = 'ObservedProperty($select=@iot.id,name),Thing($select=@iot.id)'
    select = '@iot.id,unitOfMeasurement,ObservedProperty,Thing'
    
    for ds in frost_get(f"{FROST}/Datastreams", params={'$select': select, '$expand': expand}):
        ds_id = int(ds.get('@iot.id'))

        if DS_INCLUDE and ds_id not in DS_INCLUDE:
            pass
        if DS_EXCLUDE and ds_id in DS_EXCLUDE:
            continue

        uom = ds.get('unitOfMeasurement') or {}
        unit_symbol = uom.get('symbol')

        thing = ds.get('Thing') or {}
        thing_id = int(thing.get('@iot.id')) if thing.get('@iot.id') is not None else None

        op = ds.get('ObservedProperty') or {}
        remote_op_id = int(op.get('@iot.id')) if op.get('@iot.id') is not None else None
        op_name = op.get('name')

        # --- ЛОГИКА СОВПАДЕНИЯ (Name + Unit) ---
        final_op_id = remote_op_id

        if op_name:
            # Ищем существующий Observed Property по паре (name, unit_symbol).
            cur.execute("""
                SELECT obs_prop_id FROM observed_property
                WHERE name = %s 
                  AND unit_symbol IS NOT DISTINCT FROM %s
            """, (op_name, unit_symbol))

            row = cur.fetchone()
            
            if row:
                # Нашли полное совпадение. Используем ID из базы.
                final_op_id = row[0]
                # Обновляем имя и юнит, чтобы быть уверенными в актуальности
                cur.execute(
                    """
                    UPDATE observed_property SET name = %s, unit_symbol = %s
                    WHERE obs_prop_id = %s
                    """,
                    (op_name, unit_symbol, final_op_id)
                )
            else:
                # Совпадения нет. Вставляем как новое свойство.
                if remote_op_id is not None:
                    cur.execute(
                        '''
                        INSERT INTO observed_property(obs_prop_id, name, unit_symbol)
                        VALUES (%s,%s,%s)
                        ON CONFLICT (obs_prop_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            unit_symbol = EXCLUDED.unit_symbol
                        ''',
                        (remote_op_id, op_name, unit_symbol)
                    )

        # Если имени нет, но есть ID (редкий случай), просто обновляем
        elif remote_op_id is not None:
             cur.execute(
                '''
                INSERT INTO observed_property(obs_prop_id, name, unit_symbol)
                VALUES (%s,%s,%s)
                ON CONFLICT (obs_prop_id) DO UPDATE SET
                    unit_symbol = COALESCE(EXCLUDED.unit_symbol, observed_property.unit_symbol)
                ''',
                (remote_op_id, op_name, unit_symbol)
            )

        # Привязываем Datastream к найденному или созданному свойству (final_op_id)
        cur.execute(
            '''
            INSERT INTO datastream(datastream_id, thing_id, obs_prop_id, unit_symbol)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (datastream_id) DO UPDATE SET
                thing_id = EXCLUDED.thing_id,
                obs_prop_id = EXCLUDED.obs_prop_id,
                unit_symbol = EXCLUDED.unit_symbol
            ''',
            (ds_id, thing_id, final_op_id, unit_symbol)
        )
    conn.commit()
    cur.close()

def resolve_location_id(cur, thing_id: int, at_hour: datetime):
    """
    Находит location_id, действовавший в момент at_hour.
    Ищет строго по интервалу: start_time <= at_hour < end_time
    """
    cur.execute("""
        SELECT location_id
        FROM thing_location
        WHERE thing_id=%s 
          AND start_time <= %s 
          AND end_time > %s
        LIMIT 1
    """, (thing_id, at_hour, at_hour))
    row = cur.fetchone()
    if row:
        return int(row[0])
    
    # Fallback
    cur.execute("""
        SELECT location_id FROM thing_location 
        WHERE thing_id=%s 
        ORDER BY ABS(EXTRACT(EPOCH FROM (start_time - %s))) ASC
        LIMIT 1
    """, (thing_id, at_hour))
    row = cur.fetchone()
    if row:
        return int(row[0])

    return None

def get_watermark(cur, ds_id: int, start_default: datetime):
    cur.execute('SELECT last_time FROM ingestion_state WHERE datastream_id=%s', (ds_id,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return start_default

def set_watermark(cur, ds_id: int, ts: datetime):
    cur.execute(
        '''
        INSERT INTO ingestion_state(datastream_id, last_time)
        VALUES (%s,%s)
        ON CONFLICT (datastream_id) DO UPDATE SET last_time = EXCLUDED.last_time
        ''',
        (ds_id, ts)
    )

def aggregate_and_upsert_hourly(cur, ds_id: int, thing_id: int, points: list):
    buckets = {}
    last_ts = None
    for ts, val in points:
        h = floor_hour(ts)
        fv = float(val)
        agg = buckets.get(h)
        if agg is None:
            buckets[h] = {'sum': fv, 'min': fv, 'max': fv, 'cnt': 1}
        else:
            agg['sum'] += fv
            agg['cnt'] += 1
            if fv < agg['min']:
                agg['min'] = fv
            if fv > agg['max']:
                agg['max'] = fv
        if last_ts is None or ts > last_ts:
            last_ts = ts

    skipped = 0
    for hour, a in buckets.items():
        # Определяем локацию на начало часа
        loc_id = resolve_location_id(cur, thing_id, hour)
        
        if loc_id is None:
            skipped += 1
            continue

        DECIMALS = 2
        avg_val = round(a['sum'] / a['cnt'], DECIMALS)
        min_val = round(a['min'], DECIMALS)
        max_val = round(a['max'], DECIMALS)

        cur.execute(
            '''
            INSERT INTO observation_hour(datastream_id, thing_id, location_id, hour,
                                         avg_val, min_val, max_val, cnt)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (datastream_id, hour) DO UPDATE SET
              location_id = EXCLUDED.location_id,
              avg_val = EXCLUDED.avg_val,
              min_val = LEAST(EXCLUDED.min_val, observation_hour.min_val),
              max_val = GREATEST(EXCLUDED.max_val, observation_hour.max_val),
              cnt     = observation_hour.cnt + EXCLUDED.cnt
            ''',
            (ds_id, thing_id, loc_id, hour, avg_val, a['min'], a['max'], a['cnt'])
        )

    if skipped:
        log.warning("ds %s (thing %s): skipped %s hourly rows because location is unknown", ds_id, thing_id, skipped)
    return last_ts

def ingest_observations(conn):
    cur = conn.cursor()
    # Выбираем ВСЕ datastream_id из БД, чтобы проверить их на текущем сервере
    cur.execute('SELECT datastream_id, thing_id FROM datastream WHERE thing_id IS NOT NULL ORDER BY datastream_id')
    rows = cur.fetchall()

    start_default = START_FROM_DT

    for ds_id, thing_id in rows:
        if DS_INCLUDE and ds_id not in DS_INCLUDE:
            continue
        if DS_EXCLUDE and ds_id in DS_EXCLUDE:
            continue

        wm = get_watermark(cur, ds_id, start_default)
        url = f"{FROST}/Datastreams({ds_id})/Observations"
        
        # ИСПРАВЛЕНИЕ: Использование полной точности времени в фильтре, чтобы избежать повторной загрузки
        filter_time = wm.strftime('%Y-%m-%dT%H:%M:%S.') + f"{wm.microsecond:06}Z"

        params = {
            '$select': 'result,phenomenonTime',
            '$orderby': 'phenomenonTime asc',
            '$filter': f"phenomenonTime gt {filter_time}"
        }

        batch = []
        latest = wm
        count = 0

        # Читаем данные пачками. Если frost_get вернет 404, он завершится без ошибки.
        for obs in frost_get(url, params=params):
            try:
                ts = parse_time(obs.get('phenomenonTime'))
            except Exception:
                continue

            res = obs.get('result')
            if res is None:
                continue
            try:
                val = float(res)
            except Exception:
                continue

            batch.append((ts, val))
            if len(batch) >= 1000:
                last_ts = aggregate_and_upsert_hourly(cur, ds_id, thing_id, batch)
                if last_ts and last_ts > latest:
                    latest = last_ts
                batch.clear()
                count += 1000

        # Добиваем остаток
        if batch:
            last_ts = aggregate_and_upsert_hourly(cur, ds_id, thing_id, batch)
            if last_ts and last_ts > latest:
                latest = last_ts
            count += len(batch)

        # Обновляем водяной знак только если были загружены новые данные
        if latest > wm:
            set_watermark(cur, ds_id, latest)

        conn.commit()
        log.info('Datastream %s: ingested %s points up to %s', ds_id, count, latest.isoformat())
    
    cur.close()

def main():
    conn = connect_db()
    try:
        ensure_aux_tables(conn)
        ensure_strict_observation_table(conn)
        
        upsert_locations_things(conn)
        upsert_observed_properties_and_datastreams(conn)
        
        ingest_observations(conn)
    finally:
        conn.close()

if __name__ == '__main__':
    main()