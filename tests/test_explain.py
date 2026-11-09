import duckdb

from nemsim.explain_r1 import fuel_group


def test_fuel_group_maps_genunits_sources():
    rows = duckdb.sql(f"""select f, {fuel_group('f')} from (values ('Wind'), ('Solar'), ('Black coal'),
        ('Hydro'), ('Battery Storage'), ('Natural Gas (Pipeline)'), ('Diesel oil'), (null)) t(f)""").fetchall()
    assert [g for _, g in rows] == ["wind", "solar", "coal", "hydro", "battery", "gas", "other", "other"]
