-- эти параметры нужны для работы с растровыми GDAL-драйверами и outdb-растрами
ALTER DATABASE gis SET postgis.gdal_enabled_drivers = 'ENABLE_ALL';
ALTER DATABASE gis SET postgis.enable_outdb_rasters = true;
