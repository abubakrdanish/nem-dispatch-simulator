import zipfile
from datetime import date

import duckdb
import pytest

from nemsim.mmsdm import Table, ensure_schema, files_for, load_file, table_links

LISTING = """
Monday, September 8, 2025  9:10 AM    151643095 <A HREF="/Data_Archive/X/PUBLIC_ARCHIVE%23BIDPEROFFER_D%23FILE01%23202509010000.zip">x</A><br>
Monday, September 8, 2025  9:10 AM      1912164 <A HREF="/Data_Archive/X/PUBLIC_ARCHIVE%23BIDDAYOFFER_D%23FILE01%23202509010000.zip">x</A><br>
Monday, September 8, 2025  9:10 AM      1000 <A HREF="/Data_Archive/X/PUBLIC_DVD_DISPATCHLOAD_202106010000.zip">x</A><br>
"""


def test_table_links_reads_both_name_formats_and_sizes():
    [(href, part, size)] = table_links(LISTING, "BIDPEROFFER_D")
    assert (part, size) == ("01", 151643095) and href.endswith("202509010000.zip")
    assert table_links(LISTING, "BIDDAYOFFER") == []          # no prefix matches
    assert table_links(LISTING, "DISPATCHLOAD")[0][1] == "01"


def _zip(tmp_path, name, lines):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("PUBLIC_TEST.CSV", "\n".join(lines) + "\n")
    return path


TABLE = Table("TOY", {"SETTLEMENTDATE": "timestamp", "DUID": "varchar", "MW": "double"}, where="KIND = 'ENERGY'")
HEAD = ['C,SETP.WORLD,DVD_TOY,AEMO,PUBLIC,2030/01/01,00:00:00,0,MONTHLY_ARCHIVE,0',
        'I,TEST,TOY,1,SETTLEMENTDATE,DUID,KIND,MW']
ROWS = ['D,TEST,TOY,1,"2030/01/01 00:05:00",A1,ENERGY,10.5',
        'D,TEST,TOY,1,"2030/01/01 00:05:00",A1,RAISEREG,3',
        'D,TEST,TOY,1,"2030/01/01 00:10:00",B2,ENERGY,']


def test_load_file_filters_types_and_skips_reloads(tmp_path):
    path = _zip(tmp_path, "toy_203001_01.zip", HEAD + ROWS + ['C,"END OF REPORT",6'])
    con = duckdb.connect()
    ensure_schema(con)
    assert load_file(con, TABLE, path)["rows_loaded"] == 2
    assert con.execute("select duid, mw from raw.toy order by duid").fetchall() == [("A1", 10.5), ("B2", None)]
    assert load_file(con, TABLE, path)["skipped"]


def test_load_file_rejects_a_short_file(tmp_path):
    path = _zip(tmp_path, "toy_203001_01.zip", HEAD + ROWS + ['C,"END OF REPORT",7'])
    con = duckdb.connect()
    ensure_schema(con)
    with pytest.raises(ValueError, match="read 6 records but the file says 7"):
        load_file(con, TABLE, path)
    assert con.execute("select count(*) from meta.loaded_files").fetchone()[0] == 0


def test_load_file_rejects_a_missing_column(tmp_path):
    path = _zip(tmp_path, "toy_203001_01.zip", HEAD + ROWS + ['C,"END OF REPORT",6'])
    con = duckdb.connect()
    ensure_schema(con)
    with pytest.raises(ValueError, match="columns \\['GONE'\\]"):
        load_file(con, Table("TOY", {"GONE": "double"}), path)


def test_files_for_picks_the_newest_snapshot(tmp_path):
    for name in ["dispatchprice_202509_01_20251001.zip", "dispatchprice_202509_01_20261017.zip",
                 "dispatchprice_202510_01_20261017.zip", "bidperoffer_d_202509_01.zip"]:
        (tmp_path / name).touch()
    assert [p.name for p in files_for(tmp_path, "DISPATCHPRICE", date(2025, 9, 1))] == \
        ["dispatchprice_202509_01_20261017.zip"]
    assert [p.name for p in files_for(tmp_path, "DISPATCHPRICE", None)] == ["dispatchprice_202510_01_20261017.zip"]
    assert [p.name for p in files_for(tmp_path, "BIDPEROFFER_D", date(2025, 9, 1))] == ["bidperoffer_d_202509_01.zip"]
