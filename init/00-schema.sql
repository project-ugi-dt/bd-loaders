-- расширения
CREATE EXTENSION IF NOT EXISTS postgis;

-- 1. Локации (Point)
CREATE TABLE location (
    location_id bigint PRIMARY KEY,
    name        text,
    geom        geometry(Point, 4326)
);

-- 2. Станции (Thing)
CREATE TABLE thing (
    thing_id bigint PRIMARY KEY,
    name     text
);

-- 3. Связь M-N Thing ↔ Location
CREATE TABLE thing_location (
    thing_location_id bigserial PRIMARY KEY,
    thing_id    bigint REFERENCES thing(thing_id)   ON DELETE CASCADE,
    location_id bigint REFERENCES location(location_id) ON DELETE CASCADE,
    start_time  timestamptz DEFAULT '-infinity',
    end_time    timestamptz DEFAULT 'infinity',
    UNIQUE (thing_id, location_id, start_time)
);

-- 4. ObservedProperty (универсальный справочник)
CREATE TABLE observed_property (
    obs_prop_id bigint PRIMARY KEY,
    name        text,
    unit_symbol text,
    UNIQUE (name, unit_symbol) 
);

-- 5. Datastream
CREATE TABLE datastream (
    datastream_id   bigint PRIMARY KEY,
    thing_id        bigint REFERENCES thing(thing_id) ON DELETE CASCADE,
    obs_prop_id     bigint REFERENCES observed_property(obs_prop_id),
    unit_symbol     text
);

-- 6. Сырые Observation (partition by month)
CREATE TABLE observation (
    observation_id bigserial,
    datastream_id  bigint REFERENCES datastream(datastream_id) ON DELETE CASCADE,
    phenomenon_time timestamptz NOT NULL,
    result         double precision NOT NULL
) PARTITION BY RANGE (phenomenon_time);

-- 7. Hour-агрегат (основная витрина для фронта)
CREATE TABLE observation_hour (
    datastream_id   bigint,
    thing_id        bigint,
    location_id     bigint,
    hour            timestamptz NOT NULL,
    avg_val         numeric,
    min_val         numeric,
    max_val         numeric,
    cnt             int,
    PRIMARY KEY (datastream_id, location_id, hour)
);