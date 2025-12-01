## Создание frost БД
### `/frost-bd`
Создание БД PostgreSQL с расширением PostGIS для данных сенсоров

- **Таблицы**:
  - `location`: Локации (точки на карте) с ID, именем и геометрией.
  - `thing`: Станции (устройства) с ID и именем.
  - `thing_location`: Связь M:N между станциями и локациями с временными интервалами (start_time/end_time).
  - `observed_property`: Справочник наблюдаемых свойств (phenomenon), таких как температура, с единицами измерения.
  - `datastream`: Потоки данных, связывающие станцию и свойство.
  - `observation`: Сырые наблюдения (партиционирована по месяцам по phenomenon_time для оптимизации).
  - `observation_hour`: Агрегаты по часам (среднее, мин/макс, count) для быстрого доступа в API.

- **Особенности**:
  - **Пространственные данные**: Индекс GIST на геометрию для карт
  - **Временные ряды**: Партиционирование сырых данных, часовые агрегаты для дашбордов.
  - **Оптимизация**: Индексы для быстрых запросов по (thing, location, time, phenomenon).
  - **API-поддержка**: `api_locations` для списка локаций с координатами; функции `api_last3` (3 последних значения) и `api_series` (временной ряд за период).
  - **Docker-setup**: Контейнер с PostGIS 17-3.5, volumes для данных и инициализации.

БД ориентирована на IoT/мониторинг: эффективна для реального времени, графиков и карт.

🚀Запуск
```bash
docker compose up -d
```
_____________________________________________________________________________________
## Загрузка данных в frost БД
### `/loader`
ETL-инструмент (Extract, Transform, Load) для извлечения данных из сервера FROST (основанного на стандарте SensorThings API) http://90.156.134.128:8080/FROST-Server/v1.1 и загрузки их в базу данных PostgreSQL
- `START_FROM_DT`: Дата-старт для загрузки новых данных (по умолчанию: 2024-01-01 UTC). Формат: datetime(YYYY, MM, DD, tzinfo=timezone.utc).
- `DS_INCLUDE`: Множество ID Datastreams для включения (set(), пример: {1, 2, 3}). Если пусто — все.
- `DS_EXCLUDE`: Множество ID Datastreams для исключения (set(), пример: {10, 11}). Если пусто — никаких исключений.
- Загружает и обновляет `Locations` (локации с координатами в PostGIS).
- Загружает и обновляет `Things` (устройства/сенсоры), включая историю их перемещений `HistoricalLocations` для построения временных интервалов ассоциации с локациями.
- Обновляет `ObservedProperties` (свойства наблюдений) с учетом уникальности по комбинации имени и единицы измерения (с уникальностью по наименованию + единице измерения). Если совпадение найдено, использует существующий ID; иначе создает новый.
- Загружает `Datastreams` (потоки данных), связывая их с `Things` и `ObservedProperties`.
- Для каждого `Datastream` загружает новые наблюдения, начиная с последней известной даты (или с START_FROM_DT для новых).
- Агрегирует данные по часам: рассчитывает среднее (avg), минимум (min), максимум (max) и количество (count) значений.
- Сохраняет агрегированные данные в `таблицу observation_hour`, с привязкой к `Thing`, `Location` (на основе времени) и `Datastream`.
- Отслеживает прогресс в таблице `ingestion_state` (водяной знак — last_time для каждого `Datastream`).
- Пропускает `Datastreams`, если они не найдены на сервере (код 404), предполагая, что они с другого источника.

🚀Запуск
```bash
uv sync
source .venv/nin/activate
python ingest_frost.py
```

### `/loader-rudn`
ETL-инструмент (Extract, Transform, Load) для извлечения данных из сервера FROST (основанного на стандарте SensorThings API) http://94.154.11.74/frost/v1.1 и загрузки их в базу данных PostgreSQL
- `START_FROM_DT`: Дата-старт для загрузки новых данных (по умолчанию: 2024-01-01 UTC). Формат: datetime(YYYY, MM, DD, tzinfo=timezone.utc).
- `TARGET_LOCATIONS`: Список локаций для фильтрации (в коде: ["Main RUDN University campus"]). Измените в скрипте, если нужно добавить/убрать. Если пустой список — загружает все.
- `Locations`: Извлекает локации, парсит координаты (с преобразованием из EPSG:3857 в EPSG:4326, если нужно) и сохраняет в таблицу `location`.
- `Things`: Загружает устройства, их историю локаций и сохраняет в таблицы `thing` и `thing_location` (с учетом временных интервалов).
- `Observed Properties`: Свойства наблюдений (с уникальностью по наименованию + единице измерения), сохраняет в observed_property.
- `Datastreams`: Потоки данных, включая единицы измерения, сохраняет в `datastream`.
- `MultiDatastreams`: Обработка многомерных потоков с созданием виртуальных датastreams (с фиксированными именами и единицами для РУДН, на основе массива RUDN_OBS_PROPS).
- `Observations`: Извлекает наблюдения (результаты измерений), агрегирует их по часам (среднее, мин/макс, количество) и сохраняет в observation_hour. Поддерживает инкрементальную загрузку с водяным знаком (watermark) для избежания дубликатов.
🚀Запуск
```bash
uv sync
source .venv/nin/activate
python ingest_frost.py
```
_________________________________________________________
## Создание gis БД и загрузка данных в нее
БД предназначена для хранения и обработки геопространственных данных кампуса (РУДН). Она основана на PostgreSQL с расширениями PostGIS и PostGIS Raster для поддержки векторных и растровых данных.
**Версия и окружение**: PostgreSQL 17 с PostGIS 3.5 (образ Docker: postgis/postgis:17-3.5-alpine). Включена полная поддержка GDAL-драйверов для растров и outdb-растров.
### `/loader-rudn-bd`
Создание PostgreSQL с расширениями PostGIS и PostGIS Raster и загрузка в нее векторных и растровых слоев с БД РУДНа:

