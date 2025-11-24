-- список локаций + координаты (для Leaflet)
CREATE OR REPLACE VIEW api_locations AS
SELECT l.location_id,
       l.name,
       ST_X(l.geom) lon,
       ST_Y(l.geom) lat,
       jsonb_agg(DISTINCT jsonb_build_object(
           'thing_id', t.thing_id,
           'name', t.name)) as things
FROM location l
JOIN thing_location tl ON tl.location_id = l.location_id
JOIN thing t ON t.thing_id = tl.thing_id
GROUP BY l.location_id, l.name, l.geom;

-- 3 последних значения для (thing,location)
CREATE OR REPLACE FUNCTION api_last3(
  p_thing_id bigint,
  p_location_id bigint)
RETURNS TABLE(phenomenon text, unit_symbol text, avg_val numeric, hour timestamptz)
LANGUAGE sql STABLE AS $$
SELECT op.name, op.unit_symbol, o.avg_val, o.hour
FROM   observation_hour o
JOIN   datastream ds ON ds.datastream_id = o.datastream_id
JOIN   observed_property op ON op.obs_prop_id = ds.obs_prop_id
WHERE  o.thing_id = p_thing_id
  AND  o.location_id = p_location_id
ORDER  BY o.hour DESC
LIMIT  3;
$$;

-- временной ряд для графика (dash)
CREATE OR REPLACE FUNCTION api_series(
  p_thing_id bigint,
  p_location_id bigint,
  p_phenomenon text default null,
  p_start timestamptz default now() - interval '1 week',
  p_end timestamptz default now())
RETURNS TABLE(hour timestamptz, phenomenon text, unit_symbol text,
              avg_val numeric, min_val numeric, max_val numeric)
LANGUAGE sql STABLE AS $$
SELECT o.hour, op.name, op.unit_symbol,
       o.avg_val, o.min_val, o.max_val
FROM   observation_hour o
JOIN   datastream ds ON ds.datastream_id = o.datastream_id
JOIN   observed_property op ON op.obs_prop_id = ds.obs_prop_id
WHERE  o.thing_id = p_thing_id
  AND  o.location_id = p_location_id
  AND  (p_phenomenon IS NULL OR op.name = p_phenomenon)
  AND  o.hour BETWEEN p_start AND p_end
ORDER  BY o.hour, op.name;
$$;