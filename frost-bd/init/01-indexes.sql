-- карта: точки локаций
CREATE INDEX idx_location_geom ON location USING GIST (geom);

-- всплывашка: 3 последних значения для каждого (thing,location)
CREATE INDEX idx_hour_tl_hour ON observation_hour (thing_id, location_id, hour DESC);

-- дашборд: временной ряд для одного thing+location
CREATE INDEX idx_hour_tl_phenom ON observation_hour (thing_id, location_id, hour);

-- быстрый поиск datastream по thing + observed-property
CREATE INDEX idx_ds_thing_prop ON datastream (thing_id, obs_prop_id);