**Ключи доступа к БД РУДНа**
- "<HOST>"
- "<PORT>"
- "<dbname>"
- "<user>"
- "<password>"

```bash
docker compose up -d
# зайти в контейнер и psql
docker exec -it pg-postgis-17-35 psql -U pguser -d gis
```

```sql
-- psql -U pguser -d gis
DROP SERVER IF EXISTS rem CASCADE;
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER rem FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host <HOST>,
           port <PORT>,
           dbname <dbname>,
           sslmode 'require');

CREATE USER MAPPING FOR pguser SERVER rem
  OPTIONS (user <user>, password <password>);
```
Импортируем только те public-таблицы, к которым у нас есть доступ

```sql
DROP SCHEMA IF EXISTS rem_public CASCADE;
CREATE SCHEMA rem_public;

IMPORT FOREIGN SCHEMA public
  LIMIT TO (active_tt_campus,
            boundary_campus,
            lulc_campus,
            monitoring_points_campus,
            sampling_campus,
            tree_inventory_campus)
  FROM SERVER rem INTO rem_public
  OPTIONS (import_default 'false', import_collate 'false', import_not_null 'false');
```
Растровая схема уже импортируется. Если `IMPORT FOREIGN SCHEMA rasters ...` не прошёл, то повторите:

```sql
DROP SCHEMA IF EXISTS rem_rasters CASCADE;
CREATE SCHEMA rem_rasters;

IMPORT FOREIGN SCHEMA rasters
  FROM SERVER rem INTO rem_rasters
  OPTIONS (import_default 'false', import_collate 'false', import_not_null 'false');
```

Копируем данные локально

```sql
-- public.*
DROP TABLE IF EXISTS public.active_tt_campus;
CREATE TABLE public.active_tt_campus AS SELECT * FROM rem_public.active_tt_campus;

DROP TABLE IF EXISTS public.boundary_campus;
CREATE TABLE public.boundary_campus AS SELECT * FROM rem_public.boundary_campus;

DROP TABLE IF EXISTS public.lulc_campus;
CREATE TABLE public.lulc_campus AS SELECT * FROM rem_public.lulc_campus;

DROP TABLE IF EXISTS public.monitoring_points_campus;
CREATE TABLE public.monitoring_points_campus AS SELECT * FROM rem_public.monitoring_points_campus;

DROP TABLE IF EXISTS public.sampling_campus;
CREATE TABLE public.sampling_campus AS SELECT * FROM rem_public.sampling_campus;

DROP TABLE IF EXISTS public.tree_inventory_campus;
CREATE TABLE public.tree_inventory_campus AS SELECT * FROM rem_public.tree_inventory_campus;
CREATE SCHEMA rasters;
-- rasters.*
DROP TABLE IF EXISTS rasters.akad_dsm_2024_n36;
CREATE TABLE rasters.akad_dsm_2024_n36 AS SELECT * FROM rem_rasters.akad_dsm_2024_n36;

DROP TABLE IF EXISTS rasters.akad_ortho_2024_n36;
CREATE TABLE rasters.akad_ortho_2024_n36 AS SELECT * FROM rem_rasters.akad_ortho_2024_n36;

DROP TABLE IF EXISTS rasters.campus_dsm_uav_20200609_n37;
CREATE TABLE rasters.campus_dsm_uav_20200609_n37 AS SELECT * FROM rem_rasters.campus_dsm_uav_20200609_n37;

DROP TABLE IF EXISTS rasters.campus_dtm_uav_20200609_n37;
CREATE TABLE rasters.campus_dtm_uav_20200609_n37 AS SELECT * FROM rem_rasters.campus_dtm_uav_20200609_n37;

DROP TABLE IF EXISTS rasters.campus_max_runoff_depth_2m_n37;
CREATE TABLE rasters.campus_max_runoff_depth_2m_n37 AS SELECT * FROM rem_rasters.campus_max_runoff_depth_2m_n37;

DROP TABLE IF EXISTS rasters.campus_pet_1m_20240629_14h;
CREATE TABLE rasters.campus_pet_1m_20240629_14h AS SELECT * FROM rem_rasters.campus_pet_1m_20240629_14h;

DROP TABLE IF EXISTS rasters.campus_temp_1m_20240629_14h;
CREATE TABLE rasters.campus_temp_1m_20240629_14h AS SELECT * FROM rem_rasters.campus_temp_1m_20240629_14h;

DROP TABLE IF EXISTS rasters.campus_temp_surface_1m_20240629_14h;
CREATE TABLE rasters.campus_temp_surface_1m_20240629_14h AS SELECT * FROM rem_rasters.campus_temp_surface_1m_20240629_14h;

DROP TABLE IF EXISTS rasters.campus_windspeed_ms_1m_20240629_14h;
CREATE TABLE rasters.campus_windspeed_ms_1m_20240629_14h AS SELECT * FROM rem_rasters.campus_windspeed_ms_1m_20240629_14h;
```
Индексы только на BASE TABLE

```sql
DO $$
DECLARE r record; idx text;
BEGIN
  FOR r IN
    SELECT c.table_schema, c.table_name, c.column_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema=c.table_schema AND t.table_name=c.table_name
    WHERE c.udt_name='geometry' AND t.table_type='BASE TABLE' AND c.table_schema='public'
  LOOP
    idx := format('%I_%I_%I_gix', r.table_schema, r.table_name, r.column_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.%I USING GIST(%I);',
                   idx, r.table_schema, r.table_name, r.column_name);
  END LOOP;
END$$;

VACUUM ANALYZE;
```